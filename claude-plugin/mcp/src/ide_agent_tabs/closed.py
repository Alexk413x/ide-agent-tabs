from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Any, NamedTuple

from .clock import iso, now_ms, parse_iso
from .files import read_text_if_exists, remove_stale_files, write_atomically
from .jsjson import is_finite, is_number, is_safe_integer, parse, stringify, trim, utf16_len, utf16_slice
from .messaging.sessions import VIAS, is_session_id
from .messaging.store import KEEP_MS
from .profiles import BUILTIN_PROFILES
from .winapi import open_shared_read

CLOSED_DIR = "history"
CLOSED_KEEP_MS = KEEP_MS
PREVIEW_CHARS = 120
_TAIL_BYTES = 2 * 1024 * 1024
_CODEX_DAYS_SEARCHED = 31
CACHE_WINDOWS = ("5m", "1h")
_LINES = re.compile(r"\r?\n")
_NUMERIC = re.compile(r"\d+")

ClosedSession = dict[str, Any]
Presence = Mapping[str, Any]


class TranscriptDirs(NamedTuple):
    claude: str
    codex: str


class Usage(NamedTuple):
    found: bool
    tokens: float | None
    cache: str | None
    preview: str | None
    model: str | None = None


_NO_USAGE = Usage(False, None, None, None)


def transcript_dirs(env: Mapping[str, str]) -> TranscriptDirs:
    home = os.path.expanduser("~")
    return TranscriptDirs(
        env.get("CLAUDE_CONFIG_DIR") or os.path.join(home, ".claude"), env.get("CODEX_HOME") or os.path.join(home, ".codex")
    )


def closed_path(home: str, session_id: str) -> str:
    return os.path.join(home, CLOSED_DIR, f"{session_id}.json")


def resumable_id(p: Presence) -> str | None:
    agent = p.get("agent")
    if agent in ("codex", "codex-local"):
        found = p.get("threadId") if p.get("threadId") is not None else p.get("owner")
    else:
        found = p.get("owner")
    return found if isinstance(found, str) and is_session_id(found) else None


def label_of(agent: str) -> str:
    return next((p.label for p in BUILTIN_PROFILES if p.name == agent), agent)


def preview_of(text: str) -> str | None:
    line = next((t for t in (trim(raw) for raw in _LINES.split(text)) if t != ""), None)
    if line is None:
        return None
    return f"{utf16_slice(line, 0, PREVIEW_CHARS - 1)}…" if utf16_len(line) > PREVIEW_CHARS else line


def _tail_lines(file: str) -> list[str] | None:
    try:
        with os.fdopen(open_shared_read(file), "rb") as f:
            size = os.fstat(f.fileno()).st_size
            length = min(size, _TAIL_BYTES)
            f.seek(size - length)
            lines = f.read(length).decode("utf-8", "replace").split("\n")
    except OSError:
        return None
    if length < size:
        lines.pop(0)
    return [line for line in lines if trim(line) != ""]


def _json(line: str) -> dict[str, Any] | None:
    try:
        value = parse(line)
    except (ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _obj(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _num(value: Any) -> float:
    return value if is_finite(value) and value >= 0 else 0


def _claude_slug(folder: str) -> str:
    return "".join(c if c.isascii() and c.isalnum() else "--" if ord(c) > 0xFFFF else "-" for c in folder)


def _is_file(path: str) -> bool:
    return os.path.isfile(path)


def _claude_transcript(dirs: TranscriptDirs, folder: str, session_id: str) -> str | None:
    projects = os.path.join(dirs.claude, "projects")
    direct = os.path.join(projects, _claude_slug(folder), f"{session_id}.jsonl")
    if _is_file(direct):
        return direct
    try:
        names = os.listdir(projects)
    except OSError:
        names = []
    for name in names:
        file = os.path.join(projects, name, f"{session_id}.jsonl")
        if _is_file(file):
            return file
    return None


def _texts(content: list[Any], kind: str) -> str:
    return "\n".join(c["text"] for c in map(_obj, content) if c.get("type") == kind and isinstance(c.get("text"), str))


def claude_usage(dirs: TranscriptDirs, folder: str, session_id: str) -> Usage:
    file = _claude_transcript(dirs, folder, session_id)
    lines = _tail_lines(file) if file is not None else None
    if lines is None:
        return _NO_USAGE
    tokens: float | None = None
    cache: str | None = None
    preview: str | None = None
    model: str | None = None
    for line in reversed(lines):
        entry = _json(line)
        if entry is None or entry.get("type") != "assistant" or entry.get("isSidechain") is True:
            continue
        message = _obj(entry.get("message"))
        used = _obj(message.get("usage"))
        if tokens is None and used:
            tokens = (
                _num(used.get("input_tokens")) + _num(used.get("cache_creation_input_tokens")) + _num(used.get("cache_read_input_tokens"))
            )
            if isinstance(message.get("model"), str):
                model = message["model"]
        created = _obj(used.get("cache_creation"))
        one_hour = _num(created.get("ephemeral_1h_input_tokens"))
        if cache is None and (one_hour > 0 or _num(created.get("ephemeral_5m_input_tokens")) > 0):
            cache = "1h" if one_hour > 0 else "5m"
        if preview is None and isinstance(message.get("content"), list):
            preview = preview_of(_texts(message["content"], "text"))
        if tokens is not None and cache is not None and preview is not None:
            break
    return Usage(True, tokens, cache, preview, model)


def _numeric_names(folder: str) -> list[str]:
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    return sorted((n for n in names if _NUMERIC.fullmatch(n)), reverse=True)


def _codex_rollout(dirs: TranscriptDirs, session_id: str) -> str | None:
    root = os.path.join(dirs.codex, "sessions")
    days = 0
    for year in _numeric_names(root):
        for month in _numeric_names(os.path.join(root, year)):
            for day in _numeric_names(os.path.join(root, year, month)):
                folder = os.path.join(root, year, month, day)
                try:
                    names = os.listdir(folder)
                except OSError:
                    names = []
                name = next((n for n in names if n.endswith(f"-{session_id}.jsonl")), None)
                if name is not None:
                    return os.path.join(folder, name)
                days += 1
                if days >= _CODEX_DAYS_SEARCHED:
                    return None
    return None


def codex_usage(dirs: TranscriptDirs, session_id: str) -> Usage:
    file = _codex_rollout(dirs, session_id)
    lines = _tail_lines(file) if file is not None else None
    if lines is None:
        return _NO_USAGE
    tokens: float | None = None
    preview: str | None = None
    for line in reversed(lines):
        payload = _obj(_obj(_json(line)).get("payload"))
        if tokens is None and payload.get("type") == "token_count":
            last = _obj(_obj(payload.get("info")).get("last_token_usage"))
            if is_number(last.get("input_tokens")):
                tokens = _num(last["input_tokens"])
        if (
            preview is None
            and payload.get("type") == "message"
            and payload.get("role") == "assistant"
            and isinstance(payload.get("content"), list)
        ):
            preview = preview_of(_texts(payload["content"], "output_text"))
        if tokens is not None and preview is not None:
            break
    return Usage(True, tokens, None, preview)


def usage_of(dirs: TranscriptDirs, agent: str, folder: str, session_id: str) -> Usage:
    if agent == "claude":
        return claude_usage(dirs, folder, session_id)
    if agent in ("codex", "codex-local"):
        return codex_usage(dirs, session_id)
    return _NO_USAGE


def closed_record(p: Presence, ended_at: int, dirs: TranscriptDirs) -> ClosedSession | None:
    session_id = resumable_id(p)
    agent = p.get("agent")
    folder = p.get("path")
    if session_id is None or not isinstance(agent, str) or not isinstance(folder, str):
        return None
    try:
        usage = usage_of(dirs, agent, folder, session_id)
    except (OSError, ValueError):
        usage = _NO_USAGE
    # Claude Code writes no transcript before the first prompt, and claude --resume can't open a session without one.
    if agent == "claude" and not usage.found:
        return None
    label = label_of(agent)
    model = p.get("model") if p.get("model") is not None else usage.model
    return {
        "id": session_id,
        "agent": agent,
        "label": label,
        "name": p.get("nativeName"),
        "folder": folder,
        "product": p.get("product"),
        "host": p.get("host"),
        "model": model,
        "effort": p.get("effort"),
        "harness": f"{label}{' via OpenRouter' if p.get('via') == 'ori' else ''}",
        "via": p.get("via"),
        "startedAt": p.get("startedAt"),
        "endedAt": iso(ended_at),
        "tokens": usage.tokens,
        "cache": usage.cache,
        "preview": usage.preview,
        "tab": p.get("id"),
    }


def clean_closed(home: str, now: float | None = None) -> None:
    remove_stale_files(os.path.join(home, CLOSED_DIR), [".json", ".tmp"], CLOSED_KEEP_MS, now)


def record_ended(home: str, p: Presence, ended_at: int, dirs: TranscriptDirs) -> ClosedSession | None:
    record = closed_record(p, ended_at, dirs)
    if record is None:
        return None
    write_atomically(closed_path(home, record["id"]), stringify(record, 2) + "\n")
    clean_closed(home, ended_at)
    return record


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def parse_closed(text: str | None) -> ClosedSession | None:
    o = _json(text) if text is not None else None
    if o is None:
        return None
    if (
        not isinstance(o.get("id"), str)
        or not is_session_id(o["id"])
        or not isinstance(o.get("agent"), str)
        or not isinstance(o.get("folder"), str)
    ):
        return None
    if not isinstance(o.get("endedAt"), str) or parse_iso(o["endedAt"]) is None:
        return None
    tokens = o.get("tokens")
    agent = o["agent"]
    return {
        "id": o["id"],
        "agent": agent,
        "label": o["label"] if isinstance(o.get("label"), str) else label_of(agent),
        "name": _str_or_none(o.get("name")),
        "folder": o["folder"],
        "product": _str_or_none(o.get("product")),
        "host": _str_or_none(o.get("host")),
        "model": _str_or_none(o.get("model")),
        "effort": _str_or_none(o.get("effort")),
        "harness": o["harness"] if isinstance(o.get("harness"), str) else label_of(agent),
        "via": o.get("via") if o.get("via") in VIAS else None,
        "startedAt": _str_or_none(o.get("startedAt")),
        "endedAt": o["endedAt"],
        "tokens": tokens if isinstance(tokens, (int, float)) and is_safe_integer(tokens) and tokens >= 0 else None,
        "cache": o.get("cache") if o.get("cache") in CACHE_WINDOWS else None,
        "preview": _str_or_none(o.get("preview")),
        "tab": o["tab"] if isinstance(o.get("tab"), str) else o["id"],
    }


_PUNCTUATION = "_-,;:!?.'\"()[]{}@*/\\&#%`^+<=>|~$"


def _collation_weight(c: str) -> tuple[int, int]:
    if c in _PUNCTUATION:
        return (0, _PUNCTUATION.index(c))
    if c.isdigit() and c.isascii():
        return (1, ord(c))
    if c.isascii() and c.isalpha():
        return (2, ord(c.lower()))
    return (3, ord(c))


def locale_key(text: str) -> tuple[list[tuple[int, int]], list[int]]:
    return [_collation_weight(c) for c in text], [1 if c.isupper() else 0 for c in text]


def ended_ms(record: ClosedSession) -> int:
    found = parse_iso(record["endedAt"])
    return found if found is not None else 0


def read_closed(home: str, now: float | None = None) -> list[ClosedSession]:
    now = now_ms() if now is None else now
    clean_closed(home, now)
    folder = os.path.join(home, CLOSED_DIR)
    try:
        names = [n for n in os.listdir(folder) if n.endswith(".json")]
    except OSError:
        names = []
    records: list[ClosedSession] = []
    for name in names:
        try:
            parsed = parse_closed(read_text_if_exists(os.path.join(folder, name)))
        except OSError:
            parsed = None
        if parsed is not None and now - ended_ms(parsed) <= CLOSED_KEEP_MS:
            records.append(parsed)
    records.sort(key=lambda r: locale_key(r["id"]))
    records.sort(key=lambda r: locale_key(r["endedAt"]), reverse=True)
    return records

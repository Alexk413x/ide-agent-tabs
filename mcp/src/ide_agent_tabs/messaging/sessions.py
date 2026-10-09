from __future__ import annotations

import contextlib
import math
import os
import re
import threading
from typing import Any, Callable

from ..clock import iso, now_ms, parse_iso
from ..files import file_lock, mtime_ms, read_text_if_exists, remove_file, write_atomically
from ..jsjson import MAX_SAFE_INTEGER, parse, stringify, utf16_len
from ..liveness import pid_alive

SESSIONS_DIR = "sessions"
STATES = ("idle", "busy", "permission", "waking", "unknown")
SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
STUB_MAX_AGE_MS = 60 * 60 * 1000
WAKE_TIMEOUT_MS = 20_000
IDLE_SETTLE_MS = 2_000
BUSY_STALE_MS = 15 * 60_000
HEARTBEAT_MS = 60_000
PRESENCE_BEATS_MISSED = 5
MOD_STALE_MS = 3 * HEARTBEAT_MS
DRIVERS = ("mod",)
VIAS = ("ori", "direct")
MAIL_VERSION = 2
AGENT_COLORS = ("red", "blue", "green", "yellow", "purple", "orange", "pink", "cyan")

_CLIENT_AGENTS = (
    ("claude", "claude"),
    ("codex", "codex"),
    ("gemini", "gemini"),
    ("copilot", "copilot"),
    ("opencode", "opencode"),
    ("antigravity", "agy"),
    ("grok", "grok"),
    ("qwen", "qwen"),
    ("goose", "goose"),
)

Presence = dict[str, Any]
Ended = Callable[[Presence, float], object]


def is_session_id(value: object) -> bool:
    return isinstance(value, str) and SESSION_ID.fullmatch(value) is not None


def is_model(value: str) -> bool:
    return 1 <= utf16_len(value) <= 128 and re.search(r"[\x00-\x1f\x7f]", value) is None


def is_effort(value: str) -> bool:
    return re.fullmatch(r"[A-Za-z0-9._-]{1,32}", value) is not None


def is_agent_type(value: str) -> bool:
    return re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", value) is not None


def is_agent_color(value: str) -> bool:
    return value in AGENT_COLORS


def presence_path(home: str, session_id: str) -> str:
    return os.path.join(home, SESSIONS_DIR, f"{session_id}.json")


def safe_name(value: str, limit: int = 64) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "", value)[:limit]


def agent_from_client(name: str | None) -> str:
    lower = (name or "").lower()
    if lower == "pi":
        return "pi"
    for part, agent in _CLIENT_AGENTS:
        if part in lower:
            return agent
    return safe_name(name or "") or "unknown"


def _safe_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and (not math.isfinite(value) or not value.is_integer()):
        return None
    number = int(value)
    return number if -MAX_SAFE_INTEGER <= number <= MAX_SAFE_INTEGER else None


def parse_presence(text: str | None) -> Presence | None:
    if text is None:
        return None
    try:
        o = parse(text)
    except ValueError:
        return None
    if not isinstance(o, dict) or not is_session_id(o.get("id")):
        return None
    out: Presence = {"id": o["id"]}

    def text_field(key: str, valid: Callable[[str], bool] = lambda _: True) -> None:
        value = o.get(key)
        if isinstance(value, str) and valid(value):
            out[key] = value

    def int_field(key: str, minimum: int) -> None:
        value = _safe_int(o.get(key))
        if value is not None and value >= minimum:
            out[key] = value

    text_field("agent")
    text_field("path")
    int_field("pid", 1)
    int_field("pidStart", 0)
    text_field("host")
    text_field("startedAt")
    text_field("state", lambda v: v in STATES)
    text_field("stateAt")
    int_field("nudges", 0)
    reminded = o.get("reminded")
    if isinstance(reminded, list) and all(isinstance(r, str) for r in reminded):
        out["reminded"] = list(reminded)
    text_field("threadId")
    text_field("owner")
    int_field("beatMs", 1)
    if isinstance(o.get("inputIdle"), bool):
        out["inputIdle"] = o["inputIdle"]
    text_field("handedOffTo")
    text_field("driver", lambda v: v in DRIVERS)
    int_field("modBeat", 1)
    text_field("nativeName")
    text_field("via", lambda v: v in VIAS)
    text_field("project")
    text_field("model")
    text_field("effort")
    text_field("agentType", is_agent_type)
    text_field("agentColor", is_agent_color)
    text_field("product")
    int_field("mail", 1)
    return out


# The Claude mod delivers mail and reports state in-process; a mod that stopped without cleanup leaves the
# field behind, so it counts only while the mod's heartbeat is fresh, and the classic hooks and wake lines resume.
def is_mod_driven(p: Presence, now: float) -> bool:
    beat = p.get("modBeat")
    if p.get("driver") != "mod" or beat is None:
        return False
    age = now - beat
    return -MOD_STALE_MS <= age < MOD_STALE_MS


def is_complete(p: Presence) -> bool:
    return all(p.get(k) is not None for k in ("pid", "agent", "path", "startedAt"))


def read_presence(home: str, session_id: str) -> Presence | None:
    if not is_session_id(session_id):
        return None
    try:
        return parse_presence(read_text_if_exists(presence_path(home, session_id)))
    except OSError:
        return None


def presence_text(p: Presence) -> str:
    return stringify(p, 2) + "\n"


def update_presence(home: str, session_id: str, change: Callable[[Presence | None], Presence | None]) -> Presence | None:
    file = presence_path(home, session_id)
    with file_lock(file):
        current = parse_presence(read_text_if_exists(file))
        following = change(current)
        if following is current:
            return current
        if following is None:
            remove_file(file)
        else:
            write_atomically(file, presence_text(following))
        return following


def with_state(p: Presence, state: str, now: float, nudges: int | None = None) -> Presence:
    out = {**p, "state": state, "stateAt": iso(int(now))}
    if nudges is not None:
        out["nudges"] = nudges
    return out


# A wake line that never starts a turn, such as one typed while the agent was still finishing, leaves the
# session waking; after WAKE_TIMEOUT_MS it counts as idle again, so the next send or wait retries. A busy turn
# refreshes its state on every tool call, so one silent for BUSY_STALE_MS was interrupted without a turn-end hook.
def effective_state(p: Presence, now: float) -> str:
    state = p.get("state") or "unknown"
    if state not in ("waking", "busy"):
        return state
    at = parse_iso(p.get("stateAt") or "")
    if state == "busy" and at is None:
        return state
    limit = WAKE_TIMEOUT_MS if state == "waking" else BUSY_STALE_MS
    if at is None:
        return "idle"
    elapsed = now - at
    return state if 0 <= elapsed < limit else "idle"


def _is_live(presence: Presence, mtime: float | None, alive: Callable[[int], bool], now: float) -> bool:
    beat = presence.get("beatMs")
    # A pid alone can name a new process once Windows reuses it; a server that beats proves it still runs.
    silent = beat is not None and mtime is not None and now - mtime > beat * PRESENCE_BEATS_MISSED
    return is_complete(presence) and not silent and alive(presence["pid"])


# Unlike live_sessions, this leaves a dead session's file in place: the next full scan records its end.
def live_session(home: str, session_id: str, alive: Callable[[int], bool], now: float) -> Presence | None:
    if not is_session_id(session_id):
        return None
    file = presence_path(home, session_id)
    try:
        presence = parse_presence(read_text_if_exists(file))
    except OSError:
        return None
    if presence is None or presence["id"] != session_id or not _is_live(presence, mtime_ms(file), alive, now):
        return None
    return {**presence, "state": effective_state(presence, now)}


def live_sessions(
    home: str,
    alive: Callable[[int], bool] = pid_alive,
    now: float | None = None,
    ended: Ended | None = None,
) -> list[Presence]:
    now = now_ms() if now is None else now
    folder = os.path.join(home, SESSIONS_DIR)
    try:
        names = sorted(n for n in os.listdir(folder) if n.endswith(".json"))
    except OSError:
        return []
    sessions: list[Presence] = []
    for name in names:
        file = os.path.join(folder, name)
        try:
            text = read_text_if_exists(file)
        except OSError:
            continue
        if text is None:
            continue
        presence = parse_presence(text)
        mtime = mtime_ms(file)
        if presence is not None and _is_live(presence, mtime, alive, now):
            sessions.append({**presence, "state": effective_state(presence, now)})
            continue
        dead = presence is not None and presence.get("pid") is not None
        if mtime is not None and (dead or now - mtime > STUB_MAX_AGE_MS):
            if presence is not None and ended is not None:
                with contextlib.suppress(Exception):
                    ended(presence, min(now, mtime))
            with contextlib.suppress(OSError):
                remove_file(file)
    return sessions


class _SharedRead:
    def __init__(self) -> None:
        self.done = threading.Event()
        self.value: list[Presence] = []
        self.error: BaseException | None = None


_reads: dict[tuple[Callable[[int], bool], str], _SharedRead] = {}
_reads_lock = threading.Lock()


# The shared server answers many sessions at once, and each call that lists sessions reads every presence
# file; calls that overlap share one read instead of each reading the folder.
def join_live_sessions(home: str, alive: Callable[[int], bool], now: float, ended: Ended | None = None) -> list[Presence]:
    key = (alive, os.path.abspath(home))
    with _reads_lock:
        shared = _reads.get(key)
        owner = shared is None
        if shared is None:
            shared = _SharedRead()
            _reads[key] = shared
    if not owner:
        shared.done.wait()
        if shared.error is not None:
            raise shared.error
        return list(shared.value)
    try:
        shared.value = live_sessions(home, alive, now, ended)
    except BaseException as e:
        shared.error = e
        raise
    finally:
        with _reads_lock:
            del _reads[key]
        shared.done.set()
    return list(shared.value)

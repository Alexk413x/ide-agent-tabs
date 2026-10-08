from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Callable, NamedTuple

from .clock import now_ms
from .closed import ClosedSession, ended_ms, read_closed, resumable_id
from .jsjson import JS_SPACE, js_round, number, utf16_len, utf16_slice
from .launch_plan import is_model
from .profiles import ALLOW_RESUME_KEY, CONFIG_FILE, AgentSettings
from .service import ToolError

CACHE_MS = 5 * 60_000
LONG_CACHE_MS = 60 * 60_000
MAX_CHEAP_TOKENS = 50_000
CHEAP_NOTE = "likely cached: about 10% of normal input cost"
_MIN_ID_PREFIX = 4
_NOT_ALNUM = re.compile(r"[^A-Za-z0-9]")

RESUME_ARGS: dict[str, Callable[[str], list[str]]] = {
    "claude": lambda i: ["--resume", i],
    "codex": lambda i: ["resume", i],
    "codex-local": lambda i: ["resume", i],
    "agy": lambda i: ["--conversation", i],
}


def ago(ms: float) -> str:
    s = max(0, js_round(ms / 1000))
    if s < 60:
        return f"{s}s ago"
    m = js_round(s / 60)
    if m < 60:
        return f"{m}m ago"
    h = js_round(m / 60)
    if h < 48:
        return f"{h}h ago"
    return f"{js_round(h / 24)}d ago"


def _to_fixed_1(value: float) -> str:
    return str(Decimal(value).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def size_text(tokens: float | None) -> str:
    if tokens is None:
        return "unknown size"
    if tokens < 1000:
        return f"{number(tokens)} tokens"
    if tokens < 1_000_000:
        return f"{js_round(tokens / 1000)}k tokens"
    return f"{_to_fixed_1(tokens / 1_000_000)}M tokens"


def _short_size(tokens: float | None) -> str:
    return "—" if tokens is None else size_text(tokens).replace(" tokens", "", 1)


def name_of(r: ClosedSession) -> str:
    if r.get("name") is not None:
        return r["name"]
    return f"{r['agent']}-{utf16_slice(_NOT_ALNUM.sub('', r['id']), 0, 4)}"


def cache_window_ms(r: Mapping[str, Any]) -> int:
    return LONG_CACHE_MS if r.get("cache") == "1h" else CACHE_MS


class CostCheck(NamedTuple):
    cheap: bool
    reasons: list[str]
    within_cache: bool


def cost_check(r: Mapping[str, Any], age_ms: float, model: str | None) -> CostCheck:
    window = cache_window_ms(r)
    within = age_ms <= window
    reasons: list[str] = []
    if not within:
        reasons.append(f"it ended past the {'1-hour' if window == LONG_CACHE_MS else '5-minute'} prompt cache window")
    if model is not None and model != r.get("model"):
        recorded = r.get("model") if r.get("model") is not None else "unknown model"
        reasons.append(f"model {model} differs from the session's {recorded}, so no cache applies")
    tokens = r.get("tokens")
    if tokens is not None and tokens > MAX_CHEAP_TOKENS:
        reasons.append("it holds over 50,000 tokens")
    if tokens is None and age_ms > CACHE_MS:
        reasons.append("its size is unknown and it ended over 5 minutes ago")
    return CostCheck(not reasons, reasons, within)


def _pad_end(text: str, width: int) -> str:
    return text + " " * max(0, width - utf16_len(text))


def closed_listing(records: Sequence[ClosedSession], now: float) -> str:
    if not records:
        return "No closed session in the last 7 days."
    header = ["NAME", "AGENT", "ENDED", "SIZE", "MODEL", "WHERE", "ID"]

    def cells(r: ClosedSession) -> list[str]:
        return [
            name_of(r),
            r["harness"],
            ago(now - ended_ms(r)),
            _short_size(r.get("tokens")),
            r["model"] if r.get("model") is not None else "—",
            r["product"] if r.get("product") is not None else "—",
            utf16_slice(r["id"], 0, 8),
        ]

    rows = [cells(r) for r in records]
    widths = [max(utf16_len(h), *(utf16_len(c[i]) for c in rows)) for i, h in enumerate(header)]

    def line(c: list[str]) -> str:
        return ("  " + "  ".join(_pad_end(v, widths[i]) for i, v in enumerate(c))).rstrip(JS_SPACE)

    folders: list[str] = []
    for r in records:
        if r["folder"] not in folders:
            folders.append(r["folder"])
    groups = ["\n".join([f, *(line(cells(r)) for r in records if r["folder"] == f)]) for f in folders]
    return f"{line(header)}\n" + "\n\n".join(groups)


class ResumeDeps(NamedTuple):
    home: str
    settings: Callable[[], AgentSettings]
    open_tab: Callable[[dict[str, Any]], dict[str, Any]]
    live_host: Callable[[str | None, str | None], str | None]
    live: Callable[[], list[Mapping[str, Any]]]
    now: Callable[[], int] = now_ms


class Resumes:
    def __init__(self, deps: ResumeDeps) -> None:
        self.deps = deps

    def _closed(self) -> list[ClosedSession]:
        records = read_closed(self.deps.home, self.deps.now())
        try:
            live = self.deps.live()
        except (OSError, ValueError):
            live = []
        running = {i for i in (resumable_id(p) for p in live) if i is not None}
        return [r for r in records if r["id"] not in running]

    def list(self) -> dict[str, Any]:
        now = self.deps.now()
        records = self._closed()
        return {
            "listing": closed_listing(records, now),
            "sessions": [
                {
                    "id": r["id"],
                    "name": name_of(r),
                    "agent": r["agent"],
                    "harness": r["harness"],
                    "folder": r["folder"],
                    "endedAt": r["endedAt"],
                    "ended": ago(now - ended_ms(r)),
                    "tokens": r["tokens"],
                    "size": size_text(r["tokens"]),
                    "model": r["model"],
                    "effort": r["effort"],
                    "where": r["product"],
                    "preview": r["preview"],
                    "resumable": r["agent"] in RESUME_ARGS,
                }
                for r in records
            ],
        }

    def _find(self, session_id: str) -> ClosedSession:
        records = self._closed()
        exact = next((r for r in records if r["id"] == session_id), None)
        if exact is not None:
            return exact
        matches = [r for r in records if r["id"].startswith(session_id)] if utf16_len(session_id) >= _MIN_ID_PREFIX else []
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ToolError(f"{session_id} matches {len(matches)} closed sessions; pass more of the id from closed_sessions")
        raise ToolError(f"no closed session with id {session_id} in the last 7 days; call closed_sessions for the ids")

    def resume(self, given: Mapping[str, Any]) -> dict[str, Any]:
        settings = self.deps.settings()
        if not settings.terminal.allow_resume:
            raise ToolError(
                f'resuming closed sessions is off ("Allow resuming closed sessions", {ALLOW_RESUME_KEY} in ~/.ide-agent-tabs/{CONFIG_FILE}). '
                "Turn it on in the IDE settings, or start a fresh session with handoff."
            )
        r = self._find(given["id"])
        args = RESUME_ARGS.get(r["agent"])
        short = utf16_slice(r["id"], 0, 8)
        if args is None:
            raise ToolError(
                f"{r['label']} has no resume option that Agent Tabs knows, so session {short} can't be reopened; "
                f"use handoff to start a fresh {r['label']} session with a brief."
            )
        now = self.deps.now()
        age_ms = now - ended_ms(r)
        recorded = r["model"] if r.get("model") is not None and is_model(r["model"]) else None
        wanted = given.get("model")
        model = wanted if wanted is not None else recorded
        check = cost_check(r, age_ms, wanted)
        facts = {
            "id": r["id"],
            "agent": r["agent"],
            "folder": r["folder"],
            "tokens": r["tokens"],
            "size": size_text(r["tokens"]),
            "age": ago(age_ms),
            "endedAt": r["endedAt"],
        }
        if not check.cheap and given.get("confirm") is not True:
            tokens = r.get("tokens")
            only_size = check.within_cache and len(check.reasons) == 1 and tokens is not None and tokens > MAX_CHEAP_TOKENS
            cached = " The prompt cache may still hold part of it, but not reliably." if only_size else ""
            return {
                "resumed": False,
                "needsConfirm": True,
                **facts,
                "reasons": check.reasons,
                "message": (
                    f"Not resumed: {'; '.join(check.reasons)}. Resuming makes {r['label']} re-read the full history, "
                    f"{size_text(r['tokens'])}, at full input price. It ended {ago(age_ms)}.{cached} "
                    "Handoff is the cheaper option: a fresh session that starts from a short brief. Ask the user which they want, "
                    "and call resume_tab again with confirm: true only after they agree to the cost."
                ),
            }
        ide = given.get("ide")
        if ide is None:
            try:
                ide = self.deps.live_host(r.get("host"), r.get("product"))
            except (OSError, ValueError, RuntimeError):
                ide = None
        request: dict[str, Any] = {"path": r["folder"], "agent": r["agent"], "args": args(r["id"])}
        if model is not None:
            request["model"] = model
        if r.get("via") is not None:
            request["via"] = r["via"]
        if ide is not None:
            request["ide"] = ide
        if given.get("focus") is not None:
            request["focus"] = given["focus"]
        opened = self.deps.open_tab(request)
        result: dict[str, Any] = {
            "resumed": True,
            **facts,
            "tab": opened.get("id"),
            "ide": opened.get("ide"),
            "product": opened.get("product"),
        }
        if model is not None:
            result["model"] = model
        result["cost"] = (
            CHEAP_NOTE if check.cheap else f"the user confirmed: the full history, {size_text(r['tokens'])}, is re-read at full input price"
        )
        if opened.get("note") is not None:
            result["note"] = opened["note"]
        return result

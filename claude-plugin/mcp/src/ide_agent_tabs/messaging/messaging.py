from __future__ import annotations

import contextlib
import math
import os
import re
import threading
import time
from collections.abc import Mapping, Sequence
from typing import Any, Callable, NamedTuple, Protocol

from ..clock import iso, now_ms, parse_iso
from ..closed import TranscriptDirs, locale_key, record_ended, transcript_dirs
from ..files import read_text_if_exists, remove_file
from ..jsjson import trim, utf16_len, utf16_slice
from ..parallel import Task
from ..processes import pid_alive
from ..profiles import AGENT_ENV, BUILTIN_PROFILES, TAB_ID_ENV
from ..scheduler import Job, Scheduler
from .codex_config import read_codex_config
from .db import MailError
from .history import Who, history, history_counts, log_id, older_than, previews, text_piece
from .hook import run_hook
from .notice import UNTRUSTED_NOTICE, wake_line
from .sessions import (
    AGENT_COLORS,
    HEARTBEAT_MS,
    IDLE_SETTLE_MS,
    MAIL_VERSION,
    Presence,
    agent_from_client,
    effective_state,
    is_agent_color,
    is_agent_type,
    is_effort,
    is_mod_driven,
    is_model,
    is_session_id,
    join_live_sessions,
    live_session,
    live_sessions,
    parse_presence,
    presence_path,
    read_presence,
    update_presence,
    with_state,
)
from .store import (
    MAX_READ_CHARS,
    MAX_TEXT_CHARS,
    check_message_id,
    claim_batch,
    clean_store,
    has_unread,
    log_native,
    put_back,
    send_message,
    set_delivery,
    settle_claim,
    take_batch,
    unread_summary,
    wait_for_message,
)

DEFAULT_WAIT_S = 60
MAX_WAIT_S = 600
# Antigravity CLI ends any MCP tool call after 3 minutes and has no setting to raise that.
AGY_MAX_WAIT_S = 170
CLEAN_EVERY_MS = 60 * 60 * 1000
RESTART_GRACE_MS = 60_000
START_RETRY_DELAYS_MS = (1_000, 3_000, 9_000)
REWAKE_EVERY_MS = 15_000
FOLLOW_UP_MS = MAX_WAIT_S * 1000
MOD_STATES = ("idle", "busy", "permission")
AGENT_ORDER = ("claude", "codex", "agy", "copilot", "gemini", "grok", "pi", "hermes", "opencode", "qwen", "goose", "codex-local")
MOD_DELIVERY_NOTE = "the recipient's Agent Tabs mod delivers it in-process once the session is idle"
CODEX_ID_PREFIX = "codex-"
SESSION_PREFIX_CHARS = 8

_AGENT_NAME = re.compile(r"[A-Za-z0-9._-]{1,64}")
_THREAD_ID = re.compile(r"[A-Za-z0-9-]{1,100}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_ID_PREFIXES = ("s-", CODEX_ID_PREFIX)
_SHORT_ID_CHARS = 4
_NAME_SLUG_CHARS = 24
_NAME_SUFFIX_CHARS = 2
_MAX_DATE_MS = 8.64e15


class Hosts(Protocol):
    def find_host(self, tab_id: str) -> str | None: ...

    def type_into(self, tab_id: str, host: str, text: str) -> dict[str, Any]: ...

    def describe_host(self, host: str) -> str | None: ...


class MessagingDeps(NamedTuple):
    home: str
    env: Mapping[str, str]
    pid: int
    cwd: str
    hosts: Hosts
    pid_start: int | None = None
    max_wait_s: float | None = None
    is_alive: Callable[[int], bool] | None = None
    random_id: Callable[[], str] | None = None
    now: Callable[[], float] | None = None
    sleep: Callable[[float], None] | None = None
    rewake_every_ms: float | None = None
    heartbeat_ms: float | None = None
    transcripts: TranscriptDirs | None = None
    scheduler: Scheduler | None = None


def _generated_id() -> str:
    return f"s-{os.urandom(6).hex()}"


def _may_be_tab(session_id: str) -> bool:
    return not session_id.startswith("s-") and not session_id.startswith(CODEX_ID_PREFIX)


def is_native_name(value: str) -> bool:
    return 1 <= utf16_len(value) <= 128 and _CONTROL.search(value) is None


def _agent_rank(agent: str) -> int:
    return AGENT_ORDER.index(agent) if agent in AGENT_ORDER else len(AGENT_ORDER)


def harness_of(agent: str, via: str | None = None) -> str:
    label = next((p.label for p in BUILTIN_PROFILES if p.name == agent), agent)
    return f"{label}{' via OpenRouter' if via == 'ori' else ''}"


def _id_core(session_id: str) -> str:
    prefix = next((p for p in _ID_PREFIXES if session_id.startswith(p) and len(session_id) > len(p)), None)
    rest = session_id[len(prefix) :] if prefix is not None else session_id
    return re.sub(r"[^A-Za-z0-9]", "", rest)


def folder_slug(folder: str) -> str:
    parts = [p for p in re.split(r"[\\/]+", folder) if p != ""]
    base = parts[-1] if parts else ""
    slug = "".join(c if re.fullmatch(r"[a-z0-9-]", c) else "-" * utf16_len(c) for c in base.lower())[:_NAME_SLUG_CHARS].strip("-")
    return "session" if slug == "" else slug


def _suffix_pool(session_id: str) -> str:
    core = re.sub(r"[^0-9a-f]", "", _id_core(session_id).lower())
    import hashlib

    return core + hashlib.sha256(session_id.encode("utf-8", "surrogatepass")).hexdigest()


def session_names(sessions: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    names: dict[str, str] = {}
    taken: set[str] = set()
    for s in sessions:
        if s.get("agent") == "claude" and s.get("nativeName") is not None:
            names[s["id"]] = s["nativeName"]
            taken.add(s["nativeName"])
    made = [(s["id"], folder_slug(s["path"]), _suffix_pool(s["id"])) for s in sessions if s["id"] not in names]
    for i, (session_id, slug, pool) in enumerate(made):
        rivals = [o[2] for j, o in enumerate(made) if j != i and o[1] == slug]
        n = _NAME_SUFFIX_CHARS
        while n < len(pool) and (f"{slug}-{pool[:n]}" in taken or any(r[:n] == pool[:n] for r in rivals)):
            n += 1
        names[session_id] = f"{slug}-{pool[:n]}"
    return names


def short_names(sessions: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    names: dict[str, str] = {}
    cores = [_id_core(s["id"]) for s in sessions]
    for i, s in enumerate(sessions):
        core = cores[i]
        rivals = [cores[j] for j, o in enumerate(sessions) if j != i and o["agent"] == s["agent"]]
        n = min(_SHORT_ID_CHARS, len(core))
        while n < len(core) and any(r[:n] == core[:n] for r in rivals):
            n += 1
        clash = any(r[:n] == core[:n] for r in rivals)
        names[s["id"]] = s["id"] if core == "" or clash else f"{s['agent']}-{core[:n]}"
    return names


def _shown(m: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"id": m["id"], "from": m["from"], "text": m["text"]}
    if m.get("replyTo") is not None:
        out["replyTo"] = m["replyTo"]
    out["sentAt"] = m["sentAt"]
    return out


def _ms(text: str | None) -> float:
    found = parse_iso(text or "")
    return math.nan if found is None else float(found)


def _date_iso(ms: float) -> str:
    if not math.isfinite(ms) or abs(ms) > _MAX_DATE_MS:
        raise MailError("Invalid time value")
    return iso(int(ms))


def _without(p: Presence, keys: Sequence[str]) -> Presence:
    return {k: v for k, v in p.items() if k not in keys}


class Messaging:
    def __init__(self, deps: MessagingDeps) -> None:
        self.deps = deps
        tab = deps.env.get(TAB_ID_ENV)
        self._is_tab = tab is not None and is_session_id(tab)
        self._session_id = tab if tab is not None and self._is_tab else self._new_id()
        self.agent = self._agent_from_env() or "unknown"
        self.started_at = iso(int(self._now()))
        self._last_clean = -math.inf
        self.thread_id: str | None = None
        self._own_host: Task[str | None] | None = None
        self._identify_lock = threading.Lock()
        self._follow_ups: dict[str, Job] = {}
        self._follow_lock = threading.Lock()
        self._heartbeat: Job | None = None
        self._start_error: str | None = None
        self._stopped = False
        self._scheduler = deps.scheduler

    @property
    def id(self) -> str:
        return self._session_id

    @property
    def is_tab(self) -> bool:
        return self._is_tab

    @property
    def scheduler(self) -> Scheduler:
        if self._scheduler is None:
            self._scheduler = Scheduler()
        return self._scheduler

    def _new_id(self) -> str:
        return (self.deps.random_id or _generated_id)()

    # A client that copies variables into a server's config, such as "${IDE_AGENT_TABS_AGENT}", may pass the text
    # unexpanded when the variable is unset.
    def _agent_from_env(self) -> str | None:
        value = self.deps.env.get(AGENT_ENV)
        return value if value is not None and _AGENT_NAME.fullmatch(value) is not None else None

    def _now(self) -> float:
        return (self.deps.now or now_ms)()

    def _sleep_ms(self, ms: float) -> None:
        if self.deps.sleep is not None:
            self.deps.sleep(ms)
        else:
            time.sleep(ms / 1000)

    @property
    def _alive(self) -> Callable[[int], bool]:
        return self.deps.is_alive or pid_alive

    @property
    def _beat_ms(self) -> float:
        return int(self.deps.heartbeat_ms) if self.deps.heartbeat_ms is not None else HEARTBEAT_MS

    def _ended(self, p: Presence, at: float) -> None:
        record_ended(self.deps.home, p, int(at), self.deps.transcripts or transcript_dirs(self.deps.env))

    def live(self) -> list[Presence]:
        return join_live_sessions(self.deps.home, self._alive, self._now(), self._ended)

    def record_end(self) -> None:
        own = read_presence(self.deps.home, self._session_id)
        if own is not None and own.get("pid") == self.deps.pid:
            self._ended(own, self._now())

    def _presence(self, current: Presence | None, host: Any = ...) -> Presence:
        cur = current or {}
        if host is ...:
            host = cur.get("host")
        p: Presence = {"id": self._session_id, "agent": self.agent, "path": self.deps.cwd, "pid": self.deps.pid}
        if self.deps.pid_start is not None:
            p["pidStart"] = self.deps.pid_start
        if host is not None:
            p["host"] = host
        p["startedAt"] = self.started_at
        p["state"] = cur.get("state") or "unknown"
        for key in ("stateAt", "nudges", "inputIdle"):
            if cur.get(key) is not None:
                p[key] = cur[key]
        if self.thread_id is not None:
            p["threadId"] = self.thread_id
        for key in ("handedOffTo", "driver", "modBeat", "nativeName", "via", "project", "model", "effort", "product"):
            if cur.get(key) is not None:
                p[key] = cur[key]
        p["beatMs"] = self._beat_ms
        p["mail"] = MAIL_VERSION
        return p

    def _start_heartbeat(self) -> None:
        self.stop_heartbeat()
        self._heartbeat = self.scheduler.every(self._beat_ms / 1000, self._beat)

    def _beat(self) -> None:
        file = presence_path(self.deps.home, self._session_id)
        at = self._now() / 1000
        try:
            os.utime(file, (at, at))
            touched = True
        except OSError:
            touched = False
        if not touched or (read_presence(self.deps.home, self._session_id) or {}).get("pid") != self.deps.pid:
            self._update_own(lambda p: p)

    def stop_heartbeat(self) -> None:
        if self._heartbeat is not None:
            self._heartbeat.cancel()
        self._heartbeat = None

    def start_registered(self, delays_ms: Sequence[float] = START_RETRY_DELAYS_MS, log: Callable[[str], None] | None = None) -> None:
        def attempt() -> bool:
            try:
                self.start()
            except Exception as e:  # noqa: BLE001
                self._start_error = str(e)
                if log is not None:
                    log(f"this session isn't registered: {self._start_error}")
                return False
            self._start_error = None
            return True

        if attempt():
            return
        pending = list(delays_ms)

        def retry() -> None:
            if self._stopped or attempt() or not pending:
                return
            self.scheduler.after(pending.pop(0) / 1000, retry)

        if pending:
            self.scheduler.after(pending.pop(0) / 1000, retry)

    def _warnings(self) -> dict[str, Any]:
        return {} if self._start_error is None else {"warnings": [f"this session isn't registered: {self._start_error}"]}

    def start(self) -> None:
        taken = False
        if self._is_tab:
            # A headless agent started from inside a tab inherits its IDE_AGENT_TABS_ID; the tab's own server keeps the id.
            def claim(current: Presence | None) -> Presence | None:
                nonlocal taken
                pid = (current or {}).get("pid")
                if pid is not None and pid != self.deps.pid and self._alive(pid):
                    taken = True
                    return current
                kept = self._left_by_dead_server(current)
                return self._presence(kept)

            update_presence(self.deps.home, self._session_id, claim)
        if taken:
            self._is_tab = False
            self._session_id = self._new_id()
        if not self._is_tab:
            update_presence(self.deps.home, self._session_id, lambda current: self._presence(current))
        if self._is_tab:
            self._own_host = Task(self._resolve_own_host, "agent-tabs-own-host")
        with contextlib.suppress(Exception):
            self._learn_codex_defaults()
        self._start_heartbeat()
        self.scheduler.soon(self._clean)

    # The agent of this tab can set its state just before this server starts, so only a state older than that
    # came from the dead server's agent.
    def _left_by_dead_server(self, current: Presence | None) -> Presence | None:
        if current is None or current.get("pid") is None or current["pid"] == self.deps.pid or self._alive(current["pid"]):
            return current
        rest = _without(current, ("nudges", "driver", "modBeat", "nativeName"))
        at = _ms(current.get("stateAt"))
        old = not math.isfinite(at) or at < _ms(self.started_at) - RESTART_GRACE_MS
        return {**rest, "state": "unknown"} if old and current.get("state") != "idle" else rest

    def _resolve_own_host(self) -> str | None:
        try:
            host = self.deps.hosts.find_host(self._session_id)
        except Exception:  # noqa: BLE001
            host = None
        if host is not None:
            fields = self._host_fields(host)
            with contextlib.suppress(Exception):
                self._update_own(lambda p: {**p, **fields})
        return host

    # A host id names one run of an IDE extension or one terminal, so the product label is kept in the
    # presence file: list_sessions still names the IDE after that endpoint is gone.
    def _host_fields(self, host: str) -> dict[str, str]:
        try:
            product = self.deps.hosts.describe_host(host)
        except Exception:  # noqa: BLE001
            product = None
        return {"host": host} if product is None else {"host": host, "product": product}

    # A model from open_tab or from a hook payload names what the session runs; the config is only its default.
    def _learn_codex_defaults(self) -> None:
        if self.agent != "codex":
            return
        config = read_codex_config(self.deps.env)
        if not config:
            return

        def learn(p: Presence) -> Presence:
            out = dict(p)
            if p.get("model") is None and "model" in config:
                out["model"] = config["model"]
            if p.get("effort") is None and "effort" in config:
                out["effort"] = config["effort"]
            return out

        self._update_own(learn)

    def note_thread(self, thread_id: object) -> None:
        with self._identify_lock:
            if not isinstance(thread_id, str) or _THREAD_ID.fullmatch(thread_id) is None or thread_id == self.thread_id:
                return
            self.thread_id = thread_id
            with contextlib.suppress(Exception):
                self._identify(thread_id)

    # The shared Codex daemon starts servers with the environment of whatever started the daemon, so its
    # IDE_AGENT_TABS_ID can name another tab or a closed one. A Codex session keeps that id only while the tab is open.
    def _identify(self, thread_id: str) -> None:
        new_id = f"{CODEX_ID_PREFIX}{thread_id}"
        tab_open = False
        if self._is_tab and self._own_host is not None:
            try:
                tab_open = self._own_host.result() is not None
            except Exception:  # noqa: BLE001
                tab_open = False
        if tab_open or self._session_id == new_id:
            self._update_own(lambda p: {**p, "threadId": thread_id})
            return
        old = self._session_id
        previous = read_presence(self.deps.home, old)
        carried = previous if previous is not None and previous.get("pid") == self.deps.pid else None
        taken = False
        self._session_id = new_id

        def claim(current: Presence | None) -> Presence | None:
            nonlocal taken
            pid = (current or {}).get("pid")
            if pid is not None and pid != self.deps.pid and self._alive(pid):
                taken = True
                return current
            return self._presence(current if current is not None else carried, (current or {}).get("host"))

        update_presence(self.deps.home, new_id, claim)
        if taken:
            self._session_id = old
            self._update_own(lambda p: {**p, "threadId": thread_id})
            return
        self._is_tab = False
        update_presence(self.deps.home, old, lambda current: None if (current or {}).get("pid") == self.deps.pid else current)

    def hook(self, event: str, data: Mapping[str, Any]) -> dict[str, Any] | None:
        return run_hook("codex", event, data, self.deps.home, self._session_id, self._now())

    def _update_own(self, change: Callable[[Presence], Presence]) -> Presence | None:
        def apply(current: Presence | None) -> Presence | None:
            if current is None:
                return change(self._presence(None))
            return change(current) if current.get("pid") == self.deps.pid else current

        return update_presence(self.deps.home, self._session_id, apply)

    def set_client(self, name: str | None) -> None:
        if self._agent_from_env() is not None:
            return
        self.agent = agent_from_client(name)
        self._update_own(lambda p: {**p, "agent": self.agent})
        with contextlib.suppress(Exception):
            self._learn_codex_defaults()

    def stop_sync(self) -> None:
        self._stopped = True
        self.stop_heartbeat()
        self.stop_follow_ups()
        file = presence_path(self.deps.home, self._session_id)
        try:
            if (parse_presence(read_text_if_exists(file)) or {}).get("pid") == self.deps.pid:
                remove_file(file)
        except OSError:
            return

    def _clean(self) -> None:
        now = self._now()
        if now - self._last_clean < CLEAN_EVERY_MS:
            return
        self._last_clean = now
        # Not joined: a tool call that joined this background scan could miss a session registered after it began.
        live = live_sessions(self.deps.home, self._alive, now, self._ended)
        clean_store(self.deps.home, {s["id"] for s in live}, now)

    def _clean_later(self) -> None:
        self.scheduler.soon(self._clean)

    def list_sessions(self) -> dict[str, Any]:
        now = self._now()
        sessions = join_live_sessions(self.deps.home, self._alive, now, self._ended)
        labels: dict[str, str | None] = {}
        for s in sessions:
            host = s.get("host")
            if host is not None and host not in labels:
                try:
                    labels[host] = self.deps.hosts.describe_host(host)
                except Exception:  # noqa: BLE001
                    labels[host] = None
        legacy = short_names(sessions)
        named = session_names(sessions)
        rows: list[dict[str, Any]] = []
        for s in sessions:
            native = s["agent"] == "claude" and s.get("nativeName") is not None and is_mod_driven(s, now)
            label = labels.get(s["host"]) if s.get("host") is not None else None
            product = label if label is not None else s.get("product")
            row: dict[str, Any] = {
                "name": named[s["id"]],
                "shortName": named[s["id"]],
                "legacyName": legacy[s["id"]],
                "id": s["id"],
                "session": utf16_slice(s["id"], 0, SESSION_PREFIX_CHARS),
                "agent": s["agent"],
                "route": "native" if native else "agent-tabs",
            }
            if s["agent"] == "claude" and s.get("nativeName") is not None:
                row["nativeName"] = s["nativeName"]
            row["state"] = s["state"]
            if s.get("stateAt") is not None:
                row["stateAt"] = s["stateAt"]
            row["harness"] = harness_of(s["agent"], s.get("via"))
            row["model"] = s.get("model")
            row["effort"] = s.get("effort")
            row["agentType"] = s.get("agentType")
            row["agentColor"] = s.get("agentColor")
            row["where"] = product
            row["tab"] = s["id"] if _may_be_tab(s["id"]) else None
            if product is None:
                row["host"] = None
            else:
                row["host"] = f"{product} ({s['project']})" if s.get("project") is not None else product
            row["ide"] = s.get("host")
            row["path"] = s["path"]
            row["folder"] = s["path"]
            if s.get("via") is not None:
                row["via"] = s["via"]
            row["startedAt"] = s["startedAt"]
            if s.get("handedOffTo") is not None:
                row["handedOffTo"] = s["handedOffTo"]
            row["self"] = s["id"] == self._session_id
            rows.append(row)
        rows.sort(key=lambda r: locale_key(r["id"]))
        rows.sort(key=lambda r: locale_key(r["startedAt"]))
        rows.sort(key=lambda r: locale_key(r["agent"]))
        rows.sort(key=lambda r: _agent_rank(r["agent"]))
        return {"sessions": rows, **self._warnings()}

    def send(self, given: Mapping[str, Any]) -> dict[str, Any]:
        to = given["to"]
        text = given["text"]
        reply_to = given.get("replyTo")
        if to == self._session_id:
            raise MailError("to is this session; pick another id from list_sessions")
        if trim(text) == "":
            raise MailError("text is empty")
        if utf16_len(text) > MAX_TEXT_CHARS:
            raise MailError(f"text exceeds {MAX_TEXT_CHARS} characters")
        if reply_to is not None:
            check_message_id(reply_to, "replyTo")
        now = self._now()
        recipient = live_session(self.deps.home, to, self._alive, now)
        if recipient is None:
            live = join_live_sessions(self.deps.home, self._alive, now, self._ended)
            named = session_names(live)
            legacy = short_names(live)
            recipient = (
                next((s for s in live if s["id"] == to), None)
                or next((s for s in live if named.get(s["id"]) == to), None)
                or next((s for s in live if legacy.get(s["id"]) == to), None)
            )
        if recipient is None and not is_session_id(to):
            raise MailError(f"not a session id: {to}")
        if recipient is None:
            warnings = self._warnings().get("warnings") or []
            warning = f". Warning: {warnings[0]}" if warnings else ""
            raise MailError(f"no live session with id or name {to}; call list_sessions{warning}")
        to = recipient["id"]
        if to == self._session_id:
            raise MailError("to is this session; pick another id from list_sessions")
        if (recipient.get("mail") or 0) < MAIL_VERSION:
            raise MailError(f"{to} runs an older Agent Tabs; restart that session to message it")
        out: dict[str, Any] = {"from": {"id": self._session_id, "agent": self.agent, "path": self.deps.cwd}, "to": to}
        if recipient.get("nativeName") is not None:
            out["toName"] = recipient["nativeName"]
        out["text"] = text
        if reply_to is not None:
            out["replyTo"] = reply_to
        queued = self._queued_without_wake(recipient, now)
        sent = send_message(self.deps.home, out, now, delivery=None if queued is None else queued["delivery"])
        if sent.get("duplicate"):
            return {
                "id": sent["id"],
                "to": to,
                "delivery": "queued",
                "duplicate": True,
                "note": "an identical message went to this session less than a minute ago; it was not sent again",
            }
        if queued is not None:
            wake = queued
        else:
            try:
                wake = self._wake(recipient, now)
            except Exception as e:  # noqa: BLE001
                wake = {"delivery": "queued", "note": f"Error: {e}"}
            with contextlib.suppress(Exception):
                set_delivery(self.deps.home, sent["id"], wake["delivery"])
        self._follow_up(to)
        self._clean_later()
        return {"id": sent["id"], "to": to, **wake, **self._warnings()}

    def _queued_without_wake(self, recipient: Presence, now: float) -> dict[str, Any] | None:
        if is_mod_driven(recipient, now):
            return {"delivery": "queued", "note": MOD_DELIVERY_NOTE}
        # A line typed while the user writes a prompt lands in that prompt; see _input_idle_after in hook.py.
        if effective_state(recipient, now) != "idle" or recipient.get("inputIdle") is False:
            return {"delivery": "queued"}
        return None

    def _wake(self, recipient: Presence, now: float) -> dict[str, Any]:
        queued = self._queued_without_wake(recipient, now)
        if queued is not None:
            return queued
        # An agent reports idle when its turn-end hook runs, but it can still be finishing the turn, and a
        # line typed then is lost; typing only after the session stays idle for IDLE_SETTLE_MS avoids that.
        settle = IDLE_SETTLE_MS - (now - _ms(recipient.get("stateAt")))
        if settle > IDLE_SETTLE_MS:
            return {"delivery": "queued"}
        if settle > 0:
            self._sleep_ms(settle)
            now = self._now()
        host = recipient.get("host")
        if host is None and _may_be_tab(recipient["id"]):
            host = self.deps.hosts.find_host(recipient["id"])
        if host is None:
            return {"delivery": "queued"}
        fields = {"host": host} if host == recipient.get("host") else self._host_fields(host)
        claimed: Presence | None = None

        def claim(current: Presence | None) -> Presence | None:
            nonlocal claimed
            if (
                current is None
                or current.get("pid") != recipient.get("pid")
                or current.get("stateAt") != recipient.get("stateAt")
                or effective_state(current, now) != "idle"
                or current.get("inputIdle") is False
            ):
                return current
            claimed = with_state({**current, **fields}, "waking", now)
            return claimed

        update_presence(self.deps.home, recipient["id"], claim)
        if claimed is None:
            return {"delivery": "queued"}
        typed = self._type_wake_line(recipient["id"], host)
        if not typed.get("ok") and recipient.get("host") is not None and _may_be_tab(recipient["id"]):
            try:
                found = self.deps.hosts.find_host(recipient["id"])
            except Exception:  # noqa: BLE001
                found = None
            if found is not None and found != host:
                state_at = claimed.get("stateAt")
                refound = self._host_fields(found)
                update_presence(
                    self.deps.home,
                    recipient["id"],
                    lambda current: {**current, **refound} if current is not None and current.get("stateAt") == state_at else current,
                )
                typed = self._type_wake_line(recipient["id"], found)
        if typed.get("ok"):
            return {"delivery": "woken"}
        self._restore_failed_wake(recipient, claimed)
        return {"delivery": "queued", "note": f"the session was idle, but typing the wake line failed: {typed.get('reason')}"}

    def _type_wake_line(self, session_id: str, host: str) -> dict[str, Any]:
        try:
            return self.deps.hosts.type_into(session_id, host, wake_line(self.agent, self._session_id))
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "reason": f"Error: {e}"}

    def _restore_failed_wake(self, recipient: Presence, claimed: Presence) -> None:
        def restore(current: Presence | None) -> Presence | None:
            if current is None or current.get("stateAt") != claimed.get("stateAt") or current.get("state") != "waking":
                return current
            out = {**current, "state": recipient["state"]}
            if recipient.get("stateAt") is not None:
                out["stateAt"] = recipient["stateAt"]
            return out

        update_presence(self.deps.home, recipient["id"], restore)

    def _rewake(self, peer: str) -> bool:
        if not has_unread(self.deps.home, peer, {"from": self._session_id}, self._now()):
            return False
        now = self._now()
        recipient = live_session(self.deps.home, peer, self._alive, now)
        if recipient is None:
            return False
        self._wake(recipient, now)
        return True

    @property
    def _rewake_s(self) -> float:
        return (self.deps.rewake_every_ms if self.deps.rewake_every_ms is not None else REWAKE_EVERY_MS) / 1000

    # A wake line can be lost, or the recipient can be in no state that allows one yet, so the sender keeps
    # retrying until the recipient reads the message, ends, or FOLLOW_UP_MS passes.
    def _follow_up(self, peer: str) -> None:
        with self._follow_lock:
            if peer in self._follow_ups or self._stopped:
                return
            until = self._now() + FOLLOW_UP_MS

            def stop() -> None:
                with self._follow_lock:
                    job = self._follow_ups.pop(peer, None)
                if job is not None:
                    job.cancel()

            def tick() -> None:
                if self._now() >= until:
                    stop()
                    return
                try:
                    pending = self._rewake(peer)
                except Exception:  # noqa: BLE001
                    pending = False
                if not pending:
                    stop()

            self._follow_ups[peer] = self.scheduler.every(self._rewake_s, tick)

    def follow_up_peers(self) -> list[str]:
        with self._follow_lock:
            return list(self._follow_ups)

    def stop_follow_ups(self) -> None:
        with self._follow_lock:
            jobs = list(self._follow_ups.values())
            self._follow_ups.clear()
        for job in jobs:
            job.cancel()

    def _reset_nudges(self) -> None:
        own = read_presence(self.deps.home, self._session_id)
        if own is not None and own.get("pid") == self.deps.pid and not own.get("nudges"):
            return
        with contextlib.suppress(Exception):
            self._update_own(lambda p: {**p, "nudges": 0} if p.get("nudges") else p)

    def mod_log(self, given: Mapping[str, Any]) -> dict[str, Any]:
        peer_name = given["peer"]
        direction = given["direction"]
        if not is_native_name(peer_name):
            raise MailError("peer must be one printable line of at most 128 characters")
        if direction not in ("sent", "received"):
            raise MailError("direction must be sent or received")
        own = read_presence(self.deps.home, self._session_id)
        me: dict[str, Any] = {"id": self._session_id, "agent": self.agent, "path": self.deps.cwd}
        if own is not None and own.get("nativeName") is not None:
            me["name"] = own["nativeName"]
        peer = {"name": peer_name}
        message_id = log_id(given.get("id"))
        at_given = given.get("at")
        at = _date_iso(at_given if at_given is not None and math.isfinite(at_given) else self._now())
        sent = direction == "sent"
        record: dict[str, Any] = {
            "id": message_id,
            "owner": self._session_id,
            "direction": direction,
            "from": me if sent else peer,
            "to": peer if sent else me,
            "text": utf16_slice(given["text"], 0, MAX_TEXT_CHARS),
            "sentAt": at,
        }
        if given.get("delivery") is not None:
            record["delivery"] = utf16_slice(given["delivery"], 0, 200)
        log_native(self.deps.home, record)
        return {"id": message_id}

    def _history_of(self, who: Who) -> list[dict[str, Any]]:
        if who.id is not None and not is_session_id(who.id):
            raise MailError(f"not a session id: {who.id}")
        names = [n for n in who.names if is_native_name(n)][:8]
        if who.id is None and not names:
            raise MailError("history needs session or names")
        return history(self.deps.home, Who(who.id, names))

    def mod_history(self, who: Who, before: str | None = None) -> dict[str, Any]:
        items = self._history_of(who)
        pool = older_than(items, before)
        messages = previews(pool)
        return {"total": len(items), "older": len(pool) - len(messages), "messages": messages}

    def mod_message(self, who: Who, message_id: str, offset: float = 0) -> dict[str, Any]:
        message = next((m for m in self._history_of(who) if m["id"] == message_id), None)
        if message is None:
            return {"message": None}
        total = utf16_len(message["text"])
        start = max(0, min(math.floor(offset), total))
        return {"message": {**message, "text": ""}, "text": text_piece(message["text"], start), "offset": start, "total": total}

    def session_folders(self) -> list[str]:
        live = join_live_sessions(self.deps.home, self._alive, self._now(), self._ended)
        return list(dict.fromkeys(s["path"] for s in live))

    def host_id(self) -> str | None:
        try:
            own = read_presence(self.deps.home, self._session_id)
        except Exception:  # noqa: BLE001
            return None
        return None if own is None else own.get("host")

    def mod_counts(self, whos: Sequence[Who]) -> dict[str, Any]:
        valid = [
            Who(who.id if who.id is not None and is_session_id(who.id) else None, [n for n in who.names if is_native_name(n)][:8])
            for who in whos
        ]
        counts = history_counts(self.deps.home, valid)
        return {"counts": [None if v.id is None and not v.names else n for v, n in zip(valid, counts)]}

    def mod_presence(self, given: Mapping[str, Any]) -> dict[str, Any]:
        native_name = given.get("nativeName")
        model = given.get("model")
        effort = given.get("effort")
        agent_type = given.get("agentType")
        agent_color = given.get("agentColor")
        owner = given.get("owner")
        driver = given.get("driver")
        state = given.get("state")
        if native_name is not None and not is_native_name(native_name):
            raise MailError("nativeName must be one printable line of at most 128 characters")
        if model is not None and not is_model(model):
            raise MailError("model must be one printable line of at most 128 characters")
        if effort is not None and not is_effort(effort):
            raise MailError("effort must be at most 32 letters, digits, dots, dashes or underscores")
        if agent_type is not None and not is_agent_type(agent_type):
            raise MailError("agentType must be at most 128 letters, digits, dots, colons, dashes or underscores")
        if agent_color is not None and not is_agent_color(agent_color):
            raise MailError(f"agentColor must be one of {', '.join(AGENT_COLORS)}")
        if owner is not None and not is_session_id(owner):
            raise MailError(f"not a session id: {owner}")
        now = self._now()
        claim = driver is True and self._is_tab
        # Only Claude Code is offered the mod tool, and its first call can beat the initialized notification's set_client.
        if self.agent == "unknown":
            self.agent = "claude"

        def change(p: Presence) -> Presence:
            base = {**p, "agent": self.agent}
            kept = _without(base, ("driver", "modBeat", "nativeName")) if driver is False else base
            stated = with_state(kept, state, now) if state is not None and state != effective_state(kept, now) else kept
            driven = driver is not False and (claim or p.get("driver") == "mod")
            out = dict(stated)
            if claim:
                out["driver"] = "mod"
            if driven:
                out["modBeat"] = int(now)
            if native_name is not None and driver is not False:
                out["nativeName"] = native_name
            if model is not None:
                out["model"] = model
            if effort is not None:
                out["effort"] = effort
            if agent_type is not None:
                out["agentType"] = agent_type
            if agent_color is not None and is_agent_color(agent_color):
                out["agentColor"] = agent_color
            if owner is not None:
                out["owner"] = owner
            return out

        self._update_own(change)
        own = read_presence(self.deps.home, self._session_id)
        return {"id": self._session_id, "tab": self._is_tab, "driver": (own or {}).get("driver") == "mod"}

    def mod_unread(self) -> dict[str, Any]:
        return unread_summary(self.deps.home, self._session_id, self._now())

    def mod_take(self) -> dict[str, Any]:
        batch = claim_batch(self.deps.home, self._session_id, math.inf, MAX_READ_CHARS, self._now())
        extra: dict[str, Any] = {}
        if batch["remaining"]:
            extra["remaining"] = batch["remaining"]
        if batch["unreadable"]:
            extra["unreadable"] = batch["unreadable"]
        if batch["claim"] is None or not batch["messages"]:
            return {"claim": None, "messages": [], **extra}
        return {"claim": batch["claim"], "notice": UNTRUSTED_NOTICE, "messages": [_shown(m) for m in batch["messages"]], **extra}

    def mod_settle(self, claim: str, op: str) -> dict[str, Any]:
        moved = settle_claim(self.deps.home, self._session_id, claim, op, self._now())
        if moved == 0:
            raise MailError(f"no open claim {claim}; an unsettled claim returns its messages to unread after two minutes")
        if op == "ack":
            self._reset_nudges()
            return {"claim": claim, "read": moved}
        return {"claim": claim, "released": moved}

    def read(self, cancel: threading.Event | None = None) -> dict[str, Any]:
        batch = take_batch(self.deps.home, self._session_id, None, chars=MAX_READ_CHARS, now=self._now())
        if cancel is not None and cancel.is_set():
            put_back(self.deps.home, self._session_id, batch["ids"], self._now())
            raise MailError("read_messages was cancelled; the messages stay unread")
        self._reset_nudges()
        self._clean_later()
        messages = batch["messages"]
        result: dict[str, Any] = {}
        if messages:
            result["notice"] = UNTRUSTED_NOTICE
        result["messages"] = [_shown(m) for m in messages]
        result.update(self._warnings())
        remaining = batch["remaining"]
        if remaining:
            result["remaining"] = remaining
            result["next"] = f"{remaining} more unread; call read_messages again"
        unreadable = batch["unreadable"]
        if unreadable:
            result["unreadable"] = unreadable
            result["unreadableNote"] = f"{unreadable} stored message(s) were not valid and were set aside"
        return result

    def wait(self, given: Mapping[str, Any], cancel: threading.Event | None = None) -> dict[str, Any]:
        sender = given.get("from")
        reply_to = given.get("replyTo")
        if sender is not None and not is_session_id(sender):
            raise MailError(f"from is not a session id: {sender}")
        if reply_to is not None:
            check_message_id(reply_to, "replyTo")
        limit = self.deps.max_wait_s if self.deps.max_wait_s is not None else MAX_WAIT_S
        cap = min(limit, AGY_MAX_WAIT_S if self.agent == "agy" else MAX_WAIT_S)
        timeout = given.get("timeout")
        seconds = min(max(DEFAULT_WAIT_S if timeout is None else timeout, 0), cap)
        flt: dict[str, str] = {}
        if sender is not None:
            flt["from"] = sender
        if reply_to is not None:
            flt["replyTo"] = reply_to
        retry: Job | None = None
        if sender is not None:
            peer = sender

            def rewake() -> None:
                with contextlib.suppress(Exception):
                    self._rewake(peer)

            retry = self.scheduler.every(self._rewake_s, rewake)
        try:
            message = wait_for_message(self.deps.home, self._session_id, flt, seconds * 1000, cancel, self._now)
        finally:
            if retry is not None:
                retry.cancel()
        if message is None:
            return {"message": None, "timedOut": True, "waitedSeconds": seconds}
        self._reset_nudges()
        return {"notice": UNTRUSTED_NOTICE, "message": _shown(message)}

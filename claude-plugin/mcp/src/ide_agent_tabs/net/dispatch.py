from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import sqlite3
import stat
import threading
import unicodedata
from collections import OrderedDict, deque
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from ..liveness import pid_alive
from ..messaging.db import MailError
from ..messaging.sessions import SESSIONS_DIR, Presence, is_session_id, live_session
from ..messaging.store import send_message
from . import wire
from .audit import ADMITTED, REFUSED, Audit, AuditError, AuditRecord
from .hubsync import HubRefusal, HubSync
from .outbound import is_inside
from .policy import (
    FolderGrant,
    NetworkConfig,
    PeerGrant,
    Policy,
    PolicyError,
    folder_grant,
    grant_for,
    load_network_config,
    load_policy,
    model_ok,
)
from .remote_launch import RemoteLaunchError, claude_help_text, plan_remote_open
from .store import COMPLETED, ConflictError, NetStore, NetStoreError, Reservation, db_path
from .transport import network_enabled

REPARSE_POINT = 0x400
REMEMBERED_REQUESTS = 256
MINUTE_MS = 60_000
HOUR_MS = 3_600_000
MAX_NOTICES = 256
_REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_BAD_NAME_CATEGORIES = frozenset({"Cc", "Cs", "Zl", "Zp"})
_SHELL_UNSAFE = frozenset("\"'`$;|&<>%")


class TabService(Protocol):
    def open_tab(self, given: Mapping[str, Any]) -> dict[str, Any]: ...

    def close_tab(self, tab_id: str | None = None) -> dict[str, Any]: ...

    def list_tabs(self, ide: str | None = None) -> dict[str, Any]: ...


class _Refusal(Exception):
    def __init__(self, reason: str, outcome: str = "refused") -> None:
        super().__init__(reason)
        self.reason = reason
        self.outcome = outcome


@dataclass
class _Trace:
    peer: str
    request_id: str | None
    op: str | None = None
    generation: int | None = None
    tab: str | None = None
    outcome: str = "completed"
    admitted: bool = False
    effect: bool = False


def raw_request_id(request: object) -> str | None:
    value = request.get("id") if isinstance(request, dict) else None  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    return value if isinstance(value, str) and _REQUEST_ID.fullmatch(value) is not None else None


def fallback_request_id(request: object) -> str:
    return raw_request_id(request) or "invalid"


def session_generation(presence: Mapping[str, Any]) -> str | None:
    started = presence.get("startedAt")
    return started if isinstance(started, str) and started else None


def _unsafe_component(path: str) -> bool:
    try:
        status = os.lstat(path)
    except OSError:
        return True
    return stat.S_ISLNK(status.st_mode) or bool(getattr(status, "st_file_attributes", 0) & REPARSE_POINT)


def _ancestry(path: str) -> list[str]:
    chain = [path]
    while True:
        parent = os.path.dirname(chain[-1])
        if parent == chain[-1]:
            return chain[::-1]
        chain.append(parent)


def folder_problem(folder: FolderGrant) -> str | None:
    path = folder.path
    if path.startswith(("\\\\", "//")) or not os.path.isabs(path):
        return "not-local"
    try:
        status = os.stat(path)
    except OSError:
        return "missing"
    if not stat.S_ISDIR(status.st_mode) or (status.st_dev, status.st_ino) != (folder.dev, folder.ino):
        return "identity"
    if any(_unsafe_component(part) for part in _ancestry(path)):
        return "link"
    return None


def _shell_safe_folder(path: str) -> bool:
    return path.isprintable() and not any(char in _SHELL_UNSAFE for char in path) and os.path.isdir(path)


def _digest(request: object) -> str:
    return hashlib.sha256(json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()


def _clean_name(name: str) -> str:
    return "".join(" " if unicodedata.category(c) in _BAD_NAME_CATEGORIES else c for c in name)[: wire.MAX_NAME_CHARS]


class NetDispatcher:
    def __init__(
        self,
        home: str,
        service_factory: Callable[[], TabService],
        now_ms: Callable[[], int],
        log: Callable[[str], None],
        store: NetStore | None = None,
        claude_help: Callable[[], str] = claude_help_text,
        alive: Callable[[int], bool] = pid_alive,
        audit: Audit | None = None,
        hub_sync: HubSync | None = None,
    ) -> None:
        self.home = home
        self._service_factory = service_factory
        self._now_ms = now_ms
        self._log = log
        self._claude_help = claude_help
        self._alive = alive
        self._audit = audit if audit is not None else Audit(home, now_ms)
        self._store = store
        self._service: TabService | None = None
        self._lock = threading.Lock()
        self._sent: dict[str, deque[int]] = {}
        self._seen: dict[str, OrderedDict[str, tuple[str, dict[str, Any]]]] = {}
        self._noticed: OrderedDict[tuple[str, str], int] = OrderedDict()
        self._hub_sync = hub_sync

    def handle(self, peer: str, request: object) -> dict[str, Any]:
        trace = _Trace(peer if isinstance(peer, str) else "", raw_request_id(request))  # pyright: ignore[reportUnnecessaryIsInstance]
        reply_id = fallback_request_id(request)
        try:
            self._audit.check()
        except AuditError as e:
            self._log(f"net: refusing a request: {e}")
            return wire.refused_reply(reply_id)
        reply: dict[str, Any]
        reason = "ok"
        try:
            reply = wire.ok_reply(reply_id, self._process(trace, request))
        except _Refusal as r:
            reason, trace.outcome = r.reason, r.outcome
            reply = wire.refused_reply(reply_id)
        except Exception as e:  # noqa: BLE001 - any internal failure becomes the generic refusal
            self._log(f"net: {trace.op or 'request'} failed: {type(e).__name__}")
            reason, trace.outcome = "internal", "failed"
            reply = wire.refused_reply(reply_id)
        try:
            self._audit.write(
                AuditRecord(
                    trace.peer,
                    trace.request_id,
                    trace.op,
                    ADMITTED if trace.admitted else REFUSED,
                    trace.outcome,
                    reason,
                    trace.generation,
                    trace.tab,
                )
            )
        except AuditError as e:
            self._log(f"net: {e}")
            if not trace.effect:
                return wire.refused_reply(reply_id)
        return reply

    def is_remote_origin(self, tab_id: str) -> str | None:
        if self._store is None and not os.path.exists(db_path(self.home)):
            return None
        store = self._open_store()
        return store.remote_tab_peer(tab_id) or store.opened_by(tab_id)

    def _tabs(self) -> TabService:
        with self._lock:
            if self._service is None:
                self._service = self._service_factory()
            return self._service

    def _live_tab_ids(self) -> set[str]:
        rows = self._tabs().list_tabs().get("tabs")
        return {t["id"] for t in rows if isinstance(t, dict) and isinstance(t.get("id"), str)} if isinstance(rows, list) else set()  # pyright: ignore[reportUnknownVariableType]

    def _open_store(self) -> NetStore:
        with self._lock:
            if self._store is not None:
                return self._store
        store = NetStore(self.home)
        try:
            store.reconcile(self._live_tab_ids())
            store.prune(self._now_ms())
        except BaseException:
            store.close()
            raise
        with self._lock:
            if self._store is None:
                self._store = store
                return store
        store.close()
        return self._open_store()

    def _owner(self, store: NetStore, tab: str) -> str | None:
        return store.remote_tab_peer(tab) or store.opened_by(tab)

    def _process(self, trace: _Trace, request: object) -> dict[str, Any]:
        config, _ = load_network_config(self.home)
        if not network_enabled(config):
            raise _Refusal("network-off")
        try:
            policy = load_policy(self.home)
        except PolicyError:
            raise _Refusal("policy-invalid") from None
        trace.generation = policy.generation
        grant = grant_for(policy, trace.peer)
        if grant is None:
            raise _Refusal("unknown-peer")
        try:
            parsed = wire.parse_request(request)
        except wire.WireError:
            raise _Refusal("bad-request") from None
        trace.op = parsed.op
        body = parsed.body
        if isinstance(body, (wire.IntroduceBody, wire.RemoveBody, wire.RekeyBody)):
            return self._hub(trace, grant, body)
        if isinstance(body, wire.OpenBody):
            return self._open(trace, policy, grant, parsed.id, request, body, config)
        if isinstance(body, wire.CloseBody):
            return self._close(trace, policy, grant, parsed.id, request, body)
        if isinstance(body, wire.SendBody):
            return self._send(trace, grant, parsed.id, request, body)
        return self._inventory(trace, grant, config.member_folders)

    def _granted_folder(self, grant: PeerGrant, body: wire.OpenBody) -> FolderGrant:
        if body.folder is None:
            raise _Refusal("path-not-granted")
        if body.agent not in grant.agents:
            raise _Refusal("agent-not-granted")
        folder = folder_grant(grant, body.folder)
        if folder is None:
            raise _Refusal("folder-not-granted")
        problem = folder_problem(folder)
        if problem is not None:
            raise _Refusal(f"folder-{problem}")
        if body.model is not None and body.model not in grant.models:
            raise _Refusal("model-not-granted")
        return folder

    def _member_folder(self, grant: PeerGrant, body: wire.OpenBody, member_folders: tuple[str, ...]) -> FolderGrant:
        if body.path is None:
            raise _Refusal("path-required")
        if body.model is not None and not model_ok(body.model):
            raise _Refusal("model-invalid")
        path = os.path.normpath(body.path)
        if not os.path.isabs(path):
            raise _Refusal("not-local")
        if not any(is_inside(path, folder) for folder in member_folders):
            self._notify_folder_refused(grant, path)
            raise _Refusal("folder-not-allowed")
        try:
            status = os.stat(path)
        except OSError:
            raise _Refusal("folder-missing") from None
        folder = FolderGrant("member", path, status.st_dev, status.st_ino)
        problem = folder_problem(folder)
        if problem is not None:
            raise _Refusal(f"folder-{problem}")
        return folder

    def _notify_folder_refused(self, grant: PeerGrant, path: str) -> None:
        now = self._now_ms()
        key = (grant.device_id, os.path.normcase(path))
        with self._lock:
            last = self._noticed.get(key)
            if last is not None and now - last < HOUR_MS:
                return
            self._noticed[key] = now
            self._noticed.move_to_end(key)
            while len(self._noticed) > MAX_NOTICES:
                self._noticed.popitem(last=False)
        label = wire.escape_display(grant.label)
        if _shell_safe_folder(path):
            text = (
                f"The hub member {label} asked to open a tab in {path}, which is outside network.memberFolders, "
                "so this machine refused. To allow it, ask your user, then run: "
                f'agent-tabs net folders add "{path}"'
            )
        else:
            text = (
                f"{label} asked to open a tab outside network.memberFolders; this machine refused. "
                "Run agent-tabs net folders to see the list."
            )
        self._notice_sessions(text)

    def _notice_sessions(self, text: str) -> None:
        try:
            store = self._open_store()
            for session in self._live_sessions():
                if self._owner(store, session["id"]) is None:
                    with contextlib.suppress(_Refusal):
                        self._deliver("net-notice", session["id"], text, None, "notice-failed")
        except Exception as e:  # noqa: BLE001 - a notice is best effort; the request outcome stands either way
            self._log(f"net: delivering a notice failed: {type(e).__name__}")

    def _hub(self, trace: _Trace, grant: PeerGrant, body: wire.HubBody) -> dict[str, Any]:
        if self._hub_sync is None:
            self._hub_sync = HubSync(self.home, self._log, self._notice_sessions, lambda: self._open_store().bump_generation())
        try:
            reply = self._hub_sync.handle(grant, body)
        except HubRefusal as refusal:
            raise _Refusal(refusal.reason) from None
        trace.admitted = True
        trace.effect = True
        return reply

    def _open(
        self, trace: _Trace, policy: Policy, grant: PeerGrant, request_id: str, request: object, body: wire.OpenBody, config: NetworkConfig
    ) -> dict[str, Any]:
        peer = trace.peer
        max_global = config.max_remote_tabs
        if grant.member:
            folder = self._member_folder(grant, body, config.member_folders)
        else:
            folder = self._granted_folder(grant, body)
        try:
            plan = plan_remote_open(body.agent, folder.path, self._claude_help, grant.label)
        except RemoteLaunchError:
            raise _Refusal("launch-plan") from None
        if body.model is not None:
            plan["model"] = body.model
        store = self._open_store()
        generation = store.generation()
        try:
            reservation = store.reserve_open(
                peer,
                request_id,
                _digest(request),
                generation,
                self._now_ms(),
                grant.max_tabs,
                max_global,
                grant.opens_per_hour,
                self._live_tab_ids(),
            )
        except ConflictError:
            raise _Refusal("id-conflict") from None
        if reservation is None:
            raise _Refusal("limit")
        if reservation.replayed:
            return self._replayed(trace, reservation, "tab")
        trace.admitted = True

        def still_granted() -> bool:
            try:
                current = load_policy(self.home)
            except PolicyError:
                return False
            now = grant_for(current, peer)
            if now is None or current.generation != policy.generation or now.member != grant.member or folder_problem(folder) is not None:
                return False
            if now.member:
                latest, _ = load_network_config(self.home)
                return any(is_inside(folder.path, f) for f in latest.member_folders)
            return (
                body.folder is not None
                and body.agent in now.agents
                and folder_grant(now, body.folder) == folder
                and (body.model is None or body.model in now.models)
            )

        if not store.dispatch_handoff(peer, request_id, generation, still_granted):
            raise _Refusal("superseded")
        trace.effect = True
        try:
            store.begin_launch(peer, request_id, os.path.realpath(folder.path), self._now_ms())
            opened = self._tabs().open_tab(plan)
            tab = opened.get("id")
            if not isinstance(tab, str) or not is_session_id(tab):
                raise ValueError("no tab id")
        except Exception as e:  # noqa: BLE001 - the launcher can fail in many ways; the peer gets the generic refusal
            self._log(f"net: opening a tab failed: {type(e).__name__}")
            store.refuse(peer, request_id)
            self._end_launch(store, peer, request_id)
            raise _Refusal("launch-failed", "failed") from None
        trace.tab = tab
        store.complete(peer, request_id, tab)
        store.record_remote_tab(tab, peer, self._now_ms())
        self._end_launch(store, peer, request_id)
        self._deliver(f"{grant.label}-remote", tab, body.task, None, "task-not-delivered")
        return {"tab": tab}

    def _end_launch(self, store: NetStore, peer: str, request_id: str) -> None:
        try:
            store.end_launch(peer, request_id)
        except (NetStoreError, sqlite3.Error) as e:
            self._log(f"net: clearing a launch failed: {type(e).__name__}")

    def _replayed(self, trace: _Trace, reservation: Reservation, key: str) -> dict[str, Any]:
        if reservation.state != COMPLETED or reservation.tab_id is None:
            raise _Refusal(f"replay-{reservation.state}")
        trace.admitted = True
        trace.outcome = "replayed"
        trace.tab = reservation.tab_id
        return {"tab": reservation.tab_id} if key == "tab" else {"closed": True}

    def _deliver(self, sender: str, to: str, text: str, reply_to: str | None, reason: str) -> str:
        out: dict[str, Any] = {"from": {"id": sender, "agent": "remote", "path": ""}, "to": to, "text": text}
        if reply_to is not None:
            out["replyTo"] = reply_to
        try:
            return str(send_message(self.home, out)["id"])
        except (MailError, OSError) as e:
            self._log(f"net: delivering a message failed: {type(e).__name__}")
            raise _Refusal(reason, "failed") from None

    def _close(
        self, trace: _Trace, policy: Policy, grant: PeerGrant, request_id: str, request: object, body: wire.CloseBody
    ) -> dict[str, Any]:
        peer = trace.peer
        store = self._open_store()
        if self._owner(store, body.tab) != peer:
            raise _Refusal("not-opened-by-peer")
        generation = store.generation()
        try:
            reservation = store.reserve(peer, request_id, _digest(request), "close", generation, self._now_ms(), body.tab)
        except ConflictError:
            raise _Refusal("id-conflict") from None
        trace.tab = body.tab
        if reservation.replayed:
            return self._replayed(trace, reservation, "closed")
        trace.admitted = True

        def still_granted() -> bool:
            try:
                current = load_policy(self.home)
            except PolicyError:
                return False
            return grant_for(current, peer) is not None and current.generation == policy.generation

        if not store.dispatch_handoff(peer, request_id, generation, still_granted):
            raise _Refusal("superseded")
        trace.effect = True
        try:
            self._tabs().close_tab(body.tab)
        except Exception as e:  # noqa: BLE001 - the launcher can fail in many ways; the peer gets the generic refusal
            self._log(f"net: closing a tab failed: {type(e).__name__}")
            store.refuse(peer, request_id)
            raise _Refusal("close-failed", "failed") from None
        store.complete(peer, request_id, body.tab)
        return {"closed": True}

    def _rate_allows(self, peer: str, limit: int) -> bool:
        now = self._now_ms()
        with self._lock:
            window = self._sent.setdefault(peer, deque())
            while window and now - window[0] >= MINUTE_MS:
                window.popleft()
            if len(window) >= limit:
                return False
            window.append(now)
            return True

    def _remember(self, peer: str, request_id: str, digest: str, reply: dict[str, Any]) -> None:
        with self._lock:
            seen = self._seen.setdefault(peer, OrderedDict())
            seen[request_id] = (digest, reply)
            while len(seen) > REMEMBERED_REQUESTS:
                seen.popitem(last=False)

    def _send(self, trace: _Trace, grant: PeerGrant, request_id: str, request: object, body: wire.SendBody) -> dict[str, Any]:
        peer = trace.peer
        digest = _digest(request)
        with self._lock:
            known = self._seen.get(peer, {}).get(request_id)
        if known is not None:
            if known[0] != digest:
                raise _Refusal("id-conflict")
            trace.admitted = True
            trace.outcome = "replayed"
            return dict(known[1])
        store = self._open_store()
        now = self._now_ms()
        sender = f"{grant.label}-remote"
        owned = self._owner(store, body.to) == peer
        direct = owned or body.to in grant.send
        allowed = direct or grant.member
        presence = live_session(self.home, body.to, self._alive, now)
        if not direct and body.reply_to is not None and presence is not None:
            generation = session_generation(presence)
            remote = None if generation is None else store.find_reply_grant(peer, body.to, generation, body.reply_to, now)
            if remote is not None:
                allowed = True
                sender = f"{remote}@{grant.label}"
        if not allowed:
            raise _Refusal("target-not-granted")
        if presence is None and not owned:
            raise _Refusal("no-session")
        if not self._rate_allows(peer, grant.messages_per_minute):
            raise _Refusal("rate-limit")
        trace.admitted = True
        trace.effect = True
        reply = {"id": self._deliver(sender, body.to, body.text, body.reply_to, "delivery-failed")}
        self._remember(peer, request_id, digest, reply)
        return reply

    def _live_sessions(self) -> list[Presence]:
        now = self._now_ms()
        folder = os.path.join(self.home, SESSIONS_DIR)
        try:
            files = sorted(n for n in os.listdir(folder) if n.endswith(".json"))
        except OSError:
            files = []
        return [p for p in (live_session(self.home, n[:-5], self._alive, now) for n in files if is_session_id(n[:-5])) if p is not None]

    def _inventory(self, trace: _Trace, grant: PeerGrant, member_folders: tuple[str, ...]) -> dict[str, Any]:
        from ..messaging.messaging import session_names

        trace.admitted = True
        store = self._open_store()
        live = self._live_sessions()
        names = session_names(live)
        rows: list[wire.RemoteSession] = []
        for p in live:
            if not grant.member and p["id"] not in grant.send and self._owner(store, p["id"]) != trace.peer:
                continue
            if p["agent"] in wire.SESSION_AGENTS and p["state"] in wire.STATUSES:
                rows.append(wire.RemoteSession(p["id"], _clean_name(names[p["id"]]), p["agent"], p["state"]))
        folders = [f for f in member_folders if wire.valid_path(f)][: wire.MAX_FOLDERS] if grant.member else None
        try:
            return wire.inventory_body(rows[: wire.MAX_SESSIONS], folders)
        except wire.WireError:
            raise _Refusal("inventory-invalid", "failed") from None

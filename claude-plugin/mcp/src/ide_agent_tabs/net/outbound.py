from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from collections import deque
from collections.abc import Callable, Mapping
from typing import Any

from ..clock import now_ms as current_ms
from ..jsjson import trim, utf16_len
from ..messaging.db import MailError
from ..messaging.sessions import read_presence
from ..messaging.store import MAX_TEXT_CHARS, check_message_id
from . import wire
from .origin import REMOTE_ORIGIN_REFUSAL, Caller, OriginChecker
from .policy import ALIAS, DEFAULT_PORT, NetworkConfig, PeerGrant, PolicyError, load_network_config, load_policy
from .store import NetStore, NetStoreError
from .transport import network_enabled

RATE_LIMIT = 10
WINDOW_MS = 60_000
MAX_WINDOWS = 1024
SEND_TIMEOUT_S = 30.0
OPEN_TIMEOUT_S = 60.0
OFFLINE_AFTER_MS = 150_000
NETWORK_OFF = "refused: the network is off on this machine, so nothing goes to another machine"
FOLDER_REFUSAL = "refused: this session's folder is not in network.outboundFolders, so it can't send to another machine"
RATE_REFUSAL = f"refused: this session sent {RATE_LIMIT} requests to other machines in the last minute; try again later"
PEER_REFUSED = "the peer refused the request"
FOLDER_NOT_ALLOWED = (
    "{label} refused: {path} is not in that machine's member folders. Its user can add it there with agent-tabs net folders add."
)
PEER_REPLY_INVALID = "the peer's reply was not valid, so the outcome is unknown"
OPEN_REFUSED_FIELDS = ("args", "env", "ide", "via", "focus")

_TARGET = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]{0,127})@([a-z0-9-]{1,32})")
_LABEL = re.compile(r"[a-z0-9-]{1,32}")
_MESSAGE_ID = re.compile(r"m-[0-9a-f]{16}")
_TAB_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


class OutboundError(Exception):
    pass


def parse_remote_target(to: object) -> tuple[str, str] | None:
    found = _TARGET.fullmatch(to) if isinstance(to, str) else None
    return None if found is None else (found.group(1), found.group(2))


def is_inside(path: str, folder: str) -> bool:
    def normal(value: str) -> str:
        return os.path.normcase(os.path.realpath(value))

    root = normal(folder)
    try:
        return os.path.commonpath([normal(path), root]) == root
    except ValueError:
        return False


def _field(reply: Mapping[str, Any], key: str, pattern: re.Pattern[str]) -> str:
    body = reply.get("body")
    value = body.get(key) if isinstance(body, dict) else None  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise OutboundError(PEER_REPLY_INVALID)
    return value


class Outbound:
    def __init__(
        self,
        home: str,
        *,
        now_ms: Callable[[], int] = current_ms,
        request: Callable[..., dict[str, Any]] | None = None,
        nudge: Callable[[str, str], None] | None = None,
        origin: OriginChecker | None = None,
        open_store: Callable[[str], NetStore] = NetStore,
        start: Callable[[Any], int] | None = None,
        server_port: int | None = None,
    ) -> None:
        self.home = home
        self.server_port = server_port
        self._now = now_ms
        self._request = request
        self._nudge = nudge
        self._origin = origin if origin is not None else OriginChecker(home, now_ms)
        self._open_store = open_store
        self._start = start
        self._lock = threading.Lock()
        self._windows: dict[str, deque[int]] = {}
        self._store: NetStore | None = None

    def close(self) -> None:
        self._origin.close()
        with self._lock:
            store, self._store = self._store, None
        if store is not None:
            store.close()

    def _db(self) -> NetStore:
        with self._lock:
            if self._store is None:
                self._store = self._open_store(self.home)
            return self._store

    def require_local_origin(self, caller: Caller) -> None:
        if self._origin.is_remote(caller):
            raise OutboundError(REMOTE_ORIGIN_REFUSAL)

    def _admit(self, caller: Caller) -> NetworkConfig:
        config, _ = load_network_config(self.home)
        if not network_enabled(config):
            raise OutboundError(NETWORK_OFF)
        self.require_local_origin(caller)
        if not any(is_inside(caller.path, folder) for folder in config.outbound_folders):
            raise OutboundError(FOLDER_REFUSAL)
        return config

    def _peer(self, label: str) -> PeerGrant:
        try:
            peers = load_policy(self.home).peers
        except PolicyError:
            raise OutboundError("the peer policy can't be read") from None
        found = next((p for p in peers if p.label == label), None)
        if found is None:
            raise OutboundError(f"no trusted peer is labeled {label}")
        return found

    def _take_slot(self, session: str) -> None:
        now = self._now()
        with self._lock:
            if session not in self._windows and len(self._windows) >= MAX_WINDOWS:
                self._windows.clear()
            window = self._windows.setdefault(session, deque())
            while window and now - window[0] >= WINDOW_MS:
                window.popleft()
            if len(window) >= RATE_LIMIT:
                raise OutboundError(RATE_REFUSAL)
            window.append(now)

    def _call(self, peer: PeerGrant, op: str, body: Mapping[str, Any], timeout: float) -> dict[str, Any]:
        from .client import NetClientError, send_request

        try:
            reply = (self._request or send_request)(self.home, peer.device_id, op, body, timeout=timeout, port=DEFAULT_PORT)
        except NetClientError as e:
            raise OutboundError(str(e)) from e
        if reply.get("ok") is not True:
            raise OutboundError(_refusal_text(peer, reply, body))
        return reply

    def _nudge_poller(self, peer: PeerGrant) -> None:
        if self._nudge is not None:
            self._nudge(self.home, peer.device_id)
            return
        from .poller import nudge

        nudge(self.home, peer.device_id)

    def send(self, caller: Caller, given: Mapping[str, Any]) -> dict[str, Any]:
        from .dispatch import session_generation

        target = parse_remote_target(given.get("to"))
        text = given.get("text")
        reply_to = given.get("replyTo")
        if target is None:
            raise OutboundError("to must be <session id>@<peer label> for a session on another machine")
        if not isinstance(text, str) or trim(text) == "":
            raise OutboundError("text is empty")
        if utf16_len(text) > MAX_TEXT_CHARS:
            raise OutboundError(f"text exceeds {MAX_TEXT_CHARS} characters")
        if reply_to is not None:
            try:
                check_message_id(reply_to, "replyTo")
            except MailError as e:
                raise OutboundError(str(e)) from e
        remote, label = target
        self._admit(caller)
        peer = self._peer(label)
        self._take_slot(caller.session)
        body: dict[str, Any] = {"to": remote, "text": text}
        if reply_to is not None:
            body["replyTo"] = reply_to
        message_id = _field(self._call(peer, "send", body, SEND_TIMEOUT_S), "id", _MESSAGE_ID)
        result: dict[str, Any] = {"id": message_id, "delivery": "remote"}
        presence = read_presence(self.home, caller.session)
        generation = None if presence is None else session_generation(presence)
        if generation is not None:
            try:
                self._db().add_reply_grant(caller.session, generation, peer.device_id, remote, message_id, self._now())
            except (NetStoreError, sqlite3.Error, OSError):
                result["note"] = "the message went out, but the reply grant could not be recorded, so the peer can't answer it"
        self._nudge_poller(peer)
        return result

    def open(self, caller: Caller, given: Mapping[str, Any]) -> dict[str, Any]:
        label = given.get("host")
        if not isinstance(label, str) or _LABEL.fullmatch(label) is None:
            raise OutboundError("host must be a peer label")
        for key in OPEN_REFUSED_FIELDS:
            if given.get(key):
                raise OutboundError(f"{key} can't be set when host is given; the peer builds the launch itself")
        agent, folder, task, model = given.get("agent"), given.get("path"), given.get("prompt"), given.get("model")
        if agent not in wire.OPEN_AGENTS:
            raise OutboundError(f"agent must be one of {', '.join(wire.OPEN_AGENTS)} when host is given")
        if not isinstance(folder, str) or (ALIAS.fullmatch(folder) is None and not wire.valid_path(folder)):
            raise OutboundError("path must be a folder alias the peer granted when host is given")
        if not isinstance(task, str) or trim(task) == "" or len(task) > wire.MAX_TASK_CHARS:
            raise OutboundError(f"prompt is the task and must hold 1 to {wire.MAX_TASK_CHARS} characters when host is given")
        self._admit(caller)
        peer = self._peer(label)
        if peer.member:
            if not wire.valid_path(folder):
                raise OutboundError("path must be an absolute folder path on the peer when host is a hub member")
            body: dict[str, Any] = {"agent": agent, "path": folder, "task": task}
        else:
            if ALIAS.fullmatch(folder) is None:
                raise OutboundError("path must be a folder alias the peer granted when host is given")
            body = {"agent": agent, "folder": folder, "task": task}
        self._take_slot(caller.session)
        if model is not None:
            body["model"] = model
        tab = _field(self._call(peer, "open", body, OPEN_TIMEOUT_S), "tab", _TAB_ID)
        self._nudge_poller(peer)
        return {"remote": label, "tab": tab}

    def start_listener(self, caller: Caller) -> dict[str, Any]:
        config, _ = load_network_config(self.home)
        if not network_enabled(config) or config.bind is None:
            raise OutboundError("refused: the user has not turned the network on with a bind address; this tool only restarts the listener")
        self.require_local_origin(caller)
        from .cli import Services, main

        lines: list[str] = []
        problems: list[str] = []
        kwargs: dict[str, Any] = {} if self.server_port is None else {"server_port": self.server_port}
        services = Services(home=self.home, out=lines.append, err=problems.append, **kwargs)
        code = self._start(services) if self._start is not None else main(["lan", "start"], services)
        if code != 0:
            raise OutboundError("; ".join(problems or lines) or "the listener did not start")
        return {"started": True, "status": " ".join(lines)}

    def remote_rows(self, caller: Caller, now_ms: int | None = None) -> list[dict[str, Any]]:
        config, _ = load_network_config(self.home)
        if not network_enabled(config) or not any(is_inside(caller.path, folder) for folder in config.outbound_folders):
            return []
        if self._origin.is_remote(caller):
            return []
        try:
            peers = sorted(load_policy(self.home).peers, key=lambda p: p.label)
        except PolicyError:
            return []
        from . import poller

        now = self._now() if now_ms is None else now_ms
        store = self._db()
        rows: list[dict[str, Any]] = []
        for peer in peers:
            cached = store.get_inventory(peer.device_id)
            if cached is None:
                continue
            try:
                inventory = json.loads(cached[0])
                sessions = wire.parse_inventory(inventory)
                folders = wire.parse_inventory_folders(inventory)
            except (ValueError, wire.WireError):
                continue
            age = max(0, now - cached[1])
            offline = age > OFFLINE_AFTER_MS or poller.is_failed(self.home, peer.device_id)
            rows.extend(_row(peer.label, s, age, offline, folders) for s in sessions)
        return rows


def _refusal_text(peer: PeerGrant, reply: Mapping[str, Any], body: Mapping[str, Any]) -> str:
    try:
        reason = wire.parse_reply(reply).reason
    except wire.WireError:
        return PEER_REFUSED
    path = body.get("path")
    if reason == "folder-not-allowed" and wire.valid_path(path):
        return FOLDER_NOT_ALLOWED.format(label=peer.label, path=path)
    return PEER_REFUSED


def _row(label: str, session: wire.RemoteSession, age_ms: int, offline: bool, folders: list[str] | None = None) -> dict[str, Any]:
    from ..messaging.messaging import harness_of

    remote: dict[str, Any] = {"label": label, "ageMs": age_ms, "offline": offline}
    if folders is not None:
        remote["folders"] = folders

    name = f"{wire.escape_display(session.name) or session.id}@{label}"
    where = f"{label} (remote)"
    return {
        "name": name,
        "shortName": name,
        "id": f"{session.id}@{label}",
        "session": session.id[:8],
        "agent": session.agent,
        "route": "remote",
        "state": session.status,
        "remote": remote,
        "where": where,
        "host": where,
        "harness": harness_of(session.agent),
        "model": None,
        "effort": None,
        "path": None,
        "folder": None,
        "tab": None,
        "self": False,
    }


_instances: dict[str, Outbound] = {}
_instances_lock = threading.Lock()


def outbound_for(home: str) -> Outbound:
    key = os.path.abspath(home)
    with _instances_lock:
        if key not in _instances:
            _instances[key] = Outbound(home)
        return _instances[key]


def close_all() -> None:
    with _instances_lock:
        found = list(_instances.values())
        _instances.clear()
    for outbound in found:
        outbound.close()


class SessionNet:
    def __init__(self, outbound: Outbound, caller: Callable[[], Caller]) -> None:
        self.outbound = outbound
        self._caller = caller

    def refuse_remote_origin(self) -> None:
        self.outbound.require_local_origin(self._caller())

    def send(self, given: Mapping[str, Any]) -> dict[str, Any]:
        return self.outbound.send(self._caller(), given)

    def open(self, given: Mapping[str, Any]) -> dict[str, Any]:
        return self.outbound.open(self._caller(), given)

    def start_listener(self) -> dict[str, Any]:
        return self.outbound.start_listener(self._caller())


def remote_session_rows(home: str, caller: Caller, now_ms: int) -> list[dict[str, Any]]:
    return outbound_for(home).remote_rows(caller, now_ms)

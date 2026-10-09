from __future__ import annotations

import contextlib
import hashlib
import os
import sys
import threading
from collections import OrderedDict
from typing import Any, Callable, NamedTuple

from ..liveness import pid_alive
from .host import Binding, BoundSession, ToolHost

ROOTS_REQUEST = "agent-tabs-roots"
LIVENESS_S = 15.0
MAX_CLIENTS = 1024


def _file_path(url_path: str) -> str:
    if sys.platform == "win32":
        from nturl2path import url2pathname

        return url2pathname(url_path)
    from urllib.parse import unquote

    return unquote(url_path)


class Identity(NamedTuple):
    client: str | None = None
    tab: str | None = None
    agent: str | None = None
    pid: int | None = None
    pid_start: int | None = None


def root_path(responses: object) -> str | None:
    from urllib.parse import urlsplit

    answer = responses.get(ROOTS_REQUEST) if isinstance(responses, dict) else None
    roots = answer.get("roots") if isinstance(answer, dict) else None
    for root in roots if isinstance(roots, list) else []:
        uri = root.get("uri") if isinstance(root, dict) else None
        if not isinstance(uri, str):
            continue
        try:
            parts = urlsplit(uri)
        except ValueError:
            continue
        if parts.scheme != "file" or parts.netloc not in ("", "localhost"):
            continue
        path = _file_path(parts.path)
        if os.path.isabs(path):
            return path
    return None


def session_key(identity: Identity) -> str | None:
    if identity.pid is not None:
        return f"pid:{identity.pid}:{identity.pid_start or 0}"
    return None if identity.client is None else f"client:{identity.client}"


def derived_id(key: str) -> str:
    return "s-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


class _Entry:
    def __init__(self, key: str, pid: int, owned: bool) -> None:
        self.key = key
        self.pid = pid
        self.owned = owned
        self.ready = threading.Event()
        self.session: BoundSession | None = None
        self.error: BaseException | None = None
        self.dropped = False


def _no_identity() -> dict[str, Any]:
    text = "Agent Tabs: this connection sent no session identity; restart the session so its headers helper runs"
    return {"isError": True, "content": [{"type": "text", "text": text}]}


class Hub:
    def __init__(
        self,
        host: ToolHost,
        alive: Callable[[int], bool] = pid_alive,
        server_pid: int | None = None,
        log: Callable[[str], None] = lambda _m: None,
        prepare: Callable[[], None] | None = None,
    ) -> None:
        self.host = host
        self._prepare = prepare
        self._prepared = threading.Lock()
        self.alive = alive
        self.server_pid = os.getpid() if server_pid is None else server_pid
        self.log = log
        self._entries: dict[str, _Entry] = {}
        self._cwds: OrderedDict[str, str] = OrderedDict()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._timer: threading.Thread | None = None

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._entries)

    def _snapshot(self) -> list[_Entry]:
        with self._lock:
            return list(self._entries.values())

    def start_liveness(self, every_s: float = LIVENESS_S) -> None:
        def loop() -> None:
            while not self._stop.wait(every_s):
                with contextlib.suppress(Exception):
                    self.sweep()

        self._timer = threading.Thread(target=loop, name="liveness", daemon=True)
        self._timer.start()

    def sweep(self) -> int:
        dead = [e for e in self._snapshot() if e.owned and not self.alive(e.pid)]
        for entry in dead:
            self._drop(entry, end=True)
        return len(dead)

    def live_sessions(self) -> int:
        return sum(1 for e in self._snapshot() if e.owned and self.alive(e.pid))

    def end_pid(self, pid: int) -> int:
        matching = [e for e in self._snapshot() if e.owned and e.pid == pid]
        for entry in matching:
            self._drop(entry, end=True)
        return len(matching)

    def _drop(self, entry: _Entry, end: bool) -> None:
        with self._lock:
            if self._entries.get(entry.key) is entry:
                del self._entries[entry.key]
            if entry.dropped:
                return
            entry.dropped = True
        entry.ready.wait()
        session = entry.session
        if session is None:
            return
        try:
            if end:
                session.end()
            else:
                session.release()
        except Exception as e:  # noqa: BLE001 - a failed end must not stop the sweep or the shutdown
            self.log(f"ending {entry.key} failed: {e}")

    def _cwd_of(self, identity: Identity, responses: object) -> str | None:
        client = identity.client
        with self._lock:
            if responses is None:
                return None if client is None else self._cwds.get(client)
            answered = root_path(responses) or os.path.expanduser("~")
            if client is not None:
                self._cwds[client] = answered
                self._cwds.move_to_end(client)
                while len(self._cwds) > MAX_CLIENTS:
                    self._cwds.popitem(last=False)
            return answered

    def _entry(self, key: str, identity: Identity, params: dict[str, Any]) -> _Entry | dict[str, Any]:
        with self._lock:
            entry = self._entries.get(key)
        if entry is not None:
            return entry
        cwd = self._cwd_of(identity, params.get("inputResponses"))
        if cwd is None:
            return {"resultType": "input_required", "inputRequests": {ROOTS_REQUEST: {"method": "roots/list"}}}
        owned = identity.pid is not None
        pid = identity.pid if identity.pid is not None else self.server_pid
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                return entry
            entry = _Entry(key, pid, owned)
            self._entries[key] = entry
        with self._prepared:
            if self._prepare is not None:
                self._prepare()
                self._prepare = None
        binding = Binding(derived_id(key), identity.agent or "claude", pid, cwd, identity.tab, identity.pid_start)
        try:
            entry.session = self.host.bind(binding)
        except Exception as e:  # noqa: BLE001 - the callers waiting on this entry get the error as a JSON-RPC error
            entry.error = e
            self.log(f"binding {key} failed: {e}")
            with self._lock:
                if self._entries.get(key) is entry:
                    del self._entries[key]
        finally:
            entry.ready.set()
        return entry

    def call(self, identity: Identity, params: dict[str, Any], cancel: threading.Event) -> dict[str, Any]:
        key = session_key(identity)
        if key is None:
            return _no_identity()
        found = self._entry(key, identity, params)
        if isinstance(found, dict):
            return found
        found.ready.wait()
        if found.session is None:
            raise RuntimeError(str(found.error) if found.error is not None else "the session ended")
        arguments = params.get("arguments")
        return found.session.call(str(params.get("name")), arguments if isinstance(arguments, dict) else {}, None, cancel)

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            entries = list(self._entries.values())
            self._entries.clear()
        for entry in entries:
            with self._lock:
                if entry.dropped:
                    continue
                entry.dropped = True
            entry.ready.wait()
            if entry.session is not None:
                with contextlib.suppress(Exception):
                    entry.session.release()

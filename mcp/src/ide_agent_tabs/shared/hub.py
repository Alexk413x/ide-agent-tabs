from __future__ import annotations

import hashlib
import os
import threading
import time
from collections import OrderedDict
from typing import Any, Callable, NamedTuple

from ..liveness import pid_alive
from .ancestry import START_SLACK_MS, Lookup, process_lookup
from .host import Binding, BoundSession, ToolHost
from .state import GENERATED_ID_PREFIX

ROOTS_REQUEST = "agent-tabs-roots"
LIVENESS_S = 15.0
MAX_CLIENTS = 1024
# A session with no owning process has nothing to tell the server it is gone, and each reconnect of a client that
# vanished leaves another one. A healthy session is never idle this long: a wait_for_message holds a request open
# for at most 600 s and returns before the next starts, and a call in flight counts as activity. Three times that
# cap also leaves a slow turn between calls alone; a session cut off anyway binds again with its next request.
UNOWNED_IDLE_S = 1800.0


def _file_path(url_path: str) -> str:
    from urllib.request import url2pathname

    return url2pathname(url_path)


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
    if identity.client is not None:
        return f"client:{identity.client}"
    return None if identity.tab is None else f"tab:{identity.tab}"


def derived_id(key: str) -> str:
    return GENERATED_ID_PREFIX + hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


class _Entry:
    def __init__(self, key: str, pid: int, owned: bool, pid_start: int | None, now: float) -> None:
        self.key = key
        self.pid = pid
        self.owned = owned
        self.pid_start = pid_start
        self.last_active = now
        self.calls = 0
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
        tab_owner: Callable[[str], tuple[int, int | None] | None] | None = None,
        now: Callable[[], float] = time.monotonic,
        process_info: Callable[[], Lookup] = process_lookup,
    ) -> None:
        self.host = host
        self.now = now
        self._process_info = process_info
        self.tab_owner = tab_owner
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
            last: str | None = None
            while not self._stop.wait(every_s):
                try:
                    self.sweep()
                except Exception as e:  # noqa: BLE001
                    text = f"{type(e).__name__}: {e}"
                    if text != last:
                        self.log(f"liveness sweep failed: {text}")
                    last = text
                else:
                    last = None

        self._timer = threading.Thread(target=loop, name="liveness", daemon=True)
        self._timer.start()

    # A pid can be reused once its process has ended, so an owner also counts as gone when the process now holding
    # its pid started at another time than the one the client reported. A start time that can't be read proves nothing.
    def _starts(self) -> Callable[[int], float | None]:
        info: list[Lookup | None] = [None]

        def started(pid: int) -> float | None:
            try:
                if info[0] is None:
                    info[0] = self._process_info()
                found = info[0](pid)
            except Exception:  # noqa: BLE001
                return None
            return None if found is None else found.start_ms

        return started

    def _owner_gone(self, entry: _Entry, started: Callable[[int], float | None]) -> bool:
        if not self.alive(entry.pid):
            return True
        if not entry.pid_start:
            return False
        now = started(entry.pid)
        return now is not None and abs(now - entry.pid_start) > START_SLACK_MS

    def _idle(self, entry: _Entry, now: float) -> bool:
        return not entry.owned and entry.ready.is_set() and entry.calls == 0 and now - entry.last_active >= UNOWNED_IDLE_S

    def sweep(self) -> int:
        started = self._starts()
        now = self.now()
        gone = [e for e in self._snapshot() if (self._owner_gone(e, started) if e.owned else self._idle(e, now))]
        for entry in gone:
            self._drop(entry, end=True)
        return len(gone)

    def live_sessions(self) -> int:
        started = self._starts()
        return sum(1 for e in self._snapshot() if e.owned and not self._owner_gone(e, started))

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

    def _owner_of(self, identity: Identity) -> tuple[int, int | None] | None:
        if identity.pid is not None:
            return identity.pid, identity.pid_start
        if identity.client is not None or identity.tab is None or self.tab_owner is None:
            return None
        try:
            found = self.tab_owner(identity.tab)
        except Exception as e:  # noqa: BLE001 - an unreadable record leaves the session on the server's pid
            self.log(f"reading the owner of {identity.tab} failed: {e}")
            return None
        return found if found is not None and self.alive(found[0]) else None

    def _entry(self, key: str, identity: Identity, params: dict[str, Any]) -> _Entry | dict[str, Any]:
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                entry.last_active = self.now()
                return entry
        cwd = self._cwd_of(identity, params.get("inputResponses"))
        if cwd is None:
            return {"resultType": "input_required", "inputRequests": {ROOTS_REQUEST: {"method": "roots/list"}}}
        owner = self._owner_of(identity)
        owned = owner is not None
        pid = owner[0] if owner is not None else self.server_pid
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                return entry
            entry = _Entry(key, pid, owned, owner[1] if owner is not None else None, self.now())
            self._entries[key] = entry
        with self._prepared:
            if self._prepare is not None:
                self._prepare()
                self._prepare = None
        binding = Binding(derived_id(key), identity.agent or "claude", pid, cwd, identity.tab, owner[1] if owner is not None else None)
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
        with self._lock:
            found.calls += 1
        try:
            found.ready.wait()
            if found.session is None:
                raise RuntimeError(str(found.error) if found.error is not None else "the session ended")
            arguments = params.get("arguments")
            meta = params.get("_meta")
            return found.session.call(
                str(params.get("name")), arguments if isinstance(arguments, dict) else {}, cancel, meta if isinstance(meta, dict) else {}
            )
        finally:
            with self._lock:
                found.calls -= 1
                found.last_active = self.now()

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
                try:
                    entry.session.release()
                except Exception as e:  # noqa: BLE001
                    self.log(f"releasing {entry.key} failed: {e}")

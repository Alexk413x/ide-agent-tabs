from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Callable
from typing import NamedTuple

from ..clock import now_ms as current_ms
from ..messaging.sessions import SESSIONS_DIR, Presence, is_session_id, read_presence
from ..shared.ancestry import MAX_DEPTH, START_SLACK_MS, Lookup, process_lookup
from .store import NetStore, NetStoreError, db_path

REMOTE_ORIGIN_REFUSAL = "refused: a peer machine started this session, and a session a peer started can't use this tool"
CACHE_MS = 10_000
MAX_CACHED = 512


class Caller(NamedTuple):
    session: str
    path: str
    tab: str | None = None
    pid: int | None = None
    pid_start: int | None = None


def _canonical(path: str) -> str:
    return os.path.normcase(os.path.realpath(path))


def _within(folder: str, path: str) -> bool:
    try:
        return os.path.commonpath([folder, path]) == folder
    except ValueError:
        return False


def _started_close(recorded: int | None, found: float) -> bool:
    return recorded is None or abs(found - recorded) <= START_SLACK_MS


class OriginChecker:
    def __init__(
        self,
        home: str,
        now: Callable[[], int] = current_ms,
        open_store: Callable[[str], NetStore] = NetStore,
        lookup: Callable[[], Lookup] = process_lookup,
    ) -> None:
        self.home = home
        self._now = now
        self._open_store = open_store
        self._lookup = lookup
        self._lock = threading.Lock()
        self._store: NetStore | None = None
        self._cache: dict[str, int] = {}

    def close(self) -> None:
        with self._lock:
            store, self._store = self._store, None
            self._cache.clear()
        if store is not None:
            store.close()

    def _db(self) -> NetStore:
        with self._lock:
            if self._store is None:
                self._store = self._open_store(self.home)
            return self._store

    def _launched_by_peer(self, store: NetStore, tab: str) -> bool:
        return is_session_id(tab) and (store.remote_tab_peer(tab) is not None or store.opened_by(tab) is not None)

    def _in_launching_folder(self, store: NetStore, caller: Caller) -> bool:
        if not caller.path:
            return False
        path = _canonical(caller.path)
        return any(_within(_canonical(launch.folder), path) for launch in store.inflight_launches(self._now()))

    def _presences(self) -> list[Presence]:
        folder = os.path.join(self.home, SESSIONS_DIR)
        try:
            names = [n[:-5] for n in os.listdir(folder) if n.endswith(".json")]
        except OSError:
            return []
        found = (read_presence(self.home, name) for name in names)
        return [p for p in found if p is not None]

    def _chain(self, caller: Caller) -> dict[int, float]:
        if caller.pid is None:
            return {}
        lookup = self._lookup()
        chain: dict[int, float] = {}
        pid = caller.pid
        for _ in range(MAX_DEPTH):
            info = lookup(pid)
            if info is None or pid in chain:
                break
            chain[pid] = info.start_ms
            pid = info.ppid
        return chain

    def _ancestor_tabs(self, caller: Caller) -> list[str]:
        chain = self._chain(caller)
        if not chain:
            return []
        tabs: list[str] = []
        for p in self._presences():
            if p["id"] == caller.session:
                continue
            for pid_key, start_key in (("pid", "pidStart"), ("agentPid", "agentPidStart")):
                pid = p.get(pid_key)
                if pid in chain and _started_close(p.get(start_key), chain[pid]):
                    tabs.append(p["id"])
        return tabs

    def _compute(self, caller: Caller) -> bool:
        store = self._db()
        own = [t for t in (caller.session, caller.tab) if t is not None]
        if any(self._launched_by_peer(store, tab) for tab in own):
            return True
        if any(self._launched_by_peer(store, tab) for tab in self._ancestor_tabs(caller)):
            return True
        return self._in_launching_folder(store, caller)

    def is_remote(self, caller: Caller) -> bool:
        if not os.path.exists(db_path(self.home)):
            return False
        now = self._now()
        with self._lock:
            hit = self._cache.get(caller.session)
        if hit is not None and 0 <= now - hit < CACHE_MS:
            return True
        try:
            remote = self._compute(caller)
        except (NetStoreError, sqlite3.Error, OSError):
            # Provenance that can't be read is unknown, and unknown counts as remote.
            return True
        if remote:
            with self._lock:
                if len(self._cache) >= MAX_CACHED:
                    self._cache.clear()
                self._cache[caller.session] = now
        return remote

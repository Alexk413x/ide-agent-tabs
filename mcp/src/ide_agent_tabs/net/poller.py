from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

from ..clock import now_ms as current_ms
from . import wire
from .client import NetClientError, send_request
from .policy import NetworkConfig, Policy, PolicyError, load_network_config, load_policy, policy_path
from .store import NetStore
from .transport import network_enabled

PERIOD_S = 60.0
DEADLINE_S = 30.0
MAX_CONCURRENT = 8
MIN_WAIT_S = 0.05
POLICY_CHECK_S = 2.0

Fetch = Callable[..., "dict[str, Any]"]

_running: dict[str, Poller] = {}
_running_lock = threading.Lock()


def _key(home: str) -> str:
    return os.path.abspath(home)


def nudge(home: str, peer: str) -> None:
    with _running_lock:
        poller = _running.get(_key(home))
    if poller is not None:
        poller.nudge(peer)


def is_failed(home: str, peer: str) -> bool:
    with _running_lock:
        poller = _running.get(_key(home))
    return poller is not None and poller.is_failed(peer)


class _Unanswered(Exception):
    pass


class Poller:
    def __init__(
        self,
        home: str,
        *,
        fetch: Fetch = send_request,
        now_ms: Callable[[], int] = current_ms,
        clock: Callable[[], float] = time.monotonic,
        load_config: Callable[[str], tuple[NetworkConfig, list[str]]] = load_network_config,
        load_trust: Callable[[str], Policy] = load_policy,
        open_store: Callable[[str], NetStore] = NetStore,
        log: Callable[[str], None] = lambda _message: None,
        period_s: float = PERIOD_S,
        deadline_s: float = DEADLINE_S,
        max_concurrent: int = MAX_CONCURRENT,
        policy_check_s: float = POLICY_CHECK_S,
    ) -> None:
        self.home = home
        self._fetch = fetch
        self._now_ms = now_ms
        self._clock = clock
        self._load_config = load_config
        self._load_policy = load_trust
        self._open_store = open_store
        self._log = log
        self._period_s = period_s
        self._deadline_s = deadline_s
        self._max_concurrent = max_concurrent
        self._policy_check_s = policy_check_s
        self._lock = threading.Lock()
        self._last: dict[str, float] = {}
        self._inflight: set[str] = set()
        self._failed: set[str] = set()
        self._store: NetStore | None = None
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="agent-tabs-net-poll", daemon=True)
        with _running_lock:
            _running[_key(self.home)] = self
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        with _running_lock:
            if _running.get(_key(self.home)) is self:
                del _running[_key(self.home)]
        with self._lock:
            thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(2)
        with self._lock:
            store, self._store = self._store, None
        if store is not None:
            store.close()

    def nudge(self, peer: str) -> None:
        with self._lock:
            idle = peer not in self._inflight and self._clock() - self._last.get(peer, -self._period_s) >= self._period_s
        if idle:
            self._wake.set()

    def is_failed(self, peer: str) -> bool:
        with self._lock:
            return peer in self._failed

    def _db(self) -> NetStore:
        with self._lock:
            if self._store is None:
                self._store = self._open_store(self.home)
            return self._store

    def _loop(self) -> None:
        last_error: str | None = None
        while not self._stop.is_set():
            self._wake.clear()
            mark = self._policy_mark()
            try:
                self.poll_once()
                last_error = None
            except Exception as e:  # noqa: BLE001 - one failed pass must not end the poller
                text = f"{type(e).__name__}: {e}"
                if text != last_error:
                    self._log(f"net: the inventory poll failed: {text}")
                last_error = text
            self._sleep(self._next_wait_s(), mark)

    def _policy_mark(self) -> tuple[int, int] | None:
        try:
            found = os.stat(policy_path(self.home))
        except OSError:
            return None
        return (found.st_mtime_ns, found.st_size)

    def _sleep(self, wait_s: float, mark: tuple[int, int] | None) -> None:
        # A join or an introduction saves the new member in another process, so the poller watches the policy file
        # to ask a new member for its inventory at once instead of on the next round.
        end = time.monotonic() + wait_s
        while not self._stop.is_set():
            left = end - time.monotonic()
            if left <= 0 or self._wake.wait(min(left, self._policy_check_s)):
                return
            if self._policy_mark() != mark:
                return

    def _peers(self) -> list[str]:
        config, _ = self._load_config(self.home)
        if not network_enabled(config):
            return []
        try:
            return [p.device_id for p in self._load_policy(self.home).peers]
        except PolicyError:
            return []

    def _next_wait_s(self) -> float:
        now = self._clock()
        with self._lock:
            waits = [self._last[p] + self._period_s - now for p in self._last if p not in self._inflight]
        return min(self._period_s, max(MIN_WAIT_S, min(waits))) if waits else self._period_s

    def _due(self, peers: list[str]) -> list[str]:
        now = self._clock()
        with self._lock:
            known = set(peers)
            for peer in [p for p in self._last if p not in known]:
                del self._last[peer]
                self._failed.discard(peer)
            return [p for p in peers if p not in self._inflight and now - self._last.get(p, -self._period_s) >= self._period_s]

    def poll_once(self) -> list[str]:
        due = self._due(self._peers())
        if not due:
            return []
        deadline = self._clock() + self._deadline_s
        pending = list(due)
        with self._lock:
            for peer in due:
                self._last[peer] = self._clock()
                self._inflight.add(peer)

        def worker() -> None:
            while True:
                with self._lock:
                    if not pending or self._clock() >= deadline:
                        return
                    peer = pending.pop(0)
                self._attempt(peer, max(0.1, deadline - self._clock()))

        threads = [
            threading.Thread(target=worker, name="agent-tabs-net-fetch", daemon=True) for _ in range(min(self._max_concurrent, len(due)))
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(max(0.0, deadline - self._clock()))
        with self._lock:
            unreached = list(pending)
            for peer in unreached:
                self._inflight.discard(peer)
                self._last.pop(peer, None)
            pending.clear()
        return [peer for peer in due if peer not in unreached]

    def _attempt(self, peer: str, timeout: float) -> None:
        try:
            self._refresh(peer, timeout)
        except Exception as e:  # noqa: BLE001 - any failure shows the peer as offline and keeps its last inventory
            if not isinstance(e, (NetClientError, wire.WireError, _Unanswered)):
                self._log(f"net: refreshing an inventory failed: {type(e).__name__}")
            with self._lock:
                self._failed.add(peer)
        else:
            with self._lock:
                self._failed.discard(peer)
        finally:
            with self._lock:
                self._inflight.discard(peer)

    def _refresh(self, peer: str, timeout: float) -> None:
        reply: Mapping[str, Any] = self._fetch(self.home, peer, "inventory", {}, timeout=timeout)
        if reply.get("ok") is not True:
            raise _Unanswered
        body = reply.get("body")
        sessions = wire.parse_inventory(body)
        text = json.dumps(wire.inventory_body(sessions, wire.parse_inventory_folders(body)), separators=(",", ":"), ensure_ascii=False)
        self._db().put_inventory(peer, text, self._now_ms())

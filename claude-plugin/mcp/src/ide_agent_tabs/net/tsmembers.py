from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .beaconsvc import check_remembered
from .client import NetClientError, send_request, verify_pinned
from .hubstate import HubStateError
from .hubsync import flush_pending
from .peers import update_address
from .policy import DEFAULT_PORT, PolicyError, load_policy
from .presence import record_presence
from .tailscale import OwnDevice

CYCLE_S = 60.0
VERIFY_TIMEOUT_S = 5.0
WORKERS = 4
MAX_NOTED = 256
_TASK_ERRORS = (NetClientError, PolicyError, HubStateError, OSError, TimeoutError, ValueError)


def _spawn(task: Callable[[], None]) -> None:
    threading.Thread(target=task, daemon=True).start()


class TailscaleMembers:
    def __init__(
        self,
        home: str,
        devices: Callable[[], list[OwnDevice]],
        log: Callable[[str], None],
        *,
        port: int = DEFAULT_PORT,
        timeout_s: float = VERIFY_TIMEOUT_S,
        interval_s: float = CYCLE_S,
        verify: Callable[..., str] = verify_pinned,
        request: Callable[..., dict[str, Any]] = send_request,
        spawn: Callable[[Callable[[], None]], None] = _spawn,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._home = home
        self._devices = devices
        self._log = log
        self._port = port
        self._timeout_s = timeout_s
        self._interval_s = interval_s
        self._verify = verify
        self._request = request
        self._spawn = spawn
        self._clock = clock
        self._running = threading.Lock()
        self._next_at = float("-inf")
        self._noted: set[str] = set()

    def tick(self) -> bool:
        now = self._clock()
        if now < self._next_at:
            return False
        self._next_at = now + self._interval_s
        self._spawn(self.cycle)
        return True

    def cycle(self) -> None:
        if not self._running.acquire(blocking=False):
            return
        try:
            self.move_addresses()
            with contextlib.suppress(*_TASK_ERRORS):
                flush_pending(self._home, self._request)
            check_remembered(self._home, self._log, port=self._port, timeout_s=self._timeout_s, request=self._request)
        finally:
            self._running.release()

    def move_addresses(self) -> None:
        try:
            members = [peer for peer in load_policy(self._home).peers if peer.member]
        except PolicyError:
            return
        if not members:
            return
        stored = {peer.address for peer in members}
        device_ids = [peer.device_id for peer in members]
        candidates = [device for device in self._devices() if device.address not in stored]
        if not candidates:
            return
        with ThreadPoolExecutor(max_workers=min(WORKERS, len(candidates))) as pool:
            for device in candidates:
                pool.submit(self._check, device, device_ids)

    def _check(self, device: OwnDevice, device_ids: list[str]) -> None:
        try:
            matched = self._verify(self._home, device.address, device_ids, port=self._port, timeout=self._timeout_s)
            if update_address(self._home, matched, device.address):
                record_presence(self._home, matched, True, device.address)
                self._log(f"the address of {matched[:15]} changed to {device.address} after pinned TLS matched its certificate")
        except _TASK_ERRORS as error:
            if device.address not in self._noted:
                if len(self._noted) >= MAX_NOTED:
                    self._noted.clear()
                self._noted.add(device.address)
                self._log(f"{device.host_name} at {device.address} is not a hub member, so no address changed: {error}")

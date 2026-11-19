from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from .beacon import (
    Datagram,
    Endpoint,
    HubWatch,
    Inbox,
    MemberBeacon,
    MemberCheck,
    Wire,
    build_join,
    build_member,
    wire_for,
)
from .client import NetClientError, local_device_id, send_request, verify_pinned
from .firewall import DISCOVERY_PORT
from .hubstate import HubStateError
from .hubsync import flush_pending
from .interfaces import InterfaceError, InterfaceInfo, interface_for
from .names import DeviceNameError, device_name_of
from .pairing import PAIRING_PORT, PairingError
from .peers import update_address
from .policy import DEFAULT_PORT, PolicyError, load_policy
from .presence import record_presence
from .transport import LanTransport

ANNOUNCE_INTERVAL_S = 30.0
RECONNECT_WINDOW_S = 60.0
NOTE_INTERVAL_S = 60.0
PENDING_INTERVAL_S = 60.0
ONLINE_RECORD_S = 60.0
MAX_ATTEMPTS = 256
MAX_INFLIGHT = 4
STARTUP_WORKERS = 4
STARTUP_TIMEOUT_S = 5.0
_TASK_ERRORS = (NetClientError, PolicyError, HubStateError, OSError, TimeoutError, ValueError)


def spawn(task: Callable[[], None]) -> None:
    threading.Thread(target=task, daemon=True).start()


@dataclass(frozen=True)
class BeaconState:
    open: bool
    discoverable: bool
    epoch: int = 0


@dataclass
class BeaconSettings:
    interface: Callable[[str], InterfaceInfo] = interface_for
    make_endpoint: Callable[[Wire], Endpoint] = Endpoint
    port: int = DISCOVERY_PORT
    interval_s: float = ANNOUNCE_INTERVAL_S
    poll_s: float = 0.5
    retry_s: float = 5.0
    member_port: int = DEFAULT_PORT
    join_port: int = PAIRING_PORT
    reconnect_window_s: float = RECONNECT_WINDOW_S
    request_timeout_s: float = STARTUP_TIMEOUT_S
    check_destination: Callable[[str, int, str, int], str] = LanTransport().check_destination
    run: Callable[[Callable[[], None]], None] = spawn
    clock: Callable[[], float] = time.time
    verify: Callable[..., str] = verify_pinned
    request: Callable[..., dict[str, Any]] = send_request
    flush_interval_s: float = PENDING_INTERVAL_S


class Reconnector:
    def __init__(
        self,
        home: str,
        log: Callable[[str], None],
        *,
        port: int = DEFAULT_PORT,
        window_s: float = RECONNECT_WINDOW_S,
        verify: Callable[..., str] = verify_pinned,
        run: Callable[[Callable[[], None]], None] = spawn,
        clock: Callable[[], float] = time.monotonic,
        timeout_s: float = STARTUP_TIMEOUT_S,
    ) -> None:
        self._home = home
        self._log = log
        self._port = port
        self._window_s = window_s
        self._verify = verify
        self._run = run
        self._clock = clock
        self._timeout_s = timeout_s
        self._lock = threading.Lock()
        self._attempts: dict[tuple[str, str], float] = {}
        self._inflight: set[str] = set()

    def consider(self, device_id: str, address: str) -> bool:
        now = self._clock()
        with self._lock:
            last = self._attempts.get((device_id, address))
            if last is not None and now - last < self._window_s:
                return False
            if device_id in self._inflight or len(self._inflight) >= MAX_INFLIGHT:
                return False
            if len(self._attempts) >= MAX_ATTEMPTS:
                self._attempts = {key: at for key, at in self._attempts.items() if now - at < self._window_s}
                if len(self._attempts) >= MAX_ATTEMPTS:
                    self._attempts.clear()
            self._attempts[(device_id, address)] = now
            self._inflight.add(device_id)
        self._run(lambda: self._attempt(device_id, address))
        return True

    def _attempt(self, device_id: str, address: str) -> None:
        try:
            self._verify(self._home, address, [device_id], port=self._port, timeout=self._timeout_s)
            changed = update_address(self._home, device_id, address)
            if changed:
                record_presence(self._home, device_id, True, address)
                self._log(f"the address of {device_id[:15]} changed to {address} after pinned TLS matched its certificate")
        except _TASK_ERRORS as error:
            self._log(f"a member beacon named {address} for {device_id[:15]}; the pinned TLS check failed, so the address is kept: {error}")
        finally:
            with self._lock:
                self._inflight.discard(device_id)


def check_remembered(
    home: str,
    log: Callable[[str], None],
    *,
    port: int = DEFAULT_PORT,
    timeout_s: float = STARTUP_TIMEOUT_S,
    workers: int = STARTUP_WORKERS,
    request: Callable[..., dict[str, Any]] = send_request,
) -> None:
    try:
        members = [p for p in load_policy(home).peers if p.member]
    except PolicyError:
        return
    if not members:
        return

    def one(device_id: str, address: str, label: str) -> None:
        try:
            request(home, device_id, "inventory", {}, timeout=timeout_s, port=port)
            online = True
        except _TASK_ERRORS as error:
            online = False
            log(f"the remembered address {address} of {label} did not answer: {error}")
        with contextlib.suppress(*_TASK_ERRORS):
            record_presence(home, device_id, online, address)
        if online:
            log(f"the remembered address {address} of {label} answered")

    with ThreadPoolExecutor(max_workers=min(workers, len(members))) as pool:
        for peer in members:
            pool.submit(one, peer.device_id, peer.address, peer.label)


class BeaconService:
    def __init__(
        self,
        home: str,
        bind: str,
        state: Callable[[], BeaconState],
        stop: threading.Event,
        log: Callable[[str], None],
        settings: BeaconSettings,
    ) -> None:
        self._home = home
        self._bind = bind
        self._state = state
        self._stop = stop
        self._log = log
        self._settings = settings
        self._watch = HubWatch(home)
        self._check = MemberCheck(self._watch, settings.clock)
        self._wire: Wire | None = None
        self._noted: dict[str, float] = {}
        self._flush_at = 0.0
        self._flushing = threading.Lock()
        self._next_bind: str | None = None
        self._recorded: dict[str, float] = {}
        self._reconnect = Reconnector(
            home,
            log,
            port=settings.member_port,
            window_s=settings.reconnect_window_s,
            verify=settings.verify,
            run=settings.run,
            timeout_s=settings.request_timeout_s,
        )

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self.run, daemon=True)
        thread.start()
        return thread

    def rebind(self, bind: str) -> None:
        self._next_bind = bind

    def _note(self, key: str, text: str) -> None:
        now = time.monotonic()
        last = self._noted.get(key)
        if last is not None and now - last < NOTE_INTERVAL_S:
            return
        if len(self._noted) >= MAX_ATTEMPTS:
            self._noted.clear()
        self._noted[key] = now
        self._log(text)

    def run(self) -> None:
        if ":" in self._bind:
            self._log(f"beacons need an IPv4 bind address, and {self._bind} is not one")
            return
        try:
            self._loop()
        except (OSError, ValueError, InterfaceError) as error:
            self._log(f"beacons stopped: {type(error).__name__}: {error}")

    def _resolve_wire(self) -> Wire | None:
        if self._wire is None:
            try:
                info = self._settings.interface(self._bind)
                self._wire = wire_for(info.address, info.prefix_len, self._settings.port)
            except (InterfaceError, ValueError) as error:
                self._note("interface", f"beacons are off until the interface of {self._bind} can be read: {error}")
        return self._wire

    def _open(self) -> Endpoint | None:
        wire = self._resolve_wire()
        if wire is None:
            return None
        try:
            endpoint = self._settings.make_endpoint(wire)
        except OSError as error:
            self._note("open", f"beacons are off until UDP port {wire.port} can be opened on {wire.bind}: {error}")
            return None
        for note in endpoint.notes:
            self._log(f"beacon setup: {note}")
        self._log(f"beacons on for {wire.bind} (group {wire.group}, broadcast {wire.broadcast or 'none'}, port {wire.port})")
        return endpoint

    def _loop(self) -> None:
        settings = self._settings
        endpoint: Endpoint | None = None
        inbox: Inbox | None = None
        epoch: int | None = None
        next_send = 0.0
        try:
            while not self._stop.is_set():
                moved = self._next_bind
                if moved is not None:
                    self._next_bind = None
                    self._bind, self._wire = moved, None
                    if endpoint is not None:
                        endpoint.close()
                        endpoint, inbox = None, None
                state = self._state()
                if not state.open:
                    if endpoint is not None:
                        endpoint.close()
                        endpoint, inbox = None, None
                        self._log("beacons off: the network is not allowed or LAN mode is off")
                    self._stop.wait(settings.poll_s)
                    continue
                if endpoint is None:
                    endpoint = self._open()
                    if endpoint is None:
                        self._stop.wait(settings.retry_s)
                        continue
                    inbox = Inbox(endpoint.wire, local_device_id(self._home))
                    settings.run(self._remembered)
                    next_send = 0.0
                now = time.monotonic()
                if now >= self._flush_at:
                    self._flush_at = now + settings.flush_interval_s
                    settings.run(self._flush_pending)
                if state.epoch != epoch or now >= next_send:
                    epoch = state.epoch
                    self._send(endpoint, state)
                    next_send = now + settings.interval_s
                try:
                    datagram = endpoint.recv(settings.poll_s)
                except OSError as error:
                    self._note("receive", f"beacons are off until the socket recovers: {error}")
                    endpoint.close()
                    endpoint, inbox = None, None
                    self._stop.wait(settings.retry_s)
                    continue
                if datagram is not None and inbox is not None:
                    self._receive(datagram, inbox)
        finally:
            if endpoint is not None:
                endpoint.close()

    def _flush_pending(self) -> None:
        if not self._flushing.acquire(blocking=False):
            return
        try:
            delivered = flush_pending(self._home, self._settings.request)
        except _TASK_ERRORS as error:
            self._note("pending", f"queued hub changes could not be sent: {type(error).__name__}: {error}")
            return
        finally:
            self._flushing.release()
        if delivered:
            self._log(f"delivered {delivered} queued hub change(s) to members that were offline")

    def _remembered(self) -> None:
        check_remembered(
            self._home,
            self._log,
            port=self._settings.member_port,
            timeout_s=self._settings.request_timeout_s,
            request=self._settings.request,
        )

    def _send(self, endpoint: Endpoint, state: BeaconState) -> None:
        try:
            device_id = local_device_id(self._home)
            name = device_name_of(self._home)
        except DeviceNameError as error:
            self._note("name", f"no beacon is sent: {error}")
            return
        if device_id is None:
            return
        view = self._watch()
        datagrams: list[bytes] = []
        if state.discoverable:
            hub_id, hub_name = (view.hub_id, view.hub_name) if view is not None else (None, None)
            datagrams.append(build_join(name, device_id, hub_id, hub_name, port=self._settings.join_port))
        if view is not None and view.key is not None:
            datagrams.append(build_member(view.key, view.hub_id, device_id, int(self._settings.clock()), port=self._settings.member_port))
        for data in datagrams:
            result = endpoint.send(data)
            if result.errors:
                self._note("send", f"a beacon was not sent on every path: {'; '.join(result.errors)}")

    def _receive(self, datagram: Datagram, inbox: Inbox) -> None:
        heard = inbox.accept(datagram)
        if heard is None or not isinstance(heard.announcement, MemberBeacon):
            return
        beacon = heard.announcement
        verdict = self._check.check(beacon)
        if not verdict.ok:
            self._note(f"reject:{beacon.device_id}:{verdict.reason}", f"ignored a member beacon from {heard.host}: {verdict.reason}")
            return
        if heard.host == verdict.stored_address:
            self._mark_online(beacon.device_id, heard.host)
            return
        if not self._reachable(heard.host):
            return
        self._reconnect.consider(beacon.device_id, heard.host)

    def _reachable(self, address: str) -> bool:
        wire = self._wire
        if wire is None:
            return False
        try:
            self._settings.check_destination(address, self._settings.member_port, wire.bind, wire.subnet.prefixlen)
        except (PairingError, ValueError, OSError) as error:
            self._note(f"destination:{address}", f"a member beacon from {address} is not used: {error}")
            return False
        return True

    def _mark_online(self, device_id: str, address: str) -> None:
        now = time.monotonic()
        last = self._recorded.get(device_id)
        if last is not None and now - last < ONLINE_RECORD_S:
            return
        self._recorded[device_id] = now
        with contextlib.suppress(*_TASK_ERRORS):
            record_presence(self._home, device_id, True, address)

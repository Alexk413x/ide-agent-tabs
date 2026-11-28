from __future__ import annotations

import ipaddress
import socket
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field

from . import pairing
from .beacon import Announcement, Endpoint, HubWatch, JoinAnnouncement, Solicitation, Wire
from .client import NetClientError, verify_pinned
from .discovery import PROBE, REMEMBERED, TAILSCALE, LanDiscovery, source_label
from .pairing import PAIRING_PORT, JoinProbe, PairIdentity, PairingError, probe_join
from .policy import DEFAULT_PORT, PeerGrant, PolicyError, load_policy
from .tailscale import TailscaleDiscovery, TailscaleError

_VERIFY_ERRORS = (NetClientError, PairingError, OSError, ValueError)


@dataclass(frozen=True)
class FoundDevice:
    device_id: str
    device_name: str
    address: str
    hub_name: str | None
    member: bool
    source: str

    def as_json(self) -> dict[str, object]:
        return {
            "deviceName": self.device_name,
            "deviceId": self.device_id,
            "address": self.address,
            "hub": self.hub_name,
            "member": self.member,
            "source": self.source,
        }


@dataclass(frozen=True)
class FindResult:
    devices: tuple[FoundDevice, ...] = ()
    warnings: tuple[str, ...] = ()
    probed: bool = False
    heard: int = 0
    udp_error: str | None = None
    datagrams: int = 0


@dataclass(frozen=True)
class FindRequest:
    home: str
    identity: PairIdentity
    device_id: str
    wire: Wire
    log: Callable[[str], None] = lambda _text: None
    tailscale: TailscaleDiscovery | None = None
    check: Callable[[str], None] = lambda _address: None


def lan_hosts(wire: Wire) -> list[str]:
    network = ipaddress.IPv4Network(f"{wire.bind}/24", strict=False)
    if wire.subnet.prefixlen > 24:
        network = wire.subnet
    return [str(host) for host in network.hosts() if str(host) != wire.bind]


def _port_is_open(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


@dataclass
class FindSettings:
    listen_s: float = 10.0
    probe_after_s: float = 5.0
    grace_s: float = 3.0
    poll_s: float = 0.25
    verify_workers: int = 8
    verify_timeout_s: float = 4.0
    probe_workers: int = 64
    probe_connect_s: float = 0.3
    probe_retry_s: float = 1.1
    member_port: int = DEFAULT_PORT
    join_port: int = PAIRING_PORT
    hosts: Callable[[Wire], Iterable[str]] = lan_hosts
    make_endpoint: Callable[[Wire], Endpoint] = Endpoint
    timeouts: pairing.Timeouts = pairing.DEFAULT_TIMEOUTS
    verify_member: Callable[..., str] = verify_pinned
    probe_join: Callable[..., JoinProbe] = probe_join
    port_is_open: Callable[[str, int, float], bool] = _port_is_open
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep


@dataclass
class _Row:
    device_id: str
    name: str
    address: str
    hub_name: str | None
    member: bool
    sources: set[str] = field(default_factory=lambda: set[str]())
    announcements: set[str] = field(default_factory=lambda: set[str]())


class _Rows:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rows: dict[str, _Row] = {}

    def add(
        self,
        device_id: str,
        name: str,
        address: str,
        hub_name: str | None,
        member: bool,
        source: str | None,
        announcement: str | None,
    ) -> None:
        with self._lock:
            row = self._rows.get(device_id)
            if row is None:
                row = self._rows[device_id] = _Row(device_id, name, address, hub_name, member)
            elif row.address != address:
                return
            if source is not None:
                row.sources.add(source)
            if announcement is not None:
                row.announcements.add(announcement)

    def count(self) -> int:
        with self._lock:
            return len(self._rows)

    def final(self, paths_for: Callable[[str], frozenset[str]]) -> tuple[FoundDevice, ...]:
        with self._lock:
            found = [
                FoundDevice(
                    r.device_id,
                    r.name,
                    r.address,
                    r.hub_name,
                    r.member,
                    source_label(r.sources.union(*(paths_for(identifier) for identifier in r.announcements))),
                )
                for r in self._rows.values()
            ]
        return tuple(sorted(found, key=lambda d: (not d.member, d.device_name.casefold(), d.address)))


def duplicate_name_warnings(devices: Sequence[FoundDevice]) -> tuple[str, ...]:
    groups: dict[str, list[FoundDevice]] = {}
    for device in devices:
        groups.setdefault(device.device_name.casefold(), []).append(device)
    return tuple(
        f'warning: {len(group)} devices are named "{group[0].device_name}" ({", ".join(f"{d.device_id[:15]} at {d.address}" for d in group)}). '
        "Check the device id before you join."
        for group in groups.values()
        if len(group) > 1
    )


class _Find:
    def __init__(self, request: FindRequest, settings: FindSettings) -> None:
        self.request = request
        self.settings = settings
        policy = load_policy(request.home)
        self.members: dict[str, PeerGrant] = {p.device_id: p for p in policy.peers if p.member}
        self.hub_name = policy.hub.name if policy.hub is not None else None
        self.rows = _Rows()
        self.pool = ThreadPoolExecutor(max_workers=settings.verify_workers)
        self.pending: list[Future[object]] = []
        self.lock = threading.Lock()
        self.tried: set[str] = set()
        self.probes: dict[str, JoinProbe] = {}
        self.address_locks: dict[str, threading.Lock] = {}
        self.announced: set[tuple[str, str]] = set()
        self.discovery = LanDiscovery(
            request.wire,
            request.device_id,
            hub_view=HubWatch(request.home),
            make_endpoint=settings.make_endpoint,
            poll_s=settings.poll_s,
        )
        self.heard = 0

    def log(self, text: str) -> None:
        self.request.log(f"find: {text}")

    def submit(self, task: Callable[[], object]) -> None:
        with self.lock:
            self.pending.append(self.pool.submit(task))

    def verify_member(self, address: str, device_ids: Sequence[str], source: str | None, announcement: str | None = None) -> bool:
        settings = self.settings
        try:
            matched = settings.verify_member(
                self.request.home, address, device_ids, port=settings.member_port, timeout=settings.verify_timeout_s
            )
        except _VERIFY_ERRORS as error:
            self.log(f"{address} did not verify as a member: {error}")
            return False
        self.rows.add(matched, self.members[matched].label, address, self.hub_name, True, source, announcement)
        return True

    def verify_join(
        self, address: str, expected: str | None, source: str | None, announcement: str | None = None, attempts: int = 1
    ) -> bool:
        with self.address_lock(address):
            known = self.probes.get(address)
            if known is not None and (expected is None or known.device_id == expected):
                self.add_probed(known, address, source, announcement)
                return True
            found = self.probe_device(address, expected, attempts)
            if found is None:
                return False
            self.probes[address] = found
            self.add_probed(found, address, source, announcement)
            return True

    def address_lock(self, address: str) -> threading.Lock:
        with self.lock:
            return self.address_locks.setdefault(address, threading.Lock())

    def add_probed(self, found: JoinProbe, address: str, source: str | None, announcement: str | None) -> None:
        hub = found.hub.name if found.hub is not None else None
        self.rows.add(found.device_id, found.name, address, hub, False, source, announcement)

    def probe_device(self, address: str, expected: str | None, attempts: int) -> JoinProbe | None:
        settings = self.settings
        for attempt in range(attempts):
            try:
                return settings.probe_join(
                    address,
                    settings.join_port,
                    expected,
                    self.request.identity,
                    timeouts=settings.timeouts,
                    **({} if self.request.tailscale is None else {"transport": "tailscale"}),
                )
            except _VERIFY_ERRORS as error:
                if attempt + 1 < attempts:
                    settings.sleep(settings.probe_retry_s)
                    continue
                self.log(f"{address} did not verify as a discoverable device: {error}")
        return None

    def announced_device(self, announcement: Announcement, host: str) -> None:
        key = (host, announcement.device_id)
        if key in self.announced:
            return
        self.announced.add(key)
        self.tried.add(host)
        join_ok = isinstance(announcement, JoinAnnouncement)
        self.submit(lambda: self.verify_known(host, announcement.device_id, None, announcement.id, join_ok))

    def remember(self) -> None:
        for peer in self.members.values():
            self.tried.add(peer.address)
            self.submit(lambda p=peer: self.verify_known(p.address, p.device_id, REMEMBERED))

    def verify_known(self, address: str, device_id: str, source: str | None, announcement: str | None = None, join_ok: bool = True) -> None:
        if device_id in self.members and self.verify_member(address, [device_id], source, announcement):
            return
        if join_ok:
            self.verify_join(address, device_id, source, announcement)

    def probe(self, hard_stop: float) -> None:
        settings = self.settings
        hosts = [host for host in settings.hosts(self.request.wire) if host not in self.tried]
        ports = (settings.member_port, settings.join_port)
        hits: dict[str, set[int]] = {}

        def check(host: str, port: int) -> None:
            if settings.clock() < hard_stop and settings.port_is_open(host, port, settings.probe_connect_s):
                with self.lock:
                    hits.setdefault(host, set()).add(port)

        with ThreadPoolExecutor(max_workers=settings.probe_workers) as pool:
            for host in hosts:
                for port in ports:
                    pool.submit(check, host, port)
        for host in sorted(hits):
            self.submit(lambda h=host, found=hits[host]: self.verify_probe_hit(h, found))

    def verify_probe_hit(self, host: str, ports: set[int]) -> None:
        settings = self.settings
        if settings.member_port in ports and self.members and self.verify_member(host, list(self.members), PROBE):
            return
        if settings.join_port in ports:
            self.verify_join(host, None, PROBE, attempts=2)

    def all_done(self) -> bool:
        with self.lock:
            return all(future.done() for future in self.pending)

    def verify_candidate(self, address: str, hard_stop: float) -> None:
        settings = self.settings
        try:
            self.request.check(address)
        except (PairingError, TailscaleError) as error:
            self.log(f"{address} is not tried: {error}")
            return
        ports: set[int] = set()
        for port in (settings.member_port, settings.join_port):
            if settings.clock() < hard_stop and settings.port_is_open(address, port, settings.probe_connect_s):
                ports.add(port)
        if settings.member_port in ports and self.members and self.verify_member(address, list(self.members), TAILSCALE):
            return
        if settings.join_port in ports:
            self.verify_join(address, None, TAILSCALE, attempts=2)

    def run_tailscale(self, discovery: TailscaleDiscovery) -> FindResult:
        settings = self.settings
        hard_stop = settings.clock() + settings.listen_s
        for device in discovery.scan():
            self.tried.add(device.address)
            self.submit(lambda address=device.address: self.verify_candidate(address, hard_stop))
        grace_end = hard_stop + settings.grace_s
        for future in list(self.pending):
            try:
                future.result(timeout=max(0.0, grace_end - settings.clock()))
            except FutureTimeout:
                self.log("a verification did not finish in time, so that candidate is not listed")
        self.pool.shutdown(wait=False, cancel_futures=True)
        devices = self.rows.final(lambda _identifier: frozenset())
        return FindResult(devices, duplicate_name_warnings(devices), True, 0, discovery.error, 0)

    def run(self) -> FindResult:
        if self.request.tailscale is not None:
            return self.run_tailscale(self.request.tailscale)
        settings = self.settings
        start = settings.clock()
        hard_stop = start + settings.listen_s
        self.remember()
        stream = self.discovery.listen(lambda: settings.clock() >= hard_stop)
        exhausted = False
        probe_thread: threading.Thread | None = None
        while settings.clock() < hard_stop:
            if probe_thread is not None and not probe_thread.is_alive() and self.all_done():
                break
            if probe_thread is None and (exhausted or settings.clock() - start >= settings.probe_after_s) and self.rows.count() == 0:
                probe_thread = threading.Thread(target=self.probe, args=(hard_stop,), daemon=True)
                probe_thread.start()
            if exhausted:
                settings.sleep(settings.poll_s)
                continue
            try:
                heard = next(stream)
            except StopIteration:
                exhausted = True
                continue
            if heard is not None and not isinstance(heard.announcement, Solicitation):
                self.heard += 1
                self.announced_device(heard.announcement, heard.host)
        stream.close()
        self.log_udp()
        grace_end = settings.clock() + settings.grace_s
        if probe_thread is not None:
            probe_thread.join(max(0.0, grace_end - settings.clock()))
        for future in list(self.pending):
            try:
                future.result(timeout=max(0.0, grace_end - settings.clock()))
            except FutureTimeout:
                self.log("a verification did not finish in time, so that candidate is not listed")
        self.pool.shutdown(wait=False, cancel_futures=True)
        devices = self.rows.final(self.discovery.paths_for)
        return FindResult(
            devices, duplicate_name_warnings(devices), probe_thread is not None, self.heard, self.discovery.error, self.discovery.datagrams
        )

    def log_udp(self) -> None:
        discovery = self.discovery
        if discovery.error is not None:
            return
        joined = {True: "yes", False: "no", None: "unknown"}[discovery.group_joined]
        self.log(
            f"udp: joined the group {joined}; {discovery.solicits_sent} solicitation(s) sent; {discovery.datagrams} datagram(s) received, "
            f"{discovery.own} from this machine, {discovery.solicitations} solicitation(s) from other finds, {discovery.invalid} invalid, "
            f"{discovery.ignored_beacons} member beacon(s) ignored, {self.heard} announcement(s) accepted"
            + "".join(f"; {note}" for note in discovery.setup_notes)
        )


def find_devices(request: FindRequest, settings: FindSettings | None = None) -> FindResult:
    try:
        return _Find(request, settings or FindSettings()).run()
    except PolicyError as error:
        raise NetClientError(f"policy.json is not usable: {error}") from error


NOT_FOUND_TEXT = (
    "No device was found. The likely causes are:\n"
    "- the other machine is not discoverable: run `agent-tabs net discoverable on` there\n"
    "- a network is Public: LAN mode accepts Private networks only, on both machines\n"
    "- the current network is not in network.allowedNetworks on the other machine: `agent-tabs net networks` lists it\n"
    "- the UDP rule for port 47831 is missing on a Windows machine: `agent-tabs net discoverable on` creates it\n"
    "- the machines are on different subnets\n"
    "Wi-Fi client isolation, common on guest networks, blocks all traffic between devices, so no LAN method works there. "
    "For that case, use Tailscale mode when it is available."
)


TAILSCALE_NOT_FOUND_TEXT = (
    "No device was found. The likely causes are:\n"
    "- Tailscale is not running or not logged in on this machine or the other machine\n"
    "- the other machine is not online in this Tailscale account, or belongs to another user\n"
    "- the other machine is not discoverable: run `agent-tabs net discoverable on` there, in Tailscale mode\n"
    "- the other machine runs LAN mode, and both machines must use the same mode"
)


def not_found_text(result: FindResult, wire: Wire, tailscale: bool = False) -> str:
    if tailscale:
        if result.udp_error is None:
            return TAILSCALE_NOT_FOUND_TEXT
        return f"{TAILSCALE_NOT_FOUND_TEXT}\nTailscale could not list the devices of this account: {result.udp_error}."
    notes = [f"Checked: announcements on UDP port {wire.port}"]
    if result.udp_error is not None:
        notes[0] = f"Announcements could not be received on UDP port {wire.port} ({result.udp_error})"
    if result.probed:
        notes.append(f"every address of the local /24 on ports {DEFAULT_PORT} and {PAIRING_PORT}")
    return f"{NOT_FOUND_TEXT}\n{'; '.join(notes)}."


def device_lines(result: FindResult) -> list[str]:
    lines: list[str] = []
    for device in result.devices:
        hub = device.hub_name or "no hub"
        lines.append(
            f"{device.device_name}  {device.device_id}  {device.address}  hub: {hub}  member: {'yes' if device.member else 'no'}  "
            f"source: {device.source}"
        )
    return lines

from __future__ import annotations

import contextlib
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import socket
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Union

from .firewall import DISCOVERY_PORT
from .hubstate import HubStateError, hub_state_path, read_hub_state
from .identity import format_digest, parse_device_id
from .pairing import PAIRING_PORT, PairingError, check_hub_id, check_hub_name, valid_device_name
from .policy import DEFAULT_PORT, PolicyError, load_policy, policy_path

VERSION = 1
MULTICAST_GROUP = "239.255.47.31"
MAX_DATAGRAM = 1200
SEEN_TTL_S = 300.0
MAX_SEEN = 4096
TS_WINDOW_S = 120
TRANSPORT = "lan"
MULTICAST = "multicast"
BROADCAST = "broadcast"

_ID = re.compile(r"[0-9a-f]{32}")
_MAC = re.compile(r"[0-9a-f]{64}")
_JOIN_KEYS = ("v", "kind", "id", "deviceName", "deviceId", "hubId", "hubName", "port", "transport")
_MEMBER_KEYS = ("v", "kind", "id", "hubId", "deviceId", "port", "ts", "mac")
_SOLICIT_KEYS = ("v", "kind", "id")


class BeaconError(ValueError):
    pass


@dataclass(frozen=True)
class JoinAnnouncement:
    id: str
    device_name: str
    device_id: str
    hub_id: str | None
    hub_name: str | None
    port: int = PAIRING_PORT
    transport: str = TRANSPORT


@dataclass(frozen=True)
class MemberBeacon:
    id: str
    hub_id: str
    device_id: str
    port: int
    ts: int
    mac: str = field(repr=False)


@dataclass(frozen=True)
class Solicitation:
    id: str


Announcement = Union[JoinAnnouncement, MemberBeacon]
Datum = Union[JoinAnnouncement, MemberBeacon, Solicitation]


def new_announcement_id() -> str:
    return secrets.token_hex(16)


def _encode(fields: Mapping[str, Any]) -> bytes:
    payload = json.dumps(fields, separators=(",", ":")).encode("ascii")
    if len(payload) > MAX_DATAGRAM:
        raise BeaconError("the announcement is larger than one datagram may be")
    return payload


def build_join(
    device_name: str,
    device_id: str,
    hub_id: str | None,
    hub_name: str | None,
    port: int = PAIRING_PORT,
    announcement_id: str | None = None,
) -> bytes:
    return _encode(
        {
            "v": VERSION,
            "kind": "join",
            "id": announcement_id or new_announcement_id(),
            "deviceName": device_name,
            "deviceId": device_id,
            "hubId": hub_id,
            "hubName": hub_name,
            "port": port,
            "transport": TRANSPORT,
        }
    )


def build_solicit(announcement_id: str | None = None) -> bytes:
    return _encode({"v": VERSION, "kind": "solicit", "id": announcement_id or new_announcement_id()})


def _mac(key: str, announcement_id: str, hub_id: str, device_id: str, port: int, ts: int) -> str:
    unsigned = {"v": VERSION, "kind": "member", "id": announcement_id, "hubId": hub_id, "deviceId": device_id, "port": port, "ts": ts}
    canonical = json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode("ascii")
    return hmac.new(bytes.fromhex(key), canonical, hashlib.sha256).hexdigest()


def build_member(
    key: str,
    hub_id: str,
    device_id: str,
    ts: int,
    port: int = DEFAULT_PORT,
    announcement_id: str | None = None,
) -> bytes:
    announcement_id = announcement_id or new_announcement_id()
    return _encode(
        {
            "v": VERSION,
            "kind": "member",
            "id": announcement_id,
            "hubId": hub_id,
            "deviceId": device_id,
            "port": port,
            "ts": ts,
            "mac": _mac(key, announcement_id, hub_id, device_id, port, ts),
        }
    )


def mac_is_valid(key: str, beacon: MemberBeacon) -> bool:
    try:
        expected = _mac(key, beacon.id, beacon.hub_id, beacon.device_id, beacon.port, beacon.ts)
    except ValueError:
        return False
    return hmac.compare_digest(expected.encode("ascii"), beacon.mac.encode("ascii"))


def _no_constant(name: str) -> Any:
    raise BeaconError(f"{name} is not allowed")


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for key, value in pairs:
        if key in found:
            raise BeaconError("a field repeats")
        found[key] = value
    return found


def _whole(value: object, what: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise BeaconError(f"{what} is malformed")
    return value


def _device_id(value: object) -> str:
    if not isinstance(value, str):
        raise BeaconError("deviceId is malformed")
    try:
        if format_digest(parse_device_id(value)) != value:
            raise BeaconError("deviceId is not canonical")
    except ValueError as error:
        raise BeaconError("deviceId is malformed") from error
    return value


def _hub(hub_id: object, hub_name: object) -> tuple[str | None, str | None]:
    if hub_id is None and hub_name is None:
        return None, None
    try:
        return check_hub_id(hub_id), check_hub_name(hub_name)
    except PairingError as error:
        raise BeaconError(str(error)) from error


def parse_datagram(data: bytes) -> Datum:
    if len(data) > MAX_DATAGRAM:
        raise BeaconError("the datagram is too large")
    try:
        value = json.loads(data.decode("utf-8"), parse_constant=_no_constant, object_pairs_hook=_no_duplicates)
    except (ValueError, RecursionError) as error:
        raise BeaconError("the datagram is not JSON") from error
    if not isinstance(value, dict):
        raise BeaconError("the datagram is not an object")
    version, kind = value.get("v"), value.get("kind")  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
    if isinstance(version, bool) or version != VERSION:
        raise BeaconError("the version is not supported")
    keys = {"join": _JOIN_KEYS, "member": _MEMBER_KEYS, "solicit": _SOLICIT_KEYS}.get(kind) if isinstance(kind, str) else None
    if keys is None or set(value) != set(keys):  # pyright: ignore[reportUnknownArgumentType]
        raise BeaconError("the kind is unknown or a field is missing or unknown")
    identifier = value["id"]
    if not isinstance(identifier, str) or _ID.fullmatch(identifier) is None:
        raise BeaconError("id is malformed")
    if kind == "solicit":
        return Solicitation(identifier)
    if kind == "join":
        name = value["deviceName"]
        if not isinstance(name, str) or not valid_device_name(name):
            raise BeaconError("deviceName is malformed")
        hub_id, hub_name = _hub(value["hubId"], value["hubName"])
        if value["transport"] != TRANSPORT:
            raise BeaconError("transport is not supported")
        return JoinAnnouncement(identifier, name, _device_id(value["deviceId"]), hub_id, hub_name, _whole(value["port"], "port", 1, 65535))
    mac = value["mac"]
    if not isinstance(mac, str) or _MAC.fullmatch(mac) is None:
        raise BeaconError("mac is malformed")
    try:
        hub_id = check_hub_id(value["hubId"])
    except PairingError as error:
        raise BeaconError(str(error)) from error
    return MemberBeacon(
        identifier,
        hub_id,
        _device_id(value["deviceId"]),
        _whole(value["port"], "port", 1, 65535),
        _whole(value["ts"], "ts", 0, 2**62),
        mac,
    )


@dataclass(frozen=True)
class HubView:
    hub_id: str
    hub_name: str
    key: str | None
    members: Mapping[str, str]


class HubWatch:
    def __init__(self, home: str) -> None:
        self._home = home
        self._signature: object = object()
        self._view: HubView | None = None
        self.problem: str | None = None

    def _stamp(self) -> object:
        def stat_of(path: str) -> tuple[int, int, int] | None:
            try:
                status = os.stat(path)
            except OSError:
                return None
            return status.st_mtime_ns, status.st_size, status.st_ino

        return stat_of(policy_path(self._home)), stat_of(str(hub_state_path(self._home)))

    def __call__(self) -> HubView | None:
        stamp = self._stamp()
        if stamp != self._signature:
            self._view = self._read()
            self._signature = stamp
        return self._view

    def _read(self) -> HubView | None:
        self.problem = None
        try:
            policy = load_policy(self._home)
            if policy.hub is None:
                return None
            state = read_hub_state(self._home)
        except (PolicyError, HubStateError, OSError) as error:
            self.problem = str(error)
            return None
        key = state.key if state is not None and state.hub_id == policy.hub.id else None
        members = {p.device_id: p.address for p in policy.peers if p.member}
        return HubView(policy.hub.id, policy.hub.name, key, members)


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str = ""
    stored_address: str | None = None


class MemberCheck:
    def __init__(self, view: Callable[[], HubView | None], clock: Callable[[], float] = time.time) -> None:
        self._view = view
        self._clock = clock
        self._last_ts: dict[str, int] = {}

    def check(self, beacon: MemberBeacon) -> Verdict:
        view = self._view()
        if view is None:
            return Verdict(False, "this machine is not in a hub")
        if beacon.hub_id != view.hub_id:
            return Verdict(False, "the beacon is for another hub")
        if view.key is None:
            return Verdict(False, "this machine has no key for the hub")
        if beacon.device_id not in view.members:
            return Verdict(False, "the beacon is from a device that is not a known member")
        if not mac_is_valid(view.key, beacon):
            return Verdict(False, "the beacon's MAC is not valid under this hub's key")
        if abs(self._clock() - beacon.ts) > TS_WINDOW_S:
            return Verdict(False, "the beacon's timestamp is outside the allowed window")
        if beacon.ts < self._last_ts.get(beacon.device_id, 0):
            return Verdict(False, "the beacon is older than one already accepted from that device")
        self._last_ts[beacon.device_id] = beacon.ts
        return Verdict(True, stored_address=view.members[beacon.device_id])


@dataclass(frozen=True)
class Wire:
    bind: str
    broadcast: str | None
    subnet: ipaddress.IPv4Network
    group: str = MULTICAST_GROUP
    port: int = DISCOVERY_PORT


def wire_for(address: str, prefix_len: int, port: int = DISCOVERY_PORT) -> Wire:
    prefix = prefix_len if 8 <= prefix_len <= 30 else 24
    subnet = ipaddress.IPv4Network(f"{address}/{prefix}", strict=False)
    return Wire(address, str(subnet.broadcast_address), subnet, port=port)


@dataclass(frozen=True)
class Datagram:
    data: bytes
    host: str
    port: int


@dataclass(frozen=True)
class SendResult:
    sent: tuple[str, ...]
    errors: tuple[str, ...]


class Endpoint:
    def __init__(
        self,
        wire: Wire,
        *,
        multicast: bool = True,
        broadcast: bool = True,
        make_socket: Callable[..., Any] = socket.socket,
    ) -> None:
        self.wire = wire
        self.joined = False
        self.notes: list[str] = []
        self._multicast = multicast
        self._broadcast = broadcast
        self._tx: socket.socket | None = None
        self._rx = make_socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self._open_receiver()
            if broadcast and wire.broadcast is not None:
                self._tx = self._open_broadcaster(make_socket)
        except OSError:
            self.close()
            raise

    def _try(self, what: str, call: Callable[[], object]) -> bool:
        try:
            call()
        except OSError as error:
            self.notes.append(f"{what} failed: {error.strerror or error}")
            return False
        return True

    # Both senders and receivers share the port, because `net find` runs beside the listener on one machine. A
    # socket bound to a specific address on Windows receives the group and the subnet broadcast. On other systems
    # only the wildcard address receives them, and the caller's source-subnet check removes other interfaces.
    def _open_receiver(self) -> None:
        rx, wire = self._rx, self.wire
        rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if sys.platform != "win32" and hasattr(socket, "SO_REUSEPORT"):
            self._try("SO_REUSEPORT", lambda: rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1))
        rx.bind((wire.bind if sys.platform == "win32" else "", wire.port))
        member = socket.inet_aton(wire.group) + socket.inet_aton(wire.bind)
        self.joined = self._try("joining the group", lambda: rx.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, member))
        self._multicast = self._multicast and self._try(
            "IP_MULTICAST_IF", lambda: rx.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(wire.bind))
        )
        self._try("IP_MULTICAST_TTL", lambda: rx.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1))

    def _open_broadcaster(self, make_socket: Callable[..., Any]) -> socket.socket:
        tx = make_socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            tx.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            tx.bind((self.wire.bind, 0))
        except OSError:
            tx.close()
            raise
        return tx

    def send(self, data: bytes) -> SendResult:
        sent: list[str] = []
        errors: list[str] = []
        wire = self.wire
        # The group copy leaves from the receiving socket, so its source port is the discovery port. The
        # broadcast copy leaves from another socket. A receiver reads the source port to name the path.
        paths: list[tuple[str, socket.socket | None, str | None]] = [
            (MULTICAST, self._rx if self._multicast else None, wire.group),
            (BROADCAST, self._tx, wire.broadcast),
        ]
        for name, sock, host in paths:
            if sock is None or host is None:
                continue
            try:
                sock.sendto(data, (host, wire.port))
            except OSError as error:
                errors.append(f"{name}: {error.strerror or error}")
            else:
                sent.append(name)
        return SendResult(tuple(sent), tuple(errors))

    def recv(self, timeout: float) -> Datagram | None:
        try:
            self._rx.settimeout(max(0.01, timeout))
            data, address = self._rx.recvfrom(MAX_DATAGRAM + 1)
        except (socket.timeout, ConnectionResetError):
            return None
        return Datagram(data, str(address[0]), int(address[1]))

    def close(self) -> None:
        for sock in (self._rx, self._tx):
            if sock is not None:
                with contextlib.suppress(OSError):
                    sock.close()


@dataclass(frozen=True)
class Heard:
    announcement: Datum
    host: str
    path: str


class Inbox:
    def __init__(self, wire: Wire, own_device_id: str | None, clock: Callable[[], float] = time.monotonic) -> None:
        self._wire = wire
        self._own = own_device_id
        self._clock = clock
        self._seen: dict[str, tuple[float, set[str]]] = {}
        self.rejected = 0
        self.own = 0

    def _path_of(self, datagram: Datagram) -> str:
        return MULTICAST if datagram.port == self._wire.port else BROADCAST

    def _in_scope(self, host: str) -> bool:
        try:
            address = ipaddress.IPv4Address(host)
        except ValueError:
            return False
        return address in self._wire.subnet and host != self._wire.bind

    def _remember(self, identifier: str, path: str) -> bool:
        now = self._clock()
        known = self._seen.get(identifier)
        if known is not None and known[0] > now:
            known[1].add(path)
            return False
        if len(self._seen) >= MAX_SEEN:
            self._seen = {key: value for key, value in self._seen.items() if value[0] > now}
            if len(self._seen) >= MAX_SEEN:
                self._seen.clear()
        self._seen[identifier] = (now + SEEN_TTL_S, {path})
        return True

    def paths_for(self, identifier: str) -> frozenset[str]:
        known = self._seen.get(identifier)
        return frozenset(known[1]) if known is not None else frozenset()

    def accept(self, datagram: Datagram) -> Heard | None:
        if datagram.host == self._wire.bind:
            self.own += 1
            return None
        if not self._in_scope(datagram.host):
            return None
        try:
            announcement = parse_datagram(datagram.data)
        except BeaconError:
            self.rejected += 1
            return None
        if not isinstance(announcement, Solicitation) and announcement.device_id == self._own:
            self.own += 1
            return None
        if not self._remember(announcement.id, self._path_of(datagram)):
            return None
        return Heard(announcement, datagram.host, self._path_of(datagram))

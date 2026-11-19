from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import hmac
import ipaddress
import json
import re
import secrets
import select
import socket
import ssl
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, TypeVar

from ..files import file_lock, read_text_if_exists, write_atomically
from . import tls, wire
from .client import DeadlineSocket, as_socket
from .identity import device_id as device_id_of
from .identity import format_digest, parse_device_id
from .ingress import net_dir
from .private_files import check_private, ensure_private_dir, make_private_file

VERSION = 2
PAIRING_PORT = 47830
MAX_FRAME = 8192
PAIRING_FILE = "pairing.json"
FAILURES_PER_HOUR = 3
HOUR_S = 3600
LOCK_S = 3600
MAX_LIMIT_ADDRESSES = 256
MAX_SPACED_ADDRESSES = 1024
MAX_JOIN_MEMBERS = 32
MAX_JOIN_REMOVED = 64
MAX_HUB_FRAME = wire.MAX_FRAME
JOIN_MODE = "join"
PROBE_MODE = "probe"
ALREADY_IN_ANOTHER_HUB = "already-in-another-hub"
NO_HUB = "no-hub"
INCOMPLETE_TEXT = "Pairing may be incomplete on the other machine. Run `net status` there, or `net revoke <label>` here."

DIALER = "dialer"
LISTENER = "listener"

_IDLE_S = 3600.0
_NAME = re.compile(r"[A-Za-z0-9 ._-]{1,32}")
_DIGIT_RUN = re.compile(r"[0-9]{6}")
_IPV4 = re.compile(r"(?:0|[1-9][0-9]{0,2})(?:\.(?:0|[1-9][0-9]{0,2})){3}")
_HEX32 = re.compile(r"[0-9a-f]{64}")
_RFC1918 = tuple(ipaddress.IPv4Network(block) for block in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
_HUB_ID = re.compile(r"[0-9a-f]{32}")
_HUB_LABEL = re.compile(r"[a-z0-9-]{1,32}")
_MEMBER_KEYS = frozenset({"deviceId", "label", "address", "port", "certificatePem"})
_UNCOUNTED_REASONS = (ALREADY_IN_ANOTHER_HUB, NO_HUB, "saving the trust entry failed", "the join failed")
_LIMIT_KEYS = frozenset({"addresses", "windows", "open", "failures", "lockedUntil"})

T = TypeVar("T")
Outcome = Literal["paired", "incomplete", "declined", "failed", "probed"]


class PairingError(Exception):
    pass


class LimitError(PairingError):
    pass


class Locked(LimitError):
    pass


class _Dropped(Exception):
    pass


def H(*fields: bytes) -> bytes:
    digest = hashlib.sha256()
    for item in fields:
        digest.update(len(item).to_bytes(4, "big"))
        digest.update(item)
    return digest.digest()


def commitment(cert_d: bytes, nonce_d: bytes) -> bytes:
    return H(b"atp2 commit", b"dialer", cert_d, nonce_d)


def valid_device_name(name: str) -> bool:
    return _NAME.fullmatch(name) is not None and _DIGIT_RUN.search(name) is None


def full_device_id(value: object) -> str:
    if not isinstance(value, str):
        raise PairingError("the device id is malformed")
    try:
        canonical = format_digest(parse_device_id(value))
    except ValueError as error:
        raise PairingError("the device id is malformed") from error
    if canonical != value:
        raise PairingError("the device id is not in canonical form")
    return value


def _ipv4(value: object, what: str) -> ipaddress.IPv4Address:
    if not isinstance(value, str) or _IPV4.fullmatch(value) is None:
        raise PairingError(f"{what} must be a numeric IPv4 address")
    try:
        return ipaddress.IPv4Address(value)
    except ValueError as error:
        raise PairingError(f"{what} must be a numeric IPv4 address") from error


def _port(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise PairingError("the port must be a whole number from 1 to 65535")
    return value


def _private(address: ipaddress.IPv4Address) -> bool:
    return any(address in block for block in _RFC1918)


def check_lan_destination(address: str, port: int, interface_address: str, prefix_len: int) -> str:
    destination = _ipv4(address, "the address")
    _port(port)
    own = _ipv4(interface_address, "the interface address")
    if isinstance(prefix_len, bool) or not isinstance(prefix_len, int) or not 8 <= prefix_len <= 30:
        raise PairingError("the interface prefix length must be 8 to 30")
    if not _private(own):
        raise PairingError("the selected interface is not on an RFC 1918 network")
    if not _private(destination):
        raise PairingError("the address is not an RFC 1918 private address")
    network = ipaddress.IPv4Network(f"{own}/{prefix_len}", strict=False)
    if destination not in network:
        raise PairingError("the address is not on the selected interface's subnet")
    if destination == own:
        raise PairingError("the address is this machine's own address")
    if destination in (network.network_address, network.broadcast_address):
        raise PairingError("the address is the network or broadcast address of the subnet")
    return str(destination)


@dataclass(frozen=True)
class PairIdentity:
    name: str
    der: bytes
    cert: Path
    key: Path
    password: Callable[[], bytes]

    def __post_init__(self) -> None:
        if not valid_device_name(self.name):
            raise PairingError("the device name is not allowed")


@dataclass(frozen=True)
class PeerInfo:
    name: str
    address: str
    device_id: str
    der: bytes = field(repr=False)

    @property
    def pem(self) -> str:
        return ssl.DER_cert_to_PEM_cert(self.der)


@dataclass(frozen=True)
class PairResult:
    outcome: Outcome
    reason: str
    peer: PeerInfo | None = None
    locked: bool = False


@dataclass(frozen=True)
class JoinProbe:
    name: str
    device_id: str
    hub: HubRef | None


@dataclass(frozen=True)
class HubRef:
    id: str
    name: str


@dataclass(frozen=True)
class MemberRef:
    device_id: str
    label: str
    address: str
    port: int
    pem: str = field(repr=False)


@dataclass(frozen=True)
class RemovedRef:
    device_id: str
    at_ms: int


@dataclass(frozen=True)
class HubMessage:
    hub: HubRef
    key: str = field(repr=False)
    members: tuple[MemberRef, ...] = ()
    epoch: int = 0
    removed: tuple[RemovedRef, ...] = ()


class JoinHandler(Protocol):
    def hub(self) -> HubRef | None: ...

    def message(self, peer: PeerInfo) -> HubMessage: ...

    def save(self, peer: PeerInfo, received: HubMessage | None) -> None: ...


@dataclass(frozen=True)
class Timeouts:
    first_frame_s: float = 5.0
    handshake_s: float = 30.0
    consent_s: float = 30.0
    connect_s: float = 5.0
    min_interval_s: float = 1.0


DEFAULT_TIMEOUTS = Timeouts()


@dataclass
class _Failures:
    times: list[int]
    locked_until: int


@dataclass
class _State:
    addresses: dict[str, _Failures]


def _whole(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2**53:
        raise ValueError("not a whole number")
    return value


def _whole_list(value: object) -> list[int]:
    if not isinstance(value, list):
        raise TypeError("not a list")
    return [_whole(item) for item in value]


def _addresses(value: object) -> dict[str, _Failures]:
    if not isinstance(value, dict):
        raise TypeError("addresses must be an object")
    found: dict[str, _Failures] = {}
    for address, entry in value.items():  # pyright: ignore[reportUnknownVariableType]
        if not isinstance(address, str) or not isinstance(entry, dict) or set(entry) != {"failures", "lockedUntil"}:  # pyright: ignore[reportUnknownArgumentType]
            raise ValueError("bad address entry")
        found[address] = _Failures(_whole_list(entry["failures"]), _whole(entry["lockedUntil"]))
    return found


class Spacing:
    def __init__(self, interval_s: float, clock: Callable[[], float] = time.monotonic, cap: int = MAX_SPACED_ADDRESSES) -> None:
        self._interval_s = interval_s
        self._clock = clock
        self._cap = cap
        self._last: dict[str, float] = {}

    def allow(self, address: str) -> bool:
        now = self._clock()
        last = self._last.get(address)
        if last is not None and now - last < self._interval_s:
            return False
        if len(self._last) >= self._cap:
            self._last = {key: at for key, at in self._last.items() if now - at < self._interval_s}
            if len(self._last) >= self._cap:
                self._last.clear()
        self._last[address] = now
        return True


class Limits:
    def __init__(self, home: str, clock: Callable[[], float] = time.time) -> None:
        self._path = net_dir(home) / PAIRING_FILE
        self._clock = clock

    def _load(self) -> _State:
        try:
            text = read_text_if_exists(str(self._path))
        except OSError as error:
            raise PairingError(f"{self._path} cannot be read: {error}") from error
        if text is None:
            return _State({})
        problems = check_private(self._path)
        if problems:
            raise PairingError(f"{self._path} is not private: {'; '.join(problems)}")
        try:
            value = json.loads(text)
            if not isinstance(value, dict) or not set(value) <= _LIMIT_KEYS:  # pyright: ignore[reportUnknownArgumentType]
                raise ValueError("unknown fields")
            return _State(_addresses(value.get("addresses", {})))
        except (ValueError, TypeError) as error:
            raise PairingError(f"{self._path} is not valid ({error}); delete it to reset the pairing limits") from error

    def _store(self, state: _State) -> None:
        addresses = {a: {"failures": f.times, "lockedUntil": f.locked_until} for a, f in state.addresses.items()}
        body = {"addresses": addresses}
        ensure_private_dir(self._path.parent)
        existed = self._path.exists()
        write_atomically(str(self._path), json.dumps(body, indent=2) + "\n")
        if not existed:
            make_private_file(self._path)

    def _update(self, change: Callable[[_State, int], T], *, write: bool = True) -> T:
        now = int(self._clock())
        ensure_private_dir(self._path.parent)
        with file_lock(str(self._path)):
            state = self._load()
            for entry in state.addresses.values():
                entry.times = [t for t in entry.times if now - t < HOUR_S]
            state.addresses = {a: f for a, f in state.addresses.items() if f.times or now < f.locked_until}
            result = change(state, now)
            if write:
                self._store(state)
            return result

    @staticmethod
    def _raise_if_locked(state: _State, address: str, now: int) -> None:
        entry = state.addresses.get(address)
        if entry is not None and now < entry.locked_until:
            minutes = -(-(entry.locked_until - now) // 60)
            raise Locked(
                f"pairing is locked for {address} for {minutes} more minutes after {FAILURES_PER_HOUR} failed attempts; "
                "other addresses can still pair, and `net trust` still works"
            )

    def check_locked(self, address: str) -> None:
        self._update(lambda state, now: self._raise_if_locked(state, address, now), write=False)

    def locked_addresses(self) -> dict[str, int]:
        return self._update(
            lambda state, now: {a: f.locked_until - now for a, f in state.addresses.items() if now < f.locked_until}, write=False
        )

    def record_failure(self, address: str) -> bool:
        def change(state: _State, now: int) -> bool:
            entry = state.addresses.get(address)
            if entry is None:
                if len(state.addresses) >= MAX_LIMIT_ADDRESSES:
                    oldest = min(state.addresses, key=lambda a: max(state.addresses[a].times, default=state.addresses[a].locked_until))
                    del state.addresses[oldest]
                entry = state.addresses[address] = _Failures([], 0)
            entry.times.append(now)
            if len(entry.times) >= FAILURES_PER_HOUR:
                entry.locked_until = now + LOCK_S
                entry.times = []
            return now < entry.locked_until

        return self._update(change)


def _der_length(der: bytes) -> int:
    if len(der) < 2 or der[0] != 0x30:
        raise PairingError("the certificate is not a DER sequence")
    first = der[1]
    if first < 0x80:
        return 2 + first
    count = first & 0x7F
    if count == 0 or count > 4 or len(der) < 2 + count or der[2] == 0:
        raise PairingError("the certificate has a non-canonical DER length")
    length = int.from_bytes(der[2 : 2 + count], "big")
    if length < 0x80:
        raise PairingError("the certificate has a non-canonical DER length")
    return 2 + count + length


def parse_certificate(text: object) -> bytes:
    if not isinstance(text, str) or not text:
        raise PairingError("the certificate is missing")
    try:
        der = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as error:
        raise PairingError("the certificate is not base64") from error
    if base64.b64encode(der).decode("ascii") != text:
        raise PairingError("the certificate is not canonical base64")
    if _der_length(der) != len(der):
        raise PairingError("the certificate is not exactly one DER certificate")
    try:
        pem = ssl.DER_cert_to_PEM_cert(der)
        if ssl.PEM_cert_to_DER_cert(pem) != der:
            raise PairingError("the certificate does not survive a PEM round trip")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.load_verify_locations(cadata=pem)
    except (ValueError, ssl.SSLError) as error:
        raise PairingError("the certificate cannot be parsed") from error
    if context.cert_store_stats()["x509"] != 1:
        raise PairingError("the certificate cannot be parsed")
    return der


def _hex32(value: object, what: str) -> bytes:
    if not isinstance(value, str) or _HEX32.fullmatch(value) is None:
        raise PairingError(f"{what} is malformed")
    return bytes.fromhex(value)


def _expect(value: object, kind: str, keys: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PairingError("a frame is not an object")
    if value.get("t") != kind:
        raise PairingError("a frame arrived out of order")
    if set(value) != {"t", *keys}:
        raise PairingError("a frame has unknown or missing fields")
    return value


def _check_hello(frame: dict[str, Any], own_der: bytes) -> tuple[str, bytes]:
    version = frame["v"]
    if isinstance(version, bool) or version != VERSION:
        raise PairingError("the other machine's protocol version is not supported")
    name = frame["name"]
    if not isinstance(name, str) or not valid_device_name(name):
        raise PairingError("the other machine's device name is not allowed")
    der = parse_certificate(frame["cert"])
    if hmac.compare_digest(der, own_der):
        raise PairingError("the other machine presented this machine's own certificate")
    return name, der


def check_hub_id(value: object) -> str:
    if not isinstance(value, str) or _HUB_ID.fullmatch(value) is None:
        raise PairingError("the hub id is malformed")
    return value


def check_hub_name(value: object) -> str:
    if not isinstance(value, str) or _HUB_LABEL.fullmatch(value) is None:
        raise PairingError("the hub name is malformed")
    return value


def check_hub_key(value: object) -> str:
    if not isinstance(value, str) or _HEX32.fullmatch(value) is None:
        raise PairingError("the hub key is malformed")
    return value


def canonical_pem(text: object) -> tuple[str, bytes]:
    if not isinstance(text, str):
        raise PairingError("a member certificate is missing")
    try:
        der = ssl.PEM_cert_to_DER_cert(text)
    except ValueError as error:
        raise PairingError("a member certificate is not PEM") from error
    parse_certificate(base64.b64encode(der).decode("ascii"))
    pem = ssl.DER_cert_to_PEM_cert(der)
    if pem != text:
        raise PairingError("a member certificate is not canonical PEM")
    return pem, der


def check_member_fields(value: object) -> MemberRef:
    if not isinstance(value, dict) or set(value) != _MEMBER_KEYS:  # pyright: ignore[reportUnknownArgumentType]
        raise PairingError("a member entry has missing or unknown fields")
    device = full_device_id(value["deviceId"])
    label = value["label"]
    if not isinstance(label, str) or _HUB_LABEL.fullmatch(label) is None:
        raise PairingError("a member label is malformed")
    pem, der = canonical_pem(value["certificatePem"])
    if device_id_of(der) != device:
        raise PairingError("a member certificate does not match its device id")
    return MemberRef(device, label, str(_ipv4(value["address"], "a member's address")), _port(value["port"]), pem)


def parse_removed(value: object) -> tuple[RemovedRef, ...]:
    if not isinstance(value, list) or len(value) > MAX_JOIN_REMOVED:  # pyright: ignore[reportUnknownArgumentType]
        raise PairingError("the removed list is malformed or too long")
    found: dict[str, RemovedRef] = {}
    for entry in value:  # pyright: ignore[reportUnknownVariableType]
        if not isinstance(entry, dict) or set(entry) != {"deviceId", "atMs"}:  # pyright: ignore[reportUnknownArgumentType]
            raise PairingError("a removed entry has missing or unknown fields")
        try:
            at_ms = _whole(entry["atMs"])
        except ValueError:
            raise PairingError("a removed entry has a bad time") from None
        found[full_device_id(entry["deviceId"])] = RemovedRef(entry["deviceId"], at_ms)
    return tuple(found.values())


def hub_frame(message: HubMessage) -> dict[str, object]:
    return {
        "t": "hub",
        "hub": {"id": message.hub.id, "name": message.hub.name},
        "hubKey": message.key,
        "epoch": message.epoch,
        "members": [
            {"deviceId": m.device_id, "label": m.label, "address": m.address, "port": m.port, "certificatePem": m.pem}
            for m in message.members[:MAX_JOIN_MEMBERS]
        ],
        "removed": [{"deviceId": r.device_id, "atMs": r.at_ms} for r in message.removed[-MAX_JOIN_REMOVED:]],
    }


def _hub_ref(value: object) -> HubRef:
    if not isinstance(value, dict) or set(value) != {"id", "name"}:  # pyright: ignore[reportUnknownArgumentType]
        raise PairingError("a hub entry has missing or unknown fields")
    return HubRef(check_hub_id(value["id"]), check_hub_name(value["name"]))


def parse_hub_message(value: object) -> HubMessage:
    frame = _expect(value, "hub", ("hub", "hubKey", "epoch", "members", "removed"))
    epoch = frame["epoch"]
    if isinstance(epoch, bool) or not isinstance(epoch, int) or not 0 <= epoch <= wire.MAX_EPOCH:
        raise PairingError("the hub epoch is malformed")
    members = frame["members"]
    if not isinstance(members, list) or len(members) > MAX_JOIN_MEMBERS:  # pyright: ignore[reportUnknownArgumentType]
        raise PairingError("the member list is malformed or too long")
    refs = tuple(check_member_fields(m) for m in members)  # pyright: ignore[reportUnknownVariableType]
    if len({m.device_id for m in refs}) != len(refs):
        raise PairingError("the member list repeats a device")
    return HubMessage(_hub_ref(frame["hub"]), check_hub_key(frame["hubKey"]), refs, epoch, parse_removed(frame["removed"]))


def _hub_state(value: object) -> HubRef | None:
    return None if value is None else _hub_ref(value)


def _describe(error: BaseException) -> str:
    if isinstance(error, socket.timeout):
        return "timed out"
    if isinstance(error, ssl.SSLError):
        return f"the TLS check failed: {getattr(error, 'reason', None) or error}"
    if isinstance(error, (PairingError, wire.WireError)):
        return str(error)
    if isinstance(error, OSError):
        return "the connection failed"
    return "the pairing failed"


_EXPECTED = (PairingError, wire.WireError, OSError, ValueError)


class _Session:
    def __init__(
        self,
        conn: socket.socket,
        role: str,
        address: str,
        identity: PairIdentity,
        timeouts: Timeouts,
        join: JoinHandler | None,
    ) -> None:
        self.conn = conn
        self.role = role
        self.address = address
        self.identity = identity
        self.timeouts = timeouts
        self.join = join
        self.probing = join is None and role == DIALER
        self.committed = False

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self.conn.close()

    def _send(self, frame: Mapping[str, object], deadline: float, limit: int = MAX_FRAME) -> None:
        payload = wire.encode(frame)
        if len(payload) > limit:
            raise PairingError("a frame is too large")
        wire.write_frame(as_socket(DeadlineSocket(self.conn, deadline, _IDLE_S)), payload)

    def _recv_raw(self, deadline: float, limit: int = MAX_FRAME) -> object:
        return wire.parse_json(wire.read_frame(as_socket(DeadlineSocket(self.conn, deadline, _IDLE_S)), max_size=limit))

    def _recv(self, kind: str, keys: tuple[str, ...], deadline: float) -> dict[str, Any]:
        return _expect(self._recv_raw(deadline), kind, keys)

    # Each side waits for the other's reply before it sends again, so data already waiting is trailing or out of order.
    def _no_pending(self) -> None:
        readable, _, _ = select.select([self.conn], [], [], 0)
        if readable:
            if not self.conn.recv(1, socket.MSG_PEEK):
                raise PairingError("the other machine ended the session")
            raise PairingError("the other machine sent data out of order")

    def run_dialer(self, peer_id: str) -> PairResult:
        try:
            return self._dial(peer_id)
        except _EXPECTED as error:
            return PairResult("failed", _describe(error))
        finally:
            self.close()

    def run_listener(self, admit: Callable[[], bool]) -> PairResult | None:
        try:
            return self._listen(admit)
        except _Dropped:
            return None
        except _EXPECTED as error:
            return PairResult("failed", _describe(error)) if self.committed else None
        finally:
            self.close()

    def _dial(self, peer_id: str) -> PairResult:
        peer, deadline = self._open(peer_id)
        return self._finish(peer, deadline)

    def run_probe(self, peer_id: str | None) -> JoinProbe:
        try:
            peer, deadline = self._open(peer_id)
            self._upgrade(peer, deadline)
            hub = _hub_state(self._recv("state", ("hub",), deadline)["hub"])
            return JoinProbe(peer.name, peer.device_id, hub)
        except _EXPECTED as error:
            raise PairingError(_describe(error)) from error
        finally:
            self.close()

    def _open(self, peer_id: str | None) -> tuple[PeerInfo, float]:
        deadline = time.monotonic() + self.timeouts.handshake_s
        own = self.identity
        nonce_d = secrets.token_bytes(32)
        hello: dict[str, object] = {
            "t": "hello",
            "v": VERSION,
            "name": own.name,
            "cert": base64.b64encode(own.der).decode("ascii"),
            "commit": commitment(own.der, nonce_d).hex(),
            "mode": PROBE_MODE if self.probing else JOIN_MODE,
        }
        self._send(hello, deadline)
        reply = self._recv("hello", ("v", "name", "cert", "nonce"), deadline)
        name, der_l = _check_hello(reply, own.der)
        if peer_id is not None and not hmac.compare_digest(device_id_of(der_l).encode("ascii"), peer_id.encode("ascii")):
            raise PairingError("the machine at that address is not the device that was found: its certificate does not match the device id")
        _hex32(reply["nonce"], "the listener's nonce")
        self._no_pending()
        self._send({"t": "reveal", "nonce": nonce_d.hex()}, deadline)
        self.committed = True
        return PeerInfo(name, self.address, device_id_of(der_l), der_l), deadline

    def _listen(self, admit: Callable[[], bool]) -> PairResult:
        start = time.monotonic()
        own = self.identity
        try:
            first = self._recv_raw(start + self.timeouts.first_frame_s)
        except _EXPECTED as error:
            raise _Dropped from error
        mode = first.get("mode") if isinstance(first, dict) else None
        if mode not in (JOIN_MODE, PROBE_MODE) or not admit():
            raise _Dropped
        self.probing = mode == PROBE_MODE
        deadline = start + self.timeouts.handshake_s
        frame = _expect(first, "hello", ("v", "mode", "name", "cert", "commit"))
        name, der_d = _check_hello(frame, own.der)
        commit = _hex32(frame["commit"], "the dialer's commitment")
        self._no_pending()
        nonce_l = secrets.token_bytes(32)
        reply = {
            "t": "hello",
            "v": VERSION,
            "name": own.name,
            "cert": base64.b64encode(own.der).decode("ascii"),
            "nonce": nonce_l.hex(),
        }
        self._send(reply, deadline)
        nonce_d = _hex32(self._recv("reveal", ("nonce",), deadline)["nonce"], "the dialer's nonce")
        if not hmac.compare_digest(commitment(der_d, nonce_d), commit):
            raise PairingError("the dialer's reveal does not match its commitment")
        self.committed = True
        peer = PeerInfo(name, self.address, device_id_of(der_d), der_d)
        return self._finish(peer, deadline)

    def _finish(self, peer: PeerInfo, deadline: float) -> PairResult:
        self._upgrade(peer, deadline)
        return self._join(peer)

    def _upgrade(self, peer: PeerInfo, deadline: float) -> None:
        listening = self.role == LISTENER
        if listening:
            self._no_pending()
            self._send({"t": "tls"}, deadline)
        else:
            _expect(self._recv_raw(deadline), "tls", ())
            self._no_pending()
        build = tls.server_context if listening else tls.client_context
        context = build(self.identity.cert, self.identity.key, self.identity.password, [peer.pem])
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise socket.timeout("timed out")
        self.conn.settimeout(remaining)
        wrapped = context.wrap_socket(self.conn, server_side=listening)
        self.conn = wrapped
        tls.check_session(wrapped, peer.der)

    def _conclude(self, peer: PeerInfo, save: Callable[[], None]) -> PairResult:
        try:
            save()
        except Exception as error:  # noqa: BLE001 - the caller's save can fail in any way; nothing may be sent as saved
            return PairResult("failed", f"saving the trust entry failed: {error}", peer)
        try:
            self._send({"t": "saved"}, time.monotonic() + self.timeouts.consent_s)
            self._recv("saved", (), time.monotonic() + self.timeouts.consent_s)
        except _EXPECTED:
            return PairResult("incomplete", INCOMPLETE_TEXT, peer)
        return PairResult("paired", "paired", peer)

    # Each side learns the other's hub before any secret moves, so machines in different hubs share nothing. Both
    # validate and send "ready" before either saves, so a side that rejects the hub message leaves both unsaved.
    def _join(self, peer: PeerInfo) -> PairResult:
        handler = self.join
        assert handler is not None
        deadline = time.monotonic() + self.timeouts.consent_s
        mine = handler.hub()
        self._send({"t": "state", "hub": None if mine is None else {"id": mine.id, "name": mine.name}}, deadline)
        if self.probing:
            return PairResult("probed", "probed", peer)
        theirs = _hub_state(self._recv("state", ("hub",), deadline)["hub"])
        if mine is None and theirs is None:
            raise PairingError(f"{NO_HUB}: neither machine is in a hub; create one first")
        if mine is not None and theirs is not None and mine.id != theirs.id:
            raise PairingError(
                f"{ALREADY_IN_ANOTHER_HUB}: this machine is in the hub {mine.name} and the other machine is in the hub {theirs.name}"
            )
        received: HubMessage | None = None
        if mine is not None and (theirs is None or self.role == LISTENER):
            self._send(hub_frame(handler.message(peer)), deadline, MAX_HUB_FRAME)
        else:
            received = parse_hub_message(self._recv_raw(deadline, MAX_HUB_FRAME))
            if received.hub != theirs:
                raise PairingError("the hub message names another hub than the one the other machine announced")
        ready_deadline = time.monotonic() + self.timeouts.consent_s
        self._send({"t": "ready"}, ready_deadline)
        self._recv("ready", (), ready_deadline)
        return self._conclude(peer, lambda: handler.save(peer, received))


def private_source(address: str) -> bool:
    try:
        return _private(ipaddress.IPv4Address(address))
    except ValueError:
        return False


def dial_join(
    address: str,
    port: int,
    peer_id: str,
    identity: PairIdentity,
    handler: JoinHandler,
    limits: Limits,
    *,
    timeouts: Timeouts = DEFAULT_TIMEOUTS,
) -> PairResult:
    target = str(_ipv4(address, "the address"))
    _port(port)
    full_device_id(peer_id)
    limits.check_locked(target)
    try:
        conn = socket.create_connection((target, port), timeout=timeouts.connect_s)
    except OSError as error:
        return PairResult("failed", f"cannot connect to {target}:{port}: {error.strerror or 'connection failed'}")
    return _Session(conn, DIALER, target, identity, timeouts, handler).run_dialer(peer_id)


def probe_join(
    address: str,
    port: int,
    peer_id: str | None,
    identity: PairIdentity,
    *,
    timeouts: Timeouts = DEFAULT_TIMEOUTS,
) -> JoinProbe:
    target = str(_ipv4(address, "the address"))
    _port(port)
    if peer_id is not None:
        full_device_id(peer_id)
    try:
        conn = socket.create_connection((target, port), timeout=timeouts.connect_s)
    except OSError as error:
        raise PairingError(f"cannot connect to {target}:{port}: {error.strerror or 'connection failed'}") from error
    return _Session(conn, DIALER, target, identity, timeouts, None).run_probe(peer_id)


def _is_locked(limits: Limits, address: str) -> bool:
    try:
        limits.check_locked(address)
    except PairingError:
        return True
    return False


def _counts_as_failure(result: PairResult | None) -> bool:
    if result is None:
        return True
    return result.outcome == "failed" and not result.reason.startswith(_UNCOUNTED_REASONS)


def _serve_join(
    slot: threading.Lock,
    conn: socket.socket,
    address: str,
    identity: PairIdentity,
    handler: JoinHandler,
    limits: Limits,
    timeouts: Timeouts,
    on_result: Callable[[PairResult], None] | None,
) -> None:
    admitted = False

    def admit() -> bool:
        nonlocal admitted
        admitted = slot.acquire(blocking=False)
        return admitted

    session = _Session(conn, LISTENER, address, identity, timeouts, handler)
    result: PairResult | None
    try:
        result = session.run_listener(admit)
    except Exception as error:  # noqa: BLE001 - a save bug must not leave the slot stuck
        result = PairResult("failed", f"the join failed ({type(error).__name__})") if session.committed else None
    if admitted:
        slot.release()
        if _counts_as_failure(result):
            with contextlib.suppress(PairingError):
                limits.record_failure(address)
    if result is not None and on_result is not None:
        with contextlib.suppress(Exception):
            on_result(result)


def serve_joins(
    listener: socket.socket,
    identity: PairIdentity,
    handler: JoinHandler,
    limits: Limits,
    stop: threading.Event,
    *,
    timeouts: Timeouts = DEFAULT_TIMEOUTS,
    clock: Callable[[], float] = time.monotonic,
    on_result: Callable[[PairResult], None] | None = None,
    allow_source: Callable[[str], bool] = private_source,
) -> None:
    slot = threading.Lock()
    spacing = Spacing(timeouts.min_interval_s, clock)
    listener.settimeout(0.1)
    while not stop.is_set():
        try:
            conn, peer_address = listener.accept()
        except socket.timeout:
            continue
        except OSError:
            return
        host = str(peer_address[0])
        if not allow_source(host) or not spacing.allow(host) or _is_locked(limits, host):
            conn.close()
            continue
        conn.settimeout(None)
        args = (slot, conn, host, identity, handler, limits, timeouts, on_result)
        threading.Thread(target=_serve_join, args=args, daemon=True).start()

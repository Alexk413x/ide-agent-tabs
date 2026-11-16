from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import hmac
import ipaddress
import json
import os
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
from ..liveness import pid_alive
from . import tls, wire
from .client import DeadlineSocket, as_socket
from .identity import device_id as device_id_of
from .ingress import net_dir
from .private_files import check_private, ensure_private_dir, make_private_file

VERSION = 1
PAIRING_PORT = 47830
MAX_FRAME = 8192
INVITE_PREFIX = "atp1-invite:"
MAX_INVITATION_CHARS = 1024
MAX_INVITATION_AHEAD_S = 600
PAIRING_FILE = "pairing.json"
WINDOWS_PER_HOUR = 3
FAILURES_PER_HOUR = 3
HOUR_S = 3600
LOCK_S = 3600
OPEN_CAP_S = 1200
INCOMPLETE_TEXT = "Pairing may be incomplete on the other machine. Run `net status` there, or `net revoke <label>` here."

DIALER = "dialer"
LISTENER = "listener"

_DIGIT_LIMIT = 4_294 * 1_000_000
_IDLE_S = 3600.0
_MANUAL_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
_MANUAL_TOKEN = re.compile(r"[A-Z2-7]{6}")
_BASE64URL = re.compile(r"[A-Za-z0-9_-]+")
_INVITE_TOKEN = re.compile(r"[A-Za-z0-9_-]{22}")
_WIRE_TOKEN = re.compile(r"[A-Za-z0-9_-]{1,64}")
_ID_GROUP = re.compile(r"[A-Z2-7]{7}")
_NAME = re.compile(r"[A-Za-z0-9 ._-]{1,32}")
_DIGIT_RUN = re.compile(r"[0-9]{6}")
_IPV4 = re.compile(r"(?:0|[1-9][0-9]{0,2})(?:\.(?:0|[1-9][0-9]{0,2})){3}")
_HEX32 = re.compile(r"[0-9a-f]{64}")
_RFC1918 = tuple(ipaddress.IPv4Network(block) for block in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
_INVITATION_KEYS = frozenset({"v", "name", "address", "port", "id", "token", "exp"})

T = TypeVar("T")
Outcome = Literal["paired", "incomplete", "declined", "failed"]


class PairingError(Exception):
    pass


class LimitError(PairingError):
    pass


class Busy(LimitError):
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
    return H(b"atp1 commit", b"dialer", cert_d, nonce_d)


def sas_secret(cert_d: bytes, cert_l: bytes, nonce_d: bytes, nonce_l: bytes) -> bytes:
    return H(b"atp1 sas", cert_d, cert_l, nonce_d, nonce_l)


def consent_proof(role: str, secret: bytes) -> bytes:
    return H(b"atp1 consent", role.encode("ascii"), secret)


def digits(secret: bytes) -> str:
    current = secret
    counter = 0
    while True:
        for start in range(0, 32, 4):
            word = int.from_bytes(current[start : start + 4], "big")
            if word < _DIGIT_LIMIT:
                return f"{word % 1_000_000:06d}"
        counter += 1
        # The design hashes the original S with the counter; chaining from the previous hash gives other digits.
        current = H(b"atp1 sas2", secret, counter.to_bytes(4, "big"))


def valid_device_name(name: str) -> bool:
    return _NAME.fullmatch(name) is not None and _DIGIT_RUN.search(name) is None


def new_manual_token() -> str:
    return "".join(secrets.choice(_MANUAL_ALPHABET) for _ in range(6))


def parse_manual_token(text: str) -> str:
    token = text.strip().upper()
    if _MANUAL_TOKEN.fullmatch(token) is None:
        raise PairingError("a pairing token is 6 letters and digits 2 to 7")
    return token


def new_invitation_token() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(16)).rstrip(b"=").decode("ascii")


def tokens_equal(sent: str, expected: str) -> bool:
    return hmac.compare_digest(sent.encode("utf-8"), expected.encode("utf-8"))


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
class Invitation:
    name: str
    address: str
    port: int
    id: str
    token: str
    expires: int


def _invitation_token(value: object) -> str:
    if not isinstance(value, str) or _INVITE_TOKEN.fullmatch(value) is None:
        raise PairingError("the invitation token is malformed")
    try:
        raw = base64.urlsafe_b64decode(value + "==")
    except (binascii.Error, ValueError) as error:
        raise PairingError("the invitation token is malformed") from error
    if len(raw) != 16 or base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii") != value:
        raise PairingError("the invitation token is malformed")
    return value


def _fields(name: object, address: object, port: object, group: object, token: object, expires: object) -> Invitation:
    if not isinstance(name, str) or not valid_device_name(name):
        raise PairingError("the invitation's device name is not allowed")
    if not isinstance(group, str) or _ID_GROUP.fullmatch(group) is None:
        raise PairingError("the invitation's device id is malformed")
    if isinstance(expires, bool) or not isinstance(expires, int) or expires < 0:
        raise PairingError("the invitation's expiry is malformed")
    return Invitation(name, str(_ipv4(address, "the invitation's address")), _port(port), group, _invitation_token(token), expires)


def make_invitation(name: str, address: str, port: int, device_id: str, token: str, expires: int) -> str:
    group = device_id.split("-")[0]
    _fields(name, address, port, group, token, expires)
    body = {"v": VERSION, "name": name, "address": address, "port": port, "id": group, "token": token, "exp": expires}
    raw = json.dumps(body, separators=(",", ":")).encode("utf-8")
    return INVITE_PREFIX + base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def parse_invitation(text: str, now: float) -> Invitation:
    text = text.strip()
    if len(text) > MAX_INVITATION_CHARS:
        raise PairingError("the invitation is too long")
    if not text.startswith(INVITE_PREFIX):
        raise PairingError("this is not an Agent Tabs invitation")
    body = text[len(INVITE_PREFIX) :]
    if _BASE64URL.fullmatch(body) is None:
        raise PairingError("the invitation is not base64url")
    try:
        raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
    except (binascii.Error, ValueError) as error:
        raise PairingError("the invitation is not base64url") from error
    if base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii") != body:
        raise PairingError("the invitation is not canonical base64url")
    try:
        value = wire.parse_json(raw)
    except wire.WireError as error:
        raise PairingError(f"the invitation is not valid JSON: {error}") from error
    if not isinstance(value, dict) or set(value) != _INVITATION_KEYS:
        raise PairingError("the invitation has missing or unknown fields")
    version = value["v"]
    if isinstance(version, bool) or version != VERSION:
        raise PairingError("the invitation's version is not supported")
    invitation = _fields(value["name"], value["address"], value["port"], value["id"], value["token"], value["exp"])
    if invitation.expires <= now:
        raise PairingError("the invitation has expired")
    if invitation.expires > now + MAX_INVITATION_AHEAD_S:
        raise PairingError("the invitation expires too far ahead")
    return invitation


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
class Timeouts:
    first_frame_s: float = 5.0
    digits_s: float = 60.0
    typing_s: float = 180.0
    review_s: float = 180.0
    consent_s: float = 30.0
    connect_s: float = 5.0
    window_s: float = 300.0
    min_interval_s: float = 1.0


DEFAULT_TIMEOUTS = Timeouts()


class Prompter(Protocol):
    def show_code(self, peer: PeerInfo, code: str) -> None: ...

    # Returns None for "no" and for a timeout; the session tells them apart by the time left.
    def read_code(self, peer: PeerInfo, timeout_s: float) -> str | None: ...

    # True when the person approved the summary; the caller's prompter records the label, grants and listener choices.
    def review(self, peer: PeerInfo, timeout_s: float) -> bool: ...


@dataclass
class _State:
    windows: list[int]
    failures: list[int]
    locked_until: int
    open_pid: int
    open_until: int


def _whole(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2**53:
        raise ValueError("not a whole number")
    return value


def _whole_list(value: object) -> list[int]:
    if not isinstance(value, list):
        raise TypeError("not a list")
    return [_whole(item) for item in value]


class Limits:
    def __init__(
        self,
        home: str,
        clock: Callable[[], float] = time.time,
        pid: int | None = None,
        alive: Callable[[int], bool] = pid_alive,
    ) -> None:
        self._path = net_dir(home) / PAIRING_FILE
        self._clock = clock
        self._pid = os.getpid() if pid is None else pid
        self._alive = alive

    def _load(self) -> _State:
        try:
            text = read_text_if_exists(str(self._path))
        except OSError as error:
            raise PairingError(f"{self._path} cannot be read: {error}") from error
        if text is None:
            return _State([], [], 0, 0, 0)
        problems = check_private(self._path)
        if problems:
            raise PairingError(f"{self._path} is not private: {'; '.join(problems)}")
        try:
            value = json.loads(text)
            if not isinstance(value, dict) or set(value) != {"windows", "failures", "lockedUntil", "open"}:
                raise ValueError("unknown or missing fields")
            window = value["open"]
            if window is None:
                pid, until = 0, 0
            elif isinstance(window, dict) and set(window) == {"pid", "until"}:
                pid, until = _whole(window["pid"]), _whole(window["until"])
            else:
                raise ValueError("bad open window")
            return _State(_whole_list(value["windows"]), _whole_list(value["failures"]), _whole(value["lockedUntil"]), pid, until)
        except (ValueError, TypeError) as error:
            raise PairingError(f"{self._path} is not valid ({error}); delete it to reset the pairing limits") from error

    def _store(self, state: _State) -> None:
        window = {"pid": state.open_pid, "until": state.open_until} if state.open_pid else None
        body = {"windows": state.windows, "failures": state.failures, "lockedUntil": state.locked_until, "open": window}
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
            state.windows = [t for t in state.windows if now - t < HOUR_S]
            state.failures = [t for t in state.failures if now - t < HOUR_S]
            if state.open_pid and (now >= state.open_until or not self._alive(state.open_pid)):
                state.open_pid, state.open_until = 0, 0
            result = change(state, now)
            if write:
                self._store(state)
            return result

    @staticmethod
    def _raise_if_locked(state: _State, now: int) -> None:
        if now < state.locked_until:
            minutes = -(-(state.locked_until - now) // 60)
            raise Locked(f"pairing is locked for {minutes} more minutes after {FAILURES_PER_HOUR} failed attempts; `net trust` still works")

    @staticmethod
    def _raise_if_busy(state: _State, now: int) -> None:
        if state.open_pid:
            raise Busy("pairing busy: a pairing window is already open on this machine")
        if len(state.windows) >= WINDOWS_PER_HOUR:
            minutes = -(-(min(state.windows) + HOUR_S - now) // 60)
            raise Busy(f"pairing busy: {WINDOWS_PER_HOUR} windows opened in the last hour; try again in {minutes} minutes")

    def check_locked(self) -> None:
        self._update(lambda state, now: self._raise_if_locked(state, now), write=False)

    def check(self) -> None:
        def change(state: _State, now: int) -> None:
            self._raise_if_locked(state, now)
            self._raise_if_busy(state, now)

        self._update(change, write=False)

    def open_window(self) -> None:
        def change(state: _State, now: int) -> None:
            self._raise_if_locked(state, now)
            self._raise_if_busy(state, now)
            state.windows.append(now)
            state.open_pid, state.open_until = self._pid, now + OPEN_CAP_S

        self._update(change)

    def close_window(self) -> None:
        def change(state: _State, _now: int) -> None:
            if state.open_pid == self._pid:
                state.open_pid, state.open_until = 0, 0

        self._update(change)

    def record_failure(self) -> bool:
        def change(state: _State, now: int) -> bool:
            state.failures.append(now)
            if len(state.failures) >= FAILURES_PER_HOUR:
                state.locked_until = now + LOCK_S
                state.failures = []
            return now < state.locked_until

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
        prompter: Prompter,
        save: Callable[[PeerInfo], None],
        limits: Limits,
        timeouts: Timeouts,
    ) -> None:
        self.conn = conn
        self.role = role
        self.address = address
        self.identity = identity
        self.prompter = prompter
        self.save = save
        self.limits = limits
        self.timeouts = timeouts
        self.committed = False

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self.conn.close()

    def _send(self, frame: Mapping[str, object], deadline: float) -> None:
        payload = wire.encode(frame)
        if len(payload) > MAX_FRAME:
            raise PairingError("a frame is too large")
        wire.write_frame(as_socket(DeadlineSocket(self.conn, deadline, _IDLE_S)), payload)

    def _recv_raw(self, deadline: float) -> object:
        return wire.parse_json(wire.read_frame(as_socket(DeadlineSocket(self.conn, deadline, _IDLE_S)), max_size=MAX_FRAME))

    def _recv(self, kind: str, keys: tuple[str, ...], deadline: float) -> dict[str, Any]:
        return _expect(self._recv_raw(deadline), kind, keys)

    # Each side waits for the other's reply before it sends again, so data already waiting is trailing or out of order.
    def _no_pending(self) -> None:
        readable, _, _ = select.select([self.conn], [], [], 0)
        if readable:
            if not self.conn.recv(1, socket.MSG_PEEK):
                raise PairingError("the other machine ended the session")
            raise PairingError("the other machine sent data out of order")

    def _result(self, outcome: Outcome, reason: str, peer: PeerInfo | None = None, *, counted: bool = False) -> PairResult:
        locked = self.limits.record_failure() if counted else False
        return PairResult(outcome, reason, peer, locked)

    def run_dialer(self, token: str) -> PairResult:
        try:
            return self._dial(token)
        except _EXPECTED as error:
            return PairResult("failed", _describe(error))
        finally:
            self.close()

    def run_listener(self, token: str, admit: Callable[[], bool]) -> PairResult | None:
        try:
            return self._listen(token, admit)
        except _Dropped:
            return None
        except _EXPECTED as error:
            return PairResult("failed", _describe(error)) if self.committed else None
        finally:
            self.close()

    def _dial(self, token: str) -> PairResult:
        if _WIRE_TOKEN.fullmatch(token) is None:
            raise PairingError("the pairing token is malformed")
        deadline = time.monotonic() + self.timeouts.digits_s
        own = self.identity
        nonce_d = secrets.token_bytes(32)
        hello = {
            "t": "hello",
            "v": VERSION,
            "token": token,
            "name": own.name,
            "cert": base64.b64encode(own.der).decode("ascii"),
            "commit": commitment(own.der, nonce_d).hex(),
        }
        self._send(hello, deadline)
        reply = self._recv("hello", ("v", "name", "cert", "nonce"), deadline)
        name, der_l = _check_hello(reply, own.der)
        nonce_l = _hex32(reply["nonce"], "the listener's nonce")
        self._no_pending()
        self._send({"t": "reveal", "nonce": nonce_d.hex()}, deadline)
        self.committed = True
        peer = PeerInfo(name, self.address, device_id_of(der_l), der_l)
        return self._finish(peer, sas_secret(own.der, der_l, nonce_d, nonce_l))

    def _listen(self, token: str, admit: Callable[[], bool]) -> PairResult:
        start = time.monotonic()
        own = self.identity
        try:
            first = self._recv_raw(start + self.timeouts.first_frame_s)
        except _EXPECTED as error:
            raise _Dropped from error
        sent = first.get("token") if isinstance(first, dict) else None
        if not isinstance(sent, str) or not tokens_equal(sent, token) or not admit():
            raise _Dropped
        deadline = start + self.timeouts.digits_s
        frame = _expect(first, "hello", ("v", "token", "name", "cert", "commit"))
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
        return self._finish(peer, sas_secret(der_d, own.der, nonce_d, nonce_l))

    def _finish(self, peer: PeerInfo, secret: bytes) -> PairResult:
        code = digits(secret)
        self.prompter.show_code(peer, code)
        typing_deadline = time.monotonic() + self.timeouts.typing_s
        typed = self.prompter.read_code(peer, self.timeouts.typing_s)
        expired = time.monotonic() >= typing_deadline
        if expired:
            return PairResult("failed", "timed out waiting for the code", peer)
        if typed is None:
            return self._result("declined", "you answered no", peer, counted=True)
        if not hmac.compare_digest(typed.strip().encode("utf-8"), code.encode("ascii")):
            return self._result("failed", "the code you typed does not match", peer, counted=True)
        self._upgrade(peer, typing_deadline)
        return self._review(peer, secret)

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

    def _review(self, peer: PeerInfo, secret: bytes) -> PairResult:
        other = DIALER if self.role == LISTENER else LISTENER
        consent_deadline = time.monotonic() + self.timeouts.consent_s
        self._send({"t": "consent", "role": self.role, "c": consent_proof(self.role, secret).hex()}, consent_deadline)
        consent = self._recv("consent", ("role", "c"), consent_deadline)
        proof = _hex32(consent["c"], "the consent proof")
        if consent["role"] != other or not hmac.compare_digest(proof, consent_proof(other, secret)):
            raise PairingError("the other machine's consent is invalid")
        review_deadline = time.monotonic() + self.timeouts.review_s
        approved = self.prompter.review(peer, self.timeouts.review_s)
        if not approved:
            if time.monotonic() >= review_deadline:
                return PairResult("failed", "timed out at the review", peer)
            return self._result("declined", "you answered no at the review", peer, counted=True)
        try:
            self.save(peer)
        except Exception as error:  # noqa: BLE001 - the caller's save can fail in any way; nothing may be sent as saved
            return PairResult("failed", f"saving the trust entry failed: {error}", peer)
        try:
            self._send({"t": "saved"}, time.monotonic() + self.timeouts.consent_s)
            self._recv("saved", (), time.monotonic() + self.timeouts.review_s)
        except _EXPECTED:
            return PairResult("incomplete", INCOMPLETE_TEXT, peer)
        return PairResult("paired", "paired", peer)


class _Window:
    def __init__(self) -> None:
        self.slot = threading.Lock()
        self.done = threading.Event()
        self.result: PairResult | None = None


def _serve(
    window: _Window,
    conn: socket.socket,
    address: str,
    token: str,
    identity: PairIdentity,
    prompter: Prompter,
    save: Callable[[PeerInfo], None],
    limits: Limits,
    timeouts: Timeouts,
) -> None:
    admitted = False

    def admit() -> bool:
        nonlocal admitted
        admitted = window.slot.acquire(blocking=False)
        return admitted

    session = _Session(conn, LISTENER, address, identity, prompter, save, limits, timeouts)
    try:
        result = session.run_listener(token, admit)
    except Exception as error:  # noqa: BLE001 - a prompter bug must not leave the window stuck
        result = PairResult("failed", f"the pairing failed ({type(error).__name__})") if session.committed else None
    if result is not None:
        window.result = result
        window.done.set()
    if admitted:
        window.slot.release()


def listen_once(
    listener: socket.socket,
    token: str,
    identity: PairIdentity,
    prompter: Prompter,
    save: Callable[[PeerInfo], None],
    limits: Limits,
    clock: Callable[[], float] = time.monotonic,
    *,
    timeouts: Timeouts = DEFAULT_TIMEOUTS,
    stop: threading.Event | None = None,
) -> PairResult:
    if _WIRE_TOKEN.fullmatch(token) is None:
        raise PairingError("the pairing token is malformed")
    window = _Window()
    end = clock() + timeouts.window_s
    last = -timeouts.min_interval_s
    listener.settimeout(0.1)
    while not window.done.is_set() and (stop is None or not stop.is_set()):
        if clock() >= end and not window.slot.locked():
            break
        try:
            conn, peer_address = listener.accept()
        except socket.timeout:
            continue
        except OSError:
            break
        now = clock()
        if now - last < timeouts.min_interval_s:
            conn.close()
            continue
        last = now
        conn.settimeout(None)
        args = (window, conn, str(peer_address[0]), token, identity, prompter, save, limits, timeouts)
        threading.Thread(target=_serve, args=args, daemon=True).start()
    if window.result is not None:
        return window.result
    return PairResult("failed", "the pairing window closed before another machine paired")


def dial(
    address: str,
    port: int,
    token: str,
    identity: PairIdentity,
    prompter: Prompter,
    save: Callable[[PeerInfo], None],
    limits: Limits,
    *,
    timeouts: Timeouts = DEFAULT_TIMEOUTS,
) -> PairResult:
    target = str(_ipv4(address, "the address"))
    _port(port)
    limits.check_locked()
    try:
        conn = socket.create_connection((target, port), timeout=timeouts.connect_s)
    except OSError as error:
        return PairResult("failed", f"cannot connect to {target}:{port}: {error.strerror or 'connection failed'}")
    return _Session(conn, DIALER, target, identity, prompter, save, limits, timeouts).run_dialer(token)

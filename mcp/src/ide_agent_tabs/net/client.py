from __future__ import annotations

import re
import secrets
import socket
import ssl
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from . import tls, wire
from .certgen import CERT_FILE, KEY_FILE
from .identity import der_from_pem, device_id, parse_device_id
from .ingress import net_dir
from .policy import DEFAULT_PORT, PolicyError, grant_for, load_policy
from .secrets import SecretsError, load_passphrase

IDLE_S = 30.0
CONNECT_S = 5.0
_PEER_ID = re.compile(r"[A-Za-z0-9-]{1,96}")


class NetClientError(Exception):
    pass


@dataclass(frozen=True)
class LocalIdentity:
    cert: Path
    key: Path
    passphrase: bytes


def identity_dir(home: str) -> Path:
    return net_dir(home) / "identity"


def peers_dir(home: str) -> Path:
    return net_dir(home) / "peers"


def peer_cert_path(home: str, peer_id: str) -> Path:
    return peers_dir(home) / f"{peer_id}.pem"


def read_peer_pem(home: str, peer_id: str) -> str | None:
    if _PEER_ID.fullmatch(peer_id) is None:
        return None
    try:
        text = peer_cert_path(home, peer_id).read_text(encoding="ascii")
        return text if device_id(der_from_pem(text)) == peer_id else None
    except (OSError, ValueError):
        return None


def local_device_id(home: str) -> str | None:
    try:
        return device_id(der_from_pem((identity_dir(home) / CERT_FILE).read_text(encoding="ascii")))
    except (OSError, ValueError):
        return None


def load_identity(home: str) -> LocalIdentity:
    folder = identity_dir(home)
    cert, key = folder / CERT_FILE, folder / KEY_FILE
    if not cert.is_file() or not key.is_file():
        raise NetClientError("this machine has no network identity; run agent-tabs net lan setup")
    try:
        passphrase = load_passphrase(folder)
    except (OSError, SecretsError) as error:
        raise NetClientError(f"the identity passphrase cannot be read: {error}") from error
    return LocalIdentity(cert, key, passphrase)


class DeadlineSocket:
    def __init__(self, sock: socket.socket, deadline: float, idle_s: float = IDLE_S) -> None:
        self._sock = sock
        self._deadline = deadline
        self._idle_s = idle_s

    def _arm(self) -> None:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise socket.timeout("the exchange took too long")
        self._sock.settimeout(min(self._idle_s, remaining))

    def recv(self, count: int) -> bytes:
        self._arm()
        return self._sock.recv(count)

    def sendall(self, data: bytes) -> None:
        self._arm()
        self._sock.sendall(data)


def as_socket(bounded: DeadlineSocket) -> socket.socket:
    return cast(socket.socket, bounded)


def _check_dial(home: str, address: str, port: int) -> None:
    from .dialguard import GUARD
    from .pairing import PairingError

    try:
        GUARD.check(home, address, port)
    except PairingError as error:
        raise NetClientError(f"the connection to {address} is refused: {error}") from error


def _reason(error: BaseException) -> str:
    if isinstance(error, ssl.SSLCertVerificationError):
        return "the peer's certificate does not match its pinned certificate"
    if isinstance(error, ssl.SSLError):
        return "TLS failed; the peer may not trust this machine's certificate"
    if isinstance(error, socket.timeout):
        return "timed out"
    if isinstance(error, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
        return "the peer closed the connection; it may not trust this machine"
    if isinstance(error, wire.WireError):
        return f"the peer sent an invalid frame: {error}"
    return f"cannot reach the peer: {error}"


def _exchange(tls_sock: ssl.SSLSocket, deadline: float, expected: bytes, frame: bytes, request_id: str) -> dict[str, Any]:
    tls.check_not_reused(tls_sock)
    if tls.peer_digest(tls_sock) != expected:
        raise NetClientError("the peer's certificate does not match its pinned certificate")
    sock = as_socket(DeadlineSocket(cast(socket.socket, tls_sock), deadline))
    wire.write_frame(sock, wire.hello_frame())
    wire.check_hello(wire.parse_json(wire.read_frame(sock)))
    wire.write_frame(sock, frame)
    reply = wire.parse_json(wire.read_frame(sock))
    parsed = wire.parse_reply(reply)
    if parsed.re != request_id:
        raise NetClientError("the reply answers a different request")
    return cast("dict[str, Any]", reply)


def send_request(
    home: str,
    peer_device_id: str,
    op: str,
    body: Mapping[str, Any],
    *,
    timeout: float = 30,
    port: int = DEFAULT_PORT,
    request_id: str | None = None,
    address: str | None = None,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    try:
        peer = grant_for(load_policy(home), peer_device_id)
        expected = parse_device_id(peer_device_id)
    except (PolicyError, ValueError) as error:
        raise NetClientError(f"the peer cannot be looked up: {error}") from error
    if peer is None:
        raise NetClientError("the peer is not trusted on this machine")
    pem = read_peer_pem(home, peer_device_id)
    if pem is None:
        raise NetClientError("the stored certificate of the peer is missing or does not match its id")
    identity = load_identity(home)
    rid = request_id or secrets.token_hex(16)
    payload = {"id": rid, "op": op, "body": dict(body)}
    try:
        wire.parse_request(payload)
        frame = wire.encode(payload)
        if len(frame) > wire.MAX_FRAME:
            raise wire.WireError("the request is too large")
    except wire.WireError as error:
        raise NetClientError(f"the request is invalid: {error}") from error
    target = address or peer.address
    _check_dial(home, target, port)
    try:
        context = tls.client_context(identity.cert, identity.key, lambda: identity.passphrase, [pem])
        with socket.create_connection((target, port), timeout=max(0.1, min(CONNECT_S, deadline - time.monotonic()))) as raw:
            raw.settimeout(max(0.1, deadline - time.monotonic()))
            with context.wrap_socket(raw, server_hostname=None) as tls_sock:
                return _exchange(tls_sock, deadline, expected, frame, rid)
    except NetClientError:
        raise
    except (OSError, wire.WireError, ValueError) as error:
        raise NetClientError(_reason(error)) from error


def verify_pinned(home: str, address: str, device_ids: Sequence[str], *, port: int = DEFAULT_PORT, timeout: float = CONNECT_S) -> str:
    deadline = time.monotonic() + timeout
    pinned: dict[bytes, tuple[str, str]] = {}
    for candidate in device_ids:
        pem = read_peer_pem(home, candidate)
        if pem is None:
            continue
        try:
            pinned[parse_device_id(candidate)] = (candidate, pem)
        except ValueError:
            continue
    if not pinned:
        raise NetClientError("no stored certificate matches a device to check")
    identity = load_identity(home)
    _check_dial(home, address, port)
    try:
        context = tls.client_context(identity.cert, identity.key, lambda: identity.passphrase, [pem for _, pem in pinned.values()])
        with socket.create_connection((address, port), timeout=max(0.1, min(CONNECT_S, deadline - time.monotonic()))) as raw:
            raw.settimeout(max(0.1, deadline - time.monotonic()))
            with context.wrap_socket(raw, server_hostname=None) as tls_sock:
                tls.check_not_reused(tls_sock)
                found = pinned.get(tls.peer_digest(tls_sock))
                if found is None:
                    raise NetClientError("the peer's certificate does not match its pinned certificate")
                sock = as_socket(DeadlineSocket(cast(socket.socket, tls_sock), deadline))
                wire.write_frame(sock, wire.hello_frame())
                wire.check_hello(wire.parse_json(wire.read_frame(sock)))
                return found[0]
    except NetClientError:
        raise
    except (OSError, wire.WireError, ValueError) as error:
        raise NetClientError(_reason(error)) from error

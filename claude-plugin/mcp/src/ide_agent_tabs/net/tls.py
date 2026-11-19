from __future__ import annotations

import hashlib
import hmac
import ssl
from collections.abc import Callable, Sequence
from pathlib import Path

_PARTIAL_CHAIN = 0x80000


def runtime_problem() -> str | None:
    if not ssl.HAS_TLSv1_3:
        return "this Python's ssl module has no TLS 1.3 support"
    if ssl.OPENSSL_VERSION_INFO < (1, 1, 1):
        return f"this Python links {ssl.OPENSSL_VERSION}; LAN mode needs OpenSSL 1.1.1 or later"
    return None


def _configure(context: ssl.SSLContext, cert: Path, key: Path, password: Callable[[], bytes], trusted: Sequence[str]) -> ssl.SSLContext:
    pems = [pem for pem in trusted if pem.strip()]
    if not pems:
        raise ValueError("at least one trusted peer certificate is required")
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_verify_locations(cadata="\n".join(pem.strip() for pem in pems))
    # Every device certificate has the subject CN=agent-tabs. With two pinned certificates OpenSSL finds the issuer
    # by subject, picks the wrong one and rejects a valid peer. A partial chain trusts the exact certificate in the
    # store instead. ssl.VERIFY_X509_PARTIAL_CHAIN exists from Python 3.10.
    context.verify_flags |= getattr(ssl, "VERIFY_X509_PARTIAL_CHAIN", _PARTIAL_CHAIN)
    context.load_cert_chain(str(cert), str(key), password=password)
    context.options |= ssl.OP_NO_TICKET
    return context


def server_context(cert: Path, key: Path, password: Callable[[], bytes], trusted: Sequence[str]) -> ssl.SSLContext:
    context = _configure(ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER), cert, key, password, trusted)
    # OP_NO_TICKET alone still issues TLS 1.3 tickets and lets a client resume; num_tickets = 0 stops both.
    if hasattr(context, "num_tickets"):
        context.num_tickets = 0
    return context


def client_context(cert: Path, key: Path, password: Callable[[], bytes], trusted: Sequence[str]) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    # A peer is named by its pinned certificate, not a DNS name; certificate verification stays on.
    context.check_hostname = False
    return _configure(context, cert, key, password, trusted)


def peer_digest(sock: ssl.SSLSocket) -> bytes:
    der = sock.getpeercert(binary_form=True)
    if not der:
        raise ssl.SSLError("the peer presented no certificate")
    return hashlib.sha256(der).digest()


def check_not_reused(sock: ssl.SSLSocket) -> None:
    if sock.session_reused:
        raise ssl.SSLError("the connection resumed an earlier TLS session")


def check_session(sock: ssl.SSLSocket, expected_der: bytes) -> None:
    der = sock.getpeercert(binary_form=True)
    if not der:
        raise ssl.SSLError("the peer presented no certificate")
    if not hmac.compare_digest(der, expected_der):
        raise ssl.SSLError("the peer's certificate is not the one it announced")
    if sock.version() != "TLSv1.3":
        raise ssl.SSLError("the connection is not TLS 1.3")
    check_not_reused(sock)

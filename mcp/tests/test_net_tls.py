from __future__ import annotations

import hashlib
import os
import secrets
import socket
import ssl
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any, NamedTuple
from unittest import mock

from ide_agent_tabs.net import tls

OPENSSL = r"C:\Program Files\Git\usr\bin\openssl.exe"
REPLY = b"reply:hello"


class Identity(NamedTuple):
    cert: Path
    key: Path
    password: bytes
    pem: str
    digest: bytes


def _openssl(args: list[str], passphrase: bytes, cwd: Path) -> None:
    env = {k: os.environ[k] for k in ("SYSTEMROOT", "SYSTEMDRIVE") if k in os.environ}
    env["PATH"] = os.path.dirname(OPENSSL)
    done = subprocess.run([OPENSSL, *args], input=passphrase + b"\n", capture_output=True, env=env, timeout=60, cwd=str(cwd), check=False)
    if done.returncode:
        raise RuntimeError(f"openssl {args[0]} failed: {done.stderr!r}")


def make_identity(folder: Path, name: str) -> Identity:
    key, cert = folder / f"{name}.key.pem", folder / f"{name}.cert.pem"
    password = secrets.token_urlsafe(24).encode()
    _openssl(
        ["genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:P-256", "-aes-256-cbc", "-pass", "stdin", "-out", str(key)],
        password,
        folder,
    )
    _openssl(
        [
            "req",
            "-x509",
            "-key",
            str(key),
            "-passin",
            "stdin",
            "-sha256",
            "-days",
            "30",
            "-subj",
            f"/CN=agent-tabs-{name}",
            "-addext",
            "basicConstraints=critical,CA:FALSE",
            "-addext",
            "keyUsage=critical,digitalSignature",
            "-addext",
            "extendedKeyUsage=serverAuth,clientAuth",
            "-out",
            str(cert),
        ],
        password,
        folder,
    )
    pem = cert.read_text()
    return Identity(cert, key, password, pem, hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).digest())


class Seen(NamedTuple):
    digest: bytes
    reused: bool
    version: str | None


class Listener(threading.Thread):
    def __init__(self, context: ssl.SSLContext, connections: int = 1) -> None:
        super().__init__(daemon=True)
        self.context = context
        self.connections = connections
        self.seen: list[Seen] = []
        self.errors: list[BaseException] = []
        self.socket = socket.socket()
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(4)
        self.socket.settimeout(8)
        self.port: int = self.socket.getsockname()[1]

    def run(self) -> None:
        try:
            for _ in range(self.connections):
                raw, _address = self.socket.accept()
                raw.settimeout(5)
                try:
                    with self.context.wrap_socket(raw, server_side=True) as conn:
                        self.seen.append(Seen(tls.peer_digest(conn), bool(conn.session_reused), conn.version()))
                        tls.check_not_reused(conn)
                        conn.sendall(b"reply:" + conn.recv(100))
                except (OSError, ssl.SSLError) as error:
                    self.errors.append(error)
                finally:
                    raw.close()
        except OSError as error:
            self.errors.append(error)
        finally:
            self.socket.close()


class Dialed(NamedTuple):
    digest: bytes
    reply: bytes
    reused: bool
    version: str | None
    session: Any


def dial(context: ssl.SSLContext, port: int, session: ssl.SSLSession | None = None) -> Dialed:
    with (
        socket.create_connection(("127.0.0.1", port), timeout=5) as raw,
        context.wrap_socket(raw, server_hostname=None, session=session) as conn,
    ):
        digest = tls.peer_digest(conn)
        conn.sendall(b"hello")
        reply = conn.recv(100)
        if not reply:
            raise ConnectionError("the peer closed without a reply")
        return Dialed(digest, reply, bool(conn.session_reused), conn.version(), conn.session)


def bare_client(trusted: str, *, identity: Identity | None = None, max_tls: ssl.TLSVersion | None = None) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_REQUIRED
    if max_tls is not None:
        context.minimum_version = ssl.TLSVersion.MINIMUM_SUPPORTED
        context.maximum_version = max_tls
    else:
        context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.load_verify_locations(cadata=trusted)
    if identity is not None:
        context.load_cert_chain(str(identity.cert), str(identity.key), password=lambda: identity.password)
    return context


@unittest.skipUnless(os.path.exists(OPENSSL) and tls.runtime_problem() is None, "needs Git's openssl and a TLS 1.3 Python")
class TlsTest(unittest.TestCase):
    a: Identity
    b: Identity
    c: Identity

    @classmethod
    def setUpClass(cls) -> None:
        folder = tempfile.TemporaryDirectory(prefix="iat-tls-")
        cls.addClassCleanup(folder.cleanup)
        path = Path(folder.name)
        cls.a, cls.b, cls.c = (make_identity(path, name) for name in ("a", "b", "c"))

    def server(self, identity: Identity, trusted: list[str]) -> ssl.SSLContext:
        return tls.server_context(identity.cert, identity.key, lambda: identity.password, trusted)

    def client(self, identity: Identity, trusted: list[str]) -> ssl.SSLContext:
        return tls.client_context(identity.cert, identity.key, lambda: identity.password, trusted)

    def listen(self, context: ssl.SSLContext, connections: int = 1) -> Listener:
        listener = Listener(context, connections)
        listener.start()
        self.addCleanup(listener.join, 10)
        return listener

    def test_mutual_success_and_pin_digest(self) -> None:
        listener = self.listen(self.server(self.a, [self.b.pem]))
        result = dial(self.client(self.b, [self.a.pem]), listener.port)
        listener.join(10)
        self.assertEqual(result.reply, REPLY)
        self.assertEqual(result.version, "TLSv1.3")
        self.assertEqual(result.digest, self.a.digest)
        self.assertEqual(listener.errors, [])
        self.assertEqual(listener.seen, [Seen(self.b.digest, False, "TLSv1.3")])

    def test_client_without_a_certificate_is_refused(self) -> None:
        listener = self.listen(self.server(self.a, [self.b.pem]))
        with self.assertRaises(OSError):
            dial(bare_client(self.a.pem), listener.port)
        listener.join(10)
        self.assertEqual(listener.seen, [])
        self.assertEqual(len(listener.errors), 1)

    def test_client_with_an_unknown_certificate_is_refused(self) -> None:
        listener = self.listen(self.server(self.a, [self.b.pem]))
        with self.assertRaises(OSError):
            dial(self.client(self.c, [self.a.pem]), listener.port)
        listener.join(10)
        self.assertEqual(listener.seen, [])
        self.assertEqual(len(listener.errors), 1)

    def test_wrong_server_certificate_is_refused_by_the_client(self) -> None:
        listener = self.listen(self.server(self.c, [self.b.pem]))
        with self.assertRaises(ssl.SSLError):
            dial(self.client(self.b, [self.a.pem]), listener.port)
        listener.join(10)
        self.assertEqual(listener.seen, [])

    def test_a_trusted_list_may_hold_several_peers(self) -> None:
        listener = self.listen(self.server(self.a, [self.c.pem, self.b.pem]), connections=2)
        self.assertEqual(dial(self.client(self.b, [self.a.pem]), listener.port).reply, REPLY)
        self.assertEqual(dial(self.client(self.c, [self.a.pem]), listener.port).reply, REPLY)
        listener.join(10)
        self.assertEqual([seen.digest for seen in listener.seen], [self.b.digest, self.c.digest])

    def test_no_ticket_and_no_reuse_on_a_second_connection(self) -> None:
        listener = self.listen(self.server(self.a, [self.b.pem]), connections=2)
        client = self.client(self.b, [self.a.pem])
        first = dial(client, listener.port)
        self.assertFalse(first.session.has_ticket)
        second = dial(client, listener.port, session=first.session)
        listener.join(10)
        self.assertFalse(second.reused)
        self.assertEqual([seen.reused for seen in listener.seen], [False, False])
        self.assertEqual(listener.errors, [])

    def test_tls_1_2_only_client_is_refused(self) -> None:
        listener = self.listen(self.server(self.a, [self.b.pem]))
        with self.assertRaises(ssl.SSLError):
            dial(bare_client(self.a.pem, identity=self.b, max_tls=ssl.TLSVersion.TLSv1_2), listener.port)
        listener.join(10)
        self.assertEqual(listener.seen, [])
        self.assertEqual(len(listener.errors), 1)

    def test_empty_trusted_list_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.server(self.a, [])
        with self.assertRaises(ValueError):
            self.client(self.a, [])
        with self.assertRaises(ValueError):
            self.server(self.a, ["", "  \n"])

    def test_wrong_passphrase_raises(self) -> None:
        with self.assertRaises(ssl.SSLError):
            tls.server_context(self.a.cert, self.a.key, lambda: b"wrong", [self.b.pem])
        with self.assertRaises(ssl.SSLError):
            tls.client_context(self.a.cert, self.a.key, lambda: b"wrong", [self.b.pem])

    def test_context_settings(self) -> None:
        for context in (self.server(self.a, [self.b.pem]), self.client(self.a, [self.b.pem])):
            self.assertEqual(context.minimum_version, ssl.TLSVersion.TLSv1_3)
            self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
            self.assertTrue(context.options & ssl.OP_NO_TICKET)
            self.assertEqual(context.cert_store_stats()["x509"], 1)
        self.assertFalse(self.client(self.a, [self.b.pem]).check_hostname)
        server = self.server(self.a, [self.b.pem])
        if hasattr(server, "num_tickets"):
            self.assertEqual(server.num_tickets, 0)

    def test_context_without_num_tickets_still_builds(self) -> None:
        real = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        if not hasattr(real, "num_tickets"):
            self.assertEqual(self.server(self.a, [self.b.pem]).verify_mode, ssl.CERT_REQUIRED)
        else:
            self.assertTrue(hasattr(self.server(self.a, [self.b.pem]), "num_tickets"))


class HelperTest(unittest.TestCase):
    def test_runtime_problem_is_none_on_a_capable_python(self) -> None:
        if ssl.HAS_TLSv1_3 and ssl.OPENSSL_VERSION_INFO >= (1, 1, 1):
            self.assertIsNone(tls.runtime_problem())

    def test_runtime_problem_without_tls_1_3(self) -> None:
        with mock.patch.object(ssl, "HAS_TLSv1_3", False):
            reason = tls.runtime_problem()
        assert reason is not None
        self.assertIn("TLS 1.3", reason)
        self.assertNotIn("\n", reason)

    def test_runtime_problem_with_old_openssl(self) -> None:
        old = mock.patch.object(ssl, "OPENSSL_VERSION_INFO", (1, 0, 2, 0, 0))
        with mock.patch.object(ssl, "HAS_TLSv1_3", True), old, mock.patch.object(ssl, "OPENSSL_VERSION", "OpenSSL 1.0.2u"):
            reason = tls.runtime_problem()
        assert reason is not None
        self.assertIn("OpenSSL 1.0.2u", reason)

    def test_peer_digest_hashes_the_der_certificate(self) -> None:
        sock = mock.Mock()
        sock.getpeercert.return_value = b"der-bytes"
        self.assertEqual(tls.peer_digest(sock), hashlib.sha256(b"der-bytes").digest())
        sock.getpeercert.assert_called_once_with(binary_form=True)

    def test_peer_digest_refuses_a_missing_certificate(self) -> None:
        for missing in (None, b""):
            sock = mock.Mock()
            sock.getpeercert.return_value = missing
            with self.assertRaises(ssl.SSLError):
                tls.peer_digest(sock)

    def test_check_session_accepts_the_announced_certificate_on_tls_1_3(self) -> None:
        sock = mock.Mock(session_reused=False)
        sock.getpeercert.return_value = b"der-bytes"
        sock.version.return_value = "TLSv1.3"
        tls.check_session(sock, b"der-bytes")

    def test_check_session_refuses_a_missing_or_different_certificate(self) -> None:
        for presented in (None, b"", b"other-bytes"):
            sock = mock.Mock(session_reused=False)
            sock.getpeercert.return_value = presented
            sock.version.return_value = "TLSv1.3"
            with self.assertRaises(ssl.SSLError):
                tls.check_session(sock, b"der-bytes")

    def test_check_session_refuses_an_older_version_and_a_resumed_session(self) -> None:
        older = mock.Mock(session_reused=False)
        older.getpeercert.return_value = b"der-bytes"
        older.version.return_value = "TLSv1.2"
        with self.assertRaises(ssl.SSLError):
            tls.check_session(older, b"der-bytes")
        resumed = mock.Mock(session_reused=True)
        resumed.getpeercert.return_value = b"der-bytes"
        resumed.version.return_value = "TLSv1.3"
        with self.assertRaises(ssl.SSLError):
            tls.check_session(resumed, b"der-bytes")

    def test_check_not_reused(self) -> None:
        tls.check_not_reused(mock.Mock(session_reused=False))
        with self.assertRaises(ssl.SSLError):
            tls.check_not_reused(mock.Mock(session_reused=True))


if __name__ == "__main__":
    unittest.main()

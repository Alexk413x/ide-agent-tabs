from __future__ import annotations

import hashlib
import os
import socket
import ssl
import threading
import unittest
from typing import Callable

from ide_agent_tabs.net import tls, wire
from ide_agent_tabs.net.client import NetClientError, load_identity, local_device_id, peer_cert_path, read_peer_pem, send_request
from ide_agent_tabs.net.identity import format_digest
from net_support import HAS_OPENSSL, LOOPBACK, cert_of, create_home, device_of, make_root, reset_peers, trust_peer


class FakePeer(threading.Thread):
    def __init__(self, home: str, trusted: str, serve: Callable[[ssl.SSLSocket], None]) -> None:
        super().__init__(daemon=True)
        identity = load_identity(home)
        self.context = tls.server_context(identity.cert, identity.key, lambda: identity.passphrase, [trusted])
        self.serve = serve
        self.socket = socket.socket()
        self.socket.bind((LOOPBACK, 0))
        self.socket.listen(2)
        self.socket.settimeout(10)
        self.port: int = self.socket.getsockname()[1]
        self.errors: list[BaseException] = []

    def run(self) -> None:
        try:
            raw, _ = self.socket.accept()
            with raw, self.context.wrap_socket(raw, server_side=True) as conn:
                conn.settimeout(10)
                self.serve(conn)
        except (OSError, wire.WireError) as error:
            self.errors.append(error)
        finally:
            self.socket.close()


@unittest.skipUnless(HAS_OPENSSL and tls.runtime_problem() is None, "needs an openssl and a TLS 1.3 Python")
class ClientTest(unittest.TestCase):
    a: str
    b: str
    c: str

    @classmethod
    def setUpClass(cls) -> None:
        root = make_root(cls)
        cls.a, cls.b, cls.c = (create_home(root, name) for name in ("a", "b", "c"))

    def setUp(self) -> None:
        for home in (self.a, self.b, self.c):
            reset_peers(home)
        self.a_id = device_of(self.a)
        trust_peer(self.b, self.a, "desktop")

    def peer(self, serve: Callable[[ssl.SSLSocket], None]) -> FakePeer:
        peer = FakePeer(self.a, cert_of(self.b).read_text(encoding="ascii"), serve)
        peer.start()
        self.addCleanup(peer.join, 10)
        return peer

    def send(self, port: int, op: str = "inventory", body: dict[str, object] | None = None, timeout: float = 30) -> dict[str, object]:
        return send_request(self.b, self.a_id, op, body or {}, port=port, timeout=timeout)

    def test_a_reply_is_returned_and_the_request_carries_a_fresh_id(self) -> None:
        requests: list[wire.Request] = []

        def serve(conn: ssl.SSLSocket) -> None:
            wire.check_hello(wire.parse_json(wire.read_frame(conn)))
            wire.write_frame(conn, wire.hello_frame())
            request = wire.parse_request(wire.parse_json(wire.read_frame(conn)))
            requests.append(request)
            wire.write_frame(conn, wire.encode(wire.ok_reply(request.id, {"sessions": []})))

        for _ in range(2):
            peer = self.peer(serve)
            reply = self.send(peer.port)
            peer.join(10)
            self.assertEqual(reply, {"re": requests[-1].id, "ok": True, "body": {"sessions": []}})
        self.assertNotEqual(requests[0].id, requests[1].id)
        self.assertEqual(requests[0].op, "inventory")

    def test_a_given_request_id_is_used(self) -> None:
        seen: list[str] = []

        def serve(conn: ssl.SSLSocket) -> None:
            wire.read_frame(conn)
            wire.write_frame(conn, wire.hello_frame())
            request = wire.parse_request(wire.parse_json(wire.read_frame(conn)))
            seen.append(request.id)
            wire.write_frame(conn, wire.encode(wire.refused_reply(request.id)))

        peer = self.peer(serve)
        reply = send_request(self.b, self.a_id, "inventory", {}, port=peer.port, request_id="retry-1")
        self.assertEqual(reply, {"re": "retry-1", "ok": False, "refused": True})
        self.assertEqual(seen, ["retry-1"])

    def test_a_reply_that_answers_another_request_is_an_error(self) -> None:
        def serve(conn: ssl.SSLSocket) -> None:
            wire.read_frame(conn)
            wire.write_frame(conn, wire.hello_frame())
            wire.read_frame(conn)
            wire.write_frame(conn, wire.encode(wire.ok_reply("other", {})))

        with self.assertRaisesRegex(NetClientError, "different request"):
            self.send(self.peer(serve).port)

    def test_a_bad_hello_is_an_error(self) -> None:
        def serve(conn: ssl.SSLSocket) -> None:
            wire.read_frame(conn)
            wire.write_frame(conn, wire.encode({"v": 9, "hello": wire.HELLO}))

        with self.assertRaisesRegex(NetClientError, "invalid frame"):
            self.send(self.peer(serve).port)

    def test_an_oversize_reply_is_an_error(self) -> None:
        def serve(conn: ssl.SSLSocket) -> None:
            wire.read_frame(conn)
            wire.write_frame(conn, wire.hello_frame())
            wire.read_frame(conn)
            conn.sendall((wire.MAX_FRAME + 1).to_bytes(4, "big"))

        with self.assertRaisesRegex(NetClientError, "too large"):
            self.send(self.peer(serve).port)

    def test_a_silent_peer_times_out(self) -> None:
        release = threading.Event()
        self.addCleanup(release.set)

        def serve(conn: ssl.SSLSocket) -> None:
            release.wait(10)

        with self.assertRaisesRegex(NetClientError, "timed out"):
            self.send(self.peer(serve).port, timeout=1)

    def test_a_peer_that_presents_another_certificate_is_refused(self) -> None:
        trust_peer(self.b, self.c, "other")
        wrong = FakePeer(self.c, cert_of(self.b).read_text(encoding="ascii"), lambda conn: None)
        wrong.start()
        self.addCleanup(wrong.join, 10)
        with self.assertRaisesRegex(NetClientError, "pinned"):
            self.send(wrong.port)

    def test_a_missing_listener_is_an_error(self) -> None:
        with socket.socket() as probe:
            probe.bind((LOOPBACK, 0))
            port = probe.getsockname()[1]
        with self.assertRaisesRegex(NetClientError, "cannot reach"):
            self.send(port, timeout=3)

    def test_an_unknown_peer_is_an_error(self) -> None:
        with self.assertRaisesRegex(NetClientError, "not trusted"):
            send_request(self.b, device_of(self.c), "inventory", {}, port=1)

    def test_a_malformed_peer_id_is_an_error(self) -> None:
        with self.assertRaisesRegex(NetClientError, "cannot be looked up"):
            send_request(self.b, "short", "inventory", {}, port=1)

    def test_a_missing_or_mismatched_stored_certificate_is_an_error(self) -> None:
        path = peer_cert_path(self.b, self.a_id)
        path.write_text(cert_of(self.c).read_text(encoding="ascii"), encoding="ascii")
        self.assertIsNone(read_peer_pem(self.b, self.a_id))
        with self.assertRaisesRegex(NetClientError, "stored certificate"):
            self.send(1)
        os.remove(path)
        with self.assertRaisesRegex(NetClientError, "stored certificate"):
            self.send(1)

    def test_an_invalid_or_oversize_request_is_refused_before_dialing(self) -> None:
        with self.assertRaisesRegex(NetClientError, "invalid"):
            self.send(1, op="bogus")
        with self.assertRaisesRegex(NetClientError, "invalid"):
            self.send(1, op="send", body={"to": "s-1", "text": "x" * (wire.MAX_FRAME)})
        with self.assertRaisesRegex(NetClientError, "invalid"):
            self.send(1, op="inventory", body={"extra": 1})

    def test_a_machine_without_an_identity_is_an_error(self) -> None:
        empty = make_root(self, "iat-net-empty-")
        trust_peer(empty, self.a, "desktop")
        self.assertIsNone(local_device_id(empty))
        with self.assertRaisesRegex(NetClientError, "no network identity"):
            send_request(empty, self.a_id, "inventory", {}, port=1)

    def test_the_device_id_helper_matches_the_certificate(self) -> None:
        der = ssl.PEM_cert_to_DER_cert(cert_of(self.a).read_text(encoding="ascii"))
        self.assertEqual(self.a_id, format_digest(hashlib.sha256(der).digest()))


if __name__ == "__main__":
    unittest.main()

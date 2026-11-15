from __future__ import annotations

import os
import socket
import ssl
import subprocess
import sys
import unittest
from typing import Any
from unittest import mock

from ide_agent_tabs.net import listener, tls, wire
from ide_agent_tabs.net.client import NetClientError, load_identity, read_peer_pem, send_request
from ide_agent_tabs.net.listener import IngressForwarder, NetworkWatch, Options, read_listener, run_listener
from ide_agent_tabs.net.policy import NetworkConfig
from net_support import (
    HAS_OPENSSL,
    LOOPBACK,
    FakeIngress,
    RunningListener,
    create_home,
    device_of,
    forget_peer,
    free_port,
    make_root,
    reset_peers,
    trust_peer,
    wait_until,
)

INVENTORY = {"id": "r-1", "op": "inventory", "body": {}}


def raw_connect(home: str, peer_id: str, port: int) -> ssl.SSLSocket:
    identity = load_identity(home)
    pem = read_peer_pem(home, peer_id)
    assert pem is not None
    context = tls.client_context(identity.cert, identity.key, lambda: identity.passphrase, [pem])
    return context.wrap_socket(socket.create_connection((LOOPBACK, port), timeout=5), server_hostname=None)


def read_or_none(sock: ssl.SSLSocket) -> bytes | None:
    sock.settimeout(5)
    try:
        return wire.read_frame(sock)
    except (OSError, wire.WireError):
        return None


@unittest.skipUnless(HAS_OPENSSL and tls.runtime_problem() is None, "needs an openssl and a TLS 1.3 Python")
class ListenerTest(unittest.TestCase):
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
        self.a_id, self.b_id, self.c_id = device_of(self.a), device_of(self.b), device_of(self.c)
        self.ingress = FakeIngress(self.a)
        self.addCleanup(self.ingress.close)

    def start(self, **kwargs: Any) -> RunningListener:
        running = RunningListener(self.a, self.ingress.port, **kwargs).start()
        self.addCleanup(running.stop)
        return running

    def send(self, running: RunningListener, home: str | None = None, peer: str | None = None, timeout: float = 10) -> dict[str, Any]:
        return send_request(home or self.b, peer or self.a_id, "inventory", {}, timeout=timeout, port=running.port)

    def pair(self, source: str | None = None) -> None:
        trust_peer(self.a, self.b, "laptop", source or LOOPBACK)
        trust_peer(self.b, self.a, "desktop")

    def test_round_trip_reaches_the_ingress_with_the_verified_peer_id(self) -> None:
        self.pair()
        running = self.start()
        reply = self.send(running)
        self.assertEqual(reply["ok"], True)
        self.assertEqual(reply["body"], {"peer": self.b_id})
        self.assertEqual(len(self.ingress.seen), 1)
        seen = self.ingress.seen[0]
        self.assertEqual(seen["path"], "/net/ingress")
        self.assertEqual(seen["host"], f"127.0.0.1:{self.ingress.port}")
        self.assertEqual(seen["auth"], f"Bearer {self.ingress.token}")
        self.assertEqual(seen["type"], "application/json")
        self.assertEqual(seen["body"]["peer"], self.b_id)
        self.assertEqual(seen["body"]["request"]["op"], "inventory")
        self.assertEqual(reply["re"], seen["body"]["request"]["id"])

    def test_pid_file_is_written_and_removed(self) -> None:
        self.pair()
        running = self.start()
        info = read_listener(self.a)
        assert info is not None
        self.assertEqual((info.pid, info.bind, info.port), (os.getpid(), LOOPBACK, running.port))
        running.stop()
        self.assertIsNone(read_listener(self.a))
        self.assertEqual(running.code, 0)

    def test_stop_file_ends_the_listener(self) -> None:
        self.pair()
        running = self.start()
        listener.request_stop(self.a)
        self.assertTrue(wait_until(lambda: read_listener(self.a) is None, 8))
        self.assertFalse(os.path.exists(listener.stop_file(self.a)))
        self.assertEqual(running.code, 0)

    def test_a_pinned_certificate_of_another_machine_is_refused_by_the_client(self) -> None:
        trust_peer(self.a, self.b, "laptop")
        trust_peer(self.b, self.c, "wrong")
        running = self.start()
        with self.assertRaises(NetClientError):
            self.send(running, peer=self.c_id)
        self.assertEqual(self.ingress.seen, [])

    def test_an_untrusted_certificate_is_refused(self) -> None:
        trust_peer(self.a, self.b, "laptop")
        trust_peer(self.c, self.a, "desktop")
        running = self.start()
        with self.assertRaises(NetClientError):
            self.send(running, home=self.c)
        self.assertEqual(self.ingress.seen, [])

    def test_a_source_address_that_is_not_recorded_is_dropped(self) -> None:
        self.pair(source="10.9.8.7")
        running = self.start()
        with self.assertRaises(NetClientError):
            self.send(running)
        self.assertEqual(self.ingress.seen, [])

    def test_an_oversize_frame_is_refused(self) -> None:
        self.pair()
        running = self.start()
        with raw_connect(self.b, self.a_id, running.port) as sock:
            wire.write_frame(sock, wire.hello_frame())
            self.assertIsNotNone(read_or_none(sock))
            sock.sendall((wire.MAX_FRAME + 1).to_bytes(4, "big"))
            self.assertIsNone(read_or_none(sock))
        self.assertEqual(self.ingress.seen, [])

    def test_a_bad_hello_is_refused(self) -> None:
        self.pair()
        running = self.start()
        with raw_connect(self.b, self.a_id, running.port) as sock:
            wire.write_frame(sock, wire.encode({"v": 2, "hello": wire.HELLO}))
            self.assertIsNone(read_or_none(sock))
        self.assertEqual(self.ingress.seen, [])

    def test_one_request_per_connection(self) -> None:
        self.pair()
        running = self.start()
        with raw_connect(self.b, self.a_id, running.port) as sock:
            wire.write_frame(sock, wire.hello_frame())
            self.assertIsNotNone(read_or_none(sock))
            wire.write_frame(sock, wire.encode(INVENTORY))
            self.assertIsNotNone(read_or_none(sock))
            try:
                wire.write_frame(sock, wire.encode({**INVENTORY, "id": "r-2"}))
            except OSError:
                pass
            self.assertIsNone(read_or_none(sock))
        self.assertEqual(len(self.ingress.seen), 1)

    def test_revoking_a_peer_stops_the_next_connection_without_a_restart(self) -> None:
        self.pair()
        running = self.start()
        self.assertTrue(self.send(running)["ok"])
        forget_peer(self.a, self.b_id)
        with self.assertRaises(NetClientError):
            self.send(running)
        self.assertEqual(len(self.ingress.seen), 1)

    def test_a_new_peer_is_accepted_without_a_restart(self) -> None:
        trust_peer(self.b, self.a, "desktop")
        running = self.start()
        with self.assertRaises(NetClientError):
            self.send(running)
        trust_peer(self.a, self.b, "laptop")
        self.assertTrue(self.send(running)["ok"])

    def test_a_malformed_request_gets_no_reply_and_no_ingress_call(self) -> None:
        self.pair()
        running = self.start()
        with raw_connect(self.b, self.a_id, running.port) as sock:
            wire.write_frame(sock, wire.hello_frame())
            self.assertIsNotNone(read_or_none(sock))
            wire.write_frame(sock, wire.encode({"id": "r-1", "op": "open", "body": {}, "extra": 1}))
            self.assertIsNone(read_or_none(sock))
        self.assertEqual(self.ingress.seen, [])

    def test_a_reply_for_another_request_is_replaced_by_a_refusal(self) -> None:
        self.ingress.reply = lambda peer, request: wire.ok_reply("someone-else", {})
        self.pair()
        running = self.start()
        reply = self.send(running)
        self.assertEqual(reply, {"re": reply["re"], "ok": False, "refused": True})

    def test_the_connection_limits_close_extra_connections(self) -> None:
        self.pair()
        running = self.start()
        held = [socket.create_connection((LOOPBACK, running.port), timeout=5) for _ in range(listener.PER_SOURCE)]
        self.addCleanup(lambda: [s.close() for s in held])
        extra = socket.create_connection((LOOPBACK, running.port), timeout=5)
        self.addCleanup(extra.close)
        extra.settimeout(3)
        try:
            self.assertEqual(extra.recv(1), b"")
        except ConnectionError:
            pass
        for sock in held:
            sock.close()
        results: list[bool] = []

        def served() -> bool:
            try:
                results.append(self.send(running, timeout=5)["ok"] is True)
            except NetClientError:
                return False
            return True

        self.assertTrue(wait_until(served, 10))
        self.assertEqual(results, [True])

    def test_a_silent_connection_is_closed_after_the_handshake_timeout(self) -> None:
        self.pair()
        with mock.patch.object(listener, "HANDSHAKE_S", 0.5):
            running = self.start()
            silent = socket.create_connection((LOOPBACK, running.port), timeout=5)
            self.addCleanup(silent.close)
            silent.settimeout(5)
            try:
                self.assertEqual(silent.recv(1), b"")
            except ConnectionError:
                pass

    def test_production_options_refuse_a_loopback_bind(self) -> None:
        self.pair()
        config = NetworkConfig(network="lan", bind=LOOPBACK, port=0)
        self.assertEqual(run_listener(self.a, Options(config=config)), 1)
        self.assertIsNone(read_listener(self.a))

    def test_the_config_file_cannot_select_a_loopback_bind(self) -> None:
        self.pair()
        with open(os.path.join(self.a, "config.json"), "w", encoding="utf-8") as f:
            f.write('{"network": {"network": "lan", "bind": "127.0.0.1"}}')
        self.addCleanup(os.remove, os.path.join(self.a, "config.json"))
        self.assertEqual(run_listener(self.a, Options()), 1)

    def test_a_listener_refuses_when_the_network_is_off(self) -> None:
        self.assertEqual(run_listener(self.a, Options(config=NetworkConfig(network="off", bind=LOOPBACK), bind_check=lambda _a: True)), 1)

    def test_a_public_or_unknown_network_refuses_to_start(self) -> None:
        for category in (True, None):
            options = Options(
                config=NetworkConfig(network="lan", bind=LOOPBACK, port=0),
                bind_check=lambda _a: True,
                network_check=lambda _a, value=category: value,
            )
            self.assertEqual(run_listener(self.a, options), 1)

    def test_an_address_that_is_not_local_refuses_to_start(self) -> None:
        options = Options(
            config=NetworkConfig(network="lan", bind="192.0.2.1", port=0), bind_check=lambda _a: True, network_check=lambda _a: False
        )
        self.assertEqual(run_listener(self.a, options), 1)

    def test_a_second_listener_is_refused(self) -> None:
        self.pair()
        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], stdin=subprocess.DEVNULL)
        self.addCleanup(other.wait)
        self.addCleanup(other.kill)
        listener.write_atomically(
            str(listener.listener_file(self.a)), f'{{"pid": {other.pid}, "startedMs": 1, "bind": "{LOOPBACK}", "port": 1}}\n'
        )
        self.addCleanup(os.remove, str(listener.listener_file(self.a)))
        options = Options(
            config=NetworkConfig(network="lan", bind=LOOPBACK, port=0), bind_check=lambda _a: True, network_check=lambda _a: False
        )
        self.assertEqual(run_listener(self.a, options), 1)

    def test_a_public_network_stops_a_running_listener(self) -> None:
        self.pair()
        states: list[bool | None] = [False]
        with mock.patch.object(listener, "PROFILE_REFRESH_S", 0.0), mock.patch.object(listener, "MONITOR_S", 0.05):
            running = self.start(network_check=lambda _bind: states[0])
            self.assertTrue(self.send(running)["ok"])
            states[0] = True
            self.assertTrue(wait_until(lambda: read_listener(self.a) is None, 8))
        self.assertEqual(running.code, 0)


class NetworkWatchTest(unittest.TestCase):
    def test_refreshes_every_thirty_seconds(self) -> None:
        now = [0.0]
        answers: list[bool | None] = [False]
        calls: list[str] = []

        def check(bind: str) -> bool | None:
            calls.append(bind)
            return answers[0]

        with mock.patch.object(listener, "_address_present", return_value=True):
            watch = NetworkWatch(LOOPBACK, check, lambda: now[0], lambda: frozenset({"a"}))
            self.assertIsNone(watch.initial())
            now[0] = 29.0
            self.assertIsNone(watch.tick())
            self.assertEqual(len(calls), 1)
            answers[0] = True
            now[0] = 31.0
            self.assertEqual(watch.tick(), "the network of the bind address is marked Public")
            self.assertFalse(watch.allowed())
            self.assertEqual(len(calls), 2)

    def test_a_changed_address_set_forces_a_refresh(self) -> None:
        addresses = [frozenset({"a"})]
        answers: list[bool | None] = [False]
        with mock.patch.object(listener, "_address_present", return_value=True):
            watch = NetworkWatch(LOOPBACK, lambda _b: answers[0], lambda: 0.0, lambda: addresses[0])
            self.assertIsNone(watch.initial())
            answers[0] = True
            self.assertIsNone(watch.tick())
            addresses[0] = frozenset({"a", "b"})
            self.assertIsNotNone(watch.tick())

    def test_a_missing_address_stops(self) -> None:
        with mock.patch.object(listener, "_address_present", return_value=False):
            watch = NetworkWatch(LOOPBACK, lambda _b: False, lambda: 0.0, lambda: frozenset())
            self.assertEqual(watch.tick(), "the bind address is no longer assigned to a local interface")
            self.assertFalse(watch.allowed())

    def test_an_unknown_answer_at_start_refuses_and_later_is_ignored(self) -> None:
        now = [0.0]
        answers: list[bool | None] = [None]
        with mock.patch.object(listener, "_address_present", return_value=True):
            watch = NetworkWatch(LOOPBACK, lambda _b: answers[0], lambda: now[0], lambda: frozenset())
            self.assertIsNotNone(watch.initial())
            watch = NetworkWatch(LOOPBACK, lambda _b: answers[0], lambda: now[0], lambda: frozenset())
            answers[0] = False
            self.assertIsNone(watch.initial())
            answers[0] = None
            now[0] = 60.0
            self.assertIsNone(watch.tick())
            self.assertTrue(watch.allowed())


@unittest.skipUnless(HAS_OPENSSL, "needs an openssl")
class IngressForwarderTest(unittest.TestCase):
    home: str

    @classmethod
    def setUpClass(cls) -> None:
        cls.home = create_home(make_root(cls), "a")

    def setUp(self) -> None:
        self.logs: list[str] = []

    def forwarder(self, port: int, clock: Any = None) -> IngressForwarder:
        return IngressForwarder(self.home, port, self.logs.append, clock=clock or (lambda: 0.0))

    def test_a_server_that_matches_its_state_file_gets_the_request(self) -> None:
        fake = FakeIngress(self.home)
        self.addCleanup(fake.close)
        reply = self.forwarder(fake.port).forward("PEER", INVENTORY)
        assert reply is not None
        self.assertEqual(reply["body"], {"peer": "PEER"})
        self.assertEqual(fake.seen[0]["auth"], f"Bearer {fake.token}")

    def test_a_pid_that_differs_from_the_state_file_gets_nothing(self) -> None:
        fake = FakeIngress(self.home, health_pid=os.getpid() + 1000003)
        self.addCleanup(fake.close)
        self.assertIsNone(self.forwarder(fake.port).forward("PEER", INVENTORY))
        self.assertEqual(fake.seen, [])

    def test_a_state_file_with_a_dead_pid_gets_nothing(self) -> None:
        fake = FakeIngress(self.home)
        self.addCleanup(fake.close)
        with mock.patch.object(listener, "pid_alive", return_value=False):
            self.assertIsNone(self.forwarder(fake.port).forward("PEER", INVENTORY))
        self.assertEqual(fake.seen, [])

    def test_another_program_on_the_port_is_refused_and_logged(self) -> None:
        fake = FakeIngress(self.home, serve_health=False)
        self.addCleanup(fake.close)
        with mock.patch.object(listener, "ensure_server") as ensure:
            self.assertIsNone(self.forwarder(fake.port).forward("PEER", INVENTORY))
        ensure.assert_not_called()
        self.assertEqual(fake.seen, [])
        self.assertTrue(any("belongs to another program" in line for line in self.logs))

    def test_a_non_200_answer_is_refused(self) -> None:
        fake = FakeIngress(self.home)
        self.addCleanup(fake.close)
        fake.token = "0" * 64
        self.assertIsNone(self.forwarder(fake.port).forward("PEER", INVENTORY))

    def test_a_down_server_is_started_at_most_once_every_ten_seconds(self) -> None:
        port = free_port()
        now = [100.0]
        forwarder = self.forwarder(port, lambda: now[0])
        ensured = mock.Mock(problem="did not start", health=None)
        with mock.patch.object(listener, "ensure_server", return_value=ensured) as ensure:
            self.assertIsNone(forwarder.forward("PEER", INVENTORY))
            now[0] = 105.0
            self.assertIsNone(forwarder.forward("PEER", INVENTORY))
            self.assertEqual(ensure.call_count, 1)
            now[0] = 110.5
            self.assertIsNone(forwarder.forward("PEER", INVENTORY))
            self.assertEqual(ensure.call_count, 2)
        command = ensure.call_args[0][0]
        self.assertEqual(command[1:3], ["-I", "-S"])
        self.assertTrue(os.path.isabs(command[0]))
        self.assertTrue(command[3].endswith("shared_server.py"))
        self.assertEqual(command[4:], ["--port", str(port)])

    def test_a_started_server_is_verified_before_use(self) -> None:
        fake = FakeIngress(self.home, state_pid=os.getpid() + 1000003)
        self.addCleanup(fake.close)
        health = {"service": "ide-agent-tabs", "pid": os.getpid(), "version": "1", "port": fake.port}
        down = mock.Mock(kind="free", health=None)
        with (
            mock.patch.object(listener, "probe", return_value=down),
            mock.patch.object(listener, "ensure_server", return_value=mock.Mock(problem=None, health=health)),
        ):
            self.assertIsNone(self.forwarder(fake.port).forward("PEER", INVENTORY))
        self.assertEqual(fake.seen, [])


if __name__ == "__main__":
    unittest.main()

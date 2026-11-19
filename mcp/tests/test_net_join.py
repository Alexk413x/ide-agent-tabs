from __future__ import annotations

import contextlib
import json
import os
import shutil
import socket
import threading
import unittest
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from unittest import mock

from ide_agent_tabs.net import pairing, tls, wire
from ide_agent_tabs.net.client import peers_dir, read_peer_pem
from ide_agent_tabs.net.hubjoin import HubJoiner, local_pair_identity
from ide_agent_tabs.net.hubstate import HubState, HubStateError, ensure_hub_key, hub_state_path, read_hub_state, write_hub_state
from ide_agent_tabs.net.identity import format_digest
from ide_agent_tabs.net.pairing import HubMessage, HubRef, PairResult, PeerInfo
from ide_agent_tabs.net.peers import save_peer
from ide_agent_tabs.net.policy import (
    DEFAULT_PORT,
    MEMBER_MAX_TABS,
    MEMBER_MESSAGES_PER_MINUTE,
    MEMBER_OPENS_PER_HOUR,
    Hub,
    Policy,
    grant_for,
    load_policy,
    save_policy,
)
from net_support import HAS_OPENSSL, cert_of, create_home, device_of, make_root, pem_der, wait_until

HUB_ID = "0123456789abcdef0123456789abcdef"
OTHER_HUB_ID = "fedcba9876543210fedcba9876543210"
FAST = pairing.Timeouts(first_frame_s=2.0, handshake_s=10.0, consent_s=5.0, connect_s=2.0, min_interval_s=0.0)
NEEDS = unittest.skipUnless(HAS_OPENSSL and tls.runtime_problem() is None, "needs an openssl and a TLS 1.3 Python")


class Capture:
    def __init__(self) -> None:
        self.payloads: list[bytes] = []

    @property
    def frames(self) -> list[dict[str, Any]]:
        return [json.loads(payload) for payload in self.payloads]

    @property
    def kinds(self) -> list[str]:
        return [frame["t"] for frame in self.frames]


@contextlib.contextmanager
def captured_frames() -> Iterator[Capture]:
    capture = Capture()
    real = wire.write_frame

    def record(sock: socket.socket, payload: bytes) -> None:
        capture.payloads.append(payload)
        real(sock, payload)

    with mock.patch.object(wire, "write_frame", record):
        yield capture


def set_name(home: str, name: str) -> None:
    Path(home, "config.json").write_text(json.dumps({"network": {"deviceName": name}}), encoding="utf-8")


def reset(home: str) -> None:
    for name in ("policy.json", "policy.json.lock", "hub.json", "hub.json.lock", "pairing.json", "pairing.json.lock"):
        with contextlib.suppress(OSError):
            os.remove(os.path.join(home, "net", name))
    shutil.rmtree(peers_dir(home), ignore_errors=True)


def make_hub(home: str, hub_id: str = HUB_ID, name: str = "home") -> str:
    save_policy(home, Policy((), 0, Hub(hub_id, name)))
    return ensure_hub_key(home, hub_id)


class JoinServer:
    def __init__(
        self,
        test: unittest.TestCase,
        home: str,
        *,
        handler: HubJoiner | None = None,
        allow_source: Callable[[str], bool] = lambda _address: True,
    ) -> None:
        self.home = home
        self.results: list[PairResult] = []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port: int = self.sock.getsockname()[1]
        self.stop = threading.Event()
        self.saved: list[tuple[str, str]] = []
        self.handler = handler or HubJoiner(home, lambda label, address: self.saved.append((label, address)))
        self.thread = threading.Thread(
            target=pairing.serve_joins,
            args=(self.sock, local_pair_identity(home), self.handler, pairing.Limits(home), self.stop),
            kwargs={"timeouts": FAST, "on_result": self.results.append, "allow_source": allow_source},
            daemon=True,
        )
        self.thread.start()
        test.addCleanup(self.close)

    def close(self) -> None:
        self.stop.set()
        self.thread.join(5)
        self.sock.close()

    def wait_result(self) -> PairResult:
        deadline = threading.Event()
        for _ in range(300):
            if self.results:
                return self.results[0]
            deadline.wait(0.05)
        raise AssertionError("the server finished no join")


def dial(server: JoinServer, home: str, peer_id: str | None = None, handler: Any = None) -> PairResult:
    return pairing.dial_join(
        "127.0.0.1",
        server.port,
        peer_id or device_of(server.home),
        local_pair_identity(home),
        handler or HubJoiner(home),
        pairing.Limits(home),
        timeouts=FAST,
    )


@NEEDS
class JoinTest(unittest.TestCase):
    a: str
    b: str
    c: str

    @classmethod
    def setUpClass(cls) -> None:
        root = make_root(cls)
        cls.a, cls.b, cls.c = (create_home(root, name) for name in ("a", "b", "c"))
        for home, name in ((cls.a, "alpha"), (cls.b, "bravo"), (cls.c, "charlie")):
            set_name(home, name)

    def setUp(self) -> None:
        for home in (self.a, self.b, self.c):
            reset(home)

    def members(self, home: str) -> list[Any]:
        return [p for p in load_policy(home).peers if p.member]

    def test_the_dialer_with_a_hub_brings_it_to_a_machine_without_one(self) -> None:
        key = make_hub(self.b)
        server = JoinServer(self, self.a)
        dialed = dial(server, self.b)
        listened = server.wait_result()
        self.assertEqual((dialed.outcome, listened.outcome), ("paired", "paired"))
        self.assertEqual(load_policy(self.a).hub, Hub(HUB_ID, "home"))
        state = read_hub_state(self.a)
        assert state is not None
        self.assertEqual((state.hub_id, state.key, state.members), (HUB_ID, key, ()))
        (on_a,) = self.members(self.a)
        (on_b,) = self.members(self.b)
        self.assertEqual((on_a.device_id, on_a.label, on_a.address), (device_of(self.b), "bravo", "127.0.0.1"))
        self.assertEqual((on_b.device_id, on_b.label, on_b.address), (device_of(self.a), "alpha", "127.0.0.1"))
        self.assertEqual(server.saved, [("bravo", "127.0.0.1")])

    def test_the_listener_with_a_hub_brings_it_to_a_dialer_without_one(self) -> None:
        key = make_hub(self.a)
        server = JoinServer(self, self.a)
        dialed = dial(server, self.b)
        self.assertEqual((dialed.outcome, server.wait_result().outcome), ("paired", "paired"))
        self.assertEqual(load_policy(self.b).hub, Hub(HUB_ID, "home"))
        state = read_hub_state(self.b)
        assert state is not None
        self.assertEqual(state.key, key)

    def test_saved_members_get_the_member_flag_and_the_member_limits(self) -> None:
        make_hub(self.b)
        server = JoinServer(self, self.a)
        dial(server, self.b)
        server.wait_result()
        for home, other in ((self.a, self.b), (self.b, self.a)):
            peer = grant_for(load_policy(home), device_of(other))
            assert peer is not None
            self.assertTrue(peer.member)
            self.assertEqual((peer.max_tabs, peer.opens_per_hour, peer.messages_per_minute), (4, 30, 60))
            self.assertEqual((MEMBER_MAX_TABS, MEMBER_OPENS_PER_HOUR, MEMBER_MESSAGES_PER_MINUTE), (4, 30, 60))
            self.assertEqual((peer.folders, peer.agents, peer.models, peer.send), ((), (), (), ()))
            saved_pem = read_peer_pem(home, peer.device_id)
            assert saved_pem is not None
            self.assertEqual(saved_pem.replace("\r\n", "\n"), cert_of(other).read_text(encoding="ascii").replace("\r\n", "\n"))

    def test_the_hub_message_carries_the_hub_the_key_and_the_other_members(self) -> None:
        key = make_hub(self.a)
        server = JoinServer(self, self.a)
        third = PeerInfo("charlie", "192.168.1.30", device_of(self.c), pem_der(self.c))
        HubJoiner(self.a).save(third, None)
        with captured_frames() as capture:
            dialed = dial(server, self.b)
        self.assertEqual((dialed.outcome, server.wait_result().outcome), ("paired", "paired"))
        self.assertEqual(
            sorted(capture.kinds), ["hello", "hello", "hub", "ready", "ready", "reveal", "saved", "saved", "state", "state", "tls"]
        )
        message = next(frame for frame in capture.frames if frame["t"] == "hub")
        self.assertEqual(sorted(message), ["epoch", "hub", "hubKey", "members", "removed", "t"])
        self.assertEqual(message["removed"], [])
        self.assertEqual(message["epoch"], 0)
        self.assertEqual(message["hub"], {"id": HUB_ID, "name": "home"})
        self.assertEqual(message["hubKey"], key)
        self.assertRegex(message["hubKey"], r"^[0-9a-f]{64}$")
        (member,) = message["members"]
        self.assertEqual(sorted(member), ["address", "certificatePem", "deviceId", "label", "port"])
        self.assertEqual(
            (member["deviceId"], member["label"], member["address"], member["port"]),
            (device_of(self.c), "charlie", "192.168.1.30", DEFAULT_PORT),
        )
        self.assertEqual(member["certificatePem"].replace("\r\n", "\n"), cert_of(self.c).read_text(encoding="ascii").replace("\r\n", "\n"))
        state = read_hub_state(self.b)
        assert state is not None
        self.assertEqual(
            [(m.device_id, m.label, m.address, m.port) for m in state.members],
            [(device_of(self.c), "charlie", "192.168.1.30", DEFAULT_PORT)],
        )
        pinned = grant_for(load_policy(self.b), device_of(self.c))
        assert pinned is not None
        self.assertEqual((pinned.label, pinned.address, pinned.member), ("charlie", "192.168.1.30", True))
        saved_pem = read_peer_pem(self.b, device_of(self.c))
        assert saved_pem is not None
        self.assertEqual(saved_pem.replace("\r\n", "\n"), cert_of(self.c).read_text(encoding="ascii").replace("\r\n", "\n"))

    def test_the_hub_message_leaves_out_the_joining_machine_and_non_members(self) -> None:
        make_hub(self.a)
        save_peer(self.a, device_of(self.b), cert_of(self.b).read_text(encoding="ascii"), "192.168.1.31", "bravo", member=True)
        save_peer(self.a, device_of(self.c), cert_of(self.c).read_text(encoding="ascii"), "192.168.1.32", "charlie")
        server = JoinServer(self, self.a)
        with captured_frames() as capture:
            dial(server, self.b)
            server.wait_result()
        message = next(frame for frame in capture.frames if frame["t"] == "hub")
        self.assertEqual(message["members"], [])

    def test_the_dialer_hello_names_the_join_mode(self) -> None:
        make_hub(self.b)
        server = JoinServer(self, self.a)
        with captured_frames() as capture:
            dial(server, self.b)
            server.wait_result()
        first = capture.frames[0]
        self.assertEqual(sorted(first), ["cert", "commit", "mode", "name", "t", "v"])
        self.assertEqual(first["mode"], "join")

    def test_a_certificate_that_is_not_the_expected_device_stops_the_dialer_and_saves_nothing(self) -> None:
        make_hub(self.b)
        server = JoinServer(self, self.a)
        dialed = dial(server, self.b, peer_id=device_of(self.c))
        self.assertEqual(dialed.outcome, "failed")
        self.assertIn("does not match the device id", dialed.reason)
        self.assertEqual(self.members(self.b), [])
        self.assertEqual(self.members(self.a), [])
        self.assertIsNone(load_policy(self.a).hub)
        self.assertIsNone(read_hub_state(self.a))
        self.assertEqual(server.results, [])

    def test_two_machines_in_different_hubs_refuse_and_share_nothing(self) -> None:
        key_b = make_hub(self.b, HUB_ID, "home")
        key_a = make_hub(self.a, OTHER_HUB_ID, "office")
        server = JoinServer(self, self.a)
        with captured_frames() as capture:
            dialed = dial(server, self.b)
            listened = server.wait_result()
        for result in (dialed, listened):
            self.assertEqual(result.outcome, "failed")
            self.assertIn("already-in-another-hub", result.reason)
        self.assertNotIn("hub", capture.kinds)
        for payload in capture.payloads:
            self.assertNotIn(key_a.encode(), payload)
            self.assertNotIn(key_b.encode(), payload)
        for home, hub in ((self.a, Hub(OTHER_HUB_ID, "office")), (self.b, Hub(HUB_ID, "home"))):
            policy = load_policy(home)
            self.assertEqual((policy.peers, policy.hub), ((), hub))
            self.assertFalse(os.path.isdir(os.path.join(home, "net", "peers")) and os.listdir(os.path.join(home, "net", "peers")))
        state_a, state_b = read_hub_state(self.a), read_hub_state(self.b)
        assert state_a is not None and state_b is not None
        self.assertEqual((state_a.hub_id, state_a.key, state_b.hub_id, state_b.key), (OTHER_HUB_ID, key_a, HUB_ID, key_b))
        self.assertEqual(server.saved, [])

    def test_two_machines_without_a_hub_refuse(self) -> None:
        server = JoinServer(self, self.a)
        dialed = dial(server, self.b)
        self.assertIn("no-hub", dialed.reason)
        self.assertIn("no-hub", server.wait_result().reason)
        self.assertEqual((load_policy(self.a).peers, load_policy(self.b).peers), ((), ()))

    def test_a_rejoin_in_the_same_hub_updates_the_address_and_keeps_the_key(self) -> None:
        key = make_hub(self.a)
        server = JoinServer(self, self.a)
        dial(server, self.b)
        server.wait_result()
        server.results.clear()
        for home, other in ((self.a, self.b), (self.b, self.a)):
            policy = load_policy(home)
            moved = tuple(
                p.__class__(**{**p.__dict__, "address": "10.9.9.9", "label": "kept-label"}) if p.device_id == device_of(other) else p
                for p in policy.peers
            )
            save_policy(home, policy.__class__(moved, policy.generation, policy.hub))
        dialed = dial(server, self.b)
        self.assertEqual((dialed.outcome, server.wait_result().outcome), ("paired", "paired"))
        for home, other in ((self.a, self.b), (self.b, self.a)):
            (peer,) = self.members(home)
            self.assertEqual((peer.address, peer.label), ("127.0.0.1", "kept-label"))
            self.assertEqual(peer.device_id, device_of(other))
        state = read_hub_state(self.b)
        assert state is not None
        self.assertEqual(state.key, key)

    def test_a_hub_message_the_receiver_rejects_leaves_both_sides_unsaved(self) -> None:
        make_hub(self.b)
        outer = self

        class BadMessages(HubJoiner):
            def message(self, peer: PeerInfo) -> HubMessage:
                good = super().message(peer)
                bad = pairing.MemberRef(device_of(outer.c), "charlie", "192.168.1.30", 47829, "not a certificate")
                return HubMessage(good.hub, good.key, (bad,))

        server = JoinServer(self, self.b, handler=BadMessages(self.b))
        dialed = dial(server, self.a)
        self.assertEqual(dialed.outcome, "failed")
        self.assertEqual(server.wait_result().outcome, "failed")
        self.assertEqual((self.members(self.a), self.members(self.b)), ([], []))
        self.assertIsNone(load_policy(self.a).hub)
        self.assertIsNone(read_hub_state(self.a))

    def test_a_save_that_fails_after_both_are_ready_is_reported_and_not_acknowledged(self) -> None:
        make_hub(self.b)
        server = JoinServer(self, self.a)

        class Failing(HubJoiner):
            def save(self, peer: PeerInfo, received: HubMessage | None) -> None:
                raise OSError("disk full")

        dialed = dial(server, self.b, handler=Failing(self.b))
        self.assertEqual(dialed.outcome, "failed")
        self.assertIn("saving the trust entry failed", dialed.reason)
        self.assertEqual(server.wait_result().outcome, "incomplete")

    def test_sources_outside_the_private_ranges_are_dropped_by_default(self) -> None:
        self.assertTrue(pairing.private_source("192.168.1.5"))
        self.assertTrue(pairing.private_source("10.1.2.3"))
        self.assertFalse(pairing.private_source("127.0.0.1"))
        self.assertFalse(pairing.private_source("8.8.8.8"))
        self.assertFalse(pairing.private_source("::1"))
        make_hub(self.b)
        server = JoinServer(self, self.a, allow_source=pairing.private_source)
        dialed = dial(server, self.b)
        self.assertEqual(dialed.outcome, "failed")
        self.assertEqual(self.members(self.a), [])

    def test_a_locked_address_is_not_dialed(self) -> None:
        make_hub(self.b)
        limits = pairing.Limits(self.b)
        for _ in range(3):
            limits.record_failure("127.0.0.1")
        server = JoinServer(self, self.a)
        with self.assertRaises(pairing.Locked):
            dial(server, self.b)

    def junk_join(self, server: JoinServer) -> None:
        with socket.create_connection(("127.0.0.1", server.port), timeout=5) as conn:
            wire.write_frame(conn, wire.encode({"t": "hello", "mode": "join"}))
            conn.settimeout(2)
            with contextlib.suppress(OSError):
                conn.recv(1)

    def test_failed_join_handshakes_lock_only_the_address_they_came_from(self) -> None:
        server = JoinServer(self, self.a)
        limits = pairing.Limits(self.a)
        for _ in range(3):
            self.junk_join(server)
        self.assertTrue(wait_until(lambda: "127.0.0.1" in limits.locked_addresses()))
        self.assertEqual(list(limits.locked_addresses()), ["127.0.0.1"])
        make_hub(self.b)
        self.assertEqual(dial(server, self.b).outcome, "failed")
        self.assertEqual(self.members(self.a), [])

    def test_a_lock_on_one_address_does_not_stop_a_join_from_another(self) -> None:
        limits = pairing.Limits(self.a)
        for _ in range(3):
            limits.record_failure("10.9.9.9")
        make_hub(self.b)
        server = JoinServer(self, self.a)
        dialed = dial(server, self.b)
        self.assertEqual((dialed.outcome, server.wait_result().outcome), ("paired", "paired"))
        self.assertEqual(list(limits.locked_addresses()), ["10.9.9.9"])

    def recorded_failures(self, home: str) -> dict[str, Any]:
        path = Path(home, "net", pairing.PAIRING_FILE)
        return json.loads(path.read_text(encoding="utf-8"))["addresses"] if path.exists() else {}

    def test_hub_conflicts_count_as_no_failure(self) -> None:
        make_hub(self.a, HUB_ID)
        make_hub(self.b, OTHER_HUB_ID, "other")
        server = JoinServer(self, self.a)
        for _ in range(4):
            self.assertEqual(dial(server, self.b).outcome, "failed")
        self.assertEqual(pairing.Limits(self.a).locked_addresses(), {})
        self.assertEqual(self.recorded_failures(self.a), {})

    def serve_one_join_hello(self, slot: threading.Lock) -> list[PairResult]:
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        self.addCleanup(listener.close)
        client = socket.create_connection(listener.getsockname(), timeout=5)
        self.addCleanup(client.close)
        conn, _peer = listener.accept()
        results: list[PairResult] = []
        worker = threading.Thread(
            target=pairing._serve_join,  # pyright: ignore[reportPrivateUsage]
            args=(slot, conn, "127.0.0.1", local_pair_identity(self.a), HubJoiner(self.a), pairing.Limits(self.a), FAST, results.append),
            daemon=True,
        )
        worker.start()
        wire.write_frame(client, wire.encode({"t": "hello", "mode": "join"}))
        worker.join(10)
        self.assertFalse(worker.is_alive())
        return results

    def test_a_busy_server_drops_the_connection_and_counts_no_failure(self) -> None:
        busy = threading.Lock()
        busy.acquire()
        self.assertEqual(self.serve_one_join_hello(busy), [])
        self.assertEqual(self.recorded_failures(self.a), {})
        self.assertTrue(busy.locked())

    def test_the_same_hello_counts_as_a_failure_when_the_slot_is_free(self) -> None:
        self.serve_one_join_hello(threading.Lock())
        self.assertEqual(list(self.recorded_failures(self.a)), ["127.0.0.1"])

    def probe(self, server: JoinServer) -> pairing.JoinProbe:
        return pairing.probe_join("127.0.0.1", server.port, device_of(server.home), local_pair_identity(self.b), timeouts=FAST)

    def test_a_probe_reads_the_hub_and_is_never_counted_as_a_failure(self) -> None:
        make_hub(self.a)
        server = JoinServer(self, self.a)
        for _ in range(pairing.FAILURES_PER_HOUR + 1):
            found = self.probe(server)
            self.assertEqual((found.device_id, found.hub), (device_of(self.a), HubRef(HUB_ID, "home")))
        self.assertTrue(wait_until(lambda: len(server.results) == pairing.FAILURES_PER_HOUR + 1))
        self.assertEqual({r.outcome for r in server.results}, {"probed"})
        self.assertEqual(pairing.Limits(self.a).locked_addresses(), {})
        self.assertEqual(self.recorded_failures(self.a), {})
        make_hub(self.b)
        self.assertEqual(dial(server, self.b).outcome, "paired")

    def test_a_probe_announces_itself_and_receives_no_hub_key(self) -> None:
        make_hub(self.a)
        server = JoinServer(self, self.a)
        with captured_frames() as capture:
            self.probe(server)
            self.assertTrue(wait_until(lambda: len(server.results) == 1))
        hello = next(frame for frame in capture.frames if frame["t"] == "hello" and "mode" in frame)
        self.assertEqual(hello["mode"], pairing.PROBE_MODE)
        self.assertEqual(capture.kinds, ["hello", "hello", "reveal", "tls", "state"])

    def test_only_failures_that_look_hostile_count(self) -> None:
        counts = pairing._counts_as_failure  # pyright: ignore[reportPrivateUsage]
        self.assertTrue(counts(None))
        self.assertTrue(counts(PairResult("failed", "timed out")))
        self.assertTrue(counts(PairResult("failed", "the TLS check failed: bad certificate")))
        for reason in (
            f"{pairing.ALREADY_IN_ANOTHER_HUB}: x",
            f"{pairing.NO_HUB}: neither machine is in a hub",
            "saving the trust entry failed: disk full",
            "the join failed (OSError)",
        ):
            self.assertFalse(counts(PairResult("failed", reason)), reason)
        self.assertFalse(counts(PairResult("paired", "paired")))
        self.assertFalse(counts(PairResult("incomplete", "x")))


class HubStateTest(unittest.TestCase):
    def test_the_hub_state_file_is_private_and_round_trips(self) -> None:
        home = make_root(self)
        key = "ab" * 32
        write_hub_state(home, HubState(HUB_ID, key))
        self.assertEqual(read_hub_state(home), HubState(HUB_ID, key))
        self.assertTrue(hub_state_path(home).is_file())

    def test_a_damaged_state_file_is_refused(self) -> None:
        home = make_root(self)
        write_hub_state(home, HubState(HUB_ID, "ab" * 32))
        hub_state_path(home).write_text("{not json", encoding="utf-8")
        with self.assertRaises(HubStateError):
            read_hub_state(home)

    def test_a_missing_file_reads_as_none(self) -> None:
        self.assertIsNone(read_hub_state(make_root(self)))


class HubMessageParseTest(unittest.TestCase):
    def frame(self, **changes: Any) -> dict[str, Any]:
        value: dict[str, Any] = {
            "t": "hub",
            "hub": {"id": HUB_ID, "name": "home"},
            "hubKey": "ab" * 32,
            "epoch": 0,
            "members": [],
            "removed": [],
        }
        value.update(changes)
        return value

    def test_a_good_message_parses(self) -> None:
        message = pairing.parse_hub_message(self.frame())
        self.assertEqual(
            (message.hub, message.key, message.members, message.epoch, message.removed), (HubRef(HUB_ID, "home"), "ab" * 32, (), 0, ())
        )
        self.assertEqual(pairing.parse_hub_message(self.frame(epoch=7)).epoch, 7)

    def test_removed_devices_parse_and_a_repeat_keeps_one_entry(self) -> None:
        gone = format_digest(bytes(range(32)))
        entries = [{"deviceId": gone, "atMs": 5}, {"deviceId": gone, "atMs": 9}]
        message = pairing.parse_hub_message(self.frame(removed=entries))
        self.assertEqual(message.removed, (pairing.RemovedRef(gone, 9),))

    def test_a_message_without_the_removed_list_is_refused(self) -> None:
        frame = self.frame()
        del frame["removed"]
        with self.assertRaises(pairing.PairingError):
            pairing.parse_hub_message(frame)

    def test_refused_messages(self) -> None:
        for changes in (
            {"hub": {"id": "short", "name": "home"}},
            {"hub": {"id": HUB_ID, "name": "Home"}},
            {"hub": {"id": HUB_ID}},
            {"hubKey": "AB" * 32},
            {"hubKey": "ab" * 31},
            {"members": "none"},
            {"members": [{"deviceId": "x"}]},
            {"members": [{}] * 33},
            {"epoch": -1},
            {"epoch": True},
            {"epoch": "1"},
            {"epoch": 2**31},
            {"extra": 1},
            {"removed": "none"},
            {"removed": [{"deviceId": format_digest(bytes(range(32))), "atMs": 1}] * (pairing.MAX_JOIN_REMOVED + 1)},
            {"removed": [{"deviceId": "x", "atMs": 1}]},
            {"removed": [{"deviceId": format_digest(bytes(range(32))), "atMs": -1}]},
            {"removed": [{"deviceId": format_digest(bytes(range(32)))}]},
        ):
            with self.assertRaises(pairing.PairingError, msg=str(changes)):
                pairing.parse_hub_message(self.frame(**changes))

    def test_a_member_certificate_must_match_its_device_id(self) -> None:
        member = {
            "deviceId": format_digest(bytes(range(32))),
            "label": "x",
            "address": "192.168.1.2",
            "port": 47829,
            "certificatePem": "nope",
        }
        with self.assertRaises(pairing.PairingError):
            pairing.parse_hub_message(self.frame(members=[member]))


if __name__ == "__main__":
    unittest.main()

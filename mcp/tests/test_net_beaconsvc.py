from __future__ import annotations

import contextlib
import json
import os
import queue
import threading
import time
import unittest
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from unittest import mock

from ide_agent_tabs.net import beaconsvc, cli, listener
from ide_agent_tabs.net.beacon import (
    BROADCAST,
    MULTICAST,
    Datagram,
    Endpoint,
    JoinAnnouncement,
    MemberBeacon,
    SendResult,
    Wire,
    build_member,
    parse_datagram,
)
from ide_agent_tabs.net.beaconsvc import (
    ANNOUNCE_INTERVAL_S,
    REMOTE_NOTES_PER_INTERVAL,
    BeaconService,
    BeaconSettings,
    BeaconState,
    Reconnector,
    check_remembered,
)
from ide_agent_tabs.net.client import NetClientError
from ide_agent_tabs.net.hubstate import HubState, Pending, add_pending, hub_state_path, read_hub_state, write_hub_state
from ide_agent_tabs.net.identity import format_digest
from ide_agent_tabs.net.interfaces import InterfaceInfo
from ide_agent_tabs.net.listener import Listener, Options
from ide_agent_tabs.net.pairing import PAIRING_PORT
from ide_agent_tabs.net.peers import save_peer, update_address
from ide_agent_tabs.net.policy import Hub, NetworkConfig, PeerGrant, Policy, grant_for, load_policy, save_policy
from ide_agent_tabs.net.presence import read_presence
from net_support import FakeIngress, RunningListener, cert_of, create_home, device_of, make_root, trust_peer, wait_until
from test_net_beacon import ALPHA, BRAVO, CHARLIE, HUB_ID, KEY
from test_net_join import FAST, NEEDS, reset, set_name

STORED = "192.168.1.20"
NEW = "192.168.1.77"
LAN = InterfaceInfo("Ethernet", "192.168.1.10", 24, "192.168.1.0/24", "Private", True)
LOOP = InterfaceInfo("loopback", "127.0.0.9", 8, "127.0.0.0/8", "Private", True)
OPEN = BeaconState(True, True)


@dataclass
class Script:
    sent: list[bytes] = field(default_factory=lambda: list[bytes]())
    incoming: queue.Queue[Datagram] = field(default_factory=lambda: queue.Queue[Datagram]())
    opened: int = 0
    closed: int = 0
    receives: int = 0
    wires: list[Wire] = field(default_factory=lambda: list[Wire]())


class ScriptedEndpoint(Endpoint):
    def __init__(self, wire: Wire, script: Script) -> None:
        self.wire = wire
        self.joined = True
        self.notes: list[str] = []
        self.script = script
        script.opened += 1
        script.wires.append(wire)

    def send(self, data: bytes) -> SendResult:
        self.script.sent.append(data)
        return SendResult((MULTICAST, BROADCAST), ())

    def recv(self, timeout: float) -> Datagram | None:
        self.script.receives += 1
        try:
            return self.script.incoming.get(timeout=min(timeout, 0.02))
        except queue.Empty:
            return None

    def close(self) -> None:
        self.script.closed += 1


def inline(task: Callable[[], None]) -> None:
    task()


def settings(script: Script, info: InterfaceInfo = LAN, **changes: Any) -> BeaconSettings:
    values: dict[str, Any] = {
        "interface": lambda _bind: info,
        "make_endpoint": lambda wire: ScriptedEndpoint(wire, script),
        "interval_s": 60.0,
        "poll_s": 0.02,
        "retry_s": 0.05,
        "check_destination": lambda address, _port, _interface, _prefix: address,
        "run": inline,
        "request_timeout_s": 1.0,
    }
    values.update(changes)
    return BeaconSettings(**values)


def member_datagram(key: str = KEY, device: str = BRAVO, host: str = NEW, ts: int | None = None, hub: str = HUB_ID) -> Datagram:
    data = build_member(key, hub, device, int(time.time()) if ts is None else ts)
    return Datagram(data, host, 47831)


def parsed(script: Script) -> list[Any]:
    return [parse_datagram(data) for data in list(script.sent)]


class Service:
    def __init__(
        self, test: unittest.TestCase, home: str, state: Callable[[], BeaconState], config: BeaconSettings, bind: str = "127.0.0.9"
    ) -> None:
        self.stop = threading.Event()
        self.logs: list[str] = []
        self.service = BeaconService(home, bind, state, self.stop, self.logs.append, config)
        self.thread = self.service.start()
        test.addCleanup(self.close)

    def close(self) -> None:
        self.stop.set()
        self.thread.join(5)

    @property
    def log(self) -> str:
        return "\n".join(self.logs)


class PlainHomeCase(unittest.TestCase):
    def setUp(self) -> None:
        self.home = os.path.join(make_root(self), "home")
        os.makedirs(self.home)
        set_name(self.home, "alpha")
        patch = mock.patch.object(beaconsvc, "local_device_id", lambda _home: ALPHA)
        patch.start()
        self.addCleanup(patch.stop)
        self.script = Script()

    def in_hub(self, address: str = STORED, key: str = KEY) -> None:
        write_hub_state(self.home, HubState(HUB_ID, key))
        save_policy(self.home, Policy((PeerGrant(BRAVO, "bravo", address, member=True),), 0, Hub(HUB_ID, "home")))

    def run_service(self, state: Callable[[], BeaconState] = lambda: OPEN, **changes: Any) -> Service:
        changes.setdefault("request", lambda *_args, **_options: {"ok": True})
        return Service(self, self.home, state, settings(self.script, **changes))


class SendingTest(PlainHomeCase):
    def test_the_default_cadence_is_30_seconds(self) -> None:
        self.assertEqual(ANNOUNCE_INTERVAL_S, 30.0)
        self.assertEqual(BeaconSettings().interval_s, 30.0)

    def test_a_discoverable_hub_member_sends_a_join_announcement_and_a_member_beacon_at_start(self) -> None:
        self.in_hub()
        self.run_service()
        self.assertTrue(wait_until(lambda: len(self.script.sent) >= 2, 5))
        join, member = parsed(self.script)[:2]
        assert isinstance(join, JoinAnnouncement) and isinstance(member, MemberBeacon)
        self.assertEqual(
            (join.device_name, join.device_id, join.hub_id, join.hub_name, join.port), ("alpha", ALPHA, HUB_ID, "home", PAIRING_PORT)
        )
        self.assertEqual((member.hub_id, member.device_id, member.port), (HUB_ID, ALPHA, 47829))
        self.assertLess(abs(member.ts - time.time()), 5)
        self.assertEqual(self.script.wires[0].broadcast, "192.168.1.255")

    def test_a_member_that_is_not_discoverable_sends_only_the_member_beacon(self) -> None:
        self.in_hub()
        self.run_service(lambda: BeaconState(True, False))
        self.assertTrue(wait_until(lambda: len(self.script.sent) >= 1, 5))
        time.sleep(0.2)
        self.assertEqual([type(item) for item in parsed(self.script)], [MemberBeacon])

    def test_a_discoverable_machine_without_a_hub_sends_a_join_announcement_with_no_hub(self) -> None:
        self.run_service()
        self.assertTrue(wait_until(lambda: len(self.script.sent) >= 1, 5))
        time.sleep(0.2)
        (join,) = parsed(self.script)
        assert isinstance(join, JoinAnnouncement)
        self.assertEqual((join.hub_id, join.hub_name), (None, None))

    def test_a_machine_that_is_neither_discoverable_nor_in_a_hub_sends_nothing(self) -> None:
        self.run_service(lambda: BeaconState(True, False))
        self.assertTrue(wait_until(lambda: self.script.opened == 1, 5))
        time.sleep(0.3)
        self.assertEqual(self.script.sent, [])

    def test_it_repeats_on_the_interval(self) -> None:
        self.in_hub()
        self.run_service(lambda: BeaconState(True, False), interval_s=0.15)
        self.assertTrue(wait_until(lambda: len(self.script.sent) >= 4, 5))

    def test_an_address_change_sends_at_once_even_with_a_long_interval(self) -> None:
        self.in_hub()
        epoch = [0]
        self.run_service(lambda: BeaconState(True, False, epoch[0]), interval_s=3600.0)
        self.assertTrue(wait_until(lambda: len(self.script.sent) == 1, 5))
        time.sleep(0.2)
        self.assertEqual(len(self.script.sent), 1)
        epoch[0] += 1
        self.assertTrue(wait_until(lambda: len(self.script.sent) == 2, 5))

    def test_a_rebind_reopens_the_endpoint_on_the_new_address_and_sends_the_member_beacon_at_once(self) -> None:
        self.in_hub()
        moved = InterfaceInfo("Wi-Fi", "192.168.1.88", 24, "192.168.1.0/24", "Private", True)
        service = self.run_service(
            lambda: BeaconState(True, False), interval_s=3600.0, interface=lambda bind: moved if bind == "192.168.1.88" else LAN
        )
        self.assertTrue(wait_until(lambda: len(self.script.sent) == 1, 5))
        self.assertEqual(self.script.wires[0].bind, LAN.address)
        service.service.rebind("192.168.1.88")
        self.assertTrue(wait_until(lambda: self.script.opened == 2 and len(self.script.sent) == 2, 5), service.log)
        self.assertEqual((self.script.closed, self.script.wires[1].bind), (1, "192.168.1.88"))
        self.assertIsInstance(parsed(self.script)[1], MemberBeacon)

    def test_beacons_stop_when_the_network_is_not_allowed_and_resume_when_it_is(self) -> None:
        self.in_hub()
        gate = [True]
        service = self.run_service(lambda: BeaconState(gate[0], True), interval_s=3600.0)
        self.assertTrue(wait_until(lambda: len(self.script.sent) == 2, 5))
        gate[0] = False
        self.assertTrue(wait_until(lambda: self.script.closed == 1, 5))
        self.assertIn("beacons off", service.log)
        receives, sent = self.script.receives, len(self.script.sent)
        time.sleep(0.3)
        self.assertEqual((self.script.receives, len(self.script.sent), self.script.opened), (receives, sent, 1))
        gate[0] = True
        self.assertTrue(wait_until(lambda: self.script.opened == 2 and len(self.script.sent) == sent + 2, 5))

    def test_an_ipv6_bind_address_keeps_beacons_off_and_says_why(self) -> None:
        service = Service(self, self.home, lambda: OPEN, settings(self.script), bind="fd00::5")
        self.assertTrue(wait_until(lambda: "IPv4" in service.log, 5))
        self.assertEqual(self.script.opened, 0)

    def test_an_interface_that_cannot_be_read_keeps_beacons_off_and_says_why(self) -> None:
        def unreadable(_bind: str) -> InterfaceInfo:
            raise beaconsvc.InterfaceError("no such adapter")

        service = self.run_service(interface=unreadable)
        self.assertTrue(wait_until(lambda: "no such adapter" in service.log, 5))
        self.assertEqual((self.script.opened, self.script.sent), (0, []))

    def test_a_port_that_cannot_be_opened_keeps_beacons_off_and_retries(self) -> None:
        attempts = [0]

        def make(wire: Wire) -> Endpoint:
            attempts[0] += 1
            if attempts[0] < 3:
                raise OSError(10048, "address in use")
            return ScriptedEndpoint(wire, self.script)

        service = self.run_service(make_endpoint=make)
        self.assertTrue(wait_until(lambda: self.script.opened == 1, 5))
        self.assertIn("address in use", service.log)


class ReceivingTest(PlainHomeCase):
    def verifier(self, outcome: Exception | None = None) -> tuple[list[tuple[str, list[str]]], Callable[..., str]]:
        calls: list[tuple[str, list[str]]] = []

        def verify(_home: str, address: str, device_ids: list[str], **_options: Any) -> str:
            calls.append((address, list(device_ids)))
            if outcome is not None:
                raise outcome
            return device_ids[0]

        return calls, verify

    def test_many_valid_beacons_for_one_device_and_address_cause_one_tls_attempt_per_window(self) -> None:
        self.in_hub()
        calls, verify = self.verifier(NetClientError("refused"))
        self.run_service(verify=verify, reconnect_window_s=0.4)
        self.assertTrue(wait_until(lambda: self.script.opened == 1, 5))
        for _ in range(30):
            self.script.incoming.put(member_datagram())
        self.assertTrue(wait_until(lambda: self.script.incoming.empty(), 5))
        time.sleep(0.2)
        self.assertEqual(calls, [(NEW, [BRAVO])])
        self.script.incoming.put(member_datagram(host="192.168.1.78"))
        self.assertTrue(wait_until(lambda: len(calls) == 2, 5))
        time.sleep(0.5)
        self.script.incoming.put(member_datagram())
        self.assertTrue(wait_until(lambda: len(calls) == 3, 5))

    def test_the_stored_address_changes_only_after_the_pinned_tls_check_passes(self) -> None:
        self.in_hub()
        calls, verify = self.verifier()
        service = self.run_service(verify=verify)
        self.assertTrue(wait_until(lambda: self.script.opened == 1, 5))
        self.script.incoming.put(member_datagram())
        self.assertTrue(wait_until(lambda: address_of(self.home, BRAVO) == NEW, 5))
        self.assertEqual(calls, [(NEW, [BRAVO])])
        self.assertTrue(wait_until(lambda: "changed to" in service.log, 5))
        self.assertTrue(read_presence(self.home)[BRAVO].online)

    def test_a_failed_tls_check_keeps_the_stored_address(self) -> None:
        self.in_hub()
        calls, verify = self.verifier(NetClientError("the peer's certificate does not match its pinned certificate"))
        service = self.run_service(verify=verify)
        self.assertTrue(wait_until(lambda: self.script.opened == 1, 5))
        self.script.incoming.put(member_datagram())
        self.assertTrue(wait_until(lambda: len(calls) == 1 and "the address is kept" in service.log, 5))
        self.assertEqual(address_of(self.home, BRAVO), STORED)

    def test_beacons_that_fail_the_checks_start_no_tls_attempt(self) -> None:
        self.in_hub()
        calls, verify = self.verifier()
        service = self.run_service(verify=verify)
        self.assertTrue(wait_until(lambda: self.script.opened == 1, 5))
        now = int(time.time())
        other_hub = "fedcba9876543210fedcba9876543210"
        for datagram in (
            member_datagram(key="cd" * 32),
            member_datagram(ts=now - 500),
            member_datagram(hub=other_hub),
            member_datagram(device=CHARLIE),
            member_datagram(host="10.9.9.9"),
        ):
            self.script.incoming.put(datagram)
        self.assertTrue(wait_until(lambda: self.script.incoming.empty(), 5))
        time.sleep(0.3)
        self.assertEqual(calls, [])
        self.assertIn("MAC", service.log)

    def test_beacons_with_invented_device_ids_from_one_host_write_one_log_line(self) -> None:
        self.in_hub()
        service = self.run_service()
        self.assertTrue(wait_until(lambda: self.script.opened == 1, 5))
        for number in range(30):
            self.script.incoming.put(member_datagram(device=format_digest(number.to_bytes(32, "big"))))
        self.assertTrue(wait_until(lambda: self.script.incoming.empty(), 5))
        time.sleep(0.3)
        self.assertEqual(service.log.count("ignored a member beacon"), 1)

    def test_rejected_beacons_from_many_hosts_write_a_bounded_number_of_log_lines(self) -> None:
        self.in_hub()
        service = self.run_service()
        self.assertTrue(wait_until(lambda: self.script.opened == 1, 5))
        for number in range(3, 60):
            self.script.incoming.put(member_datagram(device=format_digest(number.to_bytes(32, "big")), host=f"192.168.1.{number + 100}"))
        self.assertTrue(wait_until(lambda: self.script.incoming.empty(), 5))
        time.sleep(0.3)
        self.assertEqual(service.log.count("ignored a member beacon"), REMOTE_NOTES_PER_INTERVAL)

    def test_a_damaged_hub_file_is_logged_once_and_the_service_keeps_sending_join_beacons(self) -> None:
        self.in_hub()
        path = hub_state_path(self.home)
        body = json.loads(path.read_text(encoding="utf-8"))
        body["pending"] = [{"to": BRAVO, "op": "remove", "device": "x", "atMs": 1}]
        path.write_text(json.dumps(body), encoding="utf-8")
        service = self.run_service(interval_s=0.05)
        self.assertTrue(wait_until(lambda: len(self.script.sent) >= 3, 5), service.log)
        self.assertEqual(service.log.count("member beacons are off until the hub files can be read"), 1)
        self.assertTrue(service.thread.is_alive())
        self.assertTrue(all(isinstance(announcement, JoinAnnouncement) for announcement in parsed(self.script)))

    def test_a_beacon_under_the_old_hub_key_is_ignored_after_the_key_changes(self) -> None:
        self.in_hub()
        calls, verify = self.verifier(NetClientError("refused"))
        self.run_service(verify=verify, reconnect_window_s=0.0)
        self.assertTrue(wait_until(lambda: self.script.opened == 1, 5))
        self.script.incoming.put(member_datagram(ts=int(time.time())))
        self.assertTrue(wait_until(lambda: len(calls) == 1, 5))
        write_hub_state(self.home, HubState(HUB_ID, "cd" * 32))
        self.script.incoming.put(member_datagram(ts=int(time.time()) + 1))
        self.assertTrue(wait_until(lambda: self.script.incoming.empty(), 5))
        time.sleep(0.3)
        self.assertEqual(len(calls), 1)
        self.script.incoming.put(member_datagram(key="cd" * 32, ts=int(time.time()) + 2))
        self.assertTrue(wait_until(lambda: len(calls) == 2, 5))

    def test_a_beacon_from_the_stored_address_marks_the_member_online_without_a_tls_attempt(self) -> None:
        self.in_hub()
        calls, verify = self.verifier()
        self.run_service(verify=verify)
        self.assertTrue(wait_until(lambda: self.script.opened == 1, 5))
        self.script.incoming.put(member_datagram(host=STORED))
        self.assertTrue(wait_until(lambda: BRAVO in read_presence(self.home), 5))
        self.assertEqual((calls, read_presence(self.home)[BRAVO].online), ([], True))

    def test_a_destination_that_fails_the_lan_check_is_not_dialed(self) -> None:
        self.in_hub()
        calls, verify = self.verifier()

        def refuse(address: str, _port: int, _interface: str, _prefix: int) -> str:
            raise ValueError(f"{address} is not allowed")

        service = self.run_service(verify=verify, check_destination=refuse)
        self.assertTrue(wait_until(lambda: self.script.opened == 1, 5))
        self.script.incoming.put(member_datagram())
        self.assertTrue(wait_until(lambda: "is not used" in service.log, 5))
        self.assertEqual(calls, [])

    def test_beacons_are_not_read_while_the_gate_is_closed(self) -> None:
        self.in_hub()
        calls, verify = self.verifier()
        self.run_service(lambda: BeaconState(False, True), verify=verify)
        self.script.incoming.put(member_datagram())
        time.sleep(0.3)
        self.assertEqual((calls, self.script.opened), ([], 0))


class ReconnectorTest(unittest.TestCase):
    def test_it_limits_attempts_per_device_and_address_and_skips_a_device_already_in_flight(self) -> None:
        now = [0.0]
        pending: list[Callable[[], None]] = []
        home = os.path.join(make_root(self), "home")

        def verify(_home: str, _address: str, _ids: list[str], **_options: Any) -> str:
            raise NetClientError("no")

        reconnector = Reconnector(home, lambda _text: None, window_s=60.0, verify=verify, run=pending.append, clock=lambda: now[0])
        self.assertTrue(reconnector.consider(BRAVO, NEW))
        self.assertFalse(reconnector.consider(BRAVO, NEW))
        self.assertFalse(reconnector.consider(BRAVO, "192.168.1.78"))
        pending.pop()()
        self.assertFalse(reconnector.consider(BRAVO, NEW))
        self.assertTrue(reconnector.consider(BRAVO, "192.168.1.78"))
        pending.pop()()
        now[0] = 59.0
        self.assertFalse(reconnector.consider(BRAVO, NEW))
        now[0] = 61.0
        self.assertTrue(reconnector.consider(BRAVO, NEW))
        self.assertTrue(reconnector.consider(CHARLIE, NEW))
        self.assertEqual(len(pending), 2)

    def test_the_attempt_table_stays_bounded(self) -> None:
        reconnector = Reconnector(os.path.join(make_root(self), "home"), lambda _text: None, run=lambda task: None)
        for number in range(beaconsvc.MAX_ATTEMPTS + 50):
            reconnector.consider(f"device-{number}", f"192.168.1.{number % 250 + 1}")
            reconnector._inflight.clear()  # pyright: ignore[reportPrivateUsage]
        self.assertLessEqual(len(reconnector._attempts), beaconsvc.MAX_ATTEMPTS)  # pyright: ignore[reportPrivateUsage]


class RememberedPeersTest(PlainHomeCase):
    def members(self, count: int) -> list[str]:
        write_hub_state(self.home, HubState(HUB_ID, KEY))
        peers = tuple(PeerGrant(f"device-{n}", f"peer-{n}", f"192.168.1.{20 + n}", member=True) for n in range(count))
        save_policy(self.home, Policy(peers, 0, Hub(HUB_ID, "home")))
        return [p.device_id for p in peers]

    def test_each_member_gets_one_inventory_request_at_its_stored_address_and_the_result_is_recorded(self) -> None:
        devices = self.members(3)
        asked: list[tuple[str, str]] = []

        def request(home: str, device_id: str, op: str, body: dict[str, Any], **options: Any) -> dict[str, Any]:
            asked.append((device_id, op))
            if device_id == "device-1":
                raise NetClientError("cannot reach the peer")
            return {"ok": True}

        check_remembered(self.home, lambda _text: None, request=request)
        self.assertEqual(sorted(asked), [(d, "inventory") for d in devices])
        seen = read_presence(self.home)
        self.assertEqual({d: p.online for d, p in seen.items()}, {"device-0": True, "device-1": False, "device-2": True})
        self.assertEqual(seen["device-1"].address, "192.168.1.21")

    def test_the_requests_run_with_bounded_concurrency(self) -> None:
        self.members(12)
        lock = threading.Lock()
        active, peak = [0], [0]

        def request(*_args: Any, **_options: Any) -> dict[str, Any]:
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(0.05)
            with lock:
                active[0] -= 1
            return {"ok": True}

        check_remembered(self.home, lambda _text: None, request=request, workers=4)
        self.assertEqual(len(read_presence(self.home)), 12)
        self.assertLessEqual(peak[0], 4)
        self.assertGreater(peak[0], 1)

    def test_the_service_checks_remembered_addresses_when_the_gate_opens_and_hub_status_shows_the_result(self) -> None:
        self.members(2)
        asked: list[str] = []

        def request(_home: str, device_id: str, *_args: Any, **_options: Any) -> dict[str, Any]:
            asked.append(device_id)
            if device_id == "device-1":
                raise NetClientError("cannot reach the peer")
            return {"ok": True}

        gate = [False]
        self.run_service(lambda: BeaconState(gate[0], False), request=request)
        time.sleep(0.2)
        self.assertEqual(asked, [])
        gate[0] = True
        self.assertTrue(wait_until(lambda: len(asked) == 2, 5))
        self.assertTrue(wait_until(lambda: len(read_presence(self.home)) == 2, 5))
        lines: list[str] = []
        services = cli.Services(home=self.home, out=lines.append, err=lines.append)
        self.assertEqual(cli.main(["hub", "status"], services), 0)
        text = "\n".join(lines)
        self.assertRegex(text, r"peer-0 .*192\.168\.1\.20 +online")
        self.assertRegex(text, r"peer-1 .*192\.168\.1\.21 +offline")

    def test_a_stale_online_record_does_not_show_as_online(self) -> None:
        self.members(1)
        beaconsvc.record_presence(self.home, "device-0", True, "192.168.1.20", at_ms=1)
        lines: list[str] = []
        cli.main(["hub", "status"], cli.Services(home=self.home, out=lines.append, err=lines.append))
        self.assertRegex("\n".join(lines), r"peer-0 .*unknown")


class PendingHubChangesTest(PlainHomeCase):
    def test_queued_hub_changes_are_retried_on_the_interval_until_the_member_answers(self) -> None:
        self.in_hub()
        add_pending(self.home, [Pending(BRAVO, "rekey", "", int(time.time() * 1000))])
        calls: list[tuple[str, str]] = []

        def request(_home: str, device_id: str, op: str, _body: dict[str, Any], **_options: Any) -> dict[str, Any]:
            calls.append((device_id, op))
            if len(calls) < 3:
                raise NetClientError("cannot reach the peer")
            return {"ok": True, "body": {"epoch": 0, "key": KEY}}

        self.run_service(request=request, flush_interval_s=0.05)
        self.assertTrue(wait_until(lambda: len(calls) >= 3, 5), calls)
        self.assertTrue(
            wait_until(lambda: (read_hub_state(self.home) or HubState(HUB_ID, KEY, (), 0, (Pending(BRAVO, "rekey"),))).pending == (), 5)
        )
        self.assertEqual({call for call in calls if call[1] == "rekey"}, {(BRAVO, "rekey")})

    def test_nothing_is_sent_while_the_network_gate_is_closed(self) -> None:
        self.in_hub()
        add_pending(self.home, [Pending(BRAVO, "rekey", "", int(time.time() * 1000))])
        calls: list[str] = []
        self.run_service(
            lambda: BeaconState(False, False),
            request=lambda *args, **_options: calls.append(args[2]) or {"ok": True},
            flush_interval_s=0.02,
        )
        time.sleep(0.3)
        self.assertEqual(calls, [])


@NEEDS
class RealTlsTest(unittest.TestCase):
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
            for name in ("presence.json", "presence.json.lock"):
                with contextlib.suppress(OSError):
                    os.remove(os.path.join(home, "net", name))
        self.ingress_b = FakeIngress(self.b)
        self.addCleanup(self.ingress_b.close)
        self.ingress_c = FakeIngress(self.c)
        self.addCleanup(self.ingress_c.close)

    def form_hub(self, address_of_b: str = STORED) -> str:
        key = KEY
        for home, other, address in ((self.a, self.b, address_of_b), (self.b, self.a, "127.0.0.1")):
            write_hub_state(home, HubState(HUB_ID, key))
            save_policy(home, Policy((), 0, Hub(HUB_ID, "home")))
            save_peer(home, device_of(other), cert_of(other).read_text(encoding="ascii"), address, "peer", member=True)
        return key

    def start(self, home: str, ingress: FakeIngress) -> RunningListener:
        running = RunningListener(home, ingress.port).start()
        self.addCleanup(running.stop)
        return running

    def test_a_beacon_from_a_new_address_updates_the_address_once_pinned_tls_matches_the_certificate(self) -> None:
        self.form_hub()
        running_b = self.start(self.b, self.ingress_b)
        script = Script()
        service = Service(
            self,
            self.a,
            lambda: BeaconState(True, False),
            settings(script, LOOP, request=down, verify=_with_port(running_b.port), run=inline),
        )
        self.assertTrue(wait_until(lambda: script.opened == 1, 5))
        script.incoming.put(member_datagram(device=device_of(self.b), host="127.0.0.1"))
        self.assertTrue(wait_until(lambda: address_of(self.a, device_of(self.b)) == "127.0.0.1", 10), service.log)
        self.assertTrue(wait_until(lambda: "pinned TLS matched" in service.log, 5))
        self.assertTrue(read_presence(self.a)[device_of(self.b)].online)

    def test_a_forged_beacon_whose_address_answers_with_another_certificate_changes_nothing(self) -> None:
        self.form_hub()
        trust_peer(self.c, self.a, "alpha", "127.0.0.1")
        running_c = self.start(self.c, self.ingress_c)
        script = Script()
        service = Service(
            self,
            self.a,
            lambda: BeaconState(True, False),
            settings(script, LOOP, request=down, verify=_with_port(running_c.port), run=inline),
        )
        self.assertTrue(wait_until(lambda: script.opened == 1, 5))
        script.incoming.put(member_datagram(device=device_of(self.b), host="127.0.0.1"))
        self.assertTrue(wait_until(lambda: "the address is kept" in service.log, 10), service.log)
        self.assertIn("does not match its pinned certificate", service.log)
        self.assertEqual(address_of(self.a, device_of(self.b)), STORED)

    def test_a_forged_beacon_whose_address_answers_nothing_changes_nothing(self) -> None:
        self.form_hub()
        script = Script()
        service = Service(
            self, self.a, lambda: BeaconState(True, False), settings(script, LOOP, request=down, verify=_with_port(1), run=inline)
        )
        self.assertTrue(wait_until(lambda: script.opened == 1, 5))
        script.incoming.put(member_datagram(device=device_of(self.b), host="127.0.0.1"))
        self.assertTrue(wait_until(lambda: "the address is kept" in service.log, 10), service.log)
        self.assertEqual(address_of(self.a, device_of(self.b)), STORED)

    def test_the_address_change_reaches_the_hub_file_too(self) -> None:
        self.form_hub()
        save_peer(self.a, device_of(self.c), cert_of(self.c).read_text(encoding="ascii"), STORED, "charlie", member=True)
        write_hub_state(self.a, HubState(HUB_ID, KEY, (member_ref(self.c, "charlie", STORED),)))
        self.assertTrue(update_address(self.a, device_of(self.c), "192.168.1.99"))
        state = read_hub_state(self.a)
        assert state is not None
        self.assertEqual([(m.device_id, m.address) for m in state.members], [(device_of(self.c), "192.168.1.99")])
        self.assertEqual(address_of(self.a, device_of(self.c)), "192.168.1.99")
        self.assertFalse(update_address(self.a, device_of(self.c), "192.168.1.99"))
        self.assertFalse(update_address(self.a, CHARLIE, "192.168.1.99"))

    def test_remembered_addresses_are_tried_at_startup_over_real_pinned_tls(self) -> None:
        self.form_hub(address_of_b="127.0.0.1")
        running_b = self.start(self.b, self.ingress_b)
        check_remembered(self.a, lambda _text: None, port=running_b.port)
        self.assertTrue(read_presence(self.a)[device_of(self.b)].online)
        running_b.stop()
        check_remembered(self.a, lambda _text: None, port=running_b.port, timeout_s=1.0)
        self.assertFalse(read_presence(self.a)[device_of(self.b)].online)


@NEEDS
class ListenerBeaconTest(unittest.TestCase):
    a: str

    @classmethod
    def setUpClass(cls) -> None:
        cls.a = create_home(make_root(cls), "a")
        set_name(cls.a, "alpha")

    def setUp(self) -> None:
        reset(self.a)
        self.ingress = FakeIngress(self.a)
        self.addCleanup(self.ingress.close)
        patch = mock.patch.multiple(listener, PROFILE_REFRESH_S=0.0, MONITOR_S=0.05, CONFIG_RECHECK_S=0.05)
        patch.start()
        self.addCleanup(patch.stop)
        write_hub_state(self.a, HubState(HUB_ID, KEY))
        save_policy(self.a, Policy((), 0, Hub(HUB_ID, "home")))

    def start(self, script: Script, network: list[str | None], public: bool = False) -> tuple[Listener, Options, threading.Thread]:
        ready = threading.Event()
        options = Options(
            config=NetworkConfig(network="lan", bind="127.0.0.1", port=0, allowed_networks=("home-network",), discoverable=True),
            bind_check=lambda _address: True,
            network_check=lambda _bind: public,
            network_id=lambda _bind: network[0],
            join_port=0,
            join_timeouts=FAST,
            join_source_ok=lambda _address: True,
            notice=lambda _text: None,
            server_port=self.ingress.port,
            ready=lambda _port: ready.set(),
            beacons=settings(script, LOOP),
        )
        instance = Listener(self.a, options)
        code: list[int] = []
        thread = threading.Thread(target=lambda: (code.append(instance.run()), ready.set()), daemon=True)
        thread.start()
        self.addCleanup(lambda: (options.stop.set(), thread.join(10)))
        self.assertTrue(ready.wait(20))
        self.code = code
        return instance, options, thread

    def test_the_listener_sends_beacons_only_while_the_current_network_is_allowed(self) -> None:
        script = Script()
        network: list[str | None] = ["home-network"]
        self.start(script, network)
        self.assertTrue(wait_until(lambda: script.opened == 1 and len(script.sent) >= 2, 10))
        network[0] = "other-network"
        self.assertTrue(wait_until(lambda: script.closed == 1, 10))
        sent = len(script.sent)
        time.sleep(0.4)
        self.assertEqual(len(script.sent), sent)
        network[0] = "home-network"
        self.assertTrue(wait_until(lambda: script.opened == 2 and len(script.sent) > sent, 10))
        network[0] = None
        self.assertTrue(wait_until(lambda: script.closed == 2, 10))

    def test_a_listener_on_a_public_network_never_starts_and_so_never_opens_beacons(self) -> None:
        script = Script()
        self.start(script, ["home-network"], public=True)
        time.sleep(0.3)
        self.assertEqual((self.code, script.opened, script.sent), ([1], 0, []))

    def test_the_listener_stops_the_beacon_thread_with_its_stop_event(self) -> None:
        script = Script()
        _instance, options, thread = self.start(script, ["home-network"])
        self.assertTrue(wait_until(lambda: script.opened == 1, 10))
        options.stop.set()
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(script.closed, 1)

    def test_without_beacon_settings_the_listener_opens_no_endpoint(self) -> None:
        self.assertIsNone(Options().beacons)


def down(*_args: Any, **_options: Any) -> dict[str, Any]:
    raise NetClientError("down")


def address_of(home: str, device: str) -> str | None:
    peer = grant_for(load_policy(home), device)
    return None if peer is None else peer.address


def _with_port(port: int) -> Callable[..., str]:
    from ide_agent_tabs.net.client import verify_pinned

    def verify(home: str, address: str, device_ids: list[str], **options: Any) -> str:
        options["port"] = port
        return verify_pinned(home, address, device_ids, **options)

    return verify


def member_ref(home: str, label: str, address: str) -> Any:
    import ssl

    from ide_agent_tabs.net.pairing import MemberRef, canonical_pem

    pem, _der = canonical_pem(ssl.DER_cert_to_PEM_cert(ssl.PEM_cert_to_DER_cert(cert_of(home).read_text(encoding="ascii"))))
    return MemberRef(device_of(home), label, address, 47829, pem)


if __name__ == "__main__":
    unittest.main()

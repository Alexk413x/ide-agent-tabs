from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import queue
import socket
import threading
import time
import unittest
from collections.abc import Callable
from dataclasses import replace
from typing import Any
from unittest import mock

from ide_agent_tabs.net import beacon
from ide_agent_tabs.net.beacon import (
    BROADCAST,
    MAX_DATAGRAM,
    MULTICAST,
    BeaconError,
    Datagram,
    Endpoint,
    Heard,
    HubView,
    HubWatch,
    Inbox,
    JoinAnnouncement,
    MemberBeacon,
    MemberCheck,
    Solicitation,
    Wire,
    build_join,
    build_member,
    build_solicit,
    mac_is_valid,
    parse_datagram,
    wire_for,
)
from ide_agent_tabs.net.discovery import LanDiscovery, source_label
from ide_agent_tabs.net.hubstate import HubState, write_hub_state
from ide_agent_tabs.net.identity import format_digest
from ide_agent_tabs.net.policy import Hub, PeerGrant, Policy, save_policy
from net_support import make_root, wait_until

HUB_ID = "0123456789abcdef0123456789abcdef"
OTHER_HUB_ID = "fedcba9876543210fedcba9876543210"
KEY = "ab" * 32
OTHER_KEY = "cd" * 32
NOW = 1_800_000_000
STORED = "192.168.1.20"
SUBNET = ipaddress.IPv4Network("192.168.1.0/24")
WIRE = Wire("192.168.1.10", "192.168.1.255", SUBNET)


def device(name: str) -> str:
    return format_digest(hashlib.sha256(name.encode()).digest())


ALPHA, BRAVO, CHARLIE = device("alpha"), device("bravo"), device("charlie")


def free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class FakeSocket:
    def __init__(self, network: FakeNetwork) -> None:
        self.network = network
        self.bound: tuple[str, int] | None = None
        self.joined = False
        self.multicast_if: str | None = None
        self.inbox: queue.Queue[tuple[bytes, tuple[str, int]]] = queue.Queue()
        self.timeout = 0.1
        self.closed = False

    def setsockopt(self, level: int, option: int, value: Any) -> None:
        if option == socket.IP_ADD_MEMBERSHIP:
            self.joined = True
        elif option == socket.IP_MULTICAST_IF:
            self.multicast_if = socket.inet_ntoa(value)

    def bind(self, address: tuple[str, int]) -> None:
        host, port = address
        self.bound = (host, port or self.network.ephemeral())

    def settimeout(self, value: float) -> None:
        self.timeout = value

    def sendto(self, data: bytes, address: tuple[str, int]) -> None:
        assert self.bound is not None
        source = self.multicast_if or self.bound[0]
        self.network.deliver(self, data, address, (source, self.bound[1]))

    def recvfrom(self, size: int) -> tuple[bytes, tuple[str, int]]:
        try:
            return self.inbox.get(timeout=self.timeout)
        except queue.Empty:
            raise socket.timeout("timed out") from None

    def close(self) -> None:
        self.closed = True


class FakeNetwork:
    def __init__(self, wire: Wire) -> None:
        self.wire = wire
        self.sockets: list[FakeSocket] = []
        self.dropped: set[str] = set()
        self.sent: list[tuple[str, bytes]] = []
        self._ports = 40000

    def ephemeral(self) -> int:
        self._ports += 1
        return self._ports

    def socket(self, *_args: Any) -> FakeSocket:
        sock = FakeSocket(self)
        self.sockets.append(sock)
        return sock

    def listening(self) -> int:
        return sum(1 for s in self.sockets if s.joined and not s.closed)

    def deliver(self, sender: FakeSocket, data: bytes, destination: tuple[str, int], source: tuple[str, int]) -> None:
        host, port = destination
        kind = MULTICAST if host == self.wire.group else BROADCAST if host == self.wire.broadcast else "unicast"
        self.sent.append((kind, data))
        if kind in self.dropped:
            return
        for sock in self.sockets:
            if sock.closed or sock.bound is None or sock.bound[1] != port or sock is sender:
                continue
            if kind == MULTICAST and not sock.joined:
                continue
            sock.inbox.put((data, source))


def sender_wire(address: str = "192.168.1.20") -> Wire:
    return replace(WIRE, bind=address)


class AnnouncementFormatTest(unittest.TestCase):
    def test_a_join_announcement_carries_exactly_the_planned_fields(self) -> None:
        data = build_join("alpha", ALPHA, HUB_ID, "home", announcement_id="ab" * 16)
        self.assertEqual(
            json.loads(data),
            {
                "v": 1,
                "kind": "join",
                "id": "ab" * 16,
                "deviceName": "alpha",
                "deviceId": ALPHA,
                "hubId": HUB_ID,
                "hubName": "home",
                "port": 47830,
                "transport": "lan",
            },
        )
        self.assertEqual(list(json.loads(data)), ["v", "kind", "id", "deviceName", "deviceId", "hubId", "hubName", "port", "transport"])

    def test_a_join_announcement_without_a_hub_has_null_hub_fields(self) -> None:
        fields = json.loads(build_join("alpha", ALPHA, None, None))
        self.assertEqual((fields["hubId"], fields["hubName"]), (None, None))

    def test_a_join_announcement_whitelist_leaves_out_secrets_paths_and_names(self) -> None:
        text = build_join("alpha", ALPHA, HUB_ID, "home").decode("ascii")
        for forbidden in ("token", "key", "path", "session", "user", "folder", "pem", "mac", "ts"):
            self.assertNotIn(f'"{forbidden}', text)
        self.assertEqual(set(json.loads(text)), {"v", "kind", "id", "deviceName", "deviceId", "hubId", "hubName", "port", "transport"})

    def test_a_member_beacon_carries_the_planned_fields_and_a_mac_over_the_rest(self) -> None:
        data = build_member(KEY, HUB_ID, ALPHA, NOW, announcement_id="cd" * 16)
        fields = json.loads(data)
        self.assertEqual(list(fields), ["v", "kind", "id", "hubId", "deviceId", "port", "ts", "mac"])
        self.assertEqual(
            (fields["kind"], fields["hubId"], fields["deviceId"], fields["port"], fields["ts"]), ("member", HUB_ID, ALPHA, 47829, NOW)
        )
        self.assertRegex(fields["mac"], r"[0-9a-f]{64}")
        parsed = parse_datagram(data)
        assert isinstance(parsed, MemberBeacon)
        self.assertTrue(mac_is_valid(KEY, parsed))
        self.assertFalse(mac_is_valid(OTHER_KEY, parsed))
        self.assertFalse(mac_is_valid(KEY, replace(parsed, ts=NOW + 1)))
        self.assertFalse(mac_is_valid(KEY, replace(parsed, device_id=BRAVO)))
        self.assertFalse(mac_is_valid(KEY, replace(parsed, port=47830)))

    def test_announcements_fit_one_datagram(self) -> None:
        self.assertLess(len(build_join("a" * 32, ALPHA, HUB_ID, "h" * 32)), MAX_DATAGRAM)
        self.assertLess(len(build_member(KEY, HUB_ID, ALPHA, NOW)), MAX_DATAGRAM)

    def test_parse_reads_back_what_the_builders_wrote(self) -> None:
        join = parse_datagram(build_join("alpha", ALPHA, HUB_ID, "home", announcement_id="ab" * 16))
        self.assertEqual(join, JoinAnnouncement("ab" * 16, "alpha", ALPHA, HUB_ID, "home"))
        without_hub = parse_datagram(build_join("alpha", ALPHA, None, None))
        assert isinstance(without_hub, JoinAnnouncement)
        self.assertEqual(without_hub.device_id, ALPHA)

    def good(self, **changes: Any) -> bytes:
        fields = json.loads(build_join("alpha", ALPHA, HUB_ID, "home"))
        fields.update(changes)
        return json.dumps({k: v for k, v in fields.items() if v is not KeyError}).encode()

    def test_parse_rejects_every_malformed_form(self) -> None:
        member = json.loads(build_member(KEY, HUB_ID, ALPHA, NOW))
        cases: dict[str, bytes] = {
            "empty": b"",
            "not json": b"{",
            "a list": b"[]",
            "oversize": b" " * (MAX_DATAGRAM + 1),
            "NaN": self.good(port=float("nan")),
            "unknown field": self.good(extra=1),
            "missing field": self.good(hubName=KeyError),
            "version 2": self.good(v=2),
            "version true": self.good(v=True),
            "kind other": self.good(kind="other"),
            "bad id": self.good(id="ab"),
            "uppercase id": self.good(id="AB" * 16),
            "bad device id": self.good(deviceId="not-an-id"),
            "device id lowercase": self.good(deviceId=ALPHA.lower()),
            "bad name": self.good(deviceName="a/b"),
            "empty name": self.good(deviceName=""),
            "hub id without a name": self.good(hubName=None),
            "hub name without an id": self.good(hubId=None),
            "bad hub id": self.good(hubId="xyz"),
            "bad hub name": self.good(hubName="Home Net"),
            "port zero": self.good(port=0),
            "port true": self.good(port=True),
            "port large": self.good(port=70000),
            "port string": self.good(port="47830"),
            "transport": self.good(transport="wan"),
            "member bad mac": json.dumps({**member, "mac": "zz"}).encode(),
            "member short mac": json.dumps({**member, "mac": "ab" * 31}).encode(),
            "member ts string": json.dumps({**member, "ts": "1"}).encode(),
            "member ts negative": json.dumps({**member, "ts": -1}).encode(),
            "member ts bool": json.dumps({**member, "ts": True}).encode(),
            "member no hub": json.dumps({**member, "hubId": None}).encode(),
            "member with join fields": json.dumps({**member, "deviceName": "alpha"}).encode(),
        }
        for name, data in cases.items():
            with self.subTest(name), self.assertRaises(BeaconError):
                parse_datagram(data)

    def test_parse_rejects_a_repeated_key(self) -> None:
        text = build_join("alpha", ALPHA, None, None).decode().replace('"port":47830', '"port":47830,"port":47831')
        with self.assertRaises(BeaconError):
            parse_datagram(text.encode())

    def test_a_tampered_member_beacon_fails_the_mac_not_the_parser(self) -> None:
        fields = json.loads(build_member(KEY, HUB_ID, ALPHA, NOW))
        fields["deviceId"] = BRAVO
        parsed = parse_datagram(json.dumps(fields).encode())
        assert isinstance(parsed, MemberBeacon)
        self.assertFalse(mac_is_valid(KEY, parsed))


def beacon_from(key: str = KEY, hub_id: str = HUB_ID, device_id: str = BRAVO, ts: int = NOW, port: int = 47829) -> MemberBeacon:
    parsed = parse_datagram(build_member(key, hub_id, device_id, ts, port))
    assert isinstance(parsed, MemberBeacon)
    return parsed


class MemberCheckTest(unittest.TestCase):
    def check(self, key: str | None = KEY, now: int = NOW) -> MemberCheck:
        view = HubView(HUB_ID, "home", key, {BRAVO: STORED})
        return MemberCheck(lambda: view, lambda: float(now))

    def test_a_valid_beacon_passes_and_reports_the_stored_address(self) -> None:
        verdict = self.check().check(beacon_from())
        self.assertEqual((verdict.ok, verdict.stored_address), (True, STORED))

    def test_a_bad_mac_is_ignored(self) -> None:
        verdict = self.check().check(beacon_from(key=OTHER_KEY))
        self.assertFalse(verdict.ok)
        self.assertIn("MAC", verdict.reason)

    def test_a_beacon_for_another_hub_is_ignored(self) -> None:
        verdict = self.check().check(beacon_from(hub_id=OTHER_HUB_ID))
        self.assertEqual((verdict.ok, "another hub" in verdict.reason), (False, True))

    def test_a_timestamp_outside_the_window_is_ignored_on_both_sides(self) -> None:
        for offset in (-121, 121, -3600, 3600):
            with self.subTest(offset):
                self.assertFalse(self.check().check(beacon_from(ts=NOW + offset)).ok)
        for offset in (-120, 0, 120):
            with self.subTest(offset):
                self.assertTrue(self.check().check(beacon_from(ts=NOW + offset)).ok)

    def test_a_beacon_older_than_the_last_accepted_one_from_that_device_is_ignored(self) -> None:
        check = self.check()
        self.assertTrue(check.check(beacon_from(ts=NOW)).ok)
        older = check.check(beacon_from(ts=NOW - 5))
        self.assertEqual((older.ok, "older" in older.reason), (False, True))
        self.assertTrue(check.check(beacon_from(ts=NOW + 5)).ok)

    def test_a_beacon_from_a_device_that_is_not_a_member_is_ignored(self) -> None:
        verdict = self.check().check(beacon_from(device_id=CHARLIE))
        self.assertEqual((verdict.ok, "not a known member" in verdict.reason), (False, True))

    def test_a_machine_without_a_hub_or_a_key_ignores_everything(self) -> None:
        self.assertFalse(MemberCheck(lambda: None, lambda: float(NOW)).check(beacon_from()).ok)
        self.assertFalse(self.check(key=None).check(beacon_from()).ok)


class HubWatchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.home = os.path.join(make_root(self), "home")
        os.makedirs(self.home)
        write_hub_state(self.home, HubState(HUB_ID, KEY))
        save_policy(self.home, Policy((PeerGrant(BRAVO, "bravo", STORED, member=True),), 0, Hub(HUB_ID, "home")))

    def test_it_reads_the_hub_the_key_and_the_members(self) -> None:
        view = HubWatch(self.home)()
        self.assertEqual(view, HubView(HUB_ID, "home", KEY, {BRAVO: STORED}))

    def test_a_beacon_under_the_old_key_is_ignored_after_the_key_changes(self) -> None:
        watch = HubWatch(self.home)
        check = MemberCheck(watch, lambda: float(NOW))
        self.assertTrue(check.check(beacon_from(key=KEY, ts=NOW)).ok)
        write_hub_state(self.home, HubState(HUB_ID, OTHER_KEY))
        stale = check.check(beacon_from(key=KEY, ts=NOW + 1))
        self.assertEqual((stale.ok, "MAC" in stale.reason), (False, True))
        self.assertTrue(check.check(beacon_from(key=OTHER_KEY, ts=NOW + 2)).ok)

    def test_a_missing_or_foreign_hub_file_leaves_the_key_empty(self) -> None:
        write_hub_state(self.home, HubState(OTHER_HUB_ID, OTHER_KEY))
        view = HubWatch(self.home)()
        assert view is not None
        self.assertIsNone(view.key)

    def test_a_machine_with_no_hub_has_no_view(self) -> None:
        save_policy(self.home, Policy())
        self.assertIsNone(HubWatch(self.home)())


class SolicitationFormatTest(unittest.TestCase):
    def test_a_solicitation_carries_only_the_version_kind_and_id(self) -> None:
        data = build_solicit("44" * 16)
        self.assertEqual(json.loads(data), {"v": 1, "kind": "solicit", "id": "44" * 16})
        self.assertEqual(parse_datagram(data), Solicitation("44" * 16))

    def test_a_solicitation_with_another_field_or_a_bad_id_is_refused(self) -> None:
        for value in (
            {"v": 1, "kind": "solicit", "id": "44" * 16, "deviceId": BRAVO},
            {"v": 1, "kind": "solicit", "id": "short"},
            {"v": 1, "kind": "solicit"},
        ):
            with self.assertRaises(BeaconError, msg=value):
                parse_datagram(json.dumps(value).encode())


class InboxTest(unittest.TestCase):
    def datagram(self, data: bytes, host: str = "192.168.1.20", port: int = 50000) -> Datagram:
        return Datagram(data, host, port)

    def test_it_accepts_one_copy_per_announcement_id_and_merges_the_paths(self) -> None:
        inbox = Inbox(WIRE, ALPHA)
        data = build_join("bravo", BRAVO, None, None, announcement_id="11" * 16)
        first = inbox.accept(self.datagram(data, port=WIRE.port))
        assert first is not None and isinstance(first.announcement, JoinAnnouncement)
        self.assertEqual((first.host, first.path, first.announcement.device_id), ("192.168.1.20", MULTICAST, BRAVO))
        self.assertIsNone(inbox.accept(self.datagram(data, port=50000)))
        self.assertEqual(inbox.paths_for("11" * 16), frozenset({MULTICAST, BROADCAST}))

    def test_a_datagram_from_outside_the_subnet_or_from_this_machine_is_dropped(self) -> None:
        inbox = Inbox(WIRE, ALPHA)
        data = build_join("bravo", BRAVO, None, None)
        self.assertIsNone(inbox.accept(self.datagram(data, host="10.0.0.5")))
        self.assertIsNone(inbox.accept(self.datagram(data, host=WIRE.bind)))
        self.assertIsNone(inbox.accept(self.datagram(data, host="not an address")))

    def test_this_machines_own_device_id_is_dropped(self) -> None:
        self.assertIsNone(Inbox(WIRE, BRAVO).accept(self.datagram(build_join("bravo", BRAVO, None, None))))

    def test_datagrams_from_this_machine_are_counted_apart_from_the_rest(self) -> None:
        inbox = Inbox(WIRE, BRAVO)
        inbox.accept(self.datagram(build_join("bravo", BRAVO, None, None)))
        inbox.accept(self.datagram(build_join("charlie", CHARLIE, None, None), host=WIRE.bind))
        inbox.accept(self.datagram(build_solicit(), host=WIRE.bind))
        inbox.accept(self.datagram(build_join("charlie", CHARLIE, None, None), host="10.0.0.5"))
        self.assertEqual((inbox.own, inbox.rejected), (3, 0))

    def test_a_solicitation_from_another_machine_is_accepted_once_across_both_paths(self) -> None:
        inbox = Inbox(WIRE, ALPHA)
        data = build_solicit("33" * 16)
        heard = inbox.accept(self.datagram(data, port=WIRE.port))
        assert heard is not None
        self.assertEqual(heard.announcement, Solicitation("33" * 16))
        self.assertIsNone(inbox.accept(self.datagram(data)))

    def test_garbage_and_oversize_datagrams_are_counted_and_dropped(self) -> None:
        inbox = Inbox(WIRE, ALPHA)
        for data in (b"hello", b"{}", b"x" * (MAX_DATAGRAM + 1)):
            self.assertIsNone(inbox.accept(self.datagram(data)))
        self.assertEqual(inbox.rejected, 3)

    def test_the_seen_table_stays_bounded(self) -> None:
        inbox = Inbox(WIRE, ALPHA)
        with mock.patch.object(beacon, "MAX_SEEN", 8):
            for number in range(40):
                inbox.accept(self.datagram(build_join("bravo", BRAVO, None, None, announcement_id=f"{number:032x}")))
        self.assertLessEqual(len(inbox._seen), 8)  # pyright: ignore[reportPrivateUsage]

    def test_an_id_is_accepted_again_after_it_expires(self) -> None:
        now = [0.0]
        inbox = Inbox(WIRE, ALPHA, clock=lambda: now[0])
        data = build_join("bravo", BRAVO, None, None, announcement_id="22" * 16)
        self.assertIsNotNone(inbox.accept(self.datagram(data)))
        self.assertIsNone(inbox.accept(self.datagram(data)))
        now[0] = beacon.SEEN_TTL_S + 1
        self.assertIsNotNone(inbox.accept(self.datagram(data)))


class WireTest(unittest.TestCase):
    def test_wire_for_derives_the_broadcast_address(self) -> None:
        wire = wire_for("192.168.1.35", 24)
        self.assertEqual(
            (wire.broadcast, str(wire.subnet), wire.group, wire.port), ("192.168.1.255", "192.168.1.0/24", "239.255.47.31", 47831)
        )
        self.assertEqual(wire_for("10.1.2.3", 16).broadcast, "10.1.255.255")

    def test_an_unknown_prefix_counts_as_a_24(self) -> None:
        self.assertEqual(wire_for("192.168.7.9", 32).broadcast, "192.168.7.255")


class EndpointOptionsTest(unittest.TestCase):
    def test_the_endpoint_sets_the_multicast_interface_ttl_group_and_broadcast_options(self) -> None:
        network = FakeNetwork(WIRE)
        endpoint = Endpoint(WIRE, make_socket=network.socket)
        self.addCleanup(endpoint.close)
        receiver, broadcaster = network.sockets
        self.assertTrue(endpoint.joined)
        self.assertEqual(
            (receiver.multicast_if, receiver.bound, broadcaster.bound),
            (WIRE.bind, (WIRE.bind if os.name == "nt" else "", WIRE.port), (WIRE.bind, 40001)),
        )

    def test_a_failed_group_join_is_noted_and_the_endpoint_still_receives_broadcasts(self) -> None:
        network = FakeNetwork(WIRE)

        class NoGroup(FakeSocket):
            def setsockopt(self, level: int, option: int, value: Any) -> None:
                if option == socket.IP_ADD_MEMBERSHIP:
                    raise OSError(19, "No such device")
                super().setsockopt(level, option, value)

        endpoint = Endpoint(WIRE, make_socket=lambda *_a: NoGroup(network))  # pyright: ignore[reportArgumentType]
        self.addCleanup(endpoint.close)
        self.assertFalse(endpoint.joined)
        self.assertTrue(any("joining the group" in note for note in endpoint.notes))


def announce_join(
    make_endpoint: Callable[[Wire], Endpoint],
    wire: Wire,
    device_id: str,
    name: str,
    hub_id: str | None = None,
    hub_name: str | None = None,
    port: int = 47830,
) -> None:
    endpoint = make_endpoint(wire)
    try:
        endpoint.send(build_join(name, device_id, hub_id, hub_name, port))
    finally:
        endpoint.close()


def announcements(network: Any) -> list[tuple[str, bytes]]:
    return [(kind, data) for kind, data in network.sent if b'"solicit"' not in data]


def listen_for(receiver: LanDiscovery, seconds: float) -> list[Heard]:
    deadline = time.monotonic() + seconds
    return [heard for heard in receiver.listen(lambda: time.monotonic() >= deadline) if heard is not None]


class FakePathDiscoveryTest(unittest.TestCase):
    def listen(self, *, multicast: bool = True, broadcast: bool = True, dropped: tuple[str, ...] = ()) -> Any:
        network = FakeNetwork(WIRE)
        network.dropped.update(dropped)
        found: list[tuple[str, str, str, str, str]] = []

        def collect() -> None:
            for heard in listen_for(receiver, 1.0):
                announcement = heard.announcement
                assert isinstance(announcement, JoinAnnouncement)
                found.append(
                    (
                        announcement.device_id,
                        heard.host,
                        announcement.device_name,
                        source_label(receiver.paths_for(announcement.id)),
                        "join",
                    )
                )

        receiver = LanDiscovery(WIRE, ALPHA, make_endpoint=lambda wire: Endpoint(wire, make_socket=network.socket), poll_s=0.05)
        thread = threading.Thread(target=collect, daemon=True)
        thread.start()
        self.assertTrue(wait_until(lambda: network.listening() == 1, 5))
        announce_join(
            lambda wire: Endpoint(wire, multicast=multicast, broadcast=broadcast, make_socket=network.socket),
            sender_wire(),
            BRAVO,
            "bravo",
        )
        thread.join(10)
        return found, network

    def test_discovery_works_over_multicast_alone(self) -> None:
        found, network = self.listen(broadcast=False)
        self.assertEqual(found, [(BRAVO, "192.168.1.20", "bravo", "multicast", "join")])
        self.assertEqual([kind for kind, data in announcements(network)], [MULTICAST])

    def test_discovery_works_over_broadcast_alone(self) -> None:
        found, network = self.listen(multicast=False)
        self.assertEqual([(device_id, source) for device_id, _host, _name, source, _kind in found], [(BRAVO, "broadcast")])
        self.assertEqual([kind for kind, data in announcements(network)], [BROADCAST])

    def test_both_paths_give_one_row_that_names_both(self) -> None:
        found, network = self.listen()
        self.assertEqual([(device_id, source) for device_id, _host, _name, source, _kind in found], [(BRAVO, "multicast+broadcast")])
        self.assertEqual(sorted(kind for kind, _data in announcements(network)), [BROADCAST, MULTICAST])
        self.assertEqual(len({data for _kind, data in announcements(network)}), 1)

    def test_a_network_that_drops_multicast_still_delivers_the_broadcast_copy(self) -> None:
        found, _network = self.listen(dropped=(MULTICAST,))
        self.assertEqual([(device_id, source) for device_id, _host, _name, source, _kind in found], [(BRAVO, "broadcast")])

    def test_a_network_that_drops_broadcast_still_delivers_the_multicast_copy(self) -> None:
        found, _network = self.listen(dropped=(BROADCAST,))
        self.assertEqual([(device_id, source) for device_id, _host, _name, source, _kind in found], [(BRAVO, "multicast")])

    def test_a_find_solicits_at_start_and_once_more_and_yields_no_solicitation(self) -> None:
        network = FakeNetwork(WIRE)
        receiver = LanDiscovery(WIRE, ALPHA, make_endpoint=lambda wire: Endpoint(wire, make_socket=network.socket), poll_s=0.05)
        deadline = [time.monotonic() + 0.5]
        with mock.patch("ide_agent_tabs.net.discovery.SOLICIT_AGAIN_S", 0.2):
            heard = [h for h in receiver.listen(lambda: time.monotonic() >= deadline[0]) if h is not None]
        solicits = [parse_datagram(data) for _kind, data in network.sent if b'"solicit"' in data]
        self.assertEqual(len(solicits), 4)
        self.assertEqual(len({item.id for item in solicits if isinstance(item, Solicitation)}), 2)
        self.assertEqual((receiver.solicits_sent, heard), (2, []))

    def test_a_find_that_does_not_solicit_sends_nothing(self) -> None:
        network = FakeNetwork(WIRE)
        receiver = LanDiscovery(
            WIRE, ALPHA, make_endpoint=lambda wire: Endpoint(wire, make_socket=network.socket), poll_s=0.05, solicit=False
        )
        listen_for(receiver, 0.3)
        self.assertEqual((network.sent, receiver.solicits_sent), ([], 0))

    def test_a_network_that_drops_both_delivers_nothing(self) -> None:
        found, _network = self.listen(dropped=(MULTICAST, BROADCAST))
        self.assertEqual(found, [])

    def test_a_receiver_that_cannot_bind_reports_the_error_and_finds_nothing(self) -> None:
        def refuse(_wire: Wire) -> Endpoint:
            raise OSError(10048, "address in use")

        receiver = LanDiscovery(WIRE, ALPHA, make_endpoint=refuse)
        self.assertEqual(listen_for(receiver, 0.2), [])
        self.assertEqual(receiver.error, "address in use")

    def test_the_announcement_a_receiver_sees_is_the_join_announcement(self) -> None:
        network = FakeNetwork(WIRE)
        seen: list[bytes] = []
        listener = Endpoint(WIRE, make_socket=network.socket)
        self.addCleanup(listener.close)
        announce_join(lambda wire: Endpoint(wire, make_socket=network.socket), sender_wire(), BRAVO, "bravo", HUB_ID, "home")
        datagram = listener.recv(1.0)
        assert datagram is not None
        seen.append(datagram.data)
        self.assertEqual(
            parse_datagram(seen[0]),
            JoinAnnouncement(parse_datagram(seen[0]).id, "bravo", BRAVO, HUB_ID, "home"),
        )


def extra_loopback_addresses() -> bool:
    for address in ("127.0.0.2", "127.0.0.3"):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.bind((address, 0))
        except OSError:
            return False
    return True


@unittest.skipUnless(extra_loopback_addresses(), "needs 127.0.0.2 and 127.0.0.3 on loopback; macOS assigns only 127.0.0.1")
class RealSocketTest(unittest.TestCase):
    def test_the_two_copies_leave_from_different_ports_and_one_receiver_merges_them(self) -> None:
        port = free_udp_port()
        raw = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(raw.close)
        raw.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        raw.bind(("127.0.0.2", port))
        raw.settimeout(2)
        wire = Wire("127.0.0.3", "127.0.0.2", ipaddress.IPv4Network("127.0.0.0/8"), group="127.0.0.2", port=port)
        endpoint = Endpoint(wire)
        self.addCleanup(endpoint.close)
        sent = endpoint.send(build_join("bravo", BRAVO, None, None, announcement_id="33" * 16))
        self.assertEqual(sent.sent, (MULTICAST, BROADCAST))
        inbox = Inbox(replace(wire, bind="127.0.0.250"), ALPHA)
        heard = []
        for _ in range(2):
            data, address = raw.recvfrom(2048)
            heard.append(inbox.accept(Datagram(data, address[0], address[1])))
        self.assertEqual(sorted(h.path for h in heard if h is not None), [MULTICAST])
        self.assertEqual(inbox.paths_for("33" * 16), frozenset({MULTICAST, BROADCAST}))

    def test_a_real_receiver_reads_a_datagram_and_ignores_a_stray_one(self) -> None:
        port = free_udp_port()
        wire = Wire("127.0.0.2", None, ipaddress.IPv4Network("127.0.0.0/8"), group="127.0.0.2", port=port)
        endpoint = Endpoint(wire, multicast=False, broadcast=False)
        self.addCleanup(endpoint.close)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            sender.sendto(b"junk", ("127.0.0.2", port))
            sender.sendto(build_join("bravo", BRAVO, None, None), ("127.0.0.2", port))
        inbox = Inbox(replace(wire, bind="127.0.0.250"), ALPHA)
        found = []
        for _ in range(2):
            datagram = endpoint.recv(2.0)
            assert datagram is not None
            heard = inbox.accept(datagram)
            if heard is not None:
                found.append(heard)
        self.assertEqual([(h.announcement.device_id, h.path) for h in found], [(BRAVO, BROADCAST)])
        self.assertEqual(inbox.rejected, 1)

    def test_a_receive_with_nothing_waiting_times_out_with_none(self) -> None:
        wire = Wire("127.0.0.2", None, ipaddress.IPv4Network("127.0.0.0/8"), port=free_udp_port())
        endpoint = Endpoint(wire, multicast=False, broadcast=False)
        self.addCleanup(endpoint.close)
        self.assertIsNone(endpoint.recv(0.05))


MULTICAST_INTERFACE = os.environ.get("AGENT_TABS_TEST_MULTICAST")


@unittest.skipUnless(
    MULTICAST_INTERFACE,
    "set AGENT_TABS_TEST_MULTICAST to the address of a real LAN interface (for example 192.168.1.35) to send real multicast and broadcast",
)
class RealInterfaceTest(unittest.TestCase):
    def test_a_real_interface_delivers_both_copies_to_a_receiver_on_the_same_interface(self) -> None:
        assert MULTICAST_INTERFACE is not None
        wire = wire_for(MULTICAST_INTERFACE, int(os.environ.get("AGENT_TABS_TEST_PREFIX", "24")), free_udp_port())
        receiver = Endpoint(wire)
        sender = Endpoint(wire)
        self.addCleanup(receiver.close)
        self.addCleanup(sender.close)
        self.assertTrue(receiver.joined, receiver.notes)
        other = str(wire.subnet.broadcast_address - 1)
        inbox = Inbox(replace(wire, bind=other), ALPHA)
        sent = sender.send(build_join("bravo", BRAVO, None, None, announcement_id="44" * 16))
        self.assertEqual((sent.sent, sent.errors), ((MULTICAST, BROADCAST), ()))
        for _ in range(4):
            datagram = receiver.recv(1.0)
            if datagram is not None:
                inbox.accept(datagram)
        self.assertEqual(inbox.paths_for("44" * 16), frozenset({MULTICAST, BROADCAST}))


if __name__ == "__main__":
    unittest.main()

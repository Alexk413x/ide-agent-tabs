from __future__ import annotations

import ipaddress
import json
import socket
import threading
import time
import unittest
from typing import Any
from unittest import mock

from ide_agent_tabs.net import find, listener, pairing
from ide_agent_tabs.net.beacon import Datagram, Endpoint, Wire, build_join, build_member, wire_for
from ide_agent_tabs.net.find import (
    NOT_FOUND_TEXT,
    FindRequest,
    FindResult,
    FindSettings,
    FoundDevice,
    duplicate_name_warnings,
    find_devices,
    lan_hosts,
    not_found_text,
)
from ide_agent_tabs.net.hubjoin import local_pair_identity
from ide_agent_tabs.net.hubstate import HubState, write_hub_state
from ide_agent_tabs.net.listener import Listener, Options
from ide_agent_tabs.net.peers import save_peer
from ide_agent_tabs.net.policy import Hub, NetworkConfig, Policy, load_policy, save_policy
from net_support import FakeIngress, cert_of, create_home, device_of, make_root, wait_until
from test_net_beacon import ALPHA, BRAVO, HUB_ID, KEY
from test_net_beaconsvc import Script, ScriptedEndpoint
from test_net_hub_cli import BIND, PEER, HubCliCase
from test_net_join import FAST, NEEDS, JoinServer, make_hub, reset, set_name

LOOPBACK_NET = ipaddress.IPv4Network("127.0.0.0/8")
SCRIPT_WIRE = Wire("127.0.0.9", None, LOOPBACK_NET)
ALLOWED = "home-network"
STALE = "127.0.0.77"


class Served:
    def __init__(self, test: unittest.TestCase, home: str, *, discoverable: bool = True, timeouts: pairing.Timeouts = FAST) -> None:
        self.home = home
        self.ingress = FakeIngress(home)
        test.addCleanup(self.ingress.close)
        self.ready = threading.Event()
        self.port = 0
        self.options = Options(
            config=NetworkConfig(network="lan", bind="127.0.0.1", port=0, allowed_networks=(ALLOWED,), discoverable=discoverable),
            bind_check=lambda _address: True,
            network_check=lambda _bind: False,
            network_id=lambda _bind: ALLOWED,
            join_port=0,
            join_timeouts=timeouts,
            join_source_ok=lambda _address: True,
            notice=lambda _text: None,
            server_port=self.ingress.port,
            ready=self._ready,
        )
        self.listener = Listener(home, self.options)
        self.thread = threading.Thread(target=self.listener.run, daemon=True)
        self.thread.start()
        test.addCleanup(self.stop)
        assert self.ready.wait(20), "the listener did not start"
        if discoverable:
            assert wait_until(lambda: self.listener.join_port() is not None, 10), "the listener opened no join port"

    def _ready(self, port: int) -> None:
        self.port = port
        self.ready.set()

    @property
    def join_port(self) -> int:
        port = self.listener.join_port()
        assert port is not None
        return port

    def stop(self) -> None:
        self.options.stop.set()
        self.thread.join(10)


class FindCase(unittest.TestCase):
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
        patch = mock.patch.multiple(listener, PROFILE_REFRESH_S=0.0, MONITOR_S=0.05, CONFIG_RECHECK_S=0.05)
        patch.start()
        self.addCleanup(patch.stop)
        self.logs: list[str] = []
        self.script = Script()

    def run_find(self, served: Served | None = None, *, udp: bool = True, **changes: Any) -> FindResult:
        values: dict[str, Any] = {
            "listen_s": 1.2,
            "probe_after_s": 0.8,
            "grace_s": 5.0,
            "poll_s": 0.05,
            "probe_retry_s": 0.05,
            "timeouts": FAST,
            "hosts": lambda _wire: ["127.0.0.1"],
            "verify_timeout_s": 5.0,
        }
        if served is not None:
            values.update(member_port=served.port, join_port=served.join_port if served.listener.join_port() is not None else 1)
        if udp:
            values["make_endpoint"] = lambda wire: ScriptedEndpoint(wire, self.script)
        else:
            values["make_endpoint"] = refuse_udp
        values.update(changes)
        request = FindRequest(self.a, local_pair_identity(self.a), device_of(self.a), SCRIPT_WIRE, self.logs.append)
        return find_devices(request, FindSettings(**values))

    def announce(
        self, home: str, *, host: str = "127.0.0.1", port: int = 47831, claimed: str | None = None, hub: tuple[str, str] | None = None
    ) -> None:
        data = build_join(device_name_of(home), claimed or device_of(home), hub[0] if hub else None, hub[1] if hub else None)
        self.script.incoming.put(Datagram(data, host, port))


def device_name_of(home: str) -> str:
    from ide_agent_tabs.net.names import device_name_of as name

    return name(home)


def refuse_udp(_wire: Wire) -> Endpoint:
    raise OSError("address in use")


def form_hub(a: str, b: str, address_of_b: str = "127.0.0.1") -> None:
    for home, other, address in ((a, b, address_of_b), (b, a, "127.0.0.1")):
        write_hub_state(home, HubState(HUB_ID, KEY))
        save_policy(home, Policy((), 0, Hub(HUB_ID, "home")))
        save_peer(home, device_of(other), cert_of(other).read_text(encoding="ascii"), address, "peer", member=True)


@NEEDS
class AnnouncementTest(FindCase):
    def test_an_announced_discoverable_device_is_listed_after_tls_verification(self) -> None:
        served = Served(self, self.b)
        self.announce(self.b)
        result = self.run_find(served)
        self.assertEqual(result.devices, (FoundDevice(device_of(self.b), "bravo", "127.0.0.1", None, False, "multicast"),))
        self.assertFalse(result.probed)
        self.assertEqual(result.heard, 1)

    def test_both_copies_of_one_announcement_name_both_paths_and_list_one_row(self) -> None:
        served = Served(self, self.b)
        data = build_join("bravo", device_of(self.b), None, None, announcement_id="aa" * 16)
        self.script.incoming.put(Datagram(data, "127.0.0.1", 47831))
        self.script.incoming.put(Datagram(data, "127.0.0.1", 50123))
        result = self.run_find(served)
        self.assertEqual([(d.device_id, d.source) for d in result.devices], [(device_of(self.b), "multicast+broadcast")])

    def test_the_row_shows_the_hub_name_that_the_device_proves_over_tls(self) -> None:
        make_hub(self.b, name="homehub")
        served = Served(self, self.b)
        self.announce(self.b, hub=("0123456789abcdef0123456789abcdef", "lying-name"))
        (row,) = self.run_find(served).devices
        self.assertEqual((row.hub_name, row.member), ("homehub", False))

    def test_a_forged_announcement_whose_device_id_does_not_match_the_tls_certificate_is_not_listed(self) -> None:
        served = Served(self, self.c)
        self.announce(self.c, claimed=device_of(self.b))
        result = self.run_find(served)
        self.assertEqual(result.devices, ())
        self.assertTrue(
            any("did not verify as a discoverable device" in line and "does not match the device id" in line for line in self.logs),
            self.logs,
        )

    def test_the_genuine_announcement_from_the_same_host_is_listed(self) -> None:
        served = Served(self, self.c)
        self.announce(self.c)
        self.assertEqual([d.device_name for d in self.run_find(served).devices], ["charlie"])

    def test_an_announcement_for_a_host_that_answers_nothing_is_not_listed(self) -> None:
        self.announce(self.b)
        result = self.run_find(None, join_port=1, member_port=1)
        self.assertEqual(result.devices, ())
        self.assertTrue(any("did not verify" in line for line in self.logs))

    def test_a_member_beacon_with_a_good_mac_lists_the_member_after_pinned_tls(self) -> None:
        form_hub(self.a, self.b, address_of_b=STALE)
        served = Served(self, self.b)
        self.script.incoming.put(Datagram(build_member(KEY, HUB_ID, device_of(self.b), int(time.time())), "127.0.0.1", 47831))
        result = self.run_find(served, hosts=lambda _wire: [])
        self.assertEqual(result.devices, (FoundDevice(device_of(self.b), "peer", "127.0.0.1", "home", True, "multicast"),))

    def test_a_member_beacon_with_a_bad_mac_or_for_another_hub_lists_nothing(self) -> None:
        form_hub(self.a, self.b, address_of_b=STALE)
        served = Served(self, self.b)
        now = int(time.time())
        for key, hub in (("cd" * 32, HUB_ID), (KEY, "fedcba9876543210fedcba9876543210")):
            self.script.incoming.put(Datagram(build_member(key, hub, device_of(self.b), now), "127.0.0.1", 47831))
        result = self.run_find(served, hosts=lambda _wire: [])
        self.assertEqual(result.devices, ())

    def test_a_beacon_that_names_another_address_is_verified_at_the_address_it_came_from(self) -> None:
        form_hub(self.a, self.b, address_of_b=STALE)
        served = Served(self, self.b)
        self.script.incoming.put(Datagram(build_member(KEY, HUB_ID, device_of(self.b), int(time.time())), "127.0.0.1", 47831))
        result = self.run_find(served, hosts=lambda _wire: [])
        (row,) = result.devices
        self.assertEqual(row.address, "127.0.0.1")
        self.assertEqual(load_policy(self.a).peers[0].address, STALE)


@NEEDS
class RememberedTest(FindCase):
    def test_a_member_at_its_stored_address_is_listed_as_remembered(self) -> None:
        form_hub(self.a, self.b)
        served = Served(self, self.b)
        result = self.run_find(served, udp=False)
        self.assertEqual(result.devices, (FoundDevice(device_of(self.b), "peer", "127.0.0.1", "home", True, "remembered"),))

    def test_a_member_that_moved_is_not_listed_at_the_stale_address(self) -> None:
        form_hub(self.a, self.b, address_of_b=STALE)
        served = Served(self, self.b)
        result = self.run_find(served, udp=False, hosts=lambda _wire: [], verify_timeout_s=1.0)
        self.assertEqual(result.devices, ())


@NEEDS
class ProbeTest(FindCase):
    def test_the_probe_finds_a_discoverable_device_when_udp_is_blocked(self) -> None:
        make_hub(self.b, name="homehub")
        served = Served(self, self.b)
        result = self.run_find(served, udp=False, hosts=lambda _wire: ["127.0.0.1", "127.0.0.5"])
        self.assertEqual(result.devices, (FoundDevice(device_of(self.b), "bravo", "127.0.0.1", "homehub", False, "probe"),))
        self.assertTrue(result.probed)
        self.assertEqual((result.heard, result.udp_error), (0, "address in use"))

    def test_the_probe_starts_after_the_wait_when_udp_is_open_but_silent(self) -> None:
        served = Served(self, self.b)
        started = time.monotonic()
        result = self.run_find(served, probe_after_s=0.5)
        self.assertTrue(result.probed)
        self.assertEqual([d.source for d in result.devices], ["probe"])
        self.assertGreaterEqual(time.monotonic() - started, 0.5 - 0.05)

    def test_the_probe_is_skipped_when_an_announcement_verified_in_time(self) -> None:
        served = Served(self, self.b)
        self.announce(self.b)
        result = self.run_find(served)
        self.assertFalse(result.probed)

    def test_the_probe_lists_a_member_that_moved_by_its_certificate(self) -> None:
        form_hub(self.a, self.b, address_of_b=STALE)
        served = Served(self, self.b)
        result = self.run_find(served, udp=False, verify_timeout_s=2.0)
        self.assertEqual(result.devices, (FoundDevice(device_of(self.b), "peer", "127.0.0.1", "home", True, "probe"),))

    def test_a_host_that_only_accepts_tcp_is_not_listed(self) -> None:
        servers: list[socket.socket] = []
        ports: list[int] = []
        for _ in range(2):
            server = socket.socket()
            server.bind(("127.0.0.1", 0))
            server.listen(4)
            servers.append(server)
            ports.append(server.getsockname()[1])
            self.addCleanup(server.close)

        def accept_and_hang_up(server: socket.socket) -> None:
            while True:
                try:
                    conn, _address = server.accept()
                except OSError:
                    return
                conn.close()

        for server in servers:
            threading.Thread(target=accept_and_hang_up, args=(server,), daemon=True).start()
        result = self.run_find(None, udp=False, member_port=ports[0], join_port=ports[1], verify_timeout_s=1.0)
        self.assertEqual(result.devices, ())
        self.assertTrue(any("did not verify" in line for line in self.logs))

    def test_a_join_port_that_throttles_the_first_connection_is_retried(self) -> None:
        slow = pairing.Timeouts(first_frame_s=2.0, handshake_s=10.0, consent_s=5.0, connect_s=2.0, min_interval_s=0.5)
        served = Served(self, self.b, timeouts=slow)
        result = self.run_find(served, udp=False, probe_retry_s=0.6)
        self.assertEqual([d.device_name for d in result.devices], ["bravo"])

    def test_the_search_ends_when_the_probe_and_every_verification_are_done_before_the_limit(self) -> None:
        started = time.monotonic()
        result = self.run_find(None, udp=False, listen_s=30.0, join_port=1, member_port=1)
        self.assertLess(time.monotonic() - started, 5.0)
        self.assertEqual((result.devices, result.probed), ((), True))

    def test_the_search_ends_by_the_listening_limit_when_nothing_answers(self) -> None:
        started = time.monotonic()
        result = self.run_find(None, listen_s=0.8, join_port=1, member_port=1)
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertEqual(result.devices, ())


class HostRangeTest(unittest.TestCase):
    def test_the_probe_covers_the_24_without_self_the_network_or_the_broadcast_address(self) -> None:
        hosts = lan_hosts(wire_for("192.168.1.35", 24))
        self.assertEqual(
            (len(hosts), "192.168.1.35" in hosts, "192.168.1.0" in hosts, "192.168.1.255" in hosts), (253, False, False, False)
        )

    def test_a_larger_subnet_probes_only_the_24_that_holds_this_machine(self) -> None:
        hosts = lan_hosts(wire_for("10.1.2.3", 16))
        self.assertEqual(len(hosts), 253)
        self.assertTrue(all(h.startswith("10.1.2.") for h in hosts))

    def test_a_smaller_subnet_probes_only_its_own_hosts(self) -> None:
        hosts = lan_hosts(wire_for("192.168.1.67", 26))
        self.assertEqual((hosts[0], hosts[-1], len(hosts)), ("192.168.1.65", "192.168.1.126", 61))

    def test_the_probe_runs_with_bounded_concurrency_and_a_short_connect_timeout(self) -> None:
        lock = threading.Lock()
        active, peak, timeouts = [0], [0], set()

        def check(_host: str, _port: int, timeout: float) -> bool:
            timeouts.add(timeout)
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(0.01)
            with lock:
                active[0] -= 1
            return False

        script = Script()
        with mock.patch.object(find, "load_policy", lambda _home: Policy()):
            probe = find._Find(  # pyright: ignore[reportPrivateUsage]
                FindRequest("unused", mock.Mock(), ALPHA, wire_for("192.168.1.35", 24)),
                FindSettings(port_is_open=check, make_endpoint=lambda wire: ScriptedEndpoint(wire, script)),
            )
            probe.probe(time.monotonic() + 60)
        self.assertEqual(timeouts, {0.3})
        self.assertLessEqual(peak[0], 64)
        self.assertGreater(peak[0], 8)

    def test_the_probe_opens_no_connection_after_the_listening_limit(self) -> None:
        calls: list[str] = []
        with mock.patch.object(find, "load_policy", lambda _home: Policy()):
            probe = find._Find(  # pyright: ignore[reportPrivateUsage]
                FindRequest("unused", mock.Mock(), ALPHA, wire_for("192.168.1.35", 24)),
                FindSettings(port_is_open=lambda host, _port, _timeout: calls.append(host) or False),
            )
            probe.probe(time.monotonic() - 1)
        self.assertEqual(calls, [])


class MessageTest(unittest.TestCase):
    def test_the_not_found_text_names_the_causes_client_isolation_and_tailscale(self) -> None:
        text = not_found_text(FindResult(probed=True), wire_for("192.168.1.35", 24))
        for expected in (
            "not discoverable",
            "Public",
            "network.allowedNetworks",
            "UDP rule for port 47831",
            "different subnets",
            "client isolation",
            "all traffic between devices",
            "no LAN method works there",
            "Tailscale",
        ):
            self.assertIn(expected, text)
        self.assertIn(NOT_FOUND_TEXT, text)

    def test_the_not_found_text_reports_a_udp_socket_that_could_not_open(self) -> None:
        text = not_found_text(FindResult(udp_error="address in use"), wire_for("192.168.1.35", 24))
        self.assertIn("could not be received on UDP port 47831 (address in use)", text)

    def test_two_devices_with_one_name_give_a_warning_line(self) -> None:
        rows = (
            FoundDevice(ALPHA, "laptop", "192.168.1.20", None, False, "probe"),
            FoundDevice(BRAVO, "Laptop", "192.168.1.21", None, False, "probe"),
            FoundDevice("C" * 56, "desktop", "192.168.1.22", None, False, "probe"),
        )
        (warning,) = duplicate_name_warnings(rows)
        self.assertIn('2 devices are named "laptop"', warning)
        self.assertIn("192.168.1.21", warning)
        self.assertEqual(duplicate_name_warnings(rows[:1] + rows[2:]), ())


class FindCliCase(HubCliCase):
    def setUp(self) -> None:
        super().setUp()
        self.found: list[FoundDevice] = []
        self.requests: list[FindRequest] = []
        self.warnings: tuple[str, ...] = ()
        self.h = self.harness(self.a, find=self.fake_find)

    def serve(self, home: str, address: str = PEER) -> JoinServer:
        server = JoinServer(self, home)
        self.targets[address] = server.port
        return server

    def fake_find(self, request: FindRequest) -> FindResult:
        self.requests.append(request)
        return FindResult(tuple(self.found), self.warnings, probed=not self.found, heard=len(self.found))


@NEEDS
class FindCommandTest(FindCliCase):
    def test_find_lists_each_verified_device_on_one_row_with_every_column(self) -> None:
        self.found = [
            FoundDevice(device_of(self.b), "bravo", PEER, "home", True, "multicast+broadcast"),
            FoundDevice(device_of(self.c), "charlie", "192.168.50.21", None, False, "probe"),
        ]
        self.assertEqual(self.h.run("find"), 0, self.h.errors)
        text = self.h.text
        self.assertIn(f"bravo  {device_of(self.b)}  {PEER}  hub: home  member: yes  source: multicast+broadcast", text)
        self.assertIn(f"charlie  {device_of(self.c)}  192.168.50.21  hub: no hub  member: no  source: probe", text)

    def test_find_asks_the_finder_for_the_bind_address_subnet_and_this_devices_identity(self) -> None:
        self.found = [FoundDevice(device_of(self.b), "bravo", PEER, None, False, "probe")]
        self.assertEqual(self.h.run("find"), 0)
        (request,) = self.requests
        self.assertEqual((request.device_id, request.wire.bind, str(request.wire.subnet)), (device_of(self.a), BIND, "192.168.50.0/24"))

    def test_find_json_prints_one_object_for_agents(self) -> None:
        self.found = [FoundDevice(device_of(self.b), "bravo", PEER, "home", True, "remembered")]
        self.warnings = ("warning: example",)
        self.assertEqual(self.h.run("find", "--json"), 0)
        self.assertEqual(len(self.h.out), 1)
        self.assertEqual(
            json.loads(self.h.out[0]),
            {
                "devices": [
                    {
                        "deviceName": "bravo",
                        "deviceId": device_of(self.b),
                        "address": PEER,
                        "hub": "home",
                        "member": True,
                        "source": "remembered",
                    }
                ],
                "warnings": ["warning: example"],
                "probed": False,
            },
        )

    def test_find_that_finds_nothing_exits_1_and_explains_client_isolation_and_tailscale(self) -> None:
        self.assertEqual(self.h.run("find"), 1)
        self.assertIn("client isolation", self.h.errors)
        self.assertIn("Tailscale", self.h.errors)
        self.assertNotIn("client isolation", self.h.text)

    def test_find_json_that_finds_nothing_carries_the_message(self) -> None:
        self.assertEqual(self.h.run("find", "--json"), 1)
        body = json.loads(self.h.out[-1])
        self.assertEqual(body["devices"], [])
        self.assertIn("client isolation", body["message"])
        self.assertIn("Tailscale", body["message"])

    def test_find_prints_the_duplicate_name_warning(self) -> None:
        self.found = [
            FoundDevice(device_of(self.b), "laptop", PEER, None, False, "probe"),
            FoundDevice(device_of(self.c), "laptop", "192.168.50.21", None, False, "probe"),
        ]
        self.warnings = duplicate_name_warnings(tuple(self.found))
        self.assertEqual(self.h.run("find"), 0)
        self.assertIn('2 devices are named "laptop"', self.h.text)

    def test_find_stops_on_a_public_network_before_looking(self) -> None:
        from ide_agent_tabs.net.interfaces import InterfaceError

        self.lan_error = InterfaceError("Ethernet is a Public network.")
        self.assertEqual(self.h.run("find"), 1)
        self.assertEqual(self.requests, [])

    def test_find_accepts_a_remote_origin_because_it_changes_nothing(self) -> None:
        self.remote.value = True
        self.found = [FoundDevice(device_of(self.b), "bravo", PEER, None, False, "probe")]
        self.assertEqual(self.h.run("find"), 0)


@NEEDS
class JoinResolveTest(FindCliCase):
    def test_hub_join_with_only_a_device_id_resolves_the_address_through_find(self) -> None:
        make_hub(self.b)
        self.serve(self.b, PEER)
        self.found = [FoundDevice(device_of(self.b), "bravo", PEER, "home", False, "multicast")]
        self.assertEqual(self.h.run("hub", "join", device_of(self.b)), 0, self.h.errors)
        self.assertEqual(self.dialed, [(PEER, 47830, device_of(self.b))])
        self.assertEqual(len(self.requests), 1)
        self.assertEqual([m.device_id for m in self.members(self.a)], [device_of(self.b)])

    def test_hub_join_with_an_address_does_not_search(self) -> None:
        make_hub(self.b)
        self.serve(self.b, PEER)
        self.assertEqual(self.h.run("hub", "join", device_of(self.b), "--address", PEER), 0, self.h.errors)
        self.assertEqual(self.requests, [])

    def test_hub_join_for_a_device_that_find_cannot_verify_saves_nothing_and_says_so(self) -> None:
        self.assertEqual(self.h.run("hub", "join", device_of(self.b)), 1)
        self.assertEqual((self.dialed, self.members(self.a)), ([], []))
        self.assertIn("no verified device with that id was found", self.h.errors)
        self.assertIn("client isolation", self.h.errors)
        self.assertIn("Tailscale", self.h.errors)

    def test_hub_join_does_not_dial_a_device_that_find_listed_under_another_id(self) -> None:
        self.found = [FoundDevice(device_of(self.c), "charlie", PEER, None, False, "probe")]
        self.assertEqual(self.h.run("hub", "join", device_of(self.b)), 1)
        self.assertEqual(self.dialed, [])

    def test_create_with_mixes_a_bare_device_id_and_a_device_at_address(self) -> None:
        self.serve(self.b, PEER)
        self.serve(self.c, "192.168.50.21")
        self.found = [FoundDevice(device_of(self.b), "bravo", PEER, None, False, "broadcast")]
        code = self.h.run("hub", "create", "--name", "home", "--with", device_of(self.b), "--with", f"{device_of(self.c)}@192.168.50.21")
        self.assertEqual(code, 0, self.h.errors)
        self.assertEqual(sorted(m.label for m in self.members(self.a)), ["bravo", "charlie"])
        self.assertEqual(len(self.requests), 1)

    def test_create_with_only_addresses_does_not_search(self) -> None:
        self.serve(self.b, PEER)
        code = self.h.run("hub", "create", "--with", f"{device_of(self.b)}@{PEER}")
        self.assertEqual(code, 0, self.h.errors)
        self.assertEqual(self.requests, [])


class DefaultsTest(unittest.TestCase):
    def test_the_default_timing_matches_the_plan(self) -> None:
        settings = FindSettings()
        self.assertEqual(
            (settings.listen_s, settings.probe_after_s, settings.probe_workers, settings.probe_connect_s), (10.0, 5.0, 64, 0.3)
        )
        self.assertEqual((settings.member_port, settings.join_port), (47829, 47830))


if __name__ == "__main__":
    unittest.main()

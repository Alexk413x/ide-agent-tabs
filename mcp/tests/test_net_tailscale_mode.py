from __future__ import annotations

import contextlib
import json
import os
import socket
import threading
import time
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest import mock

from ide_agent_tabs.net import dialguard, find, listener, pairing
from ide_agent_tabs.net.beacon import Wire, wire_for
from ide_agent_tabs.net.beaconsvc import Reconnector
from ide_agent_tabs.net.client import NetClientError, send_request, verify_pinned
from ide_agent_tabs.net.discovery import TAILSCALE, source_label
from ide_agent_tabs.net.find import FindRequest, FindSettings, find_devices, not_found_text
from ide_agent_tabs.net.hub_cli import PairDeps
from ide_agent_tabs.net.hubjoin import HubJoiner, local_pair_identity
from ide_agent_tabs.net.hubstate import HubState, write_hub_state
from ide_agent_tabs.net.hubsync import HubSync
from ide_agent_tabs.net.identity import format_digest
from ide_agent_tabs.net.interfaces import InterfaceError, InterfaceInfo
from ide_agent_tabs.net.listener import Listener, Options, read_listener
from ide_agent_tabs.net.pairing import JoinProbe, PairIdentity, PairingError, probe_join
from ide_agent_tabs.net.peers import update_address
from ide_agent_tabs.net.policy import Hub, NetworkConfig, PeerGrant, Policy, grant_for, load_network_config, load_policy, save_policy
from ide_agent_tabs.net.tailscale import OwnDevice, TailscaleClient, TailscaleDiscovery, TailscaleError
from ide_agent_tabs.net.transport import LanTransport, TailscaleTransport, command_transport, transport_for
from ide_agent_tabs.net.tsmembers import TailscaleMembers
from net_support import FakeIngress, create_home, device_of, make_root, reset_peers, trust_peer, wait_until
from test_net_beacon import HUB_ID, KEY
from test_net_beaconsvc import Script, address_of, inline, settings
from test_net_cli import NEEDS, CliTestCase, Harness
from test_net_hub_cli import HubCliCase
from test_net_join import FAST, make_hub, reset, set_name
from test_net_tailscale import BINARY, IP4, ME, OTHER, PEER_IP, SELF, SELF_IP, STATUS, FakeRunner, node, status_text, whois_key, whois_text

LOOPBACK = "127.0.0.1"
LAN_ADDRESS = "192.168.1.20"
TS_ADDRESS = "100.64.0.7"
TAILSCALE_INFO = InterfaceInfo("Tailscale", SELF_IP, 32, f"{SELF_IP}/32", "Public", False)


class FakeTransport:
    def __init__(self) -> None:
        self.ready_error: Exception | None = None
        self.owner_error: Exception | None = None
        self.connections: list[str] = []
        self.destinations: list[str] = []
        self.waits: list[float] = []

    def allows_bind(self, address: str) -> bool:
        return address == LOOPBACK

    def bind_address(self, config: NetworkConfig) -> str | None:
        return config.bind

    def candidates(self, addresses: Any, describe: Any) -> list[InterfaceInfo]:
        return []

    def check_destination(self, address: str, port: int, interface_address: str, prefix_len: int) -> str:
        self.destinations.append(address)
        return address

    def check_connection(self, address: str) -> None:
        self.connections.append(address)
        if self.owner_error is not None:
            raise self.owner_error

    def firewall_scope(self, info: InterfaceInfo) -> str:
        return "100.64.0.0/10"

    def require_ready(self, address: str, allow_virtual: bool = False) -> InterfaceInfo:
        if self.ready_error is not None:
            raise self.ready_error
        return TAILSCALE_INFO

    def wait_ready(
        self,
        address: str,
        timeout_s: float,
        allow_virtual: bool = False,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> InterfaceInfo:
        self.waits.append(timeout_s)
        return self.require_ready(address, allow_virtual)


class StrictRunner(FakeRunner):
    def __init__(self, answers: dict[tuple[str, ...], Any] | None = None) -> None:
        super().__init__(answers)
        self.now = [0.0]

    def __call__(self, argv: list[str], timeout_s: float) -> str:
        if tuple(argv[1:]) not in self.answers:
            raise TailscaleError(f"tailscale exited with code 1: no answer for {argv[1:]}")
        return super().__call__(argv, timeout_s)


def client_for(answers: dict[tuple[str, ...], Any]) -> tuple[TailscaleClient, StrictRunner]:
    runner = StrictRunner(answers)
    return TailscaleClient(runner, lambda: BINARY), runner


def status_answers(peers: dict[str, Any] | None = None) -> dict[tuple[str, ...], Any]:
    return {SELF: status_text(), IP4: f"{SELF_IP}\n", STATUS: status_text(peers or {}), whois_key(PEER_IP): whois_text()}


class ModeWiringTest(unittest.TestCase):
    def test_transport_for_returns_the_transport_of_each_mode(self) -> None:
        client, runner = client_for(status_answers())
        self.assertIsInstance(transport_for(NetworkConfig(network="lan"), client), LanTransport)
        self.assertEqual(runner.calls, [])
        built = transport_for(NetworkConfig(network="tailscale"), client)
        assert isinstance(built, TailscaleTransport)
        self.assertEqual(built.allowed_owner_ids, frozenset({ME}))
        self.assertIsNone(transport_for(NetworkConfig(), client))

    def test_command_transport_falls_back_to_lan_without_asking_tailscale(self) -> None:
        client, runner = client_for(status_answers())
        for network in ("off", "lan"):
            self.assertIsInstance(command_transport(NetworkConfig(network=network), client), LanTransport)
        self.assertEqual(runner.calls, [])
        self.assertIsInstance(command_transport(NetworkConfig(network="tailscale"), client), TailscaleTransport)

    def test_a_missing_tailscale_program_leaves_the_owner_set_empty_so_every_check_refuses(self) -> None:
        def missing() -> str:
            raise TailscaleError("no tailscale program")

        built = transport_for(NetworkConfig(network="tailscale"), TailscaleClient(lambda argv, timeout: "", missing))
        assert isinstance(built, TailscaleTransport)
        self.assertEqual(built.allowed_owner_ids, frozenset())
        with self.assertRaises(PairingError):
            built.check_destination(PEER_IP, 47830, SELF_IP, 10)


class DialGuardTest(unittest.TestCase):
    def guard(self, answers: dict[tuple[str, ...], Any], network: str = "tailscale") -> tuple[dialguard.DialGuard, StrictRunner]:
        client, runner = client_for(answers)
        guard = dialguard.DialGuard(lambda _home: (NetworkConfig(network=network), []), client, lambda: runner.now[0], 30.0)
        return guard, runner

    def test_lan_mode_never_runs_tailscale(self) -> None:
        guard, runner = self.guard(status_answers(), "lan")
        guard.check("home", LAN_ADDRESS, 47829)
        self.assertEqual(runner.calls, [])

    def test_an_own_device_passes_and_is_remembered_for_a_short_time(self) -> None:
        guard, runner = self.guard(status_answers())
        guard.check("home", PEER_IP, 47829)
        calls = len(runner.calls)
        guard.check("home", PEER_IP, 47829)
        self.assertEqual(len(runner.calls), calls)
        runner.now[0] = 31.0
        guard.check("home", PEER_IP, 47829)
        self.assertGreater(len(runner.calls), calls)

    def test_a_lan_address_a_foreign_device_and_a_dead_tailscale_are_refused(self) -> None:
        guard, _ = self.guard(status_answers())
        with self.assertRaises(PairingError):
            guard.check("home", LAN_ADDRESS, 47829)
        answers = status_answers()
        answers[whois_key(PEER_IP)] = whois_text(user=OTHER)
        guard, _ = self.guard(answers)
        with self.assertRaises(PairingError):
            guard.check("home", PEER_IP, 47829)
        guard, _ = self.guard({SELF: TailscaleError("stopped"), IP4: TailscaleError("stopped")})
        with self.assertRaises(PairingError):
            guard.check("home", PEER_IP, 47829)

    def test_verify_pinned_refuses_a_dial_the_guard_refuses_before_it_connects(self) -> None:
        home = os.path.join(make_root(self), "home")
        device = format_digest(bytes(range(32)))
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(dialguard.GUARD, "check", side_effect=PairingError("not an own device")))
            stack.enter_context(mock.patch.object(socket, "create_connection", side_effect=AssertionError("connected")))
            stack.enter_context(mock.patch("ide_agent_tabs.net.client.load_identity"))
            stack.enter_context(mock.patch("ide_agent_tabs.net.client.read_peer_pem", return_value="pem"))
            with self.assertRaisesRegex(NetClientError, "not an own device"):
                verify_pinned(home, LAN_ADDRESS, [device])


class SyncedHome(unittest.TestCase):
    def setUp(self) -> None:
        self.home = os.path.join(make_root(self), "home")
        write_hub_state(self.home, HubState(HUB_ID, KEY))
        save_policy(self.home, Policy((PeerGrant("device-b", "bravo", LAN_ADDRESS, member=True),), 0, Hub(HUB_ID, "home")))
        self.logs: list[str] = []
        self.verified: list[tuple[str, list[str]]] = []

    def members(self, devices: list[OwnDevice], verify: Callable[..., str]) -> TailscaleMembers:
        def request(*_args: Any, **_options: Any) -> dict[str, Any]:
            raise NetClientError("down")

        return TailscaleMembers(
            self.home, lambda: devices, self.logs.append, verify=verify, request=request, spawn=inline, clock=lambda: 0.0
        )


class MemberAddressTest(SyncedHome):
    def test_a_member_moves_to_its_tailscale_address_after_the_pinned_check_matches_its_device_id(self) -> None:
        def verify(_home: str, address: str, ids: list[str], **_options: Any) -> str:
            self.verified.append((address, ids))
            return "device-b"

        self.members([OwnDevice("bravo", TS_ADDRESS, True)], verify).move_addresses()
        self.assertEqual(self.verified, [(TS_ADDRESS, ["device-b"])])
        self.assertEqual(address_of(self.home, "device-b"), TS_ADDRESS)
        self.assertIn("pinned TLS matched", "\n".join(self.logs))

    def test_a_member_does_not_move_when_the_device_answering_has_another_device_id(self) -> None:
        def verify(*_args: Any, **_options: Any) -> str:
            raise NetClientError("the peer's certificate does not match its pinned certificate")

        self.members([OwnDevice("phone", TS_ADDRESS, True)], verify).move_addresses()
        self.assertEqual(address_of(self.home, "device-b"), LAN_ADDRESS)
        self.assertIn("phone at 100.64.0.7 is not a hub member", "\n".join(self.logs))

    def test_a_device_that_does_not_answer_changes_nothing_and_is_logged_once(self) -> None:
        def verify(*_args: Any, **_options: Any) -> str:
            raise NetClientError("cannot reach the peer")

        subject = self.members([OwnDevice("phone", TS_ADDRESS, True)], verify)
        subject.move_addresses()
        subject.move_addresses()
        self.assertEqual(address_of(self.home, "device-b"), LAN_ADDRESS)
        self.assertEqual(len(self.logs), 1)

    def test_a_device_already_stored_as_a_member_address_is_not_checked_again(self) -> None:
        update_address(self.home, "device-b", TS_ADDRESS)

        def verify(*_args: Any, **_options: Any) -> str:
            raise AssertionError("checked a known address")

        self.members([OwnDevice("bravo", TS_ADDRESS, True)], verify).move_addresses()

    def test_the_cycle_runs_once_a_minute(self) -> None:
        now = [0.0]
        scheduled: list[Callable[[], None]] = []
        subject = TailscaleMembers(self.home, list, self.logs.append, spawn=scheduled.append, clock=lambda: now[0], interval_s=60.0)
        self.assertTrue(subject.tick())
        self.assertFalse(subject.tick())
        now[0] = 59.0
        self.assertFalse(subject.tick())
        now[0] = 61.0
        self.assertTrue(subject.tick())
        self.assertEqual(len(scheduled), 2)

    def test_lan_mode_replaces_a_tailscale_address_with_the_lan_one_after_the_pinned_check(self) -> None:
        update_address(self.home, "device-b", TS_ADDRESS)
        checks: list[tuple[str, list[str]]] = []

        def verify(_home: str, address: str, ids: list[str], **_options: Any) -> str:
            checks.append((address, ids))
            return ids[0]

        Reconnector(self.home, self.logs.append, verify=verify, run=inline).consider("device-b", LAN_ADDRESS)
        self.assertEqual(checks, [(LAN_ADDRESS, ["device-b"])])
        self.assertEqual(address_of(self.home, "device-b"), LAN_ADDRESS)

    def test_lan_mode_keeps_the_tailscale_address_when_the_pinned_check_fails(self) -> None:
        update_address(self.home, "device-b", TS_ADDRESS)

        def verify(*_args: Any, **_options: Any) -> str:
            raise NetClientError("no")

        Reconnector(self.home, self.logs.append, verify=verify, run=inline).consider("device-b", LAN_ADDRESS)
        self.assertEqual(address_of(self.home, "device-b"), TS_ADDRESS)

    def test_a_hub_introduction_accepts_a_tailscale_address_only_in_tailscale_mode(self) -> None:
        sync = HubSync(self.home, self.logs.append, lambda _text: None, lambda: None)
        self.assertFalse(sync._address_allowed(TS_ADDRESS))  # pyright: ignore[reportPrivateUsage]
        Path(self.home, "config.json").write_text(json.dumps({"network": {"network": "tailscale", "bind": SELF_IP}}), encoding="utf-8")
        self.assertTrue(sync._address_allowed(TS_ADDRESS))  # pyright: ignore[reportPrivateUsage]
        self.assertTrue(sync._address_allowed(LAN_ADDRESS))
        self.assertFalse(sync._address_allowed("8.8.8.8"))


class TailscaleFindTest(unittest.TestCase):
    def setUp(self) -> None:
        self.home = os.path.join(make_root(self), "home")
        os.makedirs(self.home)
        self.probes: list[tuple[str, dict[str, Any]]] = []
        self.checked: list[str] = []
        self.notes: list[str] = []

    def request(self, peers: dict[str, Any], check_error: Exception | None = None) -> FindRequest:
        client, _ = client_for(status_answers(peers))

        def check(address: str) -> None:
            self.checked.append(address)
            if check_error is not None:
                raise check_error

        identity = PairIdentity("alpha", b"der", Path("cert"), Path("key"), lambda: b"")
        return FindRequest(
            self.home,
            identity,
            "own-device",
            wire_for(SELF_IP, 32),
            self.notes.append,
            TailscaleDiscovery(client, {ME}),
            check,
        )

    def settings(self, answering: set[str]) -> FindSettings:
        def probe(address: str, port: int, expected: str | None, identity: PairIdentity, **options: Any) -> JoinProbe:
            self.probes.append((address, options))
            if address not in answering:
                raise PairingError("the exchange ended")
            return JoinProbe(f"name-{address[-1]}", f"device-{address[-1]}", None)

        return FindSettings(
            listen_s=2.0,
            grace_s=1.0,
            probe_join=probe,
            port_is_open=lambda _host, port, _timeout: port == pairing.PAIRING_PORT,
            sleep=lambda _seconds: None,
        )

    def peers(self) -> dict[str, Any]:
        return {
            "a": node("laptop", ME, ["100.64.0.2"]),
            "b": node("phone", ME, ["100.64.0.3"]),
            "c": node("offline", ME, ["100.64.0.4"], online=False),
            "d": node("stranger", OTHER, ["100.64.0.5"]),
        }

    def test_it_lists_a_device_that_answers_the_probe_and_drops_one_that_does_not(self) -> None:
        result = find_devices(self.request(self.peers()), self.settings({"100.64.0.2"}))
        self.assertEqual([(d.address, d.device_id, d.source) for d in result.devices], [("100.64.0.2", "device-2", "tailscale")])
        self.assertEqual(result.warnings, ())
        self.assertIsNone(result.udp_error)

    def test_it_probes_only_the_users_online_devices_with_the_tailscale_transport(self) -> None:
        find_devices(self.request(self.peers()), self.settings({"100.64.0.2", "100.64.0.3"}))
        self.assertEqual(sorted({address for address, _ in self.probes}), ["100.64.0.2", "100.64.0.3"])
        self.assertEqual(sorted(self.checked), ["100.64.0.2", "100.64.0.3"])
        for _address, options in self.probes:
            self.assertEqual(options["transport"], "tailscale")

    def test_a_candidate_the_owner_check_refuses_is_not_probed(self) -> None:
        result = find_devices(
            self.request(self.peers(), TailscaleError("the device belongs to another user")), self.settings({"100.64.0.2"})
        )
        self.assertEqual((result.devices, self.probes), ((), []))

    def test_a_silent_candidate_is_not_reported_as_an_error(self) -> None:
        result = find_devices(self.request(self.peers()), self.settings(set()))
        self.assertEqual((result.devices, result.warnings), ((), ()))

    def test_the_source_label_is_tailscale(self) -> None:
        self.assertEqual(source_label({TAILSCALE}), "tailscale")

    def test_a_status_failure_is_named_in_the_not_found_text(self) -> None:
        client, _ = client_for({STATUS: TailscaleError("tailscale exited with code 1")})
        request = self.request({})
        request = FindRequest(
            request.home, request.identity, request.device_id, request.wire, request.log, TailscaleDiscovery(client, {ME}), request.check
        )
        result = find_devices(request, self.settings(set()))
        self.assertIn("tailscale exited with code 1", result.udp_error or "")
        text = not_found_text(result, request.wire, True)
        self.assertIn("Tailscale is not running", text)
        self.assertIn("tailscale exited with code 1", text)
        self.assertNotIn("UDP", text)

    def test_lan_not_found_text_is_unchanged_by_the_tailscale_flag_default(self) -> None:
        wire: Wire = wire_for("192.168.1.10", 24)
        result = find.FindResult()
        self.assertEqual(not_found_text(result, wire), not_found_text(result, wire, False))
        self.assertIn("UDP port", not_found_text(result, wire))


class TailscaleJoinServer:
    def __init__(
        self,
        test: unittest.TestCase,
        home: str,
        transport: str,
        check_connection: Callable[[str], None] | None = None,
    ) -> None:
        self.sock = socket.socket()
        self.sock.bind((LOOPBACK, 0))
        self.sock.listen(8)
        self.port: int = self.sock.getsockname()[1]
        self.stop = threading.Event()
        self.lines: list[str] = []
        self.results: list[pairing.PairResult] = []
        self.saved: list[tuple[str, str]] = []
        kwargs: dict[str, Any] = {
            "timeouts": FAST,
            "on_result": self.results.append,
            "allow_source": lambda _address: True,
            "log": self.lines.append,
            "transport": transport,
            "check_connection": check_connection,
        }
        self.thread = threading.Thread(
            target=pairing.serve_joins,
            args=(
                self.sock,
                local_pair_identity(home),
                HubJoiner(home, lambda label, address: self.saved.append((label, address))),
                pairing.Limits(home),
                self.stop,
            ),
            kwargs=kwargs,
            daemon=True,
        )
        self.thread.start()
        test.addCleanup(self.close)

    def close(self) -> None:
        self.stop.set()
        self.thread.join(5)
        self.sock.close()


@NEEDS
class TransportMismatchTest(unittest.TestCase):
    a: str
    b: str

    @classmethod
    def setUpClass(cls) -> None:
        root = make_root(cls)
        cls.a, cls.b = (create_home(root, name) for name in ("a", "b"))
        for home, name in ((cls.a, "alpha"), (cls.b, "bravo")):
            set_name(home, name)

    def setUp(self) -> None:
        reset(self.a)
        reset(self.b)
        make_hub(self.a)

    def dial(self, server: TailscaleJoinServer, transport: str) -> pairing.PairResult:
        return pairing.dial_join(
            LOOPBACK,
            server.port,
            device_of(self.a),
            local_pair_identity(self.b),
            HubJoiner(self.b),
            pairing.Limits(self.b),
            timeouts=FAST,
            transport=transport,
        )

    def test_a_tailscale_dialer_is_refused_by_a_lan_listener_and_both_modes_are_named(self) -> None:
        server = TailscaleJoinServer(self, self.a, "lan")
        dialed = self.dial(server, "tailscale")
        self.assertEqual(dialed.outcome, "failed")
        self.assertIn("this machine is in tailscale mode and the other machine is in lan mode", dialed.reason)
        self.assertTrue(wait_until(lambda: bool(server.results), 10))
        listened = server.results[0]
        self.assertEqual(listened.outcome, "failed")
        self.assertIn("this machine is in lan mode and the other machine is in tailscale mode", listened.reason)
        self.assertEqual(server.saved, [])
        self.assertIsNone(grant_for(load_policy(self.b), device_of(self.a)))
        self.assertIsNone(grant_for(load_policy(self.a), device_of(self.b)))

    def test_a_lan_dialer_is_refused_by_a_tailscale_listener_and_both_modes_are_named(self) -> None:
        server = TailscaleJoinServer(self, self.a, "tailscale")
        dialed = self.dial(server, "lan")
        self.assertEqual(dialed.outcome, "failed")
        self.assertIn("this machine is in lan mode and the other machine is in tailscale mode", dialed.reason)
        self.assertTrue(wait_until(lambda: bool(server.results), 10))
        self.assertIn("this machine is in tailscale mode and the other machine is in lan mode", server.results[0].reason)
        self.assertEqual(server.saved, [])

    def test_a_refusal_for_the_mode_does_not_count_toward_the_failure_lock(self) -> None:
        server = TailscaleJoinServer(self, self.a, "lan")
        for _ in range(4):
            self.dial(server, "tailscale")
        self.assertTrue(wait_until(lambda: len(server.results) == 4, 20))
        self.assertEqual(pairing.Limits(self.a).locked_addresses(), {})

    def test_two_tailscale_machines_join(self) -> None:
        server = TailscaleJoinServer(self, self.a, "tailscale")
        dialed = self.dial(server, "tailscale")
        self.assertEqual(dialed.outcome, "paired", dialed.reason)
        self.assertTrue(wait_until(lambda: bool(server.saved), 10))

    def test_a_probe_in_the_wrong_mode_fails_and_one_in_the_right_mode_answers(self) -> None:
        server = TailscaleJoinServer(self, self.a, "tailscale")
        with self.assertRaisesRegex(PairingError, "network-mode-mismatch"):
            probe_join(LOOPBACK, server.port, None, local_pair_identity(self.b), timeouts=FAST)
        found = probe_join(LOOPBACK, server.port, None, local_pair_identity(self.b), timeouts=FAST, transport="tailscale")
        self.assertEqual(found.device_id, device_of(self.a))

    def test_the_owner_check_closes_a_join_connection_and_logs_once(self) -> None:
        refusals: list[str] = []

        def refuse(address: str) -> None:
            refusals.append(address)
            raise TailscaleError("the device belongs to another user")

        server = TailscaleJoinServer(self, self.a, "tailscale", refuse)
        for _ in range(3):
            with self.assertRaises(PairingError):
                probe_join(LOOPBACK, server.port, None, local_pair_identity(self.b), timeouts=FAST, transport="tailscale")
        self.assertEqual(refusals, [LOOPBACK] * 3)
        owner_lines = [line for line in server.lines if "owner check" in line]
        self.assertEqual(len(owner_lines), 1)
        self.assertIn("the device belongs to another user", owner_lines[0])
        self.assertEqual(server.results, [])


@NEEDS
class TailscaleListenerTest(unittest.TestCase):
    a: str
    b: str

    @classmethod
    def setUpClass(cls) -> None:
        root = make_root(cls)
        cls.a, cls.b = (create_home(root, name) for name in ("a", "b"))
        for home, name in ((cls.a, "alpha"), (cls.b, "bravo")):
            set_name(home, name)

    def setUp(self) -> None:
        reset(self.a)
        reset(self.b)
        reset_peers(self.a)
        reset_peers(self.b)
        self.a_id = device_of(self.a)
        self.ingress = FakeIngress(self.a)
        self.addCleanup(self.ingress.close)
        patch = mock.patch.multiple(listener, PROFILE_REFRESH_S=0.0, MONITOR_S=0.05, CONFIG_RECHECK_S=0.05)
        patch.start()
        self.addCleanup(patch.stop)
        trust_peer(self.a, self.b, "laptop", LOOPBACK)
        trust_peer(self.b, self.a, "desktop")
        self.fake = FakeTransport()
        self.script = Script()
        self.ticks: list[Callable[[], None]] = []

    def start(self, **changes: Any) -> tuple[Listener, Options, int]:
        ready = threading.Event()
        ports: list[int] = []
        options = Options(
            config=NetworkConfig(network="tailscale", bind=LOOPBACK, port=0, discoverable=changes.pop("discoverable", False)),
            transport=self.fake,
            network_check=lambda _bind: True,
            join_port=0,
            join_timeouts=FAST,
            join_source_ok=lambda _address: True,
            notice=lambda _text: None,
            server_port=self.ingress.port,
            ready=lambda port: self.note_ready(ports, port, ready),
            beacons=settings(self.script, InterfaceInfo("loopback", "127.0.0.9", 8, "127.0.0.0/8", "Private", True)),
            members=TailscaleMembers(self.a, list, lambda _text: None, spawn=self.ticks.append),
            **changes,
        )
        instance = Listener(self.a, options)
        code: list[int] = []
        thread = threading.Thread(target=lambda: (code.append(instance.run()), ready.set()), daemon=True)
        thread.start()
        self.addCleanup(lambda: (options.stop.set(), thread.join(10)))
        self.assertTrue(ready.wait(20))
        self.assertEqual(code, [], "the listener stopped")
        return instance, options, ports[0]

    @staticmethod
    def note_ready(ports: list[int], port: int, ready: threading.Event) -> None:
        ports.append(port)
        ready.set()

    def request(self, port: int) -> dict[str, Any]:
        return send_request(self.b, self.a_id, "inventory", {}, timeout=5, port=port)

    def log(self) -> str:
        return Path(listener.log_file(self.a)).read_text(encoding="utf-8")

    def test_it_never_starts_beacons_or_sends_an_announcement(self) -> None:
        instance, _options, _port = self.start(discoverable=True)
        time.sleep(0.5)
        self.assertEqual((self.script.opened, self.script.sent), (0, []))
        self.assertIsNone(instance._beacons)  # pyright: ignore[reportPrivateUsage]

    def test_it_runs_the_member_address_cycle_instead_of_beacons(self) -> None:
        self.start()
        self.assertTrue(wait_until(lambda: bool(self.ticks), 5))

    def test_the_gate_is_open_exactly_when_the_transport_is_ready(self) -> None:
        instance, _options, port = self.start()
        self.assertTrue(wait_until(lambda: instance._gate.allowed, 5))  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(self.request(port)["ok"], True)
        self.fake.ready_error = InterfaceError("Tailscale is stopped")
        self.assertTrue(wait_until(lambda: not instance._gate.allowed, 5))  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(instance._gate.reason, "Tailscale is stopped")  # pyright: ignore[reportPrivateUsage]
        with self.assertRaises(NetClientError):
            self.request(port)
        self.fake.ready_error = None
        self.assertTrue(wait_until(lambda: instance._gate.allowed, 5))  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(self.request(port)["ok"], True)

    def test_allowed_networks_are_neither_read_nor_needed(self) -> None:
        instance, options, port = self.start()
        assert options.config is not None
        self.assertEqual(options.config.allowed_networks, ())
        self.assertTrue(wait_until(lambda: instance._gate.allowed, 5))  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(self.request(port)["ok"], True)

    def test_an_accepted_connection_that_fails_the_owner_check_is_closed_and_logged_once(self) -> None:
        _instance, _options, port = self.start()
        self.fake.owner_error = TailscaleError("Tailscale refuses laptop: the device belongs to another user")
        for _ in range(3):
            with self.assertRaises(NetClientError):
                self.request(port)
        self.assertEqual(self.ingress.seen, [])
        self.assertEqual(self.fake.connections[:3], [LOOPBACK] * 3)
        lines = [line for line in self.log().splitlines() if "owner check" in line]
        self.assertEqual(len(lines), 1)
        self.assertIn("the device belongs to another user", lines[0])
        self.fake.owner_error = None
        self.assertEqual(self.request(port)["ok"], True)

    def test_the_join_service_runs_in_the_mode_and_checks_the_owner(self) -> None:
        instance, _options, _port = self.start(discoverable=True)
        self.assertTrue(wait_until(lambda: instance.join_port() is not None, 10))
        join_port = instance.join_port()
        assert join_port is not None
        with self.assertRaisesRegex(PairingError, "network-mode-mismatch"):
            probe_join(LOOPBACK, join_port, None, local_pair_identity(self.b), timeouts=FAST)
        found = probe_join(LOOPBACK, join_port, None, local_pair_identity(self.b), timeouts=FAST, transport="tailscale")
        self.assertEqual(found.device_id, self.a_id)
        self.assertIn(LOOPBACK, self.fake.connections)

    def test_it_fails_closed_when_tailscale_never_gets_ready(self) -> None:
        self.fake.ready_error = InterfaceError("no Tailscale address")
        options = Options(
            config=NetworkConfig(network="tailscale", bind=LOOPBACK, port=0),
            transport=self.fake,
            bind_check=lambda _address: True,
            ready_wait_s=120.0,
            server_port=self.ingress.port,
        )
        self.assertEqual(Listener(self.a, options).run(), 1)
        self.assertEqual(self.fake.waits, [120.0])
        self.assertIn("the Tailscale address 127.0.0.1 is not ready after 120 s: no Tailscale address", self.log())
        self.assertIsNone(read_listener(self.a))

    def test_it_refuses_a_bind_outside_the_tailscale_range(self) -> None:
        options = Options(
            config=NetworkConfig(network="tailscale", bind="127.0.0.2", port=0),
            transport=self.fake,
            server_port=self.ingress.port,
        )
        self.assertEqual(Listener(self.a, options).run(), 1)
        self.assertIn("the bind address is not in 100.64.0.0/10", self.log())


class TailscaleCliCase(CliTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.client, self.runner = client_for(status_answers({"a": node("laptop", ME, [PEER_IP])}))
        self.ts_rules: list[tuple[str, str, str]] = []
        self.h = self.harness()

    def tailscale_firewall(self, bind: str, scope: str, alias: str) -> str:
        self.h.events.append("tailscale-firewall")
        self.ts_rules.append((bind, scope, alias))
        return "Created the Tailscale firewall rule."

    def interface(self, address: str) -> InterfaceInfo:
        if address.startswith("100."):
            return InterfaceInfo("Tailscale", address, 32, f"{address}/32", "Public", False)
        return InterfaceInfo("Ethernet", address, 24, ".".join(address.split(".")[:3]) + ".0/24", "Private", True)

    def harness(self, **overrides: Any) -> Harness:
        values: dict[str, Any] = {
            "tailscale": self.client,
            "apply_tailscale_firewall": self.tailscale_firewall,
            "interface_for": self.interface,
        }
        values.update(overrides)
        harness = Harness(self, self.a, **values)
        harness.services.is_remote_origin = lambda _home: False
        return harness

    def config(self) -> dict[str, Any]:
        return json.loads(Path(self.a, "config.json").read_text(encoding="utf-8"))["network"]


class TailscaleCommandTest(TailscaleCliCase):
    def test_on_sets_the_mode_and_bind_applies_the_tailscale_rule_and_starts_the_listener(self) -> None:
        self.assertEqual(self.h.run("tailscale", "on"), 0, self.h.errors)
        self.assertEqual((self.config()["network"], self.config()["bind"]), ("tailscale", SELF_IP))
        self.assertEqual(self.ts_rules, [(SELF_IP, "100.64.0.0/10", "Tailscale")])
        self.assertEqual(self.h.firewall, [])
        self.assertEqual(len(self.h.spawned), 1)
        self.assertEqual(len(self.h.confirmed), 1)
        self.assertIn("Turn the Tailscale network on", self.h.confirmed[0][0])
        self.assertIn(SELF_IP, "\n".join(self.h.confirmed[0]))
        self.assertIn("tailscale-on", Path(self.a, "net", "cli.log").read_text(encoding="utf-8"))

    def test_on_does_not_touch_allowed_networks(self) -> None:
        self.assertEqual(self.h.run("tailscale", "on"), 0, self.h.errors)
        self.assertNotIn("allowedNetworks", self.config())
        cli_config = load_network_config(self.a)[0]
        self.assertEqual(cli_config.allowed_networks, ())

    def test_on_keeps_an_existing_allowed_network_list(self) -> None:
        Path(self.a, "config.json").write_text(json.dumps({"network": {"allowedNetworks": ["home-net"]}}), encoding="utf-8")
        self.assertEqual(self.h.run("tailscale", "on"), 0, self.h.errors)
        self.assertEqual(self.config()["allowedNetworks"], ["home-net"])
        self.h.answer_stop()
        self.assertEqual(self.h.run("lan", "on", "--bind", "10.0.0.5"), 0, self.h.errors)
        self.assertEqual(self.config()["allowedNetworks"], ["home-net"])

    def test_on_fails_closed_when_tailscale_is_not_usable(self) -> None:
        client, _ = client_for({SELF: status_text(), IP4: TailscaleError("Tailscale is stopped")})
        harness = self.harness(tailscale=client)
        self.assertEqual(harness.run("tailscale", "on"), 1)
        self.assertIn("installed, running and logged in", harness.errors)
        self.assertIn("Tailscale is stopped", harness.errors)
        self.assertEqual(harness.confirmed, [])
        self.assertFalse(os.path.exists(os.path.join(self.a, "config.json")))
        self.assertEqual((self.ts_rules, harness.spawned), ([], []))

    def test_on_refuses_a_tagged_machine(self) -> None:
        answers = status_answers()
        answers[SELF] = status_text(Tags=["tag:server"])
        client, _ = client_for(answers)
        harness = self.harness(tailscale=client)
        self.assertEqual(harness.run("tailscale", "on"), 1)
        self.assertIn("tagged", harness.errors)

    def test_on_asks_for_the_same_confirmation_as_lan_on_and_changes_nothing_when_declined(self) -> None:
        harness = self.harness(confirm=lambda _lines: False)
        self.assertEqual(harness.run("tailscale", "on"), 1)
        self.assertIn("not confirmed", harness.errors)
        self.assertFalse(os.path.exists(os.path.join(self.a, "config.json")))
        self.assertEqual(self.ts_rules, [])
        self.h.run("lan", "on", "--bind", "10.0.0.5")
        self.assertEqual(len(self.h.confirmed), 1)

    def test_switching_from_lan_to_tailscale_replaces_the_rule_and_restarts_the_listener(self) -> None:
        self.assertEqual(self.h.run("lan", "on", "--bind", "10.0.0.5"), 0, self.h.errors)
        self.assertEqual(self.h.firewall, [("10.0.0.5", "10.0.0.0/24", "Ethernet")])
        self.h.answer_stop()
        self.assertEqual(self.h.run("tailscale", "on"), 0, self.h.errors)
        self.assertEqual(self.config()["network"], "tailscale")
        self.assertEqual(self.ts_rules, [(SELF_IP, "100.64.0.0/10", "Tailscale")])
        self.assertEqual(len(self.h.firewall), 1)
        self.assertEqual(len(self.h.spawned), 2)
        self.assertEqual(self.h.events.count("firewall"), 1)
        self.assertEqual(self.h.events.index("tailscale-firewall") > self.h.events.index("firewall"), True)
        self.assertIn("LAN mode turns off", "\n".join(self.h.confirmed[-1]))

    def test_lan_on_from_tailscale_switches_back_replaces_the_rule_and_restarts_the_listener(self) -> None:
        self.assertEqual(self.h.run("tailscale", "on"), 0, self.h.errors)
        self.h.answer_stop()
        self.assertEqual(self.h.run("lan", "on", "--bind", "10.0.0.5"), 0, self.h.errors)
        self.assertEqual((self.config()["network"], self.config()["bind"]), ("lan", "10.0.0.5"))
        self.assertEqual(self.h.firewall, [("10.0.0.5", "10.0.0.0/24", "Ethernet")])
        self.assertEqual(len(self.ts_rules), 1)
        self.assertEqual(len(self.h.spawned), 2)
        self.assertIn("Tailscale mode turns off", "\n".join(self.h.confirmed[-1]))

    def test_lan_on_still_refuses_a_tailscale_bind(self) -> None:
        self.assertEqual(self.h.run("lan", "on", "--bind", SELF_IP), 1)
        self.assertIn("--bind must be a private IPv4", self.h.errors)

    def test_off_returns_to_network_off_only_from_tailscale_mode(self) -> None:
        self.assertEqual(self.h.run("tailscale", "off"), 0)
        self.assertIn("not on", self.h.text)
        self.assertEqual(self.h.run("lan", "on", "--bind", "10.0.0.5"), 0, self.h.errors)
        self.assertEqual(self.h.run("tailscale", "off"), 0)
        self.assertEqual(self.config()["network"], "lan")
        self.h.answer_stop()
        self.assertEqual(self.h.run("tailscale", "on"), 0, self.h.errors)
        self.h.answer_stop()
        self.assertEqual(self.h.run("tailscale", "off"), 0, self.h.errors)
        self.assertEqual(self.config()["network"], "off")
        self.assertEqual(self.h.firewall_removals, 1)

    def test_lan_start_starts_the_listener_in_the_enabled_mode(self) -> None:
        self.assertEqual(self.h.run("tailscale", "on"), 0, self.h.errors)
        spawned = len(self.h.spawned)
        os.remove(listener_path(self.a))
        self.assertEqual(self.h.run("lan", "start"), 0, self.h.errors)
        self.assertEqual(len(self.h.spawned), spawned + 1)
        self.assertIn("Tailscale listener started", "\n".join(self.h.notices))

    def test_an_unknown_tailscale_command_is_a_usage_error(self) -> None:
        self.assertEqual(self.h.run("tailscale"), 2)
        self.assertEqual(self.h.run("tailscale", "sideways"), 2)
        self.assertIn("tailscale on [--at-login] | tailscale off", self.h.errors)


def listener_path(home: str) -> str:
    return str(listener.listener_file(home))


class TailscaleStatusTest(TailscaleCliCase):
    def test_status_shows_the_mode_the_address_and_the_open_gate_instead_of_the_allowed_networks(self) -> None:
        self.assertEqual(self.h.run("tailscale", "on"), 0, self.h.errors)
        self.h.out.clear()
        self.assertEqual(self.h.run("status"), 0, self.h.errors)
        lines = self.h.out
        self.assertIn("network: tailscale", lines)
        self.assertIn(f"bind: {SELF_IP}", lines)
        self.assertIn(f"tailscale address: {SELF_IP}", lines)
        self.assertIn("network gate: open (Tailscale)", lines)
        for line in lines:
            self.assertFalse(line.startswith(("allowed networks", "current network")), line)

    def test_status_shows_the_reason_when_tailscale_is_not_ready(self) -> None:
        self.assertEqual(self.h.run("tailscale", "on"), 0, self.h.errors)
        client, _ = client_for({IP4: TailscaleError("Tailscale is stopped")})
        down = self.harness(tailscale=client)
        self.assertEqual(down.run("status"), 0, down.errors)
        self.assertIn("tailscale address: unavailable", down.out)
        self.assertIn("network gate: Tailscale is stopped", down.out)

    def test_lan_status_is_unchanged(self) -> None:
        self.assertEqual(self.h.run("lan", "on", "--bind", "10.0.0.5"), 0, self.h.errors)
        self.h.out.clear()
        self.assertEqual(self.h.run("status"), 0, self.h.errors)
        self.assertIn("network: lan", self.h.out)
        self.assertTrue(any(line.startswith("allowed networks:") for line in self.h.out))
        self.assertFalse(any("tailscale" in line for line in self.h.out))

    def test_networks_commands_refuse_in_tailscale_mode_and_change_nothing(self) -> None:
        self.assertEqual(self.h.run("tailscale", "on"), 0, self.h.errors)
        before = self.config()
        self.assertEqual(self.h.run("networks", "add"), 1)
        self.assertIn("doesn't use allowed networks", self.h.errors)
        self.assertEqual(self.h.run("networks", "remove", "x"), 1)
        self.assertEqual(self.config(), before)
        self.h.out.clear()
        self.assertEqual(self.h.run("networks"), 0)
        self.assertIn("allowed networks: not used in Tailscale mode", self.h.out)


class TailscaleHubCliTest(HubCliCase):
    def setUp(self) -> None:
        super().setUp()
        self.client, _ = client_for(status_answers({"a": node("laptop", ME, [PEER_IP])}))
        Path(self.a, "config.json").write_text(
            json.dumps({"network": {"deviceName": "alpha", "network": "tailscale", "bind": SELF_IP}}), encoding="utf-8"
        )
        self.ts_rules: list[tuple[str, str, str]] = []
        self.addresses = [SELF_IP]
        self.h = self.ts_harness()

    def ts_harness(self) -> Harness:
        def interface(address: str) -> InterfaceInfo:
            return InterfaceInfo("Tailscale", address, 32, f"{address}/32", "Public", False)

        def firewall(bind: str, scope: str, alias: str) -> str:
            self.ts_rules.append((bind, scope, alias))
            return "Created the Tailscale firewall rule."

        def refuse_lan(*_args: Any) -> InterfaceInfo:
            raise AssertionError("the LAN readiness check ran in Tailscale mode")

        def no_network_id(_bind: str) -> str | None:
            raise AssertionError("the network id was read in Tailscale mode")

        deps = PairDeps(
            refuse_if_elevated=lambda: None,
            local_addresses=lambda: list(self.addresses),
            require_lan_ready=refuse_lan,
            find_openssl=lambda _configured, _env: None,
            dial_join=self.dial_join,
            timeouts=FAST,
        )
        return self.harness(
            self.a,
            pair=deps,
            tailscale=self.client,
            apply_tailscale_firewall=firewall,
            interface_for=interface,
            network_id=no_network_id,
        )

    def test_find_builds_a_tailscale_request_and_lists_what_the_probe_returns(self) -> None:
        self.announce(self.b, PEER_IP)
        self.assertEqual(self.h.run("find"), 0, self.h.errors)
        request = self.requests[0]
        self.assertIsNotNone(request.tailscale)
        self.assertIn("bravo", self.h.text)

    def test_find_without_a_device_prints_the_tailscale_causes(self) -> None:
        self.assertEqual(self.h.run("find"), 1)
        self.assertIn("Tailscale is not running or not logged in", self.h.errors)
        self.assertNotIn("UDP", self.h.errors)

    def test_hub_join_runs_over_tailscale_without_the_network_gate_and_keeps_allowed_networks(self) -> None:
        make_hub(self.b)
        server = TailscaleJoinServer(self, self.b, "tailscale")
        self.targets[PEER_IP] = server.port
        self.announce(self.b, PEER_IP)
        self.assertEqual(self.h.run("hub", "join", device_of(self.b)), 0, self.h.errors)
        self.assertEqual(self.dialed, [(PEER_IP, pairing.PAIRING_PORT, device_of(self.b))])
        self.assertEqual(self.config()["network"], "tailscale")
        self.assertNotIn("allowedNetworks", self.config())
        self.assertEqual(self.ts_rules, [(SELF_IP, "100.64.0.0/10", "Tailscale")])
        self.assertEqual(address_of(self.a, device_of(self.b)), "127.0.0.1")
        self.assertNotIn("allowed network:", self.h.text)

    def test_hub_join_against_a_lan_mode_machine_is_refused_with_both_modes_named(self) -> None:
        make_hub(self.b)
        server = TailscaleJoinServer(self, self.b, "lan")
        self.targets[PEER_IP] = server.port
        self.announce(self.b, PEER_IP)
        self.assertEqual(self.h.run("hub", "join", device_of(self.b)), 1)
        self.assertIn("this machine is in tailscale mode and the other machine is in lan mode", self.h.errors)
        self.assertEqual(load_policy(self.a).peers, ())

    def test_the_owner_check_runs_before_every_join_dial(self) -> None:
        make_hub(self.b)
        self.announce(self.b, "100.64.0.9")
        self.assertEqual(self.h.run("hub", "join", device_of(self.b)), 1)
        self.assertEqual(self.dialed, [])
        self.assertIn("did not join", self.h.errors)

    def test_discoverable_on_keeps_the_mode_and_needs_no_network_id(self) -> None:
        self.assertEqual(self.h.run("discoverable", "on"), 0, self.h.errors)
        self.assertEqual((self.config()["network"], self.config()["discoverable"]), ("tailscale", True))
        self.assertNotIn("allowedNetworks", self.config())
        self.assertEqual(self.ts_rules, [(SELF_IP, "100.64.0.0/10", "Tailscale")])

    def test_discoverable_status_shows_the_gate_of_the_mode(self) -> None:
        self.assertEqual(self.h.run("discoverable", "status"), 0, self.h.errors)
        self.assertIn("network gate: open (Tailscale)", self.h.out)
        self.assertFalse(any(line.startswith("allowed networks") for line in self.h.out))


if __name__ == "__main__":
    unittest.main()

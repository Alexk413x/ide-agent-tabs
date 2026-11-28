from __future__ import annotations

import dataclasses
import json
import os
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from ide_agent_tabs.clock import now_ms
from ide_agent_tabs.net import cli, hub_cli, pairing
from ide_agent_tabs.net.client import peer_cert_path
from ide_agent_tabs.net.find import FindRequest, FindResult, FoundDevice
from ide_agent_tabs.net.hub_cli import PairDeps
from ide_agent_tabs.net.hubstate import read_hub_state
from ide_agent_tabs.net.interfaces import InterfaceError, InterfaceInfo
from ide_agent_tabs.net.listener import Build, listener_file
from ide_agent_tabs.net.netid import tagged_network_id
from ide_agent_tabs.net.pairing import PairResult, PeerInfo
from ide_agent_tabs.net.policy import Hub, PeerGrant, Policy, load_network_config, load_policy, policy_path, save_policy
from ide_agent_tabs.net.selfupdate import Install, UpdateCommand
from ide_agent_tabs.net.store import NetStore
from ide_agent_tabs.profiles import TAB_ID_ENV
from ide_agent_tabs.version import PACKAGE_VERSION
from net_support import HAS_OPENSSL, OPENSSL, create_home, device_of, make_root
from test_net_cli import NEEDS, Harness
from test_net_join import FAST, HUB_ID, JoinServer, make_hub, reset, set_name

BIND = "192.168.50.10"
PEER = "192.168.50.20"
NETWORK = "win:Home-Net:192.168.50.0_24:0123456789ab"
OTHER_NETWORK = "win:Cafe:10.0.0.0_24:ba9876543210"
REFUSAL = "refused: a session opened by another machine can't change this machine's network settings"


class Remote:
    def __init__(self) -> None:
        self.value = False


@NEEDS
class HubCliCase(unittest.TestCase):
    homes: list[str]
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
            if os.path.exists(os.path.join(home, "config.json")):
                os.remove(os.path.join(home, "config.json"))
            set_name(home, {self.a: "alpha", self.b: "bravo", self.c: "charlie"}[home])
            for name in ("listener.json", "listener.stop"):
                if os.path.exists(os.path.join(home, "net", name)):
                    os.remove(os.path.join(home, "net", name))
        self.remote = Remote()
        self.network: str | None = NETWORK
        self.lan_error: Exception | None = None
        self.addresses = [BIND]
        self.dialed: list[tuple[str, int, str]] = []
        self.targets: dict[str, int] = {}
        self.found: list[FoundDevice] = []
        self.warnings: tuple[str, ...] = ()
        self.datagrams = 0
        self.requests: list[FindRequest] = []
        self.user_home = make_root(self, "iat-hub-user-")
        self.session = os.path.join(self.user_home, "code", "app")
        os.makedirs(self.session)
        self.own_version = PACKAGE_VERSION
        self.h = self.harness(self.a)

    def deps(self) -> PairDeps:
        return PairDeps(
            refuse_if_elevated=lambda: None,
            local_addresses=lambda: list(self.addresses),
            require_lan_ready=self.lan_ready,
            find_openssl=lambda _configured, _env: None,
            dial_join=self.dial_join,
            timeouts=FAST,
        )

    def harness(self, home: str, **overrides: Any) -> Harness:
        values: dict[str, Any] = {
            "pair": self.deps(),
            "cwd": lambda: self.session,
            "new_hub_id": lambda: HUB_ID,
            "network_id": lambda _bind: self.network,
            "is_remote_origin": lambda _home: self.remote.value,
            "user_home": self.user_home,
            "find": self.fake_find,
            "plugin_version": self.own_version,
            "update_command": lambda: None,
            "update_runner": self.refuse_real_update,
        }
        values.update(overrides)
        return Harness(self, home, **values)

    def refuse_real_update(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"a test ran an update command: {args!r}")

    def fake_find(self, request: FindRequest) -> FindResult:
        self.requests.append(request)
        return FindResult(tuple(self.found), self.warnings, probed=not self.found, heard=len(self.found), datagrams=self.datagrams)

    def serve(self, home: str, address: str = PEER) -> JoinServer:
        server = JoinServer(self, home)
        self.targets[address] = server.port
        return server

    def announce(self, home: str, address: str = PEER, name: str = "bravo") -> None:
        self.found.append(FoundDevice(device_of(home), name, address, None, False, "multicast"))

    def lan_ready(self, address: str, allow_virtual: bool) -> InterfaceInfo:
        if self.lan_error is not None:
            raise self.lan_error
        return InterfaceInfo("Ethernet", address, 24, "192.168.50.0/24", "Private", True)

    def dial_join(self, address: str, port: int, peer_id: str, *args: Any, **kwargs: Any) -> PairResult:
        self.dialed.append((address, port, peer_id))
        target = self.targets.get(address)
        if target is None:
            return PairResult("failed", f"cannot connect to {address}:{port}: connection refused")
        return pairing.dial_join("127.0.0.1", target, peer_id, *args, plugin_version=self.own_version, **kwargs)

    def assertConfigUntouched(self) -> None:
        self.assertEqual(json.loads(Path(self.a, "config.json").read_text(encoding="utf-8")), {"network": {"deviceName": "alpha"}})

    def config(self) -> dict[str, Any]:
        return json.loads(Path(self.a, "config.json").read_text(encoding="utf-8"))["network"]

    def members(self, home: str) -> list[PeerGrant]:
        return [p for p in load_policy(home).peers if p.member]


class DiscoverableTest(HubCliCase):
    def test_on_sets_the_network_the_bind_and_discoverable_and_starts_everything(self) -> None:
        self.assertEqual(self.h.run("discoverable", "on"), 0, self.h.errors)
        config, warnings = load_network_config(self.a)
        self.assertEqual(warnings, [])
        self.assertEqual((config.network, config.bind, config.discoverable), ("lan", BIND, True))
        self.assertEqual(config.allowed_networks, (NETWORK,))
        self.assertEqual(config.member_folders, (os.path.join(self.user_home, "code"),))
        self.assertEqual(config.outbound_folders, config.member_folders)
        self.assertEqual(self.h.firewall, [(BIND, "192.168.50.0/24", "Ethernet")])
        self.assertEqual(self.h.logins, [self.a])
        self.assertEqual(len(self.h.spawned), 1)
        self.assertEqual(self.h.confirmed, [])
        for part in ("alpha", device_of(self.a), f"{BIND} port 47830", NETWORK):
            self.assertIn(part, self.h.text)

    def test_on_refuses_a_public_or_domain_network_and_changes_nothing(self) -> None:
        self.lan_error = InterfaceError("Ethernet is a Public network.")
        self.assertEqual(self.h.run("discoverable", "on"), 1)
        self.assertIn("Public", self.h.errors)
        self.assertConfigUntouched()
        self.assertEqual((self.h.spawned, self.h.logins, self.h.firewall), ([], [], []))

    def test_on_needs_a_readable_network_id_and_changes_nothing(self) -> None:
        self.network = None
        self.assertEqual(self.h.run("discoverable", "on"), 1)
        self.assertIn("can't be read", self.h.errors)
        self.assertConfigUntouched()
        self.assertEqual(self.h.spawned, [])

    def test_bind_overrides_the_choice_between_addresses(self) -> None:
        self.addresses = [BIND, "192.168.50.11"]
        self.assertEqual(self.h.run("discoverable", "on"), 1)
        self.assertIn("more than one private address", self.h.errors)
        self.assertEqual(self.h.run("discoverable", "on", "--bind", "192.168.50.11"), 0, self.h.errors)
        self.assertEqual(self.config()["bind"], "192.168.50.11")

    def test_on_keeps_other_allowed_networks_and_user_set_folders(self) -> None:
        mine = os.path.join(self.user_home, "mine")
        os.makedirs(mine)
        Path(self.a, "config.json").write_text(
            json.dumps(
                {"jev": {"enabled": False}, "network": {"deviceName": "alpha", "allowedNetworks": [OTHER_NETWORK], "memberFolders": [mine]}}
            )
        )
        self.assertEqual(self.h.run("discoverable", "on"), 0, self.h.errors)
        config, _ = load_network_config(self.a)
        self.assertEqual(config.allowed_networks, (OTHER_NETWORK, NETWORK))
        self.assertEqual(config.member_folders, (mine,))
        self.assertEqual(config.outbound_folders, (os.path.join(self.user_home, "code"),))
        self.assertEqual(json.loads(Path(self.a, "config.json").read_text())["jev"], {"enabled": False})

    def test_turning_it_on_twice_lists_the_network_once(self) -> None:
        self.assertEqual(self.h.run("discoverable", "on"), 0)
        self.assertEqual(self.h.run("discoverable", "on"), 0)
        self.assertEqual(load_network_config(self.a)[0].allowed_networks, (NETWORK,))

    def test_off_persists_and_leaves_the_network_the_login_task_and_members_alone(self) -> None:
        self.assertEqual(self.h.run("discoverable", "on"), 0)
        spawned = len(self.h.spawned)
        self.assertEqual(self.h.run("discoverable", "off"), 0, self.h.errors)
        config, _ = load_network_config(self.a)
        self.assertEqual((config.discoverable, config.network, config.bind, config.allowed_networks), (False, "lan", BIND, (NETWORK,)))
        self.assertEqual(self.h.firewall_removals, 0)
        self.assertEqual(len(self.h.spawned), spawned)
        self.assertNotIn("remove-login", " ".join(self.h.events))
        self.assertEqual(self.h.confirmed, [])
        self.assertIn("members stay connected", self.h.text)

    def test_net_off_also_turns_discoverable_off_so_a_later_lan_on_does_not_resume_joins(self) -> None:
        Path(self.a, "config.json").write_text(json.dumps({"network": {"network": "lan", "bind": BIND, "discoverable": True}}))
        self.assertEqual(self.h.run("off"), 0, self.h.errors)
        config, _ = load_network_config(self.a)
        self.assertEqual((config.network, config.discoverable), ("off", False))

    def test_status_reports_the_state_the_network_gate_and_the_listener(self) -> None:
        self.assertEqual(self.h.run("discoverable"), 0)
        self.assertIn("discoverable: off", self.h.text)
        self.assertEqual(self.h.run("discoverable", "on"), 0)
        self.h.out.clear()
        self.assertEqual(self.h.run("discoverable", "status"), 0)
        for part in ("discoverable: on", f"current network: {NETWORK}", "network gate: open", "listener: running", "serving joins: yes"):
            self.assertIn(part, self.h.text)
        self.network = OTHER_NETWORK
        self.h.write_listener_file(self.a, joins=False)
        self.h.out.clear()
        self.h.run("discoverable", "status")
        self.assertIn("network gate: this network is not in network.allowedNetworks", self.h.text)
        self.assertIn("serving joins: no", self.h.text)
        self.assertNotIn("although discoverable is on", self.h.text)
        self.network = None
        self.h.out.clear()
        self.h.run("discoverable", "status")
        self.assertIn("current network: unreadable", self.h.text)
        self.assertIn("can't be read", self.h.text)

    def test_status_does_not_claim_joins_that_the_running_listener_does_not_serve(self) -> None:
        self.h.run("discoverable", "on")
        self.h.write_listener_file(self.a, joins=False)
        self.h.out.clear()
        self.assertEqual(self.h.run("discoverable", "status"), 0)
        self.assertIn("network gate: open", self.h.text)
        self.assertNotIn("serving joins: yes", self.h.text)
        self.assertIn("serving joins: no (the running listener reports that it serves no joins, although discoverable is on)", self.h.text)

    def test_status_says_starting_in_the_first_seconds_after_a_start(self) -> None:
        self.h.run("discoverable", "on")
        self.h.write_listener_file(self.a, startedMs=1_000_000, joins=False)
        self.h.services = dataclasses.replace(self.h.services, now_ms=lambda: 1_004_000)
        self.h.out.clear()
        self.assertEqual(self.h.run("discoverable", "status"), 0)
        self.assertIn("serving joins: starting (the listener started 4 s ago)", self.h.text)
        self.assertNotIn("although discoverable is on", self.h.text)
        self.h.services = dataclasses.replace(self.h.services, now_ms=lambda: 1_000_000 + cli.JOINS_START_S * 1000)
        self.h.out.clear()
        self.h.run("discoverable", "status")
        self.assertIn("although discoverable is on", self.h.text)

    def test_status_names_a_listener_of_another_build_and_how_to_restart_it(self) -> None:
        self.h.run("discoverable", "on")
        self.h.write_listener_file(self.a, version="0.11.0", build="0.11.0-bbbbbbbbbbbb", joins=None)
        self.h.out.clear()
        self.assertEqual(self.h.run("discoverable", "status"), 0)
        self.assertIn(
            f"listener build: 0.11.0-bbbbbbbbbbbb, not the installed build {self.h.build.id}. `agent-tabs net lan start` restarts it.",
            self.h.text,
        )
        self.assertNotIn("serving joins: yes", self.h.text)

    def test_on_restarts_a_listener_of_another_build_and_says_so(self) -> None:
        self.h.write_listener_file(self.a, version=None, build=None, joins=None)
        thread = self.h.answer_stop()
        self.assertEqual(self.h.run("discoverable", "on"), 0, self.h.errors)
        thread.join(5)
        self.assertIn("records no build, so an older plugin version started it. Restarting it.", self.h.text)
        self.assertEqual(len(self.h.spawned), 1)

    def test_a_remote_session_cannot_restart_the_listener_through_discoverable_on(self) -> None:
        self.h.write_listener_file(self.a, version=None, build=None, joins=None)
        self.remote.value = True
        self.assertEqual(self.h.run("discoverable", "on"), 1)
        self.assertIn("can't change this machine's network settings", self.h.errors)
        self.assertEqual(self.h.spawned, [])

    def test_net_status_shows_discoverable_and_the_allowed_networks(self) -> None:
        self.h.run("discoverable", "on")
        self.h.out.clear()
        self.assertEqual(self.h.run("status"), 0)
        for part in ("discoverable: on", f"allowed networks: {NETWORK}", "network gate: open"):
            self.assertIn(part, self.h.text)
        self.network = OTHER_NETWORK
        self.h.out.clear()
        self.h.run("status")
        self.assertIn("this network is not in network.allowedNetworks", self.h.text)

    def test_usage_errors(self) -> None:
        self.assertEqual(self.h.run("discoverable", "maybe"), 2)
        self.assertEqual(self.h.run("discoverable", "off", "extra"), 2)

    def test_a_bad_value_in_the_config_turns_the_network_off_with_a_warning(self) -> None:
        Path(self.a, "config.json").write_text(json.dumps({"network": {"network": "lan", "bind": BIND, "discoverable": "yes"}}))
        config, warnings = load_network_config(self.a)
        self.assertEqual((config.network, config.discoverable), ("off", False))
        self.assertTrue(any("network.discoverable must be true or false" in w for w in warnings))


class GatewayMacTest(HubCliCase):
    LEGACY = "win:Home-Net:192.168.50.0_24:aaaaaaaaaaaa"
    UPGRADED = "win:Home-Net:192.168.50.0_24:bbbbbbbbbbbb"

    def test_discoverable_on_replaces_an_old_format_entry_instead_of_adding_a_second_one(self) -> None:
        Path(self.a, "config.json").write_text(json.dumps({"network": {"allowedNetworks": [self.LEGACY, OTHER_NETWORK]}}))
        self.network = tagged_network_id(self.UPGRADED, legacy=self.LEGACY)
        self.assertEqual(self.h.run("discoverable", "on"), 0, self.h.errors)
        self.assertEqual(load_network_config(self.a)[0].allowed_networks, (self.UPGRADED, OTHER_NETWORK))

    def test_networks_add_replaces_an_old_format_entry_and_a_current_entry_is_left_alone(self) -> None:
        Path(self.a, "config.json").write_text(json.dumps({"network": {"network": "lan", "bind": BIND, "allowedNetworks": [self.LEGACY]}}))
        self.network = tagged_network_id(self.UPGRADED, legacy=self.LEGACY)
        self.assertEqual(self.h.run("networks", "add"), 0, self.h.errors)
        self.assertEqual(load_network_config(self.a)[0].allowed_networks, (self.UPGRADED,))
        self.assertEqual(self.h.run("networks", "add"), 0)
        self.assertIn("already allowed", self.h.text)
        self.assertEqual(load_network_config(self.a)[0].allowed_networks, (self.UPGRADED,))

    def test_the_view_says_the_old_format_entry_does_not_allow_the_network_and_how_to_fix_it(self) -> None:
        Path(self.a, "config.json").write_text(json.dumps({"network": {"network": "lan", "bind": BIND, "allowedNetworks": [self.LEGACY]}}))
        self.network = tagged_network_id(self.UPGRADED, legacy=self.LEGACY)
        self.assertEqual(self.h.run("networks"), 0)
        self.assertIn(f"current network: {self.UPGRADED} (this network is listed only by its older id", self.h.text)
        self.assertIn("net networks add", self.h.text)

    def test_a_fallback_id_prints_a_warning_when_it_is_added(self) -> None:
        self.network = tagged_network_id(
            NETWORK, fallback="the MAC address of the default gateway can't be read, so the id is built from less"
        )
        self.assertEqual(self.h.run("discoverable", "on"), 0, self.h.errors)
        self.assertIn("warning: the MAC address of the default gateway can't be read", self.h.errors)
        self.assertEqual(load_network_config(self.a)[0].allowed_networks, (NETWORK,))


class NetworksTest(HubCliCase):
    def test_the_view_lists_the_allowed_networks_and_the_current_one(self) -> None:
        self.assertEqual(self.h.run("networks"), 0)
        self.assertIn("allowed networks: none", self.h.text)
        self.assertIn("current network: unknown", self.h.text)
        self.h.run("discoverable", "on")
        self.h.out.clear()
        self.h.run("networks")
        self.assertIn(f"allowed networks: {NETWORK}", self.h.text)
        self.assertIn(f"current network: {NETWORK} (allowed)", self.h.text)

    def test_add_lists_the_current_network_once_and_remove_drops_it(self) -> None:
        Path(self.a, "config.json").write_text(json.dumps({"network": {"network": "lan", "bind": BIND}}))
        self.assertEqual(self.h.run("networks", "add"), 0, self.h.errors)
        self.assertEqual(load_network_config(self.a)[0].allowed_networks, (NETWORK,))
        self.assertEqual(self.h.run("networks", "add"), 0)
        self.assertIn("already allowed", self.h.text)
        self.assertEqual(load_network_config(self.a)[0].allowed_networks, (NETWORK,))
        self.assertEqual(self.h.run("networks", "remove", NETWORK), 0, self.h.errors)
        self.assertEqual(load_network_config(self.a)[0].allowed_networks, ())
        self.assertEqual(self.h.confirmed, [])

    def test_add_reads_the_network_of_the_current_address_when_the_saved_one_is_gone(self) -> None:
        Path(self.a, "config.json").write_text(json.dumps({"network": {"network": "lan", "bind": "192.168.50.11"}}))
        self.addresses = [BIND]
        seen: list[str] = []

        def network_id(address: str) -> str | None:
            seen.append(address)
            return NETWORK

        h = self.harness(self.a, bind_available=lambda address: address != "192.168.50.11", network_id=network_id)
        self.assertEqual(h.run("networks", "add"), 0, h.errors)
        self.assertEqual(seen, [BIND])
        self.assertEqual(load_network_config(self.a)[0].allowed_networks, (NETWORK,))

    def test_add_needs_a_bind_address_and_a_readable_network(self) -> None:
        self.assertEqual(self.h.run("networks", "add"), 1)
        self.assertIn("no bind address", self.h.errors)
        Path(self.a, "config.json").write_text(json.dumps({"network": {"network": "lan", "bind": BIND}}))
        self.network = None
        self.assertEqual(self.h.run("networks", "add"), 1)
        self.assertIn("can't be read", self.h.errors)
        self.assertEqual(load_network_config(self.a)[0].allowed_networks, ())

    def test_remove_refuses_unknown_or_malformed_ids(self) -> None:
        Path(self.a, "config.json").write_text(json.dumps({"network": {"allowedNetworks": [NETWORK]}}))
        self.assertEqual(self.h.run("networks", "remove", OTHER_NETWORK), 1)
        self.assertIn("is not an allowed network", self.h.errors)
        self.assertEqual(self.h.run("networks", "remove", "has space"), 2)
        self.assertEqual(self.h.run("networks", "remove"), 2)
        self.assertEqual(self.h.run("networks", "add", "extra"), 2)
        self.assertEqual(load_network_config(self.a)[0].allowed_networks, (NETWORK,))

    def test_other_keys_survive(self) -> None:
        Path(self.a, "config.json").write_text(json.dumps({"theme": "dark", "network": {"network": "lan", "bind": BIND, "port": 47829}}))
        self.h.run("networks", "add")
        saved = json.loads(Path(self.a, "config.json").read_text())
        self.assertEqual((saved["theme"], saved["network"]["port"]), ("dark", 47829))


class HubJoinTest(HubCliCase):
    def test_join_dials_the_open_join_and_sets_up_the_machine(self) -> None:
        make_hub(self.b)
        server = self.serve(self.b)
        self.announce(self.b)
        self.assertEqual(self.h.run("hub", "join", device_of(self.b)), 0, self.h.errors)
        self.assertEqual(self.dialed, [(PEER, 47830, device_of(self.b))])
        (member,) = self.members(self.a)
        self.assertEqual((member.device_id, member.label), (device_of(self.b), "bravo"))
        self.assertEqual((member.max_tabs, member.opens_per_hour, member.messages_per_minute), (4, 30, 60))
        self.assertEqual(load_policy(self.a).hub, load_policy(self.b).hub)
        state = read_hub_state(self.a)
        assert state is not None
        self.assertEqual(state.hub_id, HUB_ID)
        config, _ = load_network_config(self.a)
        self.assertEqual((config.network, config.bind, config.discoverable), ("lan", BIND, False))
        self.assertEqual(config.allowed_networks, (NETWORK,))
        self.assertEqual(config.member_folders, (os.path.join(self.user_home, "code"),))
        self.assertEqual(config.outbound_folders, config.member_folders)
        self.assertEqual((len(self.h.spawned), self.h.logins, self.h.confirmed), (1, [self.a], []))
        self.assertIn("joined the hub home as bravo", self.h.text)
        self.assertEqual(server.wait_result().outcome, "paired")

    def test_join_with_the_wrong_device_id_saves_and_changes_nothing(self) -> None:
        make_hub(self.b)
        self.serve(self.b)
        self.announce(self.c, name="charlie")
        self.assertEqual(self.h.run("hub", "join", device_of(self.c)), 1)
        self.assertIn("does not match the device id", self.h.errors)
        self.assertIn("Nothing was saved", self.h.errors)
        self.assertEqual((load_policy(self.a).peers, load_policy(self.a).hub), ((), None))
        self.assertConfigUntouched()
        self.assertEqual((self.h.spawned, self.h.logins), ([], []))
        self.assertEqual(self.members(self.b), [])

    def test_a_machine_in_another_hub_is_refused_and_nothing_is_saved_on_either_side(self) -> None:
        make_hub(self.a, "fedcba9876543210fedcba9876543210", "office")
        make_hub(self.b)
        self.serve(self.b)
        self.announce(self.b)
        self.assertEqual(self.h.run("hub", "join", device_of(self.b)), 1)
        self.assertIn("already-in-another-hub", self.h.errors)
        self.assertEqual((self.members(self.a), self.members(self.b)), ([], []))
        self.assertConfigUntouched()
        self.assertEqual(self.h.spawned, [])

    def test_a_join_that_cannot_connect_changes_nothing(self) -> None:
        self.announce(self.b)
        self.assertEqual(self.h.run("hub", "join", device_of(self.b)), 1)
        self.assertIn("cannot connect", self.h.errors)
        self.assertConfigUntouched()

    def test_a_found_address_off_the_subnet_a_bad_id_and_this_machine_are_refused_before_dialing(self) -> None:
        self.announce(self.b, "10.1.2.3")
        self.assertEqual(self.h.run("hub", "join", device_of(self.b)), 1)
        self.assertEqual(self.h.run("hub", "join", "not-an-id"), 1)
        self.announce(self.a, name="alpha")
        self.assertEqual(self.h.run("hub", "join", device_of(self.a)), 1)
        self.assertIn("own device id", self.h.errors)
        self.assertEqual(self.dialed, [])

    def test_join_takes_no_address_option(self) -> None:
        self.assertEqual(self.h.run("hub", "join", device_of(self.b), "--address", PEER), 2)
        self.assertEqual(self.dialed, [])

    def test_an_unreadable_network_stops_the_join_before_any_trust_is_saved(self) -> None:
        make_hub(self.b)
        self.serve(self.b)
        self.announce(self.b)
        self.network = None
        self.assertEqual(self.h.run("hub", "join", device_of(self.b)), 1)
        self.assertIn("can't be read", self.h.errors)
        self.assertEqual((self.dialed, self.members(self.a)), ([], []))

    def test_a_public_network_stops_the_join_before_dialing(self) -> None:
        self.lan_error = InterfaceError("Ethernet is a Public network.")
        self.announce(self.b)
        self.assertEqual(self.h.run("hub", "join", device_of(self.b)), 1)
        self.assertEqual((self.dialed, self.requests), ([], []))

    def test_join_requires_a_device_id(self) -> None:
        self.assertEqual(self.h.run("hub", "join"), 2)

    def test_create_with_makes_the_hub_then_joins_each_device_and_reports_each(self) -> None:
        self.serve(self.b, PEER)
        self.serve(self.c, "192.168.50.21")
        self.announce(self.b)
        self.announce(self.c, "192.168.50.21", "charlie")
        code = self.h.run("hub", "create", "--name", "home", "--with", device_of(self.b), "--with", device_of(self.c))
        self.assertEqual(code, 0, self.h.errors)
        self.assertEqual(load_policy(self.a).hub, Hub(HUB_ID, "home"))
        self.assertEqual(sorted(m.label for m in self.members(self.a)), ["bravo", "charlie"])
        self.assertEqual(load_policy(self.b).hub, load_policy(self.a).hub)
        self.assertEqual(load_policy(self.c).hub, load_policy(self.a).hub)
        state_a, state_b, state_c = (read_hub_state(h) for h in (self.a, self.b, self.c))
        assert state_a is not None and state_b is not None and state_c is not None
        self.assertEqual(state_a.key, state_b.key)
        self.assertEqual(state_a.key, state_c.key)
        self.assertEqual(sum("joined the hub home as" in line for line in self.h.out), 2)
        config, _ = load_network_config(self.a)
        self.assertEqual((config.network, config.allowed_networks), ("lan", (NETWORK,)))
        self.assertEqual((len(self.h.spawned), len(self.requests)), (1, 1))

    def test_create_with_reports_a_device_that_fails_and_still_sets_up_for_the_one_that_joined(self) -> None:
        self.serve(self.b, PEER)
        self.announce(self.b)
        code = self.h.run("hub", "create", "--with", device_of(self.b), "--with", device_of(self.c))
        self.assertEqual(code, 1)
        self.assertEqual([m.label for m in self.members(self.a)], ["bravo"])
        self.assertIn("joined the hub", self.h.text)
        self.assertIn(f"{device_of(self.c)[:15]}: did not join: no verified device with that id was found", self.h.errors)
        self.assertEqual(load_network_config(self.a)[0].network, "lan")

    def test_create_with_reuses_an_existing_hub(self) -> None:
        make_hub(self.a)
        self.serve(self.b)
        self.announce(self.b)
        self.assertEqual(self.h.run("hub", "create", "--with", device_of(self.b)), 0, self.h.errors)
        self.assertIn("Using the hub home", self.h.text)
        self.assertEqual(load_policy(self.a).hub, Hub(HUB_ID, "home"))

    def test_create_with_a_different_name_for_an_existing_hub_is_refused(self) -> None:
        make_hub(self.a)
        self.assertEqual(self.h.run("hub", "create", "--name", "other", "--with", device_of(self.b)), 1)
        self.assertIn("does not match", self.h.errors)

    def test_create_with_does_not_accept_a_device_id_with_an_address(self) -> None:
        self.serve(self.b)
        self.announce(self.b)
        self.assertEqual(self.h.run("hub", "create", "--with", f"{device_of(self.b)}@{PEER}"), 1)
        self.assertEqual(self.dialed, [])

    def test_create_without_with_still_refuses_when_a_hub_exists(self) -> None:
        make_hub(self.a)
        self.assertEqual(self.h.run("hub", "create"), 1)
        self.assertIn("already in the hub", self.h.errors)

    def test_create_makes_the_hub_key(self) -> None:
        self.assertEqual(self.h.run("hub", "create"), 0, self.h.errors)
        state = read_hub_state(self.a)
        assert state is not None
        self.assertEqual((state.hub_id, len(state.key), state.members), (HUB_ID, 64, ()))

    def test_leave_removes_the_key(self) -> None:
        self.h.run("hub", "create")
        self.assertEqual(self.h.run("hub", "leave"), 0, self.h.errors)
        self.assertIsNone(read_hub_state(self.a))


class Completed:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


class JoinUpdateTest(HubCliCase):
    NEW_ROOT = os.path.join(os.sep, "plugins", "ide-agent-tabs", "9.0.0")
    COMMAND = UpdateCommand(
        (sys.executable, "plugin", "update", "ide-agent-tabs@market"),
        "tool plugin marketplace update market && tool plugin update ide-agent-tabs@market",
        (sys.executable, "plugin", "marketplace", "update", "market"),
    )

    def setUp(self) -> None:
        super().setUp()
        self.own_version = "0.14.3"
        self.calls: list[list[str]] = []
        self.runner_result = Completed()
        self.launchers: list[str] = []
        self.install: Install | None = Install(self.NEW_ROOT, "9.0.0")
        self.command: UpdateCommand | None = self.COMMAND
        self.h = self.update_harness()

    def run_update(self, argv: list[str], **kwargs: Any) -> Completed:
        self.calls.append(argv)
        return self.runner_result

    def spawn_from(self, home: str, port: int, launcher: str) -> int | None:
        self.launchers.append(launcher)
        self.h.spawned.append((home, port))
        self.h.write_listener_file(home, version="9.0.0", build="9.0.0-bbbbbbbbbbbb")
        return os.getpid()

    def update_harness(self, **overrides: Any) -> Harness:
        values: dict[str, Any] = {
            "update_command": lambda: self.command,
            "update_runner": self.run_update,
            "fresh_install": lambda: self.install,
            "install_build": lambda install: Build(install.version, f"{install.version}-bbbbbbbbbbbb"),
            "spawn_from": self.spawn_from,
        }
        values.update(overrides)
        return self.harness(self.a, **values)

    def join_newer(self, version: str = "9.0.0") -> JoinServer:
        make_hub(self.b)
        server = JoinServer(self, self.b, plugin_version=version)
        self.targets[PEER] = server.port
        self.announce(self.b)
        return server

    def test_a_newer_machine_updates_this_one_restarts_the_listener_and_the_join_stands(self) -> None:
        server = self.join_newer()
        self.assertEqual(self.h.run("hub", "join", device_of(self.b)), 0, self.h.errors)
        self.assertEqual(server.wait_result().outcome, "paired")
        self.assertEqual([call[1:] for call in self.calls], [list(self.COMMAND.refresh or ())[1:], list(self.COMMAND.argv)[1:]])
        for part in (
            "the other machine runs ide-agent-tabs 9.0.0 and this machine runs 0.14.3.",
            f"Updating this machine with: {self.COMMAND.display}",
            "Open sessions keep the old skill text and tool list until they restart.",
            "joined the hub home as bravo",
        ):
            self.assertIn(part, self.h.text)
        self.assertEqual(self.launchers, [os.path.join(self.NEW_ROOT, "mcp", "launch", "net_listener.py")])
        self.assertEqual(self.h.spawned, [(self.a, self.h.services.server_port)])
        log = Path(self.a, "net", "cli.log").read_text(encoding="utf-8")
        self.assertIn("self-update attempt=1 theirs=9.0.0 mine=0.14.3", log)
        self.assertIn("self-update ok=1", log)

    def test_a_running_listener_of_the_old_build_restarts_on_the_new_one(self) -> None:
        self.join_newer()
        self.h.write_listener_file(self.a, version="0.14.3", build="0.14.3-aaaaaaaaaaaa", joins=None)
        thread = self.h.answer_stop()
        self.assertEqual(self.h.run("hub", "join", device_of(self.b)), 0, self.h.errors)
        thread.join(5)
        self.assertIn("Restarting it.", self.h.text)
        self.assertEqual(self.launchers, [os.path.join(self.NEW_ROOT, "mcp", "launch", "net_listener.py")])

    def test_a_join_that_failed_is_tried_once_more_after_the_update(self) -> None:
        self.join_newer()
        attempts: list[int] = []
        real = self.dial_join

        def flaky(address: str, port: int, peer_id: str, *args: Any, **kwargs: Any) -> PairResult:
            attempts.append(1)
            if len(attempts) == 1:
                return PairResult(
                    "failed", "the other machine's protocol version is not supported", PeerInfo("bravo", address, peer_id, b"x", "9.0.0")
                )
            return real(address, port, peer_id, *args, **kwargs)

        self.h = self.update_harness(pair=dataclasses.replace(self.deps(), dial_join=flaky))
        self.assertEqual(self.h.run("hub", "join", device_of(self.b)), 0, self.h.errors)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual([m.label for m in self.members(self.a)], ["bravo"])

    def test_a_failed_join_is_not_retried_when_the_update_fails(self) -> None:
        self.join_newer()
        attempts: list[int] = []

        def failing(address: str, port: int, peer_id: str, *args: Any, **kwargs: Any) -> PairResult:
            attempts.append(1)
            return PairResult("failed", "the join failed", PeerInfo("bravo", address, peer_id, b"x", "9.0.0"))

        self.runner_result = Completed(1, "", "network unreachable")
        self.h = self.update_harness(pair=dataclasses.replace(self.deps(), dial_join=failing))
        self.assertEqual(self.h.run("hub", "join", device_of(self.b)), 1)
        self.assertEqual(len(attempts), 1)

    def test_a_failed_update_prints_the_manual_command_and_the_join_goes_on(self) -> None:
        self.join_newer()
        self.runner_result = Completed(1, "", "network unreachable")
        self.assertEqual(self.h.run("hub", "join", device_of(self.b)), 0, self.h.errors)
        self.assertIn("warning: the update failed: exit 1: network unreachable", self.h.errors)
        self.assertIn(f"To update ide-agent-tabs, run: {self.COMMAND.display}", self.h.text)
        self.assertNotIn("Open sessions keep", self.h.text)
        self.assertEqual(self.launchers, [])
        self.assertIn("joined the hub home as bravo", self.h.text)
        self.assertIn("self-update ok=0", Path(self.a, "net", "cli.log").read_text(encoding="utf-8"))

    def test_an_install_with_no_command_prints_the_generic_text_and_runs_nothing(self) -> None:
        self.join_newer()
        self.command = None
        self.assertEqual(self.h.run("hub", "join", device_of(self.b)), 0, self.h.errors)
        self.assertIn("Update ide-agent-tabs with the client that installed it.", self.h.text)
        self.assertEqual(self.calls, [])

    def test_an_update_that_installs_nothing_newer_keeps_the_listener_build(self) -> None:
        self.join_newer()
        self.install = Install(self.NEW_ROOT, "0.14.3")
        self.assertEqual(self.h.run("hub", "join", device_of(self.b)), 0, self.h.errors)
        self.assertIn("no newer install", self.h.errors)
        self.assertEqual(self.launchers, [])

    def test_an_older_machine_is_told_to_update_itself_and_nothing_runs_here(self) -> None:
        self.join_newer("0.1.0")
        self.assertEqual(self.h.run("hub", "join", device_of(self.b)), 0, self.h.errors)
        self.assertIn("the other machine runs ide-agent-tabs 0.1.0 and this machine runs 0.14.3.", self.h.text)
        self.assertIn("The other machine must update. It updates itself when its unattended update is on", self.h.text)
        self.assertEqual(self.calls, [])

    def test_a_machine_that_reports_no_version_is_told_to_update(self) -> None:
        make_hub(self.b)
        self.serve(self.b)
        self.announce(self.b)
        with mock.patch.object(pairing, "_LISTEN_MODES", (pairing.JOIN_MODE, pairing.PROBE_MODE)):
            self.assertEqual(self.h.run("hub", "join", device_of(self.b)), 0, self.h.errors)
        self.assertIn("reports no ide-agent-tabs version", self.h.text)
        self.assertIn("cannot update itself", self.h.text)
        self.assertEqual(self.calls, [])

    def test_the_same_version_prints_nothing_about_versions(self) -> None:
        self.join_newer("0.14.3")
        self.assertEqual(self.h.run("hub", "join", device_of(self.b)), 0, self.h.errors)
        self.assertNotIn("ide-agent-tabs 0.14.3", self.h.text)
        self.assertEqual(self.calls, [])

    def test_create_with_updates_once_for_several_newer_machines(self) -> None:
        make_hub(self.b)
        make_hub(self.c)
        for home, address, name in ((self.b, PEER, "bravo"), (self.c, "192.168.50.21", "charlie")):
            self.targets[address] = JoinServer(self, home, plugin_version="9.0.0").port
            self.announce(home, address, name)
        code = self.h.run("hub", "create", "--name", "home", "--with", device_of(self.b), "--with", device_of(self.c))
        self.assertEqual(code, 0, self.h.errors)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.h.text.count("Updating this machine with"), 1)

    def test_a_remote_session_cannot_start_an_update_through_a_join(self) -> None:
        self.join_newer()
        self.remote.value = True
        self.assertEqual(self.h.run("hub", "join", device_of(self.b)), 1)
        self.assertEqual((self.calls, self.dialed), ([], []))


class AutoUpdateTest(HubCliCase):
    def test_status_defaults_to_on_and_shows_the_command(self) -> None:
        command = UpdateCommand(("claude", "plugin", "update", "ide-agent-tabs@market"), "claude plugin update ide-agent-tabs@market")
        h = self.harness(self.a, update_command=lambda: command)
        self.assertEqual(h.run("auto-update"), 0, h.errors)
        self.assertEqual(h.out, ["unattended update: on", "update command: claude plugin update ide-agent-tabs@market"])
        h.out.clear()
        self.assertEqual(self.h.run("auto-update", "status"), 0)
        self.assertEqual(self.h.out, ["unattended update: on", "update command: none known for this install"])

    def test_off_and_on_change_network_auto_update_and_keep_the_other_keys(self) -> None:
        Path(self.a, "config.json").write_text(json.dumps({"theme": "dark", "network": {"deviceName": "alpha", "port": 47829}}))
        self.assertEqual(self.h.run("auto-update", "off"), 0, self.h.errors)
        saved = json.loads(Path(self.a, "config.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["network"], {"deviceName": "alpha", "port": 47829, "autoUpdate": False})
        self.assertEqual(saved["theme"], "dark")
        self.h.out.clear()
        self.h.run("auto-update", "status")
        self.assertIn("unattended update: off", self.h.text)
        self.assertEqual(self.h.run("auto-update", "on"), 0, self.h.errors)
        self.assertIs(self.config()["autoUpdate"], True)
        log = Path(self.a, "net", "cli.log").read_text(encoding="utf-8")
        self.assertIn("auto-update state=off", log)
        self.assertIn("auto-update state=on", log)

    def test_a_session_opened_by_another_machine_cannot_change_the_switch(self) -> None:
        remote = self.harness(self.a, is_remote_origin=lambda _home: True)
        for action in ("on", "off"):
            self.assertEqual(remote.run("auto-update", action), 1)
        self.assertIn(REFUSAL, remote.errors)
        self.assertConfigUntouched()
        self.assertEqual(remote.run("auto-update", "status"), 0)

    def test_usage_errors(self) -> None:
        self.assertEqual(self.h.run("auto-update", "maybe"), 2)
        self.assertEqual(self.h.run("auto-update", "on", "extra"), 2)


GUARDED: list[tuple[str, ...]] = [
    ("auto-update", "on"),
    ("auto-update", "off"),
    ("folders", "add", "/x"),
    ("folders", "remove", "/x"),
    ("networks", "add"),
    ("networks", "remove", NETWORK),
    ("discoverable", "on"),
    ("discoverable", "off"),
    ("hub", "create"),
    ("hub", "join", "x"),
    ("hub", "create", "--with", "x"),
    ("hub", "leave"),
    ("revoke", "laptop"),
    ("rename", "laptop", "renamed"),
    ("grant", "laptop", "--agent", "claude"),
]
READ_ONLY: list[tuple[str, ...]] = [
    ("auto-update",),
    ("auto-update", "status"),
    ("status",),
    ("folders",),
    ("networks",),
    ("discoverable",),
    ("discoverable", "status"),
    ("hub", "status"),
    ("id",),
]


class OriginGuardTest(HubCliCase):
    def seed(self) -> None:
        save_policy(self.a, Policy((PeerGrant(device_id=device_of(self.b), label="laptop", address=PEER),), 0, None))
        Path(self.a, "config.json").write_text(json.dumps({"network": {"network": "lan", "bind": BIND, "allowedNetworks": [NETWORK]}}))

    def snapshot(self) -> tuple[str, str]:
        return Path(policy_path(self.a)).read_text(encoding="utf-8"), Path(self.a, "config.json").read_text(encoding="utf-8")

    def test_every_settings_command_refuses_for_a_remote_origin_and_changes_nothing(self) -> None:
        self.seed()
        before = self.snapshot()
        self.remote.value = True
        for command in GUARDED:
            with self.subTest(command=command):
                self.h.err.clear()
                self.assertEqual(self.h.run(*command), 1)
                self.assertEqual(self.h.err, [f"error: {REFUSAL}"])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual((self.h.spawned, self.h.logins, self.h.firewall, self.dialed), ([], [], [], []))

    def test_every_read_only_command_still_works_for_a_remote_origin(self) -> None:
        self.seed()
        self.remote.value = True
        for command in READ_ONLY:
            with self.subTest(command=command):
                self.assertEqual(self.h.run(*command), 0, self.h.errors)

    def test_a_local_origin_is_not_refused(self) -> None:
        self.seed()
        for command in (("networks", "remove", NETWORK), ("discoverable", "off")):
            self.assertEqual(self.h.run(*command), 0, self.h.errors)

    def test_the_real_check_refuses_a_session_started_by_a_peer(self) -> None:
        self.seed()
        store = NetStore(self.a)
        try:
            store.record_remote_tab("tab-from-peer", "peer-device", now_ms())
        finally:
            store.close()
        checked = self.harness(self.a)
        values = {key: value for key, value in checked.services.__dict__.items() if key != "is_remote_origin"}
        services = cli.Services(**values)
        with mock.patch.dict(os.environ, {TAB_ID_ENV: "tab-from-peer"}):
            self.assertEqual(cli.main(["discoverable", "off"], services), 1)
        self.assertIn(REFUSAL, " ".join(checked.err))
        with mock.patch.dict(os.environ, {TAB_ID_ENV: "tab-local"}):
            self.assertEqual(cli.main(["discoverable", "off"], services), 0)

    def test_the_real_check_is_false_when_no_store_exists(self) -> None:
        self.assertFalse(cli._caller_is_remote(make_root(self)))  # pyright: ignore[reportPrivateUsage]


class PrepareTest(HubCliCase):
    def test_a_single_physical_private_address_is_shown_and_used(self) -> None:
        self.addresses = [BIND, "10.0.0.5", "8.8.8.8", "127.0.0.1", "169.254.1.1"]

        def interface(address: str) -> InterfaceInfo:
            return InterfaceInfo(
                "Ethernet" if address == BIND else "vEthernet (WSL)", address, 24, "192.168.50.0/24", "Private", address == BIND
            )

        h = self.harness(self.a, interface_for=interface)
        self.assertEqual(h.run("discoverable", "on"), 0, h.errors)
        self.assertIn(f"Using {BIND} on Ethernet.", h.text)
        self.assertEqual(self.config()["bind"], BIND)

    def test_no_physical_private_address_names_the_fix(self) -> None:
        self.addresses = ["8.8.8.8", "127.0.0.1"]
        self.assertEqual(self.h.run("discoverable", "on"), 1)
        self.assertIn("--bind", self.h.errors)
        self.assertIn("run the command again", self.h.errors)
        self.assertConfigUntouched()

    def test_a_configured_bind_is_used_without_listing_addresses(self) -> None:
        Path(self.a, "config.json").write_text(json.dumps({"network": {"deviceName": "alpha", "bind": "192.168.50.11"}}))
        self.addresses = []
        self.assertEqual(self.h.run("discoverable", "on"), 0, self.h.errors)
        self.assertEqual(self.config()["bind"], "192.168.50.11")

    def test_a_saved_bind_that_is_gone_is_replaced_by_the_current_address(self) -> None:
        Path(self.a, "config.json").write_text(json.dumps({"network": {"deviceName": "alpha", "bind": "192.168.50.11"}}))
        self.addresses = [BIND, "127.0.0.1"]
        h = self.harness(self.a, bind_available=lambda address: address != "192.168.50.11")
        self.assertEqual(h.run("discoverable", "on"), 0, h.errors)
        self.assertIn("The saved address 192.168.50.11 is no longer assigned to this machine.", h.text)
        self.assertIn(f"Using {BIND} on Ethernet.", h.text)
        self.assertEqual(self.config()["bind"], BIND)

    def test_prepare_uses_the_current_address_rather_than_the_saved_one(self) -> None:
        Path(self.a, "config.json").write_text(json.dumps({"network": {"deviceName": "alpha", "bind": "192.168.50.11"}}))
        self.addresses = [BIND]
        svc = self.harness(self.a, bind_available=lambda address: address != "192.168.50.11").services
        prepared = hub_cli.prepare(svc, self.deps(), None)
        self.assertEqual((prepared.bind, prepared.iface.address), (BIND, BIND))

    def test_a_saved_bind_that_is_gone_with_no_current_address_names_the_fix(self) -> None:
        Path(self.a, "config.json").write_text(json.dumps({"network": {"deviceName": "alpha", "bind": "192.168.50.11"}}))
        self.addresses = ["127.0.0.1"]
        h = self.harness(self.a, bind_available=lambda address: address != "192.168.50.11")
        self.assertEqual(h.run("discoverable", "on"), 1)
        self.assertIn("--bind", h.errors)
        self.assertEqual(self.config()["bind"], "192.168.50.11")

    def test_a_bind_option_for_an_address_that_is_gone_is_still_used_as_given(self) -> None:
        h = self.harness(self.a, bind_available=lambda _address: False)
        self.assertEqual(h.run("discoverable", "on", "--bind", BIND), 0, h.errors)
        self.assertEqual(self.config()["bind"], BIND)

    def test_the_bind_option_wins_over_the_configured_bind(self) -> None:
        Path(self.a, "config.json").write_text(json.dumps({"network": {"deviceName": "alpha", "bind": "192.168.50.11"}}))
        self.assertEqual(self.h.run("discoverable", "on", "--bind", BIND), 0, self.h.errors)
        self.assertEqual(self.config()["bind"], BIND)

    @unittest.skipUnless(HAS_OPENSSL, "needs an openssl")
    def test_a_missing_identity_is_created_with_the_found_openssl(self) -> None:
        home = make_root(self)
        deps = dataclasses.replace(self.deps(), find_openssl=lambda _configured, _env: str(OPENSSL))
        h = self.harness(home, pair=deps)
        self.assertEqual(h.run("discoverable", "on"), 0, h.errors)
        self.assertIn(f"identity with openssl at {OPENSSL}", h.text)
        self.assertIsNotNone(device_of(home))
        self.assertEqual(json.loads(Path(home, "config.json").read_text())["network"]["openssl"], str(OPENSSL))

    def test_a_missing_openssl_stops_before_anything_starts(self) -> None:
        home = make_root(self)
        h = self.harness(home)
        self.assertEqual(h.run("discoverable", "on"), 1)
        self.assertIn("No openssl", h.errors)
        self.assertIn("lan setup --openssl", h.errors)
        self.assertEqual((h.spawned, h.logins, h.firewall), ([], [], []))
        self.assertFalse(os.path.exists(os.path.join(home, "net", "identity")))


class FindOpensslTest(unittest.TestCase):
    def test_find_openssl_prefers_git_for_windows_then_the_path(self) -> None:
        root = make_root(self)
        git = os.path.join(root, "Git", "usr", "bin")
        os.makedirs(git)
        exe = os.path.join(git, "openssl.exe")
        Path(exe).write_text("x")
        other = os.path.join(root, "path-openssl")
        Path(other).write_text("x")
        self.assertEqual(hub_cli.find_openssl(None, {"ProgramFiles": root}, "win32", lambda _n: other), exe)
        self.assertIsNone(hub_cli.find_openssl(None, {"ProgramFiles": os.path.join(root, "none")}, "win32", lambda _n: other))
        self.assertEqual(hub_cli.find_openssl(None, {}, "linux", lambda _n: other), other)
        self.assertIsNone(hub_cli.find_openssl(None, {}, "linux", lambda _n: None))
        self.assertEqual(hub_cli.find_openssl(other, {}, "linux", lambda _n: None), other)
        self.assertIsNone(hub_cli.find_openssl("relative/openssl", {}, "linux", lambda _n: None))


class PolicyFilesTest(unittest.TestCase):
    def test_the_hub_state_file_stays_out_of_policy_json(self) -> None:
        home = make_root(self)
        make_hub(home)
        text = Path(policy_path(home)).read_text(encoding="utf-8")
        self.assertNotIn("key", text.lower())
        self.assertFalse(peer_cert_path(home, "x").exists())
        self.assertFalse(os.path.exists(listener_file(home)))


if __name__ == "__main__":
    unittest.main()

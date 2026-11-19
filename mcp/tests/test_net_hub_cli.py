from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from ide_agent_tabs.clock import now_ms
from ide_agent_tabs.net import cli, pairing
from ide_agent_tabs.net.client import peer_cert_path
from ide_agent_tabs.net.hubstate import read_hub_state
from ide_agent_tabs.net.interfaces import InterfaceError, InterfaceInfo
from ide_agent_tabs.net.listener import listener_file
from ide_agent_tabs.net.netid import tagged_network_id
from ide_agent_tabs.net.pair_cli import PairDeps
from ide_agent_tabs.net.pairing import PairResult
from ide_agent_tabs.net.policy import Hub, PeerGrant, Policy, load_network_config, load_policy, policy_path, save_policy
from ide_agent_tabs.net.store import NetStore
from ide_agent_tabs.profiles import TAB_ID_ENV
from net_support import create_home, device_of, make_root
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
        self.user_home = make_root(self, "iat-hub-user-")
        self.session = os.path.join(self.user_home, "code", "app")
        os.makedirs(self.session)
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
        }
        values.update(overrides)
        return Harness(self, home, **values)

    def lan_ready(self, address: str, allow_virtual: bool) -> InterfaceInfo:
        if self.lan_error is not None:
            raise self.lan_error
        return InterfaceInfo("Ethernet", address, 24, "192.168.50.0/24", "Private", True)

    def dial_join(self, address: str, port: int, peer_id: str, *args: Any, **kwargs: Any) -> PairResult:
        self.dialed.append((address, port, peer_id))
        target = self.targets.get(address)
        if target is None:
            return PairResult("failed", f"cannot connect to {address}:{port}: connection refused")
        return pairing.dial_join("127.0.0.1", target, peer_id, *args, **kwargs)

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
        self.h.out.clear()
        self.h.run("discoverable", "status")
        self.assertIn("network gate: this network is not in network.allowedNetworks", self.h.text)
        self.assertIn("serving joins: no", self.h.text)
        self.network = None
        self.h.out.clear()
        self.h.run("discoverable", "status")
        self.assertIn("current network: unreadable", self.h.text)
        self.assertIn("can't be read", self.h.text)

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

    def test_the_view_calls_the_old_format_entry_allowed(self) -> None:
        Path(self.a, "config.json").write_text(json.dumps({"network": {"network": "lan", "bind": BIND, "allowedNetworks": [self.LEGACY]}}))
        self.network = tagged_network_id(self.UPGRADED, legacy=self.LEGACY)
        self.assertEqual(self.h.run("networks"), 0)
        self.assertIn(f"current network: {self.UPGRADED} (allowed)", self.h.text)

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
    def serve(self, home: str, address: str = PEER) -> JoinServer:
        server = JoinServer(self, home)
        self.targets[address] = server.port
        return server

    def test_join_dials_the_open_join_and_sets_up_the_machine(self) -> None:
        make_hub(self.b)
        server = self.serve(self.b)
        self.assertEqual(self.h.run("hub", "join", device_of(self.b), "--address", PEER), 0, self.h.errors)
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
        self.assertEqual(self.h.run("hub", "join", device_of(self.c), "--address", PEER), 1)
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
        self.assertEqual(self.h.run("hub", "join", device_of(self.b), "--address", PEER), 1)
        self.assertIn("already-in-another-hub", self.h.errors)
        self.assertEqual((self.members(self.a), self.members(self.b)), ([], []))
        self.assertConfigUntouched()
        self.assertEqual(self.h.spawned, [])

    def test_a_join_that_cannot_connect_changes_nothing(self) -> None:
        self.assertEqual(self.h.run("hub", "join", device_of(self.b), "--address", PEER), 1)
        self.assertIn("cannot connect", self.h.errors)
        self.assertConfigUntouched()

    def test_an_address_off_the_subnet_and_a_bad_id_are_refused_before_dialing(self) -> None:
        self.assertEqual(self.h.run("hub", "join", device_of(self.b), "--address", "10.1.2.3"), 1)
        self.assertEqual(self.h.run("hub", "join", "not-an-id", "--address", PEER), 1)
        self.assertEqual(self.h.run("hub", "join", device_of(self.a), "--address", PEER), 1)
        self.assertIn("own device id", self.h.errors)
        self.assertEqual(self.dialed, [])

    def test_an_unreadable_network_stops_the_join_before_any_trust_is_saved(self) -> None:
        make_hub(self.b)
        self.serve(self.b)
        self.network = None
        self.assertEqual(self.h.run("hub", "join", device_of(self.b), "--address", PEER), 1)
        self.assertIn("can't be read", self.h.errors)
        self.assertEqual((self.dialed, self.members(self.a)), ([], []))

    def test_a_public_network_stops_the_join_before_dialing(self) -> None:
        self.lan_error = InterfaceError("Ethernet is a Public network.")
        self.assertEqual(self.h.run("hub", "join", device_of(self.b), "--address", PEER), 1)
        self.assertEqual(self.dialed, [])

    def test_join_requires_a_device_id(self) -> None:
        self.assertEqual(self.h.run("hub", "join"), 2)

    def test_create_with_makes_the_hub_then_joins_each_device_and_reports_each(self) -> None:
        self.serve(self.b, PEER)
        self.serve(self.c, "192.168.50.21")
        code = self.h.run(
            "hub", "create", "--name", "home", "--with", f"{device_of(self.b)}@{PEER}", "--with", f"{device_of(self.c)}@192.168.50.21"
        )
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
        self.assertEqual(len(self.h.spawned), 1)

    def test_create_with_reports_a_device_that_fails_and_still_sets_up_for_the_one_that_joined(self) -> None:
        self.serve(self.b, PEER)
        code = self.h.run("hub", "create", "--with", f"{device_of(self.b)}@{PEER}", "--with", f"{device_of(self.c)}@192.168.50.21")
        self.assertEqual(code, 1)
        self.assertEqual([m.label for m in self.members(self.a)], ["bravo"])
        self.assertIn("joined the hub", self.h.text)
        self.assertIn(f"{device_of(self.c)[:15]}@192.168.50.21: did not join", self.h.errors)
        self.assertEqual(load_network_config(self.a)[0].network, "lan")

    def test_create_with_reuses_an_existing_hub(self) -> None:
        make_hub(self.a)
        self.serve(self.b)
        self.assertEqual(self.h.run("hub", "create", "--with", f"{device_of(self.b)}@{PEER}"), 0, self.h.errors)
        self.assertIn("Using the hub home", self.h.text)
        self.assertEqual(load_policy(self.a).hub, Hub(HUB_ID, "home"))

    def test_create_with_a_different_name_for_an_existing_hub_is_refused(self) -> None:
        make_hub(self.a)
        self.assertEqual(self.h.run("hub", "create", "--name", "other", "--with", f"{device_of(self.b)}@{PEER}"), 1)
        self.assertIn("does not match", self.h.errors)

    def test_create_with_rejects_an_empty_address(self) -> None:
        self.assertEqual(self.h.run("hub", "create", "--with", f"{device_of(self.b)}@"), 2)
        self.assertEqual(self.h.run("hub", "create", "--with", f"@{PEER}"), 2)

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


GUARDED: list[tuple[str, ...]] = [
    ("folders", "add", "/x"),
    ("folders", "remove", "/x"),
    ("networks", "add"),
    ("networks", "remove", NETWORK),
    ("discoverable", "on"),
    ("discoverable", "off"),
    ("hub", "create"),
    ("hub", "join", "x", "--address", PEER),
    ("hub", "create", "--with", f"x@{PEER}"),
    ("hub", "leave"),
    ("revoke", "laptop"),
    ("grant", "laptop", "--agent", "claude"),
]
READ_ONLY: list[tuple[str, ...]] = [
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

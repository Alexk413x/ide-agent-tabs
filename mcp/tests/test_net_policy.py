from __future__ import annotations

import json
import os
import unittest
from typing import Any

from ide_agent_tabs.net import policy
from ide_agent_tabs.net.policy import FolderGrant, Hub, NetworkConfig, PeerGrant, Policy, PolicyError
from support import temp_home

DEVICE_A = "AAAAAAA-BBBBBBB-CCCCCCC-DDDDDDD-EEEEEEE-FFFFFFF-GGGGGGG-HHHHHHH"
DEVICE_B = "BBBBBBB-CCCCCCC-DDDDDDD-EEEEEEE-FFFFFFF-GGGGGGG-HHHHHHH-AAAAAAA"


RAW_A = '{"label": "a", "address": "10.0.0.1"}'
RAW_B = '{"label": "b", "address": "10.0.0.2"}'


def write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def write_config(home: str, network: Any) -> None:
    write(os.path.join(home, "config.json"), json.dumps({"network": network}))


def write_policy(home: str, obj: Any) -> None:
    write(policy.policy_path(home), obj if isinstance(obj, str) else json.dumps(obj))


def peer_obj(**extra: Any) -> dict[str, Any]:
    return {"label": "laptop", "address": "192.168.1.20", **extra}


class NetworkConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self.home = temp_home(self)

    def test_missing_file_and_missing_section_are_off_without_warnings(self) -> None:
        self.assertEqual(policy.load_network_config(self.home), (NetworkConfig(), []))
        write(os.path.join(self.home, "config.json"), "{}")
        self.assertEqual(policy.load_network_config(self.home), (NetworkConfig(), []))

    def test_defaults(self) -> None:
        config = NetworkConfig()
        self.assertEqual((config.network, config.bind, config.port, config.openssl), ("off", None, 47829, None))
        self.assertEqual((config.outbound_folders, config.max_remote_tabs), ((), 4))

    def test_reads_every_key(self) -> None:
        folder = os.path.abspath(os.sep + "work")
        openssl = os.path.abspath(os.sep + os.path.join("usr", "bin", "openssl"))
        write_config(
            self.home,
            {"network": "lan", "bind": "10.1.2.3", "port": 5000, "openssl": openssl, "outboundFolders": [folder], "maxRemoteTabs": 2},
        )
        config, warnings = policy.load_network_config(self.home)
        self.assertEqual(warnings, [])
        self.assertEqual(config, NetworkConfig("lan", "10.1.2.3", 5000, openssl, (folder,), 2))

    def test_member_folders_and_allowed_networks_default_to_empty(self) -> None:
        config = NetworkConfig()
        self.assertEqual((config.member_folders, config.allowed_networks), ((), ()))
        write_config(self.home, {"network": "lan", "bind": "10.1.2.3"})
        config, _ = policy.load_network_config(self.home)
        self.assertEqual((config.member_folders, config.allowed_networks), ((), ()))

    def test_member_folders_and_allowed_networks_are_read(self) -> None:
        folder = os.path.abspath(os.sep + "projects")
        ids = ["{3F2A9C1E-0B7D-4E55-9A41-6C2D8E7F1A00}", "gw:aa-bb-cc-dd-ee-ff.192.168.1.0.24", "home_net.1"]
        write_config(self.home, {"network": "lan", "memberFolders": [folder], "allowedNetworks": ids})
        config, warnings = policy.load_network_config(self.home)
        self.assertEqual(warnings, [])
        self.assertEqual(config.member_folders, (folder,))
        self.assertEqual(config.allowed_networks, tuple(ids))
        write_config(self.home, {"network": "lan", "memberFolders": [], "allowedNetworks": []})
        config, warnings = policy.load_network_config(self.home)
        self.assertEqual((config.member_folders, config.allowed_networks, warnings), ((), (), []))

    def test_invalid_member_folders_and_allowed_networks_name_the_key(self) -> None:
        cases: list[tuple[dict[str, Any], str]] = [
            ({"memberFolders": "x"}, "network.memberFolders"),
            ({"memberFolders": {"a": 1}}, "network.memberFolders"),
            ({"memberFolders": ["relative"]}, "network.memberFolders"),
            ({"memberFolders": [os.path.abspath(os.sep), 5]}, "network.memberFolders"),
            ({"memberFolders": [""]}, "network.memberFolders"),
            ({"allowedNetworks": "home"}, "network.allowedNetworks"),
            ({"allowedNetworks": [5]}, "network.allowedNetworks"),
            ({"allowedNetworks": [""]}, "network.allowedNetworks"),
            ({"allowedNetworks": ["has space"]}, "network.allowedNetworks"),
            ({"allowedNetworks": ["slash/net"]}, "network.allowedNetworks"),
            ({"allowedNetworks": ["x" * 129]}, "network.allowedNetworks"),
            ({"allowedNetworks": ["home", "home"]}, "network.allowedNetworks"),
            ({"allowedNetworks": ["ok", None]}, "network.allowedNetworks"),
        ]
        for extra, key in cases:
            with self.subTest(case=extra):
                write_config(self.home, {"network": "lan", **extra})
                config, warnings = policy.load_network_config(self.home)
                self.assertEqual(config, NetworkConfig())
                self.assertTrue(any(key in w for w in warnings))

    def test_allowed_network_id_length_limits(self) -> None:
        write_config(self.home, {"network": "lan", "allowedNetworks": ["x", "y" * 128]})
        config, warnings = policy.load_network_config(self.home)
        self.assertEqual((config.allowed_networks, warnings), (("x", "y" * 128), []))

    def test_unknown_key_warns_and_keeps_the_rest(self) -> None:
        write_config(self.home, {"network": "lan", "bind": "192.168.0.9", "color": "red"})
        config, warnings = policy.load_network_config(self.home)
        self.assertEqual((config.network, config.bind), ("lan", "192.168.0.9"))
        self.assertEqual(len(warnings), 1)
        self.assertIn("network.color", warnings[0])

    def test_later_stage_and_unknown_modes_are_off_with_a_warning(self) -> None:
        for mode in ("tailscale", "team", "LAN", 1, True):
            with self.subTest(mode=mode):
                write_config(self.home, {"network": mode, "bind": "192.168.0.9"})
                config, warnings = policy.load_network_config(self.home)
                self.assertEqual(config, NetworkConfig())
                self.assertEqual(len(warnings), 1)

    def test_tailscale_warning_names_the_later_stage(self) -> None:
        write_config(self.home, {"network": "tailscale", "bind": "100.100.1.1"})
        _, warnings = policy.load_network_config(self.home)
        self.assertTrue(any("later stage" in w for w in warnings))

    def test_invalid_values_fall_back_to_off(self) -> None:
        cases: list[dict[str, Any]] = [
            {"network": "lan", "bind": "0.0.0.0"},
            {"network": "lan", "bind": "8.8.8.8"},
            {"network": "lan", "bind": 5},
            {"network": "lan", "port": 0},
            {"network": "lan", "port": 70000},
            {"network": "lan", "port": True},
            {"network": "lan", "port": "47829"},
            {"network": "lan", "openssl": "openssl"},
            {"network": "lan", "outboundFolders": "x"},
            {"network": "lan", "outboundFolders": ["relative"]},
            {"network": "lan", "maxRemoteTabs": -1},
            {"network": "lan", "maxRemoteTabs": 1.5},
        ]
        for case in cases:
            with self.subTest(case=case):
                write_config(self.home, case)
                config, warnings = policy.load_network_config(self.home)
                self.assertEqual(config, NetworkConfig())
                self.assertTrue(warnings)

    def test_unusable_config_is_off_with_a_warning(self) -> None:
        for text in ("not json", "[]", '{"network": NaN}', '{"network": []}', '{"network": "lan"}'):
            with self.subTest(text=text):
                write(os.path.join(self.home, "config.json"), text)
                config, warnings = policy.load_network_config(self.home)
                self.assertEqual(config, NetworkConfig())
                self.assertTrue(warnings)


class BindTest(unittest.TestCase):
    def test_table(self) -> None:
        cases = {
            "192.168.1.5": True,
            "192.168.255.255": True,
            "10.0.0.1": True,
            "10.255.255.254": True,
            "172.16.0.1": True,
            "172.31.255.1": True,
            "fd00::1": True,
            "fc00::5": True,
            "fdff:ffff::1": True,
            "127.0.0.1": False,
            "::1": False,
            "0.0.0.0": False,
            "::": False,
            "169.254.1.1": False,
            "fe80::1": False,
            "fe80::1%eth0": False,
            "100.100.1.1": False,
            "100.64.0.1": False,
            "8.8.8.8": False,
            "172.15.0.1": False,
            "172.32.0.1": False,
            "192.169.0.1": False,
            "11.0.0.1": False,
            "2001:db8::1": False,
            "::ffff:192.168.1.5": False,
            "224.0.0.1": False,
            "255.255.255.255": False,
            "": False,
            "localhost": False,
            "192.168.1.5 ": False,
            "192.168.1": False,
        }
        for address, expected in cases.items():
            with self.subTest(address=address):
                self.assertIs(policy.is_allowed_bind(address), expected)

    def test_non_string_is_refused(self) -> None:
        self.assertFalse(policy.is_allowed_bind(None))  # type: ignore[arg-type]


class PolicyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.home = temp_home(self)

    def test_missing_file_is_an_empty_policy(self) -> None:
        self.assertEqual(policy.load_policy(self.home), Policy())

    def test_new_peer_has_documented_defaults_and_no_grants(self) -> None:
        write_policy(self.home, {"peers": {DEVICE_A: peer_obj()}})
        loaded = policy.load_policy(self.home)
        peer = policy.grant_for(loaded, DEVICE_A)
        assert peer is not None
        self.assertEqual(
            peer,
            PeerGrant(DEVICE_A, "laptop", "192.168.1.20", (), (), (), (), 1, 6, 10, False),
        )
        self.assertIsNone(policy.grant_for(loaded, DEVICE_B))

    def test_full_peer_loads(self) -> None:
        folder = os.path.abspath(os.sep + "proj")
        write_policy(
            self.home,
            {
                "generation": 7,
                "peers": {
                    DEVICE_A: peer_obj(
                        folders=[{"alias": "proj", "path": folder, "dev": 3, "ino": 99}],
                        agents=["claude", "codex"],
                        models=["opus"],
                        send=["s-1", "s_2"],
                        maxTabs=2,
                        opensPerHour=3,
                        messagesPerMinute=5,
                        approveEachOpen=True,
                    ),
                    DEVICE_B: {"label": "desk", "address": "fd00::2"},
                },
            },
        )
        loaded = policy.load_policy(self.home)
        self.assertEqual(loaded.generation, 7)
        peer = policy.grant_for(loaded, DEVICE_A)
        assert peer is not None
        self.assertEqual(peer.folders, (FolderGrant("proj", folder, 3, 99),))
        self.assertEqual((peer.agents, peer.models, peer.send), (("claude", "codex"), ("opus",), ("s-1", "s_2")))
        self.assertEqual((peer.max_tabs, peer.opens_per_hour, peer.messages_per_minute, peer.approve_each_open), (2, 3, 5, True))
        self.assertEqual(policy.folder_grant(peer, "proj"), FolderGrant("proj", folder, 3, 99))
        self.assertIsNone(policy.folder_grant(peer, "other"))

    def test_invalid_policies_fail_closed(self) -> None:
        folder = os.path.abspath(os.sep + "proj")
        good_folder = {"alias": "proj", "path": folder, "dev": 1, "ino": 2}
        cases: dict[str, Any] = {
            "not json": "{",
            "array": "[]",
            "nan": '{"generation": NaN, "peers": {}}',
            "duplicate device id": f'{{"peers": {{"{DEVICE_A}": {RAW_A}, "{DEVICE_A}": {RAW_B}}}}}',
            "duplicate key in peer": f'{{"peers": {{"{DEVICE_A}": {{"label": "a", "label": "b", "address": "10.0.0.1"}}}}}}',
            "device ids differing in case": {
                "peers": {DEVICE_A: peer_obj(label="a"), DEVICE_A.lower(): peer_obj(label="b")},
            },
            "duplicate label": {"peers": {DEVICE_A: peer_obj(), DEVICE_B: peer_obj()}},
            "unknown top key": {"peers": {}, "extra": 1},
            "negative generation": {"generation": -1, "peers": {}},
            "peers not an object": {"peers": []},
            "peer not an object": {"peers": {DEVICE_A: []}},
            "bad device id": {"peers": {"not a device id!": peer_obj()}},
            "unknown peer key": {"peers": {DEVICE_A: peer_obj(color="red")}},
            "person": {"peers": {DEVICE_A: peer_obj(person="sam")}},
            "expires": {"peers": {DEVICE_A: peer_obj(expires=123)}},
            "paused": {"peers": {DEVICE_A: peer_obj(paused=True)}},
            "missing label": {"peers": {DEVICE_A: {"address": "10.0.0.1"}}},
            "uppercase label": {"peers": {DEVICE_A: peer_obj(label="Laptop")}},
            "long label": {"peers": {DEVICE_A: peer_obj(label="a" * 33)}},
            "empty label": {"peers": {DEVICE_A: peer_obj(label="")}},
            "underscore label": {"peers": {DEVICE_A: peer_obj(label="my_pc")}},
            "label with newline": {"peers": {DEVICE_A: peer_obj(label="a\n")}},
            "missing address": {"peers": {DEVICE_A: {"label": "a"}}},
            "hostname address": {"peers": {DEVICE_A: peer_obj(address="laptop.local")}},
            "scoped address": {"peers": {DEVICE_A: peer_obj(address="fe80::1%eth0")}},
            "unspecified address": {"peers": {DEVICE_A: peer_obj(address="0.0.0.0")}},
            "unsupported agent": {"peers": {DEVICE_A: peer_obj(agents=["copilot"])}},
            "duplicate agent": {"peers": {DEVICE_A: peer_obj(agents=["claude", "claude"])}},
            "agents not a list": {"peers": {DEVICE_A: peer_obj(agents="claude")}},
            "empty model": {"peers": {DEVICE_A: peer_obj(models=[""])}},
            "send with a space": {"peers": {DEVICE_A: peer_obj(send=["a b"])}},
            "limit as bool": {"peers": {DEVICE_A: peer_obj(maxTabs=True)}},
            "limit negative": {"peers": {DEVICE_A: peer_obj(opensPerHour=-1)}},
            "limit too large": {"peers": {DEVICE_A: peer_obj(messagesPerMinute=policy.MAX_LIMIT + 1)}},
            "limit as float": {"peers": {DEVICE_A: peer_obj(maxTabs=1.0)}},
            "approve as int": {"peers": {DEVICE_A: peer_obj(approveEachOpen=1)}},
            "folder unknown key": {"peers": {DEVICE_A: peer_obj(folders=[{**good_folder, "x": 1}])}},
            "folder missing ino": {"peers": {DEVICE_A: peer_obj(folders=[{"alias": "proj", "path": folder, "dev": 1}])}},
            "folder relative path": {"peers": {DEVICE_A: peer_obj(folders=[{**good_folder, "path": "proj"}])}},
            "folder UNC path": {"peers": {DEVICE_A: peer_obj(folders=[{**good_folder, "path": "\\\\server\\share"}])}},
            "folder bad alias": {"peers": {DEVICE_A: peer_obj(folders=[{**good_folder, "alias": "Proj"}])}},
            "folder duplicate alias": {"peers": {DEVICE_A: peer_obj(folders=[good_folder, good_folder])}},
            "folder dev as string": {"peers": {DEVICE_A: peer_obj(folders=[{**good_folder, "dev": "1"}])}},
            "folder not an object": {"peers": {DEVICE_A: peer_obj(folders=["proj"])}},
        }
        for name, case in cases.items():
            with self.subTest(name):
                write_policy(self.home, case)
                with self.assertRaises(PolicyError):
                    policy.load_policy(self.home)

    def test_valid_neighbours_of_the_invalid_cases_load(self) -> None:
        write_policy(self.home, {"peers": {DEVICE_A: peer_obj(label="a-1"), DEVICE_B: peer_obj(label="b" * 32, address="fd12::1")}})
        self.assertEqual(len(policy.load_policy(self.home).peers), 2)

    def test_one_bad_peer_refuses_the_whole_file(self) -> None:
        write_policy(self.home, {"peers": {DEVICE_A: peer_obj(), DEVICE_B: peer_obj(label="b", agents=["copilot"])}})
        with self.assertRaises(PolicyError):
            policy.load_policy(self.home)

    def test_save_round_trips(self) -> None:
        folder = os.path.abspath(os.sep + "proj")
        original = Policy(
            (
                PeerGrant(
                    DEVICE_A,
                    "laptop",
                    "192.168.1.20",
                    (FolderGrant("proj", folder, 4, 5),),
                    ("claude",),
                    ("opus", "sonnet"),
                    ("s-1",),
                    2,
                    3,
                    4,
                    True,
                ),
                PeerGrant(DEVICE_B, "desk", "fd00::9"),
            ),
            5,
        )
        policy.save_policy(self.home, original)
        self.assertEqual(policy.load_policy(self.home), original)
        self.assertEqual(os.listdir(os.path.dirname(policy.policy_path(self.home))), ["policy.json"])

    def test_save_refuses_what_load_would_refuse(self) -> None:
        for bad in (
            Policy((PeerGrant(DEVICE_A, "Bad Label", "10.0.0.1"),)),
            Policy((PeerGrant(DEVICE_A, "a", "10.0.0.1"), PeerGrant(DEVICE_A, "b", "10.0.0.2"))),
            Policy((PeerGrant(DEVICE_A, "a", "10.0.0.1", agents=("copilot",)),)),
        ):
            with self.subTest(bad=bad), self.assertRaises(PolicyError):
                policy.save_policy(self.home, bad)
        self.assertFalse(os.path.exists(policy.policy_path(self.home)))

    def test_save_does_not_change_generation(self) -> None:
        policy.save_policy(self.home, Policy((), 3))
        self.assertEqual(policy.load_policy(self.home).generation, 3)


HUB_ID = "0123456789abcdef0123456789abcdef"


class HubPolicyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.home = temp_home(self)

    def test_a_policy_without_a_hub_loads_with_no_hub_and_no_members(self) -> None:
        write_policy(self.home, {"generation": 2, "peers": {DEVICE_A: peer_obj()}})
        loaded = policy.load_policy(self.home)
        self.assertIsNone(loaded.hub)
        self.assertFalse(loaded.peers[0].member)

    def test_a_hub_and_member_peers_load(self) -> None:
        write_policy(
            self.home,
            {
                "hub": {"id": HUB_ID, "name": "home-1"},
                "peers": {DEVICE_A: peer_obj(member=True), DEVICE_B: {"label": "old", "address": "10.0.0.2"}},
            },
        )
        loaded = policy.load_policy(self.home)
        self.assertEqual(loaded.hub, Hub(HUB_ID, "home-1"))
        self.assertEqual([(p.label, p.member) for p in loaded.peers], [("laptop", True), ("old", False)])

    def test_a_hub_with_no_members_loads(self) -> None:
        write_policy(self.home, {"hub": {"id": HUB_ID, "name": "a"}, "peers": {}})
        self.assertEqual(policy.load_policy(self.home).hub, Hub(HUB_ID, "a"))

    def test_invalid_hubs_and_members_fail_closed(self) -> None:
        cases: dict[str, Any] = {
            "hub not an object": {"hub": "home", "peers": {}},
            "hub null": {"hub": None, "peers": {}},
            "hub unknown key": {"hub": {"id": HUB_ID, "name": "a", "x": 1}, "peers": {}},
            "hub missing id": {"hub": {"name": "a"}, "peers": {}},
            "hub missing name": {"hub": {"id": HUB_ID}, "peers": {}},
            "hub id too short": {"hub": {"id": HUB_ID[:-1], "name": "a"}, "peers": {}},
            "hub id too long": {"hub": {"id": HUB_ID + "0", "name": "a"}, "peers": {}},
            "hub id uppercase": {"hub": {"id": HUB_ID.upper().replace("0", "A"), "name": "a"}, "peers": {}},
            "hub id not hex": {"hub": {"id": "g" * 32, "name": "a"}, "peers": {}},
            "hub id with newline": {"hub": {"id": HUB_ID[:-1] + "\n", "name": "a"}, "peers": {}},
            "hub name uppercase": {"hub": {"id": HUB_ID, "name": "Home"}, "peers": {}},
            "hub name empty": {"hub": {"id": HUB_ID, "name": ""}, "peers": {}},
            "hub name too long": {"hub": {"id": HUB_ID, "name": "a" * 33}, "peers": {}},
            "hub name with a space": {"hub": {"id": HUB_ID, "name": "a b"}, "peers": {}},
            "hub name as number": {"hub": {"id": HUB_ID, "name": 5}, "peers": {}},
            "duplicate hub key": f'{{"hub": {{"id": "{HUB_ID}", "id": "{HUB_ID}", "name": "a"}}, "peers": {{}}}}',
            "member as int": {"hub": {"id": HUB_ID, "name": "a"}, "peers": {DEVICE_A: peer_obj(member=1)}},
            "member as string": {"hub": {"id": HUB_ID, "name": "a"}, "peers": {DEVICE_A: peer_obj(member="true")}},
            "member without a hub": {"peers": {DEVICE_A: peer_obj(member=True)}},
        }
        for name, case in cases.items():
            with self.subTest(name):
                write_policy(self.home, case)
                with self.assertRaises(PolicyError):
                    policy.load_policy(self.home)

    def test_a_non_member_peer_without_a_hub_still_loads_with_member_false(self) -> None:
        write_policy(self.home, {"peers": {DEVICE_A: peer_obj(member=False)}})
        self.assertFalse(policy.load_policy(self.home).peers[0].member)

    def test_save_round_trips_the_hub_and_the_member_flag(self) -> None:
        original = Policy(
            (PeerGrant(DEVICE_A, "laptop", "192.168.1.20", member=True), PeerGrant(DEVICE_B, "desk", "fd00::9")), 4, Hub(HUB_ID, "home")
        )
        policy.save_policy(self.home, original)
        self.assertEqual(policy.load_policy(self.home), original)
        with open(policy.policy_path(self.home), encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual(saved["hub"], {"id": HUB_ID, "name": "home"})
        self.assertIs(saved["peers"][DEVICE_A]["member"], True)
        self.assertNotIn("member", saved["peers"][DEVICE_B])

    def test_a_policy_without_a_hub_saves_without_a_hub_key(self) -> None:
        policy.save_policy(self.home, Policy((PeerGrant(DEVICE_A, "laptop", "192.168.1.20"),), 1))
        with open(policy.policy_path(self.home), encoding="utf-8") as f:
            self.assertEqual(set(json.load(f)), {"generation", "peers"})

    def test_save_refuses_a_member_without_a_hub(self) -> None:
        with self.assertRaises(PolicyError):
            policy.save_policy(self.home, Policy((PeerGrant(DEVICE_A, "laptop", "192.168.1.20", member=True),)))
        with self.assertRaises(PolicyError):
            policy.save_policy(self.home, Policy((), 0, Hub("short", "home")))
        self.assertFalse(os.path.exists(policy.policy_path(self.home)))


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from typing import Any

from ide_agent_tabs.clock import now_ms
from ide_agent_tabs.net import cli
from ide_agent_tabs.net.client import peer_cert_path, peers_dir
from ide_agent_tabs.net.hub import default_member_folder
from ide_agent_tabs.net.listener import log_file
from ide_agent_tabs.net.outbound import OFFLINE_AFTER_MS
from ide_agent_tabs.net.policy import Hub, PeerGrant, Policy, load_network_config, load_policy, policy_path, save_policy
from ide_agent_tabs.net.private_files import ensure_private_dir
from ide_agent_tabs.net.store import NetStore
from net_support import make_root
from test_net_cli import Harness

HUB_ID = "0123456789abcdef0123456789abcdef"
HOME_HUB = Hub(HUB_ID, "home")
DEVICE_A = "AAAAAAA-BBBBBBB-CCCCCCC-DDDDDDD-EEEEEEE-FFFFFFF-GGGGGGG-HHHHHHH"
DEVICE_B = "BBBBBBB-CCCCCCC-DDDDDDD-EEEEEEE-FFFFFFF-GGGGGGG-HHHHHHH-AAAAAAA"
DEVICE_C = "CCCCCCC-DDDDDDD-EEEEEEE-FFFFFFF-GGGGGGG-HHHHHHH-AAAAAAA-BBBBBBB"


class DefaultMemberFolderTest(unittest.TestCase):
    def check(self, session: str, home: str, expected: str) -> None:
        self.assertEqual(default_member_folder(session, home), expected, f"{session} with home {home}")

    def test_windows_paths_give_the_folder_below_the_drive_root(self) -> None:
        home = "C:\\Users\\me"
        self.check("C:\\Projects\\X", home, "C:\\Projects")
        self.check("C:\\Projects\\Plugins\\ide-agent-tabs", home, "C:\\Projects")
        self.check("C:\\Projects", home, "C:\\Projects")
        self.check("D:\\work\\a\\b\\c", home, "D:\\work")

    def test_a_session_inside_the_home_folder_gives_the_folder_one_level_below_home(self) -> None:
        self.check("C:\\Users\\me\\code\\x", "C:\\Users\\me", "C:\\Users\\me\\code")
        self.check("C:\\Users\\me\\code", "C:\\Users\\me", "C:\\Users\\me\\code")
        self.check("C:\\Users\\me\\code\\x\\y\\z", "C:\\Users\\me", "C:\\Users\\me\\code")
        self.check("/home/me/dev/x", "/home/me", "/home/me/dev")
        self.check("/Users/me/Documents/x", "/Users/me", "/Users/me/Documents")

    def test_posix_paths_give_the_folder_below_the_root(self) -> None:
        self.check("/srv/work/x", "/home/me", "/srv")
        self.check("/work/a/b", "/home/me", "/work")
        self.check("/work", "/home/me", "/work")
        self.check("/mnt/data/projects/x", "/root", "/mnt")

    def test_the_home_folder_itself_gives_the_session_folder(self) -> None:
        self.check("C:\\Users\\me", "C:\\Users\\me", "C:\\Users\\me")
        self.check("/home/me", "/home/me", "/home/me")
        self.check("/root", "/root", "/root")

    def test_a_drive_root_or_slash_gives_the_session_folder(self) -> None:
        self.check("C:\\", "C:\\Users\\me", "C:\\")
        self.check("c:/", "C:\\Users\\me", "c:\\")
        self.check("/", "/home/me", "/")

    def test_a_folder_that_contains_the_home_folder_gives_the_session_folder(self) -> None:
        self.check("C:\\Users\\other\\x", "C:\\Users\\me", "C:\\Users\\other\\x")
        self.check("C:\\Users", "C:\\Users\\me", "C:\\Users")
        self.check("/home/other/x", "/home/me", "/home/other/x")
        self.check("/home", "/home/me", "/home")

    def test_system_folders_give_the_session_folder(self) -> None:
        for session in (
            "C:\\Windows\\System32\\x",
            "C:\\Program Files\\App\\x",
            "C:\\Program Files (x86)\\App",
            "C:\\ProgramData\\x\\y",
            "c:\\WINDOWS\\Temp",
        ):
            with self.subTest(session):
                self.check(session, "C:\\Users\\me", session)
        for session in ("/usr/local/src/x", "/etc/x", "/bin/x", "/sbin/x", "/var/lib/x", "/System/Library/x", "/Library/x/y", "/opt/x/y"):
            with self.subTest(session):
                self.check(session, "/home/me", session)

    def test_system_names_only_count_for_their_own_style_of_path(self) -> None:
        self.check("C:\\opt\\x", "C:\\Users\\me", "C:\\opt")
        self.check("/Windows/x", "/home/me", "/Windows")

    def test_a_system_folder_inside_the_home_folder_is_not_a_system_folder(self) -> None:
        self.check("C:\\Users\\me\\Windows\\x", "C:\\Users\\me", "C:\\Users\\me\\Windows")
        self.check("/home/me/var/x", "/home/me", "/home/me/var")

    def test_comparisons_ignore_case_for_windows_paths_only(self) -> None:
        self.check("c:\\users\\ME\\Code\\x", "C:\\Users\\me", "c:\\users\\ME\\Code")
        self.check("/home/ME/dev/x", "/home/me", "/home/ME/dev/x")

    def test_the_result_is_normalised(self) -> None:
        self.check("C:\\Projects\\a\\..\\X\\", "C:\\Users\\me", "C:\\Projects")
        self.check("C:/Projects/X", "C:\\Users\\me", "C:\\Projects")
        self.check("/srv//work/./x/", "/home/me", "/srv")

    def test_an_empty_home_is_ignored(self) -> None:
        self.check("C:\\Projects\\X", "", "C:\\Projects")
        self.check("/srv/x", "", "/srv")

    def test_a_home_on_another_drive_is_ignored(self) -> None:
        self.check("D:\\Users\\me\\x", "C:\\Users\\me", "D:\\Users")


class HubCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = make_root(self)
        self.home = os.path.join(self.root, "agent-home")
        os.makedirs(self.home)
        self.session_cwd = ""
        self.h = self.harness()
        self.user_home = self.h.user_home
        self.code = os.path.join(self.user_home, "code")
        self.session = os.path.join(self.code, "app", "src")
        os.makedirs(self.session)
        self.session_cwd = self.session

    def harness(self, **overrides: Any) -> Harness:
        return Harness(self, self.home, cwd=lambda: self.session_cwd, new_hub_id=lambda: HUB_ID, **overrides)

    def write_config(self, **network: Any) -> None:
        Path(self.home, "config.json").write_text(json.dumps({"theme": "dark", "network": network}), encoding="utf-8")

    def config(self) -> dict[str, Any]:
        return json.loads(Path(self.home, "config.json").read_text(encoding="utf-8"))

    def save(self, peers: list[PeerGrant], hub: Hub | None = HOME_HUB) -> None:
        save_policy(self.home, Policy(tuple(peers), 3, hub))

    def member(self, device: str = DEVICE_A, label: str = "laptop", address: str = "192.168.1.20") -> PeerGrant:
        return PeerGrant(device_id=device, label=label, address=address, member=True)

    def generation(self) -> int:
        store = NetStore(self.home)
        try:
            return store.generation()
        finally:
            store.close()


class HubCreateTest(HubCase):
    def test_it_creates_the_hub_and_sets_both_folder_lists_to_the_default(self) -> None:
        self.assertEqual(self.h.run("hub", "create", "--name", "home-1"), 0, self.h.errors)
        policy = load_policy(self.home)
        self.assertEqual(policy.hub, Hub(HUB_ID, "home-1"))
        self.assertEqual(policy.generation, 0)
        config, warnings = load_network_config(self.home)
        self.assertEqual(warnings, [])
        self.assertEqual((config.member_folders, config.outbound_folders), ((self.code,), (self.code,)))
        self.assertIn(HUB_ID, self.h.text)
        self.assertIn(self.code, self.h.text)

    def test_it_asks_for_no_confirmation(self) -> None:
        self.assertEqual(self.h.run("hub", "create"), 0, self.h.errors)
        self.assertEqual(self.h.confirmed, [])

    def test_the_default_name_comes_from_the_device_name(self) -> None:
        self.write_config(deviceName="Desk 1")
        self.assertEqual(self.h.run("hub", "create"), 0, self.h.errors)
        hub = load_policy(self.home).hub
        assert hub is not None
        self.assertEqual(hub.name, "desk-1")

    def test_the_default_name_is_a_valid_name_without_a_device_name_setting(self) -> None:
        self.assertEqual(self.h.run("hub", "create"), 0, self.h.errors)
        hub = load_policy(self.home).hub
        assert hub is not None
        self.assertRegex(hub.name, r"[a-z0-9-]{1,32}")

    def test_the_hub_id_is_32_lowercase_hex_characters_by_default(self) -> None:
        self.assertEqual(self.h.run("hub", "create"), 0)
        plain = cli.Services(home=self.home)
        self.assertRegex(plain.new_hub_id(), r"[0-9a-f]{32}")
        self.assertNotEqual(plain.new_hub_id(), plain.new_hub_id())

    def test_a_bad_name_is_refused_and_nothing_changes(self) -> None:
        for name in ("Home", "a b", "", "a" * 33, "é"):
            with self.subTest(name):
                self.assertEqual(self.h.run("hub", "create", "--name", name), 1)
        self.assertFalse(os.path.exists(policy_path(self.home)))
        self.assertFalse(os.path.exists(os.path.join(self.home, "config.json")))

    def test_a_second_create_is_refused_and_keeps_the_first_hub(self) -> None:
        self.assertEqual(self.h.run("hub", "create", "--name", "first"), 0)
        before = Path(policy_path(self.home)).read_text(encoding="utf-8")
        self.assertEqual(self.h.run("hub", "create", "--name", "second"), 1)
        self.assertIn("already in the hub first", self.h.errors)
        self.assertEqual(Path(policy_path(self.home)).read_text(encoding="utf-8"), before)

    def test_lists_the_user_set_are_kept_and_only_empty_lists_get_the_default(self) -> None:
        mine = os.path.join(self.root, "mine")
        os.makedirs(mine)
        self.write_config(network="lan", bind="192.168.1.5", memberFolders=[mine])
        self.assertEqual(self.h.run("hub", "create"), 0, self.h.errors)
        config, _ = load_network_config(self.home)
        self.assertEqual((config.member_folders, config.outbound_folders), ((mine,), (self.code,)))
        self.assertEqual((config.network, config.bind), ("lan", "192.168.1.5"))
        self.assertEqual(self.config()["theme"], "dark")

    def test_both_lists_are_left_alone_when_both_are_set(self) -> None:
        mine = os.path.join(self.root, "mine")
        os.makedirs(mine)
        self.write_config(memberFolders=[mine], outboundFolders=[mine])
        self.assertEqual(self.h.run("hub", "create"), 0, self.h.errors)
        config, _ = load_network_config(self.home)
        self.assertEqual((config.member_folders, config.outbound_folders), ((mine,), (mine,)))

    def test_the_folder_option_names_the_session_folder(self) -> None:
        other = os.path.join(self.user_home, "work", "x")
        os.makedirs(other)
        self.assertEqual(self.h.run("hub", "create", "--folder", other), 0, self.h.errors)
        config, _ = load_network_config(self.home)
        self.assertEqual(config.member_folders, (os.path.join(self.user_home, "work"),))

    def test_a_session_at_the_home_folder_gets_the_home_folder_itself(self) -> None:
        self.session_cwd = self.user_home
        self.assertEqual(self.h.run("hub", "create"), 0, self.h.errors)
        config, _ = load_network_config(self.home)
        self.assertEqual(config.member_folders, (self.user_home,))

    def test_a_missing_folder_or_a_file_is_refused_and_nothing_changes(self) -> None:
        self.assertEqual(self.h.run("hub", "create", "--folder", os.path.join(self.root, "missing")), 1)
        file = os.path.join(self.root, "afile")
        Path(file).write_text("x", encoding="utf-8")
        self.assertEqual(self.h.run("hub", "create", "--folder", file), 1)
        self.assertFalse(os.path.exists(policy_path(self.home)))

    def test_it_keeps_existing_peers_and_the_generation(self) -> None:
        save_policy(self.home, Policy((PeerGrant(DEVICE_B, "old", "192.168.1.9"),), 5))
        self.assertEqual(self.h.run("hub", "create"), 0, self.h.errors)
        policy = load_policy(self.home)
        self.assertEqual((policy.generation, [p.label for p in policy.peers]), (5, ["old"]))

    def test_usage_errors(self) -> None:
        self.assertEqual(self.h.run("hub"), 2)
        self.assertEqual(self.h.run("hub", "bogus"), 2)
        self.assertEqual(self.h.run("hub", "create", "extra"), 2)
        self.assertIn("agent-tabs net hub", self.h.errors)


class HubStatusTest(HubCase):
    def test_no_hub(self) -> None:
        self.assertEqual(self.h.run("hub", "status"), 0)
        self.assertEqual(self.h.out, ["hub: none"])

    def test_a_hub_with_no_members(self) -> None:
        self.save([])
        self.assertEqual(self.h.run("hub", "status"), 0)
        self.assertIn(f"hub: home ({HUB_ID})", self.h.text)
        self.assertIn("members: none", self.h.text)
        self.assertIn("member folders: none", self.h.text)
        self.assertIn("allowed networks: none", self.h.text)

    def test_members_folders_and_networks_are_listed(self) -> None:
        self.write_config(memberFolders=[self.code], allowedNetworks=["gw:aa-bb.192.168.1.0.24", "home_net"])
        self.save([self.member(), self.member(DEVICE_B, "desk", "192.168.1.21"), PeerGrant(DEVICE_C, "old", "192.168.1.30")])
        self.assertEqual(self.h.run("hub", "status"), 0)
        lines = self.h.out
        self.assertIn(f"  laptop  {DEVICE_A[:15]}  192.168.1.20  unknown", lines)
        self.assertIn(f"  desk  {DEVICE_B[:15]}  192.168.1.21  unknown", lines)
        self.assertNotIn("  old  ", self.h.text)
        self.assertIn(f"member folders: {self.code}", lines)
        self.assertIn("allowed networks: gw:aa-bb.192.168.1.0.24, home_net", lines)

    def test_online_and_offline_follow_the_cached_inventory(self) -> None:
        self.save([self.member(), self.member(DEVICE_B, "desk", "192.168.1.21")])
        store = NetStore(self.home)
        try:
            store.put_inventory(DEVICE_A, '{"sessions":[]}', now_ms() - 1000)
            store.put_inventory(DEVICE_B, '{"sessions":[]}', now_ms() - OFFLINE_AFTER_MS - 60_000)
        finally:
            store.close()
        self.assertEqual(self.h.run("hub", "status"), 0)
        self.assertIn("  192.168.1.20  online", self.h.text)
        self.assertIn("  192.168.1.21  offline", self.h.text)

    def test_status_creates_no_store(self) -> None:
        self.save([self.member()])
        self.assertEqual(self.h.run("hub", "status"), 0)
        self.assertFalse(os.path.exists(os.path.join(self.home, "net", "net.db")))

    def test_an_unreadable_policy_is_an_error(self) -> None:
        Path(policy_path(self.home)).parent.mkdir(parents=True, exist_ok=True)
        Path(policy_path(self.home)).write_text("{not json", encoding="utf-8")
        self.assertEqual(self.h.run("hub", "status"), 1)


class HubLeaveTest(HubCase):
    def test_it_removes_the_hub_and_every_member_but_keeps_other_peers(self) -> None:
        self.save([self.member(), self.member(DEVICE_B, "desk", "192.168.1.21"), PeerGrant(DEVICE_C, "old", "192.168.1.30")])
        ensure_private_dir(peers_dir(self.home))
        for device in (DEVICE_A, DEVICE_B, DEVICE_C):
            peer_cert_path(self.home, device).write_text("pem", encoding="ascii")
        before = self.generation()
        self.assertEqual(self.h.run("hub", "leave"), 0, self.h.errors)
        policy = load_policy(self.home)
        self.assertIsNone(policy.hub)
        self.assertEqual([p.label for p in policy.peers], ["old"])
        self.assertFalse(peer_cert_path(self.home, DEVICE_A).exists())
        self.assertFalse(peer_cert_path(self.home, DEVICE_B).exists())
        self.assertTrue(peer_cert_path(self.home, DEVICE_C).exists())
        self.assertEqual(self.generation(), before + 1)
        self.assertIn("removed 2 member(s)", self.h.text)
        self.assertIn(f"policy generation is {before + 1}", self.h.text)

    def test_it_asks_for_no_confirmation(self) -> None:
        self.save([self.member()])
        self.assertEqual(self.h.run("hub", "leave"), 0, self.h.errors)
        self.assertEqual(self.h.confirmed, [])

    def test_it_leaves_the_config_alone(self) -> None:
        self.write_config(memberFolders=[self.code], outboundFolders=[self.code], allowedNetworks=["home_net"])
        self.save([self.member()])
        before = self.config()
        self.assertEqual(self.h.run("hub", "leave"), 0, self.h.errors)
        self.assertEqual(self.config(), before)

    def test_leaving_twice_is_refused_and_does_not_raise_the_generation_again(self) -> None:
        self.save([self.member()])
        self.assertEqual(self.h.run("hub", "leave"), 0)
        after = self.generation()
        self.assertEqual(self.h.run("hub", "leave"), 1)
        self.assertIn("not in a hub", self.h.errors)
        self.assertEqual(self.generation(), after)

    def test_a_hub_can_be_created_again_after_leaving(self) -> None:
        self.assertEqual(self.h.run("hub", "create", "--name", "one"), 0)
        self.assertEqual(self.h.run("hub", "leave"), 0)
        self.assertEqual(self.h.run("hub", "create", "--name", "two"), 0, self.h.errors)
        hub = load_policy(self.home).hub
        assert hub is not None
        self.assertEqual(hub.name, "two")


class StatusHubTest(HubCase):
    def test_status_shows_the_hub_and_the_member_folders(self) -> None:
        self.write_config(memberFolders=[self.code])
        self.save([self.member()])
        self.assertEqual(self.h.run("status"), 0)
        self.assertIn(f"hub: home ({HUB_ID})", self.h.text)
        self.assertIn(f"member folders: {self.code}", self.h.text)
        self.assertIn(f"  laptop  {DEVICE_A[:15]}  192.168.1.20  grants: hub member", self.h.text)

    def test_status_without_a_hub(self) -> None:
        self.assertEqual(self.h.run("status"), 0)
        self.assertIn("hub: none", self.h.text)
        self.assertIn("member folders: none", self.h.text)

    def test_status_reports_an_unreadable_policy_for_the_hub_too(self) -> None:
        Path(policy_path(self.home)).parent.mkdir(parents=True, exist_ok=True)
        Path(policy_path(self.home)).write_text("{not json", encoding="utf-8")
        self.assertEqual(self.h.run("status"), 0)
        self.assertIn("hub: unreadable", self.h.text)


class FoldersTest(HubCase):
    def folders(self) -> tuple[str, ...]:
        return load_network_config(self.home)[0].member_folders

    def make(self, *parts: str) -> str:
        path = os.path.join(self.root, *parts)
        os.makedirs(path, exist_ok=True)
        return os.path.realpath(path)

    def test_the_view_lists_the_folders_or_none(self) -> None:
        self.assertEqual(self.h.run("folders"), 0)
        self.assertEqual(self.h.out, ["member folders: none", "outbound folders: none"])
        self.h.out.clear()
        self.write_config(memberFolders=[self.code, self.root], outboundFolders=[self.session])
        self.assertEqual(self.h.run("folders"), 0)
        self.assertEqual(self.h.out, [f"member folders: {self.code}, {self.root}", f"outbound folders: {self.session}"])
        self.h.out.clear()
        self.assertEqual(self.h.run("folders", "--outbound"), 0)
        self.assertEqual(self.h.out, [f"outbound folders: {self.session}"])

    def test_outbound_add_and_remove_change_only_the_outbound_folders(self) -> None:
        first, second = self.make("a"), self.make("b")
        self.write_config(memberFolders=[first], outboundFolders=[first])
        self.assertEqual(self.h.run("folders", "add", second, "--outbound"), 0, self.h.errors)
        config = load_network_config(self.home)[0]
        self.assertEqual((config.member_folders, config.outbound_folders), ((first,), (first, second)))
        self.assertEqual(self.h.out[-1], f"outbound folders: {first}, {second}")
        self.assertEqual(self.h.run("folders", "--outbound", "add", second), 0)
        self.assertIn("already an outbound folder", self.h.out[-1])
        self.assertEqual(self.h.run("folders", "remove", first, "--outbound"), 0, self.h.errors)
        config = load_network_config(self.home)[0]
        self.assertEqual((config.member_folders, config.outbound_folders), ((first,), (second,)))
        self.assertEqual(self.h.run("folders", "remove", self.make("c"), "--outbound"), 1)
        self.assertIn("is not an outbound folder", self.h.errors)
        self.assertIn("folders action=remove count=1 kind=outbound", Path(log_file(self.home, "cli.log")).read_text(encoding="utf-8"))

    def test_a_session_opened_by_another_machine_cannot_change_the_outbound_folders(self) -> None:
        folder = self.make("a")
        remote = self.harness(is_remote_origin=lambda _home: True)
        for args in (("add", folder, "--outbound"), ("remove", folder, "--outbound")):
            self.assertEqual(remote.run("folders", *args), 1)
        self.assertIn(cli.NETWORK_SETTINGS_REFUSAL, remote.errors)
        self.assertFalse(os.path.exists(os.path.join(self.home, "config.json")))
        self.assertEqual(remote.run("folders", "--outbound"), 0)

    def test_add_stores_the_real_path_and_keeps_the_other_keys(self) -> None:
        first, second = self.make("a"), self.make("b")
        self.write_config(network="lan", bind="192.168.1.5", memberFolders=[first])
        self.assertEqual(self.h.run("folders", "add", second), 0, self.h.errors)
        self.assertEqual(self.folders(), (first, second))
        self.assertEqual(self.h.out[-1], f"member folders: {first}, {second}")
        self.assertEqual(self.config()["theme"], "dark")
        self.assertEqual(self.config()["network"]["network"], "lan")

    def test_add_without_any_config_creates_one(self) -> None:
        folder = self.make("a")
        self.assertEqual(self.h.run("folders", "add", folder), 0, self.h.errors)
        self.assertEqual(self.folders(), (folder,))

    def test_add_normalises_the_path(self) -> None:
        folder = self.make("a", "b")
        self.assertEqual(self.h.run("folders", "add", os.path.join(folder, "..", "b") + os.sep), 0, self.h.errors)
        self.assertEqual(self.folders(), (folder,))

    def test_adding_a_listed_folder_again_changes_nothing(self) -> None:
        folder = self.make("a")
        self.assertEqual(self.h.run("folders", "add", folder), 0)
        before = Path(self.home, "config.json").read_text(encoding="utf-8")
        self.assertEqual(self.h.run("folders", "add", folder), 0)
        self.assertIn("already a member folder", self.h.out[-1])
        self.assertEqual(Path(self.home, "config.json").read_text(encoding="utf-8"), before)

    @unittest.skipUnless(os.name == "nt", "Windows paths ignore case")
    def test_adding_the_same_folder_in_another_case_is_a_duplicate_on_windows(self) -> None:
        folder = self.make("CaseDir")
        self.assertEqual(self.h.run("folders", "add", folder), 0)
        self.assertEqual(self.h.run("folders", "add", folder.upper()), 0)
        self.assertEqual(self.folders(), (folder,))

    def test_add_refuses_a_relative_missing_or_file_path_and_changes_nothing(self) -> None:
        file = os.path.join(self.root, "afile")
        Path(file).write_text("x", encoding="utf-8")
        for path in ("relative", os.path.join(self.root, "missing"), file):
            with self.subTest(path):
                self.assertEqual(self.h.run("folders", "add", path), 1)
        self.assertFalse(os.path.exists(os.path.join(self.home, "config.json")))

    def test_add_refuses_a_network_path(self) -> None:
        self.assertEqual(self.h.run("folders", "add", "\\\\server\\share\\x"), 1)
        self.assertIn("network", self.h.errors)

    def test_remove_drops_one_folder(self) -> None:
        first, second = self.make("a"), self.make("b")
        self.write_config(memberFolders=[first, second])
        self.assertEqual(self.h.run("folders", "remove", first), 0, self.h.errors)
        self.assertEqual(self.folders(), (second,))
        self.assertEqual(self.h.run("folders", "remove", second + os.sep), 0, self.h.errors)
        self.assertEqual(self.folders(), ())
        self.assertEqual(self.h.out[-1], "member folders: none")

    def test_remove_works_for_a_folder_that_no_longer_exists(self) -> None:
        gone = os.path.join(self.root, "gone")
        self.write_config(memberFolders=[gone])
        self.assertEqual(self.h.run("folders", "remove", gone), 0, self.h.errors)
        self.assertEqual(self.folders(), ())

    def test_remove_refuses_a_folder_that_is_not_listed_or_not_absolute(self) -> None:
        first = self.make("a")
        self.write_config(memberFolders=[first])
        self.assertEqual(self.h.run("folders", "remove", self.make("b")), 1)
        self.assertIn("is not a member folder", self.h.errors)
        self.assertEqual(self.h.run("folders", "remove", "relative"), 1)
        self.assertEqual(self.folders(), (first,))

    def test_the_commands_ask_for_no_confirmation_and_log_each_change(self) -> None:
        folder = self.make("a")
        self.assertEqual(self.h.run("folders", "add", folder), 0)
        self.assertEqual(self.h.run("folders", "remove", folder), 0)
        self.assertEqual(self.h.confirmed, [])
        log = Path(log_file(self.home, "cli.log")).read_text(encoding="utf-8")
        self.assertIn("folders action=add", log)
        self.assertIn("folders action=remove", log)

    def test_usage_errors(self) -> None:
        self.assertEqual(self.h.run("folders", "add"), 2)
        self.assertEqual(self.h.run("folders", "remove"), 2)
        self.assertEqual(self.h.run("folders", "bogus", "x"), 2)
        self.assertEqual(self.h.run("folders", "add", "a", "b"), 2)
        self.assertIn("agent-tabs net folders", self.h.errors)

    def test_a_default_set_by_hub_create_can_be_changed(self) -> None:
        self.assertEqual(self.h.run("hub", "create"), 0, self.h.errors)
        self.assertEqual(self.folders(), (self.code,))
        extra = self.make("extra")
        self.assertEqual(self.h.run("folders", "add", extra), 0)
        self.assertEqual(self.h.run("folders", "remove", self.code), 0)
        self.assertEqual(self.folders(), (extra,))


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from ide_agent_tabs import cli as top_cli
from ide_agent_tabs.files import file_lock
from ide_agent_tabs.net import cli, confirm, notify, tls
from ide_agent_tabs.net.cli import Services
from ide_agent_tabs.net.client import peer_cert_path
from ide_agent_tabs.net.firewall import FirewallError
from ide_agent_tabs.net.identity import format_digest, parse_device_id
from ide_agent_tabs.net.interfaces import InterfaceError, InterfaceInfo
from ide_agent_tabs.net.listener import Build, listener_file, log_file, read_listener, stop_file, write_atomically
from ide_agent_tabs.net.login_task import LoginTaskError
from ide_agent_tabs.net.names import default_label
from ide_agent_tabs.net.pairing import Limits
from ide_agent_tabs.net.policy import PeerGrant, Policy, load_policy, policy_path, save_policy
from ide_agent_tabs.net.private_files import check_private
from ide_agent_tabs.net.store import NetStore, db_path
from net_support import HAS_OPENSSL, OPENSSL, cert_of, create_home, device_of, make_root, reset_peers

NEEDS = unittest.skipUnless(HAS_OPENSSL and tls.runtime_problem() is None, "needs an openssl and a TLS 1.3 Python")


class Harness:
    def __init__(self, test: unittest.TestCase, home: str, **overrides: Any) -> None:
        self.home = home
        self.out: list[str] = []
        self.err: list[str] = []
        self.confirmed: list[list[str]] = []
        self.notices: list[str] = []
        self.spawned: list[tuple[str, int]] = []
        self.events: list[str] = []
        self.logins: list[str] = []
        self.firewall: list[tuple[str, str, str]] = []
        self.interface_error: Exception | None = None
        self.unsupported: str | None = None
        self.login_error: Exception | None = None
        self.firewall_error: Exception | None = None
        self.firewall_removals = 0
        self.build = Build("0.14.2", "0.14.2-aaaaaaaaaaaa")
        self.listener_joins = True
        self.user_home = make_root(test, "iat-net-user-")
        values: dict[str, Any] = {
            "home": home,
            "out": self.out.append,
            "err": self.err.append,
            "confirm": self._confirm,
            "notify": self.notices.append,
            "spawn": self._spawn,
            "build": lambda: self.build,
            "bind_available": lambda _address: True,
            "register_login": self._register_login,
            "remove_login": self._remove_login,
            "login_status": lambda _home: "none",
            "apply_firewall": self._apply_firewall,
            "remove_firewall": self._remove_firewall,
            "interface_for": self._interface_for,
            "unsupported_reason": lambda: self.unsupported,
            "user_home": self.user_home,
            "env": {},
            "start_wait_s": 2.0,
            "stop_wait_s": 2.0,
        }
        values.update(overrides)
        self.services = Services(**values)

    def _confirm(self, lines: Any) -> bool:
        self.confirmed.append(list(lines))
        return True

    def _spawn(self, home: str, server_port: int) -> int | None:
        self.spawned.append((home, server_port))
        self.write_listener_file(home)
        return os.getpid()

    def write_listener_file(self, home: str, **changes: Any) -> None:
        info: dict[str, Any] = {
            "pid": os.getpid(),
            "startedMs": 1,
            "bind": "10.0.0.5",
            "port": 47829,
            "version": self.build.version,
            "build": self.build.id,
            "joins": self.listener_joins,
        }
        info.update(changes)
        write_atomically(str(listener_file(home)), json.dumps({key: value for key, value in info.items() if value is not None}))

    def answer_stop(self) -> threading.Thread:
        def consume() -> None:
            while not stop_file(self.home).exists():
                threading.Event().wait(0.02)
            os.remove(listener_file(self.home))

        thread = threading.Thread(target=consume, daemon=True)
        thread.start()
        return thread

    def _register_login(self, home: str) -> str:
        self.events.append("register-login")
        if self.login_error is not None:
            raise self.login_error
        self.logins.append(home)
        return "Registered the login task."

    def _remove_login(self, _home: str) -> str:
        config = json.loads(Path(self.home, "config.json").read_text())
        self.events.append(f"remove-login after {config['network']['network']} and listener {'up' if read_listener(self.home) else 'down'}")
        if self.login_error is not None:
            raise self.login_error
        return "Removed the login task."

    def _interface_for(self, address: str) -> InterfaceInfo:
        if self.interface_error is not None:
            raise self.interface_error
        return InterfaceInfo("Ethernet", address, 24, ".".join(address.split(".")[:3]) + ".0/24", "Private", True)

    def _apply_firewall(self, bind: str, subnet: str, alias: str) -> str:
        self.events.append("firewall")
        if self.firewall_error is not None:
            raise self.firewall_error
        self.firewall.append((bind, subnet, alias))
        return "Created the firewall rule."

    def _remove_firewall(self) -> str:
        self.events.append("remove-firewall")
        if self.firewall_error is not None:
            raise self.firewall_error
        self.firewall_removals += 1
        return "Removed the firewall rule."

    def run(self, *args: str) -> int:
        return cli.main(list(args), self.services)

    @property
    def text(self) -> str:
        return "\n".join(self.out)

    @property
    def errors(self) -> str:
        return "\n".join(self.err)


def trust_args(cert: Path, identity: str, address: str = "192.168.1.20", label: str = "laptop") -> list[str]:
    return ["trust", str(cert), "--id", identity, "--address", address, "--label", label]


@NEEDS
class CliTestCase(unittest.TestCase):
    a: str
    b: str
    c: str

    @classmethod
    def setUpClass(cls) -> None:
        root = make_root(cls)
        cls.a, cls.b, cls.c = (create_home(root, name) for name in ("a", "b", "c"))

    def setUp(self) -> None:
        reset_peers(self.a)
        for name in ("config.json",):
            with contextlib.suppress(OSError):
                os.remove(os.path.join(self.a, name))
        for path in (listener_file(self.a), stop_file(self.a), Path(log_file(self.a, "cli.log"))):
            with contextlib.suppress(OSError):
                os.remove(path)
        self.h = Harness(self, self.a)
        self.b_id, self.c_id = device_of(self.b), device_of(self.c)

    def trust_b(self, address: str = "192.168.1.20", label: str = "laptop") -> None:
        self.assertEqual(self.h.run(*trust_args(cert_of(self.b), self.b_id, address, label)), 0, self.h.errors)

    def peer(self) -> PeerGrant:
        return load_policy(self.a).peers[0]

    def make_folder(self, name: str = "work") -> str:
        folder = os.path.realpath(tempfile.mkdtemp(prefix=f"iat-net-{name}-"))
        self.addCleanup(os.rmdir, folder)
        return folder

    def trust_in_claude(self, folder: str) -> None:
        path = Path(self.h.user_home) / ".claude.json"
        path.write_text(json.dumps({"projects": {folder.replace("\\", "/"): {"hasTrustDialogAccepted": True}}}), encoding="utf-8")


class SetupTest(unittest.TestCase):
    @NEEDS
    def test_setup_without_a_terminal_changes_nothing(self) -> None:
        home = make_root(self)
        harness = Harness(self, home, confirm=confirm.confirm)
        with mock.patch.object(confirm, "_console", return_value=False):
            self.assertEqual(harness.run("lan", "setup", "--openssl", str(OPENSSL)), 1)
        self.assertIn("interactive terminal", harness.errors)
        self.assertFalse(os.path.exists(os.path.join(home, "net", "identity")))
        self.assertEqual(harness.notices, [])

    @NEEDS
    def test_setup_creates_the_identity_once(self) -> None:
        home = make_root(self)
        harness = Harness(self, home)
        self.assertEqual(harness.run("id"), 1)
        self.assertIn("lan setup", harness.errors)
        self.assertEqual(harness.run("lan", "setup", "--openssl", str(OPENSSL)), 0, harness.errors)
        self.assertEqual(len(harness.confirmed), 1)
        self.assertIn(str(OPENSSL), "\n".join(harness.confirmed[0]))
        identity = device_of(home)
        self.assertIn(f"device id: {identity}", harness.text)
        self.assertEqual(json.loads(Path(home, "config.json").read_text())["network"], {"openssl": str(OPENSSL)})
        self.assertTrue(Path(home, "net", "ingress.token").is_file())
        self.assertEqual(check_private(Path(home, "net", "identity")), [])
        self.assertIn("lan-setup", Path(log_file(home, "cli.log")).read_text())
        self.assertEqual(harness.notices, ["network identity created"])
        harness.out.clear()
        self.assertEqual(harness.run("lan", "setup", "--openssl", str(OPENSSL)), 0)
        self.assertEqual(len(harness.confirmed), 1)
        self.assertIn("already has an identity", harness.text)
        harness.out.clear()
        self.assertEqual(harness.run("id"), 0)
        self.assertIn(identity, harness.text)
        self.assertIn(str(cert_of(home)), harness.text)

    def test_setup_needs_an_absolute_openssl_that_exists(self) -> None:
        harness = Harness(self, make_root(self))
        self.assertEqual(harness.run("lan", "setup", "--openssl", "openssl"), 1)
        self.assertEqual(harness.run("lan", "setup", "--openssl", os.path.join(harness.home, "missing")), 1)
        self.assertEqual(harness.run("lan", "setup"), 2)
        self.assertEqual(harness.confirmed, [])


class TrustTest(CliTestCase):
    def test_a_mismatched_id_is_refused(self) -> None:
        self.assertEqual(self.h.run(*trust_args(cert_of(self.b), self.c_id)), 1)
        self.assertIn("does not match", self.h.errors)
        self.assertEqual(load_policy(self.a).peers, ())
        self.assertEqual(self.h.confirmed, [])
        self.assertFalse(peer_cert_path(self.a, self.c_id).exists())

    def test_short_and_mistyped_ids_are_refused(self) -> None:
        for bad in (self.b_id[:15], self.b_id.replace("-", "")[:-1], self.b_id[:-1] + ("A" if self.b_id[-1] != "A" else "B")):
            self.assertEqual(self.h.run(*trust_args(cert_of(self.b), bad)), 1, bad)
        self.assertEqual(load_policy(self.a).peers, ())

    def test_a_file_that_is_not_a_certificate_is_refused(self) -> None:
        junk = Path(self.make_folder("junk")) / "x.pem"
        self.addCleanup(os.remove, junk)
        junk.write_text("not a certificate")
        self.assertEqual(self.h.run(*trust_args(junk, self.b_id)), 1)
        self.assertEqual(self.h.run(*trust_args(Path(str(junk) + ".missing"), self.b_id)), 1)

    def test_own_id_is_refused(self) -> None:
        self.assertEqual(self.h.run(*trust_args(cert_of(self.a), device_of(self.a))), 1)
        self.assertIn("own device id", self.h.errors)

    def test_trusting_stores_the_certificate_and_grants_nothing(self) -> None:
        self.trust_b()
        self.assertIn("Effective grants: none", self.h.text)
        self.assertEqual(self.h.notices, ["trusted peer laptop at 192.168.1.20"])
        self.assertIn(f"device id: {self.b_id}", "\n".join(self.h.confirmed[0]))
        pem = peer_cert_path(self.a, self.b_id)
        self.assertEqual(pem.read_text().strip(), cert_of(self.b).read_text().strip())
        self.assertEqual(check_private(pem), [])
        saved = json.loads(Path(policy_path(self.a)).read_text())["peers"][self.b_id]
        self.assertEqual((saved["folders"], saved["agents"], saved["models"], saved["send"]), ([], [], [], []))
        self.h.out.clear()
        self.assertEqual(self.h.run("status"), 0)
        self.assertIn("laptop", self.h.text)
        self.assertIn("192.168.1.20", self.h.text)
        self.assertIn("grants: none", self.h.text)
        self.assertIn("trust", Path(log_file(self.a, "cli.log")).read_text())

    def test_an_id_in_another_form_is_stored_in_the_canonical_form(self) -> None:
        loose = self.b_id.replace("-", "").lower()
        self.assertEqual(self.h.run(*trust_args(cert_of(self.b), loose)), 0, self.h.errors)
        self.assertEqual(self.peer().device_id, format_digest(parse_device_id(self.b_id)))

    def test_a_new_address_changes_only_the_address(self) -> None:
        self.trust_b()
        save_policy(self.a, Policy((PeerGrant(self.b_id, "laptop", "192.168.1.20", agents=("claude",), max_tabs=3),)))
        self.h.out.clear()
        self.h.confirmed.clear()
        self.assertEqual(self.h.run(*trust_args(cert_of(self.b), self.b_id, "192.168.1.21", "renamed")), 0, self.h.errors)
        self.assertIn("Updated the address of laptop", self.h.text)
        self.assertIn("192.168.1.20 -> 192.168.1.21", "\n".join(self.h.confirmed[0]))
        peer = self.peer()
        self.assertEqual((peer.label, peer.address, peer.agents, peer.max_tabs), ("laptop", "192.168.1.21", ("claude",), 3))

    def test_the_same_address_again_needs_no_confirmation(self) -> None:
        self.trust_b()
        self.h.confirmed.clear()
        self.assertEqual(self.h.run(*trust_args(cert_of(self.b), self.b_id)), 0)
        self.assertEqual(self.h.confirmed, [])
        self.assertIn("already trusted", self.h.text)

    def test_a_label_of_another_peer_is_refused(self) -> None:
        self.trust_b()
        self.assertEqual(self.h.run(*trust_args(cert_of(self.c), self.c_id, "192.168.1.30", "laptop")), 1)
        self.assertEqual(len(load_policy(self.a).peers), 1)

    def test_bad_addresses_and_labels_are_refused(self) -> None:
        for address in ("0.0.0.0", "224.0.0.1", "fe80::1%5", "host.local", ""):
            self.assertEqual(self.h.run(*trust_args(cert_of(self.b), self.b_id, address)), 1, address)
        for label in ("Laptop", "a b", "", "x" * 33):
            self.assertEqual(self.h.run(*trust_args(cert_of(self.b), self.b_id, label=label)), 1, label)
        self.assertEqual(load_policy(self.a).peers, ())

    def test_trust_without_a_terminal_changes_nothing(self) -> None:
        harness = Harness(self, self.a, confirm=confirm.confirm)
        with mock.patch.object(confirm, "_console", return_value=False):
            self.assertEqual(harness.run(*trust_args(cert_of(self.b), self.b_id)), 1)
        self.assertEqual(load_policy(self.a).peers, ())
        self.assertFalse(peer_cert_path(self.a, self.b_id).exists())

    def test_declining_changes_nothing(self) -> None:
        harness = Harness(self, self.a, confirm=lambda _lines: False)
        self.assertEqual(harness.run(*trust_args(cert_of(self.b), self.b_id)), 1)
        self.assertEqual(load_policy(self.a).peers, ())


class GrantTest(CliTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.trust_b()
        self.h.out.clear()
        self.h.confirmed.clear()

    def test_a_folder_grant_records_the_file_identity(self) -> None:
        folder = self.make_folder()
        self.trust_in_claude(folder)
        self.assertEqual(self.h.run("grant", "laptop", "--folder", f"work={folder}", "--agent", "claude"), 0, self.h.errors)
        grant = self.peer().folders[0]
        status = os.stat(folder)
        self.assertEqual((grant.alias, grant.path, grant.dev, grant.ino), ("work", folder, status.st_dev, status.st_ino))
        self.assertNotEqual(grant.ino, 0)
        self.assertEqual(self.peer().agents, ("claude",))
        self.assertIn(f"st_ino {status.st_ino}", "\n".join(self.h.confirmed[0]))
        self.assertIn(f"work={folder}", self.h.text)
        self.assertEqual(self.h.notices[-1], "changed the grants of peer laptop")

    def test_a_folder_that_the_agent_has_not_trusted_is_refused(self) -> None:
        folder = self.make_folder()
        self.assertEqual(self.h.run("grant", "laptop", "--folder", f"work={folder}", "--agent", "claude"), 1)
        self.assertIn("claude has not trusted", self.h.errors)
        self.assertEqual(self.peer().folders, ())
        self.assertEqual(self.h.confirmed, [])

    def test_a_distrusted_entry_does_not_count(self) -> None:
        folder = self.make_folder()
        path = Path(self.h.user_home) / ".claude.json"
        path.write_text(json.dumps({"projects": {folder: {"hasTrustDialogAccepted": False}}}), encoding="utf-8")
        self.assertEqual(self.h.run("grant", "laptop", "--folder", f"work={folder}", "--agent", "claude"), 1)

    def test_codex_trust_is_read_from_config_toml(self) -> None:
        folder = self.make_folder()
        codex = Path(self.h.user_home) / ".codex"
        codex.mkdir()
        config = codex / "config.toml"
        config.write_text(f'model = "x"\n[projects.{json.dumps(folder)}]\ntrust_level = "untrusted"\n', encoding="utf-8")
        self.assertEqual(self.h.run("grant", "laptop", "--folder", f"work={folder}", "--agent", "codex"), 1)
        self.assertIn("codex has not trusted", self.h.errors)
        config.write_text(f'[projects.{json.dumps(folder)}]\ntrust_level = "trusted"  # yes\n[other]\nx = 1\n', encoding="utf-8")
        self.assertEqual(self.h.run("grant", "laptop", "--folder", f"work={folder}", "--agent", "codex"), 0, self.h.errors)
        self.assertEqual(self.peer().agents, ("codex",))

    def test_trust_in_a_later_table_does_not_count(self) -> None:
        folder = self.make_folder()
        codex = Path(self.h.user_home) / ".codex"
        codex.mkdir()
        (codex / "config.toml").write_text(
            f'[projects.{json.dumps(folder)}]\nnote = 1\n[other]\ntrust_level = "trusted"\n', encoding="utf-8"
        )
        self.assertEqual(self.h.run("grant", "laptop", "--folder", f"work={folder}", "--agent", "codex"), 1)

    def test_adding_an_agent_checks_the_folders_already_granted(self) -> None:
        folder = self.make_folder()
        self.trust_in_claude(folder)
        self.assertEqual(self.h.run("grant", "laptop", "--folder", f"work={folder}", "--agent", "claude"), 0)
        self.assertEqual(self.h.run("grant", "laptop", "--agent", "codex"), 1)
        self.assertIn("codex has not trusted", self.h.errors)
        self.assertEqual(self.peer().agents, ("claude",))

    def test_a_folder_without_an_agent_is_recorded_when_no_agent_is_granted_yet(self) -> None:
        folder = self.make_folder()
        self.assertEqual(self.h.run("grant", "laptop", "--folder", f"work={folder}"), 0, self.h.errors)
        self.assertEqual(self.peer().folders[0].path, folder)

    def test_a_link_is_stored_as_its_real_path(self) -> None:
        folder = self.make_folder()
        link = os.path.join(self.make_folder("links"), "link")
        if sys.platform == "win32":
            done = subprocess.run(["cmd", "/c", "mklink", "/J", link, folder], capture_output=True, check=False)
            if done.returncode:
                self.skipTest("cannot create a junction")
            self.addCleanup(os.rmdir, link)
        else:
            os.symlink(folder, link)
            self.addCleanup(os.remove, link)
        self.assertEqual(self.h.run("grant", "laptop", "--folder", f"work={link}"), 0, self.h.errors)
        self.assertEqual(self.peer().folders[0].path, folder)

    def test_bad_folders_are_refused(self) -> None:
        folder = self.make_folder()
        missing = os.path.join(folder, "missing")
        for spec in (
            "work=" + "\\\\server\\share\\x",
            "work=//server/share/x",
            "work=relative",
            f"work={missing}",
            folder,
            f"Bad Alias={folder}",
        ):
            self.assertEqual(self.h.run("grant", "laptop", "--folder", spec), 1, spec)
        self.assertEqual(self.peer().folders, ())
        self.assertEqual(self.h.confirmed, [])

    def test_an_alias_is_replaced_and_lists_repeat(self) -> None:
        one, two = self.make_folder("one"), self.make_folder("two")
        self.assertEqual(self.h.run("grant", "laptop", "--folder", f"work={one}", "--model", "m1", "--send", "s-1"), 0)
        self.assertEqual(
            self.h.run("grant", "laptop", "--folder", f"work={two}", "--model", "m1", "--model", "m2", "--send", "s-1", "--send", "s-2"), 0
        )
        peer = self.peer()
        self.assertEqual([f.path for f in peer.folders], [two])
        self.assertEqual((peer.models, peer.send), (("m1", "m2"), ("s-1", "s-2")))

    def test_usage_errors(self) -> None:
        self.assertEqual(self.h.run("grant", "laptop"), 2)
        self.assertEqual(self.h.run("grant", "laptop", "--agent", "copilot"), 2)
        self.assertEqual(self.h.run("grant"), 2)
        self.assertEqual(self.h.run("grant", "nobody", "--model", "m"), 1)
        self.assertEqual(self.h.run("grant", "laptop", "--send", "bad id"), 1)
        self.assertEqual(self.h.run("grant", "laptop", "--model", ""), 1)

    def test_grant_without_a_terminal_changes_nothing(self) -> None:
        harness = Harness(self, self.a, confirm=confirm.confirm)
        with mock.patch.object(confirm, "_console", return_value=False):
            self.assertEqual(harness.run("grant", "laptop", "--model", "m"), 1)
        self.assertEqual(self.peer().models, ())


class RevokeAndOffTest(CliTestCase):
    def generation(self) -> int:
        store = NetStore(self.a)
        try:
            return store.generation()
        finally:
            store.close()

    def test_revoke_removes_the_peer_and_raises_the_generation(self) -> None:
        self.trust_b()
        before = self.generation()
        self.h.confirmed.clear()
        self.assertEqual(self.h.run("revoke", "laptop"), 0, self.h.errors)
        self.assertEqual(self.h.confirmed, [])
        self.assertEqual(load_policy(self.a).peers, ())
        self.assertFalse(peer_cert_path(self.a, self.b_id).exists())
        self.assertEqual(self.generation(), before + 1)
        self.assertEqual(self.h.run("revoke", "laptop"), 1)
        self.assertEqual(self.generation(), before + 1)

    def test_rename_changes_only_the_label_without_confirmation(self) -> None:
        self.trust_b()
        before = load_policy(self.a).peers[0]
        self.h.confirmed.clear()
        self.assertEqual(self.h.run("rename", "laptop", "marbles-xps"), 0, self.h.errors)
        self.assertEqual(self.h.confirmed, [])
        after = load_policy(self.a).peers[0]
        self.assertEqual(after.label, "marbles-xps")
        self.assertEqual(
            (after.device_id, after.address, after.folders, after.agents), (before.device_id, before.address, before.folders, before.agents)
        )
        self.assertTrue(peer_cert_path(self.a, self.b_id).exists())
        self.assertIn("marbles-xps", self.h.text)

    def test_rename_refuses_a_session_that_another_machine_opened_and_changes_nothing(self) -> None:
        self.trust_b()
        remote = Harness(self, self.a, is_remote_origin=lambda _home: True)
        self.assertEqual(remote.run("rename", "laptop", "marbles-xps"), 1)
        self.assertEqual(remote.err, [f"error: {cli.NETWORK_SETTINGS_REFUSAL}"])
        self.assertEqual(load_policy(self.a).peers[0].label, "laptop")

    def test_rename_refuses_a_bad_or_taken_label_and_an_unknown_peer(self) -> None:
        self.trust_b()
        for args in (("laptop", "Bad Label"), ("laptop", "x" * 33), ("nobody", "fine")):
            self.assertEqual(self.h.run("rename", *args), 1, args)
        self.assertEqual(load_policy(self.a).peers[0].label, "laptop")
        self.assertEqual(self.h.run("rename", "laptop"), 2)

    def test_off_sets_the_choice_keeps_other_keys_raises_the_generation_and_stops_the_listener(self) -> None:
        Path(self.a, "config.json").write_text(json.dumps({"jev": {"enabled": True}, "network": {"network": "lan", "bind": "10.0.0.5"}}))
        self.h._spawn(self.a, 1)
        before = self.generation()

        def consume() -> None:
            while not stop_file(self.a).exists():
                threading.Event().wait(0.02)
            os.remove(listener_file(self.a))

        thread = threading.Thread(target=consume, daemon=True)
        thread.start()
        self.assertEqual(self.h.run("off"), 0, self.h.errors)
        thread.join(5)
        config = json.loads(Path(self.a, "config.json").read_text())
        self.assertEqual(config["jev"], {"enabled": True})
        self.assertEqual(config["network"], {"network": "off", "bind": "10.0.0.5"})
        self.assertEqual(self.generation(), before + 1)
        self.assertIsNone(read_listener(self.a))
        self.assertEqual(self.h.confirmed, [])

    def test_off_commits_first_then_stops_the_listener_then_removes_the_task_and_the_rule(self) -> None:
        Path(self.a, "config.json").write_text(json.dumps({"network": {"network": "lan", "bind": "10.0.0.5"}}))
        self.assertEqual(self.h.run("off"), 0, self.h.errors)
        self.assertEqual(self.h.events, ["remove-login after off and listener down", "remove-firewall"])
        self.assertIn("Removed the login task.", self.h.text)
        self.assertIn("Removed the firewall rule.", self.h.text)

    def test_off_reports_a_login_task_that_stays_and_still_removes_the_rule(self) -> None:
        self.h.login_error = LoginTaskError("schtasks exited 1 and the task remains")
        self.assertEqual(self.h.run("off"), 1)
        self.assertIn("login task was not removed", self.h.errors)
        self.assertIn("schtasks exited 1", self.h.errors)
        self.assertIn("refuses while the network is off", self.h.errors)
        self.assertEqual(self.h.firewall_removals, 1)
        self.assertEqual(json.loads(Path(self.a, "config.json").read_text())["network"]["network"], "off")

    def test_off_reports_a_firewall_rule_that_stays(self) -> None:
        self.h.firewall_error = FirewallError("the Windows prompt for administrator rights was declined")
        self.assertEqual(self.h.run("off"), 1)
        self.assertIn("declined", self.h.errors)
        self.assertIn("Agent Tabs LAN", self.h.errors)

    def test_off_without_a_config_creates_one(self) -> None:
        self.assertEqual(self.h.run("off"), 0, self.h.errors)
        self.assertEqual(json.loads(Path(self.a, "config.json").read_text()), {"network": {"network": "off"}})

    def test_off_reports_a_listener_that_does_not_stop(self) -> None:
        self.h._spawn(self.a, 1)
        self.h.services.stop_wait_s = 0.3
        self.assertEqual(self.h.run("off"), 1)
        self.assertIn("did not stop", self.h.errors)
        self.assertTrue(stop_file(self.a).exists())


class LanTest(CliTestCase):
    def config(self) -> dict[str, Any]:
        return json.loads(Path(self.a, "config.json").read_text())

    def test_on_validates_the_bind_address(self) -> None:
        for bind in ("127.0.0.1", "0.0.0.0", "8.8.8.8", "::", "::1", "fe80::1", "10.0.0.5%1", "host"):
            self.assertEqual(self.h.run("lan", "on", "--bind", bind), 1, bind)
        self.assertEqual(self.h.confirmed, [])
        self.assertEqual(self.h.spawned, [])
        self.assertFalse(os.path.exists(os.path.join(self.a, "config.json")))

    def test_on_refuses_an_address_that_is_not_local(self) -> None:
        harness = Harness(self, self.a, bind_available=lambda _address: False)
        self.assertEqual(harness.run("lan", "on", "--bind", "10.0.0.5"), 1)
        self.assertIn("not assigned", harness.errors)

    def test_on_needs_setup_first(self) -> None:
        home = make_root(self)
        harness = Harness(self, home)
        self.assertEqual(harness.run("lan", "on", "--bind", "10.0.0.5"), 1)
        self.assertIn("lan setup", harness.errors)

    def test_on_confirms_records_the_choice_and_starts_the_listener(self) -> None:
        Path(self.a, "config.json").write_text(json.dumps({"jev": {"enabled": False}}))
        self.assertEqual(self.h.run("lan", "on", "--bind", "10.0.0.5", "--at-login"), 0, self.h.errors)
        self.assertEqual(self.config(), {"jev": {"enabled": False}, "network": {"network": "lan", "bind": "10.0.0.5"}})
        self.assertIn("10.0.0.5", "\n".join(self.h.confirmed[0]))
        self.assertEqual(self.h.spawned, [(self.a, 47828)])
        self.assertEqual(self.h.logins, [self.a])
        self.assertIn("Registered the login task.", self.h.text)
        self.assertIn("At login", os.linesep.join(self.h.confirmed[0]))
        self.assertEqual(self.h.firewall, [("10.0.0.5", "10.0.0.0/24", "Ethernet")])
        self.assertIn("The listener runs", self.h.text)
        self.assertEqual(self.h.notices, ["LAN network turned on at 10.0.0.5"])
        self.assertIn("lan-on", Path(log_file(self.a, "cli.log")).read_text())

    def test_on_without_at_login_registers_no_login_task(self) -> None:
        self.assertEqual(self.h.run("lan", "on", "--bind", "10.0.0.5"), 0, self.h.errors)
        self.assertEqual(self.h.logins, [])
        self.assertNotIn("At login", os.linesep.join(self.h.confirmed[0]))

    def test_on_reports_a_login_task_that_is_refused_and_still_starts_the_listener(self) -> None:
        self.h.login_error = LoginTaskError("refused because other users could change what the task runs")
        self.assertEqual(self.h.run("lan", "on", "--bind", "10.0.0.5", "--at-login"), 1)
        self.assertIn("login task was not registered", self.h.errors)
        self.assertIn("other users could change", self.h.errors)
        self.assertEqual(len(self.h.spawned), 1)
        self.assertEqual(self.config()["network"]["network"], "lan")

    def test_on_keeps_going_when_the_firewall_prompt_is_declined(self) -> None:
        self.h.firewall_error = FirewallError("the Windows prompt for administrator rights was declined")
        self.assertEqual(self.h.run("lan", "on", "--bind", "10.0.0.5"), 0, self.h.errors)
        self.assertIn("declined", self.h.errors)
        self.assertIn("Peers can connect only when the firewall allows them", self.h.errors)
        self.assertEqual(len(self.h.spawned), 1)

    def test_on_scopes_the_rule_to_the_interface_subnet_not_to_peer_addresses(self) -> None:
        self.trust_b("192.168.1.20", "laptop")
        self.assertEqual(self.h.run("lan", "on", "--bind", "10.0.0.5"), 0, self.h.errors)
        self.assertEqual(self.h.firewall, [("10.0.0.5", "10.0.0.0/24", "Ethernet")])

    def test_trust_and_revoke_leave_the_firewall_alone(self) -> None:
        Path(self.a, "config.json").write_text(json.dumps({"network": {"network": "lan", "bind": "10.0.0.5"}}))
        self.trust_b("192.168.1.20", "laptop")
        self.assertEqual(self.h.run(*trust_args(cert_of(self.b), self.b_id, "192.168.1.21", "laptop")), 0, self.h.errors)
        self.assertEqual(self.h.run("revoke", "laptop"), 0, self.h.errors)
        self.assertEqual(self.h.firewall, [])
        self.assertEqual(self.h.firewall_removals, 0)

    def test_on_keeps_going_when_the_interface_cannot_be_described(self) -> None:
        self.h.interface_error = InterfaceError("Windows has no network interface with the address 10.0.0.5")
        self.assertEqual(self.h.run("lan", "on", "--bind", "10.0.0.5"), 0, self.h.errors)
        self.assertIn("no network interface", self.h.errors)
        self.assertEqual(self.h.firewall, [])
        self.assertEqual(len(self.h.spawned), 1)

    def test_on_creates_no_rule_for_a_custom_port(self) -> None:
        Path(self.a, "config.json").write_text(json.dumps({"network": {"port": 5000}}))
        self.assertEqual(self.h.run("lan", "on", "--bind", "10.0.0.5"), 0, self.h.errors)
        self.assertIn("not the listener port", self.h.errors)
        self.assertEqual(self.h.firewall, [])

    def test_on_without_a_terminal_changes_nothing(self) -> None:
        harness = Harness(self, self.a, confirm=confirm.confirm)
        with mock.patch.object(confirm, "_console", return_value=False):
            self.assertEqual(harness.run("lan", "on", "--bind", "10.0.0.5"), 1)
        self.assertFalse(os.path.exists(os.path.join(self.a, "config.json")))
        self.assertEqual(harness.spawned, [])

    def test_on_reports_a_listener_that_does_not_start(self) -> None:
        harness = Harness(self, self.a, spawn=lambda _home, _port: None)
        self.assertEqual(harness.run("lan", "on", "--bind", "10.0.0.5"), 1)
        self.assertIn("could not be started", harness.errors)

    def test_start_refuses_when_the_network_is_off(self) -> None:
        self.assertEqual(self.h.run("lan", "start"), 1)
        self.assertIn("off", self.h.errors)
        Path(self.a, "config.json").write_text(json.dumps({"network": {"network": "lan"}}))
        self.assertEqual(self.h.run("lan", "start"), 1)
        self.assertEqual(self.h.spawned, [])

    def test_start_runs_without_confirmation_and_leaves_an_audit_line_and_a_notice(self) -> None:
        Path(self.a, "config.json").write_text(json.dumps({"network": {"network": "lan", "bind": "10.0.0.5"}}))
        harness = Harness(self, self.a, confirm=lambda _lines: self.fail("start asked for confirmation"))
        self.assertEqual(harness.run("lan", "start"), 0, harness.errors)
        self.assertEqual(harness.spawned, [(self.a, 47828)])
        self.assertEqual(harness.notices, ["LAN listener started at 10.0.0.5"])
        self.assertIn("lan-start", Path(log_file(self.a, "cli.log")).read_text())
        harness.out.clear()
        self.assertEqual(harness.run("lan", "start"), 0)
        self.assertEqual(len(harness.spawned), 1)
        self.assertIn("already runs", harness.text)

    def lan_config(self) -> None:
        Path(self.a, "config.json").write_text(json.dumps({"network": {"network": "lan", "bind": "10.0.0.5"}}))

    def test_start_restarts_a_listener_that_records_no_build(self) -> None:
        self.lan_config()
        self.h.write_listener_file(self.a, version=None, build=None, joins=None)
        thread = self.h.answer_stop()
        self.assertEqual(self.h.run("lan", "start"), 0, self.h.errors)
        thread.join(5)
        self.assertIn(
            f"The listener (pid {os.getpid()}) records no build, so an older plugin version started it. Restarting it.", self.h.text
        )
        self.assertEqual(len(self.h.spawned), 1)
        info = read_listener(self.a)
        assert info is not None
        self.assertEqual((info.version, info.build), (self.h.build.version, self.h.build.id))

    def test_start_restarts_a_listener_from_another_build_of_an_older_version(self) -> None:
        self.lan_config()
        self.h.write_listener_file(self.a, version="0.11.0", build="0.11.0-bbbbbbbbbbbb")
        thread = self.h.answer_stop()
        self.assertEqual(self.h.run("lan", "start"), 0, self.h.errors)
        thread.join(5)
        self.assertIn("runs build 0.11.0-bbbbbbbbbbbb, not the installed build 0.14.2-aaaaaaaaaaaa. Restarting it.", self.h.text)
        self.assertEqual(len(self.h.spawned), 1)

    def test_start_keeps_a_listener_of_the_installed_build(self) -> None:
        self.lan_config()
        self.h.write_listener_file(self.a)
        self.assertEqual(self.h.run("lan", "start"), 0, self.h.errors)
        self.assertIn("already runs", self.h.text)
        self.assertEqual(self.h.spawned, [])

    def test_start_keeps_a_listener_from_a_newer_version(self) -> None:
        self.lan_config()
        self.h.write_listener_file(self.a, version="9.0.0", build="9.0.0-cccccccccccc")
        self.assertEqual(self.h.run("lan", "start"), 0, self.h.errors)
        self.assertIn("already runs", self.h.text)
        self.assertEqual(self.h.spawned, [])

    def test_start_starts_nothing_when_the_old_listener_does_not_stop(self) -> None:
        self.lan_config()
        self.h.write_listener_file(self.a, build=None)
        self.assertEqual(self.h.run("lan", "start"), 1)
        self.assertIn("did not stop", self.h.errors)
        self.assertEqual(self.h.spawned, [])

    def test_on_restarts_an_old_listener_too(self) -> None:
        self.h.write_listener_file(self.a, build=None, version=None)
        thread = self.h.answer_stop()
        self.assertEqual(self.h.run("lan", "on", "--bind", "10.0.0.5"), 0, self.h.errors)
        thread.join(5)
        self.assertIn("Restarting it.", self.h.text)
        self.assertEqual(len(self.h.spawned), 1)

    def test_stop_asks_the_listener_to_stop_and_keeps_the_config(self) -> None:
        Path(self.a, "config.json").write_text(json.dumps({"network": {"network": "lan", "bind": "10.0.0.5"}}))
        self.assertEqual(self.h.run("lan", "stop"), 0)
        self.assertIn("No listener", self.h.text)
        self.h._spawn(self.a, 1)

        def consume() -> None:
            while not stop_file(self.a).exists():
                threading.Event().wait(0.02)
            os.remove(listener_file(self.a))

        thread = threading.Thread(target=consume, daemon=True)
        thread.start()
        self.assertEqual(self.h.run("lan", "stop"), 0, self.h.errors)
        thread.join(5)
        self.assertEqual(self.config()["network"]["network"], "lan")

    def test_unknown_lan_commands_are_usage_errors(self) -> None:
        self.assertEqual(self.h.run("lan"), 2)
        self.assertEqual(self.h.run("lan", "bogus"), 2)
        self.assertEqual(self.h.run("lan", "on"), 2)


class StatusAndDispatchTest(CliTestCase):
    def test_status_is_read_only(self) -> None:
        self.assertEqual(self.h.run("status"), 0)
        self.assertIn("network: off", self.h.text)
        self.assertIn("peers: none", self.h.text)
        self.assertIn(f"identity: {device_of(self.a)}", self.h.text)
        self.assertIn("listener: not running", self.h.text)
        self.assertIn("login task: none", self.h.text)
        self.assertIn("pairing: available", self.h.text)
        self.assertNotIn("unsupported", self.h.text)
        self.assertFalse(os.path.exists(os.path.join(self.a, "net", "pairing.json")))
        self.assertFalse(os.path.exists(os.path.join(self.a, "net", "pairing.json.lock")))
        self.assertFalse(os.path.exists(policy_path(self.a)))

    def test_status_shows_why_the_network_feature_is_unsupported(self) -> None:
        self.h.unsupported = "User Account Control is off on this computer."
        self.assertEqual(self.h.run("status"), 0)
        self.assertIn("unsupported: User Account Control is off on this computer.", self.h.text)

    def test_status_shows_the_pairing_lockout(self) -> None:
        limits = Limits(self.a)
        for _ in range(3):
            limits.record_failure("192.168.1.77")
        self.assertEqual(self.h.run("status"), 0)
        self.assertRegex(self.h.text, r"pairing: available; locked for 192\.168\.1\.77 \(\d+ more minutes\)")
        self.assertIn("other addresses can still pair", self.h.text)
        self.assertIn("net trust", self.h.text)

    def test_status_reports_an_unreadable_pairing_file_and_changes_no_firewall_rule(self) -> None:
        damaged = Path(self.a, "net", "pairing.json")
        damaged.write_text("{not json", encoding="utf-8")
        self.addCleanup(damaged.unlink, missing_ok=True)
        self.assertEqual(self.h.run("status"), 0)
        self.assertRegex(self.h.text, r"pairing: unreadable \(")
        self.assertNotIn("firewall", self.h.events)
        self.assertEqual(self.h.firewall_removals, 0)

    def test_status_does_not_warn_about_the_device_name(self) -> None:
        Path(self.a, "config.json").write_text(json.dumps({"network": {"deviceName": "Desk"}}))
        self.assertEqual(self.h.run("status"), 0)
        self.assertNotIn("deviceName", self.h.text)

    def test_status_shows_the_listener_and_the_configuration(self) -> None:
        Path(self.a, "config.json").write_text(json.dumps({"network": {"network": "lan", "bind": "10.0.0.5", "port": 5000}}))
        self.h._spawn(self.a, 1)
        self.assertEqual(self.h.run("status"), 0)
        self.assertIn("network: lan", self.h.text)
        self.assertIn("bind: 10.0.0.5", self.h.text)
        self.assertIn("port: 5000", self.h.text)
        self.assertIn(f"running (pid {os.getpid()}", self.h.text)

    def test_status_shows_the_build_of_the_running_listener_and_whether_it_serves_joins(self) -> None:
        Path(self.a, "config.json").write_text(json.dumps({"network": {"network": "lan", "bind": "10.0.0.5"}}))
        self.h.write_listener_file(self.a)
        self.assertEqual(self.h.run("status"), 0)
        self.assertIn(f"listener build: {self.h.build.id}", self.h.text)
        self.assertIn("serving joins: yes", self.h.text)

    def test_status_names_a_listener_that_records_no_build_and_does_not_claim_it_serves_joins(self) -> None:
        Path(self.a, "config.json").write_text(json.dumps({"network": {"network": "lan", "bind": "10.0.0.5", "discoverable": True}}))
        self.h.write_listener_file(self.a, version=None, build=None, joins=None)
        self.assertEqual(self.h.run("status"), 0)
        self.assertIn("listener build: not recorded", self.h.text)
        self.assertIn("`agent-tabs net lan start` restarts it", self.h.text)
        self.assertNotIn("serving joins: yes", self.h.text)

    def test_status_reports_an_unreadable_policy(self) -> None:
        Path(policy_path(self.a)).write_text("{not json")
        self.assertEqual(self.h.run("status"), 0)
        self.assertIn("unreadable", self.h.text)

    def test_status_does_not_create_the_store(self) -> None:
        self.h.run("status")
        before = os.path.exists(db_path(self.a))
        self.h.run("status")
        self.assertEqual(os.path.exists(db_path(self.a)), before)

    def test_unknown_commands_print_the_usage(self) -> None:
        self.assertEqual(self.h.run(), 2)
        self.assertEqual(self.h.run("bogus"), 2)
        self.assertEqual(self.h.run("status", "extra"), 2)
        self.assertIn("agent-tabs net id", self.h.errors)

    def test_net_is_registered_in_the_agent_tabs_command(self) -> None:
        self.assertEqual(top_cli.COMMANDS["net"], "ide_agent_tabs.net.cli")
        self.assertIn("net <id|status|off|trust|grant|revoke|rename|lan|hub|folders|discoverable|networks|find>", top_cli.usage())
        captured = io.StringIO()
        with mock.patch.dict(os.environ, {"IDE_AGENT_TABS_HOME": self.a}), contextlib.redirect_stdout(captured):
            self.assertEqual(top_cli.main(["net", "status"]), 0)
        self.assertIn("network: off", captured.getvalue())


class SetNetworkTest(unittest.TestCase):
    def test_a_change_waits_for_the_config_lock_and_keeps_what_another_writer_saved_meanwhile(self) -> None:
        home = make_root(self)
        path = os.path.join(home, "config.json")
        Path(path).write_text(json.dumps({"network": {"bind": "10.0.0.5"}}), encoding="utf-8")
        finished = threading.Event()
        errors: list[BaseException] = []

        def change() -> None:
            try:
                cli.set_network(home, {"discoverable": True})
            except BaseException as error:  # noqa: BLE001 - reported to the test thread
                errors.append(error)
            finally:
                finished.set()

        with file_lock(path):
            worker = threading.Thread(target=change, daemon=True)
            worker.start()
            self.assertFalse(finished.wait(0.5))
            Path(path).write_text(json.dumps({"network": {"bind": "10.0.0.5", "memberFolders": ["/x"]}}), encoding="utf-8")
        self.assertTrue(finished.wait(10))
        worker.join(5)
        self.assertEqual(errors, [])
        self.assertEqual(
            json.loads(Path(path).read_text(encoding="utf-8"))["network"],
            {"bind": "10.0.0.5", "memberFolders": ["/x"], "discoverable": True},
        )

    def test_a_config_that_is_not_json_is_a_cli_error(self) -> None:
        home = make_root(self)
        Path(home, "config.json").write_text("{not json", encoding="utf-8")
        with self.assertRaisesRegex(cli.CliError, "is not valid JSON"):
            cli.set_network(home, {"discoverable": True})

    def test_a_network_section_that_is_not_an_object_is_a_cli_error(self) -> None:
        home = make_root(self)
        Path(home, "config.json").write_text(json.dumps({"network": []}), encoding="utf-8")
        with self.assertRaisesRegex(cli.CliError, "must be an object"):
            cli.set_network(home, {"discoverable": True})


class DefaultLabelTest(unittest.TestCase):
    def test_the_default_label_is_lowercased_made_safe_and_unique(self) -> None:
        def policy(*labels: str) -> Policy:
            return Policy(tuple(PeerGrant(format_digest(bytes([index]) * 32), label, "192.168.50.5") for index, label in enumerate(labels)))

        self.assertEqual(default_label("Work PC.1", policy()), "work-pc-1")
        self.assertEqual(default_label("Work PC", policy("work-pc")), "work-pc-2")
        self.assertEqual(default_label("Work PC", policy("work-pc", "work-pc-2")), "work-pc-3")
        self.assertEqual(default_label("...", policy()), "peer")
        long = "a" * 32
        label = default_label(long, policy(long))
        self.assertEqual(label, "a" * 30 + "-2")
        self.assertEqual(len(label), 32)


class ConfirmTest(unittest.TestCase):
    def test_only_yes_confirms(self) -> None:
        shown: list[str] = []
        for answer, expected in (("yes", True), ("  yes ", True), ("y", False), ("Yes", False), ("", False), ("no", False)):
            self.assertEqual(confirm.confirm(["change"], read=lambda _p, a=answer: a, write=shown.append), expected, answer)
        self.assertEqual(shown, ["change"] * 6)

    def test_end_of_input_declines(self) -> None:
        def eof(_prompt: str) -> str:
            raise EOFError

        self.assertFalse(confirm.confirm(["change"], read=eof, write=lambda _t: None))

    def test_a_missing_terminal_is_refused_before_anything_is_shown(self) -> None:
        shown: list[str] = []
        with mock.patch.object(confirm, "_console", return_value=False), self.assertRaises(confirm.ConfirmError):
            confirm.confirm(["change"], write=shown.append)
        self.assertEqual(shown, [])

    def test_both_streams_must_be_terminals(self) -> None:
        with mock.patch.object(confirm, "_console", side_effect=[True, False]):
            self.assertFalse(confirm.interactive())
        with mock.patch.object(confirm, "_console", side_effect=[False, True]):
            self.assertFalse(confirm.interactive())
        with mock.patch.object(confirm, "_console", return_value=True):
            self.assertTrue(confirm.interactive())

    def test_a_stream_without_a_descriptor_is_not_a_terminal(self) -> None:
        self.assertFalse(confirm._console(io.StringIO()))


class NotifyTest(unittest.TestCase):
    def capture(self, platform: str, **patches: Any) -> tuple[bool, list[tuple[list[str], dict[str, str]]]]:
        calls: list[tuple[list[str], dict[str, str]]] = []

        def spawn(command: Any, env: Any) -> None:
            calls.append((list(command), dict(env)))

        shell = patches.get("shell", "powershell-stand-in")
        with (
            mock.patch.object(notify, "sys", mock.Mock(platform=platform)),
            mock.patch.object(notify.os.path, "isfile", return_value=True),
            mock.patch.object(notify, "powershell_path", return_value=shell),
        ):
            sent = notify.notify(patches.get("what", "trusted peer laptop"), spawn)
        return sent, calls

    def test_windows_passes_the_text_in_the_environment(self) -> None:
        sent, calls = self.capture("win32", what="trusted peer 'x'; calc")
        self.assertTrue(sent)
        command, env = calls[0]
        self.assertEqual(command[:4], ["powershell-stand-in", "-NoProfile", "-NonInteractive", "-Command"])
        self.assertNotIn("calc", command[4])
        self.assertEqual(env[notify.NOTICE_ENV], "Agent Tabs: trusted peer 'x'; calc")

    def test_macos_uses_osascript_with_an_argument(self) -> None:
        _, calls = self.capture("darwin")
        command, _ = calls[0]
        self.assertEqual(command[0], "/usr/bin/osascript")
        self.assertEqual(command[-1], "Agent Tabs: trusted peer laptop")

    def test_linux_uses_notify_send_at_an_absolute_path(self) -> None:
        _, calls = self.capture("linux")
        command, _ = calls[0]
        self.assertTrue(os.path.isabs(command[0]) or command[0].startswith("/"))
        self.assertEqual(command[1:], ["Agent Tabs", "Agent Tabs: trusted peer laptop"])

    def test_text_is_one_short_line(self) -> None:
        _, calls = self.capture("linux", what="a\nb\x00" + "x" * 500)
        text = calls[0][0][-1]
        self.assertNotIn("\n", text)
        self.assertLessEqual(len(text), notify.MAX_CHARS)

    def test_it_never_raises(self) -> None:
        def broken(_command: Any, _env: Any) -> None:
            raise OSError("no")

        with mock.patch.object(notify, "sys", mock.Mock(platform="linux")), mock.patch.object(notify.os.path, "isfile", return_value=True):
            self.assertFalse(notify.notify("x", broken))
        with mock.patch.object(notify, "sys", mock.Mock(platform="linux")), mock.patch.object(notify.os.path, "isfile", return_value=False):
            self.assertFalse(notify.notify("x", broken))


if __name__ == "__main__":
    unittest.main()

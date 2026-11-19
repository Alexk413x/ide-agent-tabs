from __future__ import annotations

import contextlib
import json
import os
import shutil
import unittest
from pathlib import Path
from typing import Any

from ide_agent_tabs.messaging.sessions import SESSIONS_DIR
from ide_agent_tabs.messaging.store import mail_to
from ide_agent_tabs.net import migration
from ide_agent_tabs.net.cli import Services
from ide_agent_tabs.net.cli import main as net_main
from ide_agent_tabs.net.client import NetClientError, peer_cert_path, peers_dir, send_request
from ide_agent_tabs.net.dispatch import NetDispatcher
from ide_agent_tabs.net.hubstate import read_hub_state
from ide_agent_tabs.net.hubsync import flush_pending
from ide_agent_tabs.net.migration import Marker, Migrator, marker_path, migrated_hub_id, read_marker
from ide_agent_tabs.net.policy import (
    MEMBER_MAX_TABS,
    MEMBER_MESSAGES_PER_MINUTE,
    MEMBER_OPENS_PER_HOUR,
    FolderGrant,
    Hub,
    PeerGrant,
    Policy,
    grant_for,
    load_network_config,
    load_policy,
    save_policy,
)
from ide_agent_tabs.net.private_files import ensure_private_dir
from net_support import LOOPBACK, TEST_NETWORK, FakeIngress, RunningListener, cert_of, create_home, device_of, make_root
from test_net_dispatch import NOW, FakeService, write_presence
from test_net_join import NEEDS, reset, set_name

NAMES = {"a": "alpha", "b": "bravo", "c": "charlie"}
HOME_NETWORK = "home-network"


def pin(home: str, other: str, label: str, *, address: str = LOOPBACK, folder: str | None = None) -> str:
    device = device_of(other)
    ensure_private_dir(peers_dir(home))
    peer_cert_path(home, device).write_text(cert_of(other).read_text(encoding="ascii"), encoding="ascii")
    folders: tuple[FolderGrant, ...] = ()
    if folder is not None:
        status = os.stat(folder)
        folders = (FolderGrant("work", folder, status.st_dev, status.st_ino),)
    policy = load_policy(home)
    peer = PeerGrant(device, label, address, folders=folders, agents=("claude",), send=("session-1",))
    save_policy(home, Policy((*policy.peers, peer), policy.generation))
    return device


def write_config(home: str, **network: Any) -> None:
    Path(home, "config.json").write_text(json.dumps({"network": network}), encoding="utf-8")


def network_section(home: str) -> dict[str, Any]:
    return json.loads(Path(home, "config.json").read_text(encoding="utf-8"))["network"]


class MigrationCase(unittest.TestCase):
    homes: dict[str, str]

    @classmethod
    def setUpClass(cls) -> None:
        root = make_root(cls)
        cls.homes = {name: create_home(root, name) for name in NAMES}

    def setUp(self) -> None:
        for name, home in self.homes.items():
            reset(home)
            for leftover in ("hub-migration.json", "hub-migration.json.lock"):
                with contextlib.suppress(OSError):
                    os.remove(os.path.join(home, "net", leftover))
            with contextlib.suppress(OSError):
                os.remove(os.path.join(home, "config.json"))
            set_name(home, NAMES[name])
            shutil.rmtree(os.path.join(home, SESSIONS_DIR), ignore_errors=True)
        self.a, self.b, self.c = (self.homes[name] for name in "abc")
        self.user_home = make_root(self, "iat-migrate-user-")
        self.folder = os.path.join(self.user_home, "code", "app")
        os.makedirs(self.folder)
        self.posted: list[str] = []
        self.sessions = 0

    def post(self, _home: str, text: str) -> int:
        if self.sessions:
            self.posted.append(text)
        return self.sessions

    def migrator(self, network: str | None = HOME_NETWORK) -> Migrator:
        return Migrator(network_id=lambda _bind: network, user_home=self.user_home, post=self.post)

    def legacy_a(self) -> None:
        pin(self.a, self.b, "bravo", folder=self.folder)
        pin(self.a, self.c, "charlie")
        write_config(self.a, network="lan", bind="192.168.1.10", deviceName="alpha")


@NEEDS
class SingleMachineTest(MigrationCase):
    def test_pinned_peers_become_members_of_one_hub_with_the_member_limits_and_no_old_grants(self) -> None:
        self.legacy_a()
        self.sessions = 1
        moved = self.migrator().start(self.a)
        self.assertEqual(moved, ("bravo", "charlie"))
        policy = load_policy(self.a)
        assert policy.hub is not None
        self.assertEqual(policy.hub.id, migrated_hub_id([device_of(self.a), device_of(self.b), device_of(self.c)]))
        self.assertEqual(policy.hub.name, "home")
        for name in "bc":
            peer = grant_for(policy, device_of(self.homes[name]))
            assert peer is not None
            self.assertTrue(peer.member)
            self.assertEqual((peer.folders, peer.agents, peer.models, peer.send), ((), (), (), ()))
            self.assertEqual(
                (peer.max_tabs, peer.opens_per_hour, peer.messages_per_minute),
                (MEMBER_MAX_TABS, MEMBER_OPENS_PER_HOUR, MEMBER_MESSAGES_PER_MINUTE),
            )
            self.assertEqual(peer.address, LOOPBACK)
            self.assertEqual(
                peer_cert_path(self.a, peer.device_id).read_text(encoding="ascii"), cert_of(self.homes[name]).read_text(encoding="ascii")
            )

    def test_the_hub_has_a_random_key_the_members_and_a_queued_key_exchange(self) -> None:
        self.legacy_a()
        self.migrator().start(self.a)
        state = read_hub_state(self.a)
        assert state is not None
        self.assertEqual(state.epoch, 0)
        self.assertRegex(state.key, r"[0-9a-f]{64}")
        self.assertNotIn(state.key, state.hub_id)
        self.assertEqual(sorted(m.label for m in state.members), ["bravo", "charlie"])
        self.assertEqual(sorted((p.to, p.op) for p in state.pending), sorted([(device_of(self.b), "rekey"), (device_of(self.c), "rekey")]))

    def test_empty_folders_and_allowed_networks_get_the_defaults(self) -> None:
        self.legacy_a()
        self.migrator().start(self.a)
        config, warnings = load_network_config(self.a)
        self.assertEqual(warnings, [])
        default = os.path.join(self.user_home, "code")
        self.assertEqual(
            (config.allowed_networks, config.member_folders, config.outbound_folders), ((HOME_NETWORK,), (default,), (default,))
        )
        self.assertEqual((config.network, config.bind), ("lan", "192.168.1.10"))

    def test_settings_that_are_set_are_kept(self) -> None:
        self.legacy_a()
        write_config(
            self.a,
            network="lan",
            bind="192.168.1.10",
            allowedNetworks=["mine"],
            memberFolders=[self.folder],
            outboundFolders=[self.user_home],
        )
        self.migrator().start(self.a)
        config, _ = load_network_config(self.a)
        self.assertEqual(
            (config.allowed_networks, config.member_folders, config.outbound_folders), (("mine",), (self.folder,), (self.user_home,))
        )

    def test_the_session_folder_is_the_fallback_when_no_peer_had_a_folder_grant(self) -> None:
        pin(self.a, self.b, "bravo")
        write_config(self.a, network="lan", bind="192.168.1.10")
        self.migrator().start(self.a, self.folder)
        self.assertEqual(load_network_config(self.a)[0].member_folders, (os.path.join(self.user_home, "code"),))

    def test_no_default_is_invented_without_a_folder_source(self) -> None:
        pin(self.a, self.b, "bravo")
        write_config(self.a, network="lan", bind="192.168.1.10")
        self.migrator().start(self.a)
        config, _ = load_network_config(self.a)
        self.assertEqual((config.member_folders, config.outbound_folders), ((), ()))

    def test_a_network_that_is_off_gets_no_allowed_network(self) -> None:
        pin(self.a, self.b, "bravo")
        self.migrator().start(self.a)
        self.assertEqual(load_network_config(self.a)[0].allowed_networks, ())
        self.assertFalse(read_marker(self.a).network_pending)  # pyright: ignore[reportOptionalMemberAccess]

    def test_an_unreadable_network_id_is_added_by_a_later_tick(self) -> None:
        self.legacy_a()
        self.sessions = 1
        migrator = self.migrator(None)
        migrator.start(self.a)
        self.assertEqual(load_network_config(self.a)[0].allowed_networks, ())
        marker = read_marker(self.a)
        assert marker is not None
        self.assertTrue(marker.network_pending)
        migrator.tick(self.a, None)
        self.assertEqual(load_network_config(self.a)[0].allowed_networks, ())
        migrator.tick(self.a, "later-network")
        self.assertEqual(load_network_config(self.a)[0].allowed_networks, ("later-network",))
        migrator.tick(self.a, "third-network")
        self.assertEqual(load_network_config(self.a)[0].allowed_networks, ("later-network",))
        self.assertFalse(migration.pending(self.a))

    def test_the_notice_names_the_machines_and_the_revoke_command_and_posts_once(self) -> None:
        self.legacy_a()
        self.sessions = 2
        migrator = self.migrator()
        migrator.start(self.a)
        self.assertEqual(len(self.posted), 1)
        text = self.posted[0]
        for part in ("bravo, charlie", "full member role", "agent-tabs net revoke <label>", os.path.join(self.user_home, "code")):
            self.assertIn(part, text)
        migrator.start(self.a)
        migrator.tick(self.a, HOME_NETWORK)
        self.assertEqual(len(self.posted), 1)

    def test_the_notice_waits_until_a_session_can_receive_it(self) -> None:
        self.legacy_a()
        migrator = self.migrator()
        migrator.start(self.a)
        self.assertEqual(self.posted, [])
        self.assertTrue(migration.pending(self.a))
        migrator.tick(self.a, HOME_NETWORK)
        self.assertEqual(self.posted, [])
        self.sessions = 1
        migrator.tick(self.a, HOME_NETWORK)
        migrator.tick(self.a, HOME_NETWORK)
        self.assertEqual(len(self.posted), 1)
        self.assertFalse(migration.pending(self.a))

    def test_the_real_notice_reaches_each_live_local_session_through_the_mailbox(self) -> None:
        write_presence(self.a, "local-1")
        write_presence(self.a, "local-2")
        self.assertEqual(migration.post_to_sessions(self.a, "hello"), 2)
        for session in ("local-1", "local-2"):
            self.assertEqual([m["text"] for m in mail_to(self.a, session)], ["hello"])

    def test_the_real_notice_reaches_nobody_when_no_session_is_live(self) -> None:
        self.assertEqual(migration.post_to_sessions(self.a, "hello"), 0)

    def test_a_second_start_changes_nothing(self) -> None:
        self.legacy_a()
        self.sessions = 1
        migrator = self.migrator()
        migrator.start(self.a)
        before = [Path(self.a, "net", name).read_bytes() for name in ("policy.json", "hub.json", "hub-migration.json")]
        config_before = Path(self.a, "config.json").read_bytes()
        self.assertEqual(migrator.start(self.a), ())
        after = [Path(self.a, "net", name).read_bytes() for name in ("policy.json", "hub.json", "hub-migration.json")]
        self.assertEqual((before, config_before), (after, Path(self.a, "config.json").read_bytes()))

    def test_a_machine_with_no_peers_gets_no_hub_and_a_peer_trusted_later_stays_a_plain_peer(self) -> None:
        write_config(self.a, network="lan", bind="192.168.1.10")
        migrator = self.migrator()
        self.assertEqual(migrator.start(self.a), ())
        self.assertIsNone(load_policy(self.a).hub)
        self.assertEqual(read_marker(self.a), Marker())
        self.assertFalse(os.path.exists(os.path.join(self.a, "net", "hub.json")))
        pin(self.a, self.b, "bravo")
        self.assertEqual(migrator.start(self.a), ())
        policy = load_policy(self.a)
        self.assertIsNone(policy.hub)
        self.assertFalse(policy.peers[0].member)
        self.assertEqual(policy.peers[0].send, ("session-1",))
        self.assertEqual(load_network_config(self.a)[0].allowed_networks, ())

    def test_a_machine_that_already_has_a_hub_is_left_alone(self) -> None:
        pin(self.a, self.b, "bravo")
        policy = load_policy(self.a)
        save_policy(self.a, Policy(policy.peers, 0, Hub("0123456789abcdef0123456789abcdef", "mine")))
        self.assertEqual(self.migrator().start(self.a), ())
        after = load_policy(self.a)
        self.assertEqual(after.hub, Hub("0123456789abcdef0123456789abcdef", "mine"))
        self.assertFalse(after.peers[0].member)

    def test_a_peer_without_a_certificate_stays_as_it_is(self) -> None:
        self.legacy_a()
        os.remove(peer_cert_path(self.a, device_of(self.c)))
        self.assertEqual(self.migrator().start(self.a), ("bravo",))
        policy = load_policy(self.a)
        self.assertFalse(grant_for(policy, device_of(self.c)).member)  # pyright: ignore[reportOptionalMemberAccess]
        self.assertEqual(policy.hub.id, migrated_hub_id([device_of(self.a), device_of(self.b)]))  # pyright: ignore[reportOptionalMemberAccess]

    def test_a_damaged_marker_counts_as_done(self) -> None:
        self.legacy_a()
        Path(marker_path(self.a)).write_text("not json", encoding="utf-8")
        self.assertEqual(self.migrator().start(self.a), ())
        self.assertIsNone(load_policy(self.a).hub)

    def test_the_command_line_runs_the_migration_once_before_the_command(self) -> None:
        self.legacy_a()
        out: list[str] = []
        services = Services(
            home=self.a,
            out=out.append,
            err=out.append,
            migrator=self.migrator(),
            user_home=self.user_home,
            cwd=lambda: self.user_home,
            is_remote_origin=lambda _home: False,
        )
        self.assertEqual(net_main(["hub", "status"], services), 0, out)
        self.assertTrue(any(line.startswith("hub: home (") for line in out), out)
        self.assertTrue(any("bravo" in line for line in out), out)
        before = Path(self.a, "net", "policy.json").read_bytes()
        self.assertEqual(net_main(["hub", "status"], services), 0)
        self.assertEqual(Path(self.a, "net", "policy.json").read_bytes(), before)

    def test_a_listener_start_runs_the_migration_before_it_reads_the_settings(self) -> None:
        self.legacy_a()
        ingress = FakeIngress(self.a)
        self.addCleanup(ingress.close)
        running = RunningListener(self.a, ingress.port)
        running.options.migration = self.migrator(TEST_NETWORK)
        running.start()
        self.addCleanup(running.stop)
        self.assertIsNotNone(load_policy(self.a).hub)
        self.assertEqual(load_network_config(self.a)[0].allowed_networks, (TEST_NETWORK,))


@NEEDS
class TwoMachineTest(MigrationCase):
    def setUp(self) -> None:
        super().setUp()
        for home, name in ((self.a, "alpha"), (self.b, "bravo")):
            write_config(home, network="lan", bind="192.168.1.10", deviceName=name)
        pin(self.a, self.b, "bravo", folder=self.folder)
        pin(self.b, self.a, "alpha")
        self.ingresses: dict[str, FakeIngress] = {}
        self.running: dict[str, RunningListener] = {}

    def serve(self, *names: str) -> None:
        for name in names:
            home = self.homes[name]
            dispatcher = NetDispatcher(home, FakeService, lambda: NOW, lambda _text: None, alive=lambda _pid: True)  # pyright: ignore[reportArgumentType]
            ingress = FakeIngress(home, lambda peer, request, d=dispatcher: d.handle(peer, request))
            self.addCleanup(ingress.close)
            listener = RunningListener(home, ingress.port).start()
            self.addCleanup(listener.stop)
            self.running[device_of(home)] = listener

    def request(self, home: str, to: str, op: str, body: dict[str, Any], **options: Any) -> dict[str, Any]:
        if to not in self.running:
            raise NetClientError("cannot reach the peer")
        return send_request(home, to, op, body, port=self.running[to].port, address=LOOPBACK, **options)

    def version(self, name: str) -> tuple[int, str]:
        state = read_hub_state(self.homes[name])
        assert state is not None
        return state.version()

    def test_both_machines_derive_the_same_hub_id_and_name_from_public_data_only(self) -> None:
        self.migrator().start(self.a)
        self.migrator().start(self.b)
        hub_a, hub_b = load_policy(self.a).hub, load_policy(self.b).hub
        assert hub_a is not None
        self.assertEqual(hub_a, hub_b)
        self.assertEqual(hub_a.id, migrated_hub_id(sorted([device_of(self.a), device_of(self.b)])))
        self.assertNotEqual(self.version("a")[1], self.version("b")[1])

    def test_the_machine_that_upgrades_second_brings_both_to_one_key_over_mutual_tls(self) -> None:
        self.serve("a", "b")
        self.migrator().start(self.a)
        key_a = self.version("a")
        self.assertEqual(flush_pending(self.a, self.request), 0)
        state_a = read_hub_state(self.a)
        assert state_a is not None
        self.assertEqual(state_a.pending, ())
        self.assertIsNone(read_hub_state(self.b))
        self.assertIsNone(load_policy(self.b).hub)
        self.migrator().start(self.b)
        key_b = self.version("b")
        self.assertNotEqual(key_a, key_b)
        self.assertEqual(flush_pending(self.b, self.request), 1)
        self.assertEqual(self.version("a"), self.version("b"))
        self.assertEqual(self.version("a"), max(key_a, key_b))
        self.assertEqual(load_policy(self.a).hub, load_policy(self.b).hub)
        for home, other in ((self.a, self.b), (self.b, self.a)):
            peer = grant_for(load_policy(home), device_of(other))
            assert peer is not None
            self.assertTrue(peer.member)

    def test_machines_that_upgrade_together_end_with_one_key(self) -> None:
        self.serve("a", "b")
        self.migrator().start(self.a)
        self.migrator().start(self.b)
        flush_pending(self.a, self.request)
        flush_pending(self.b, self.request)
        self.assertEqual(self.version("a"), self.version("b"))

    def test_an_upgraded_machine_whose_peer_is_offline_keeps_the_exchange_queued(self) -> None:
        self.migrator().start(self.a)
        self.assertEqual(flush_pending(self.a, self.request), 0)
        state = read_hub_state(self.a)
        assert state is not None
        self.assertEqual([(p.to, p.op) for p in state.pending], [(device_of(self.b), "rekey")])


if __name__ == "__main__":
    unittest.main()

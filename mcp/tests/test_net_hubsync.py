from __future__ import annotations

import json
import socket
import ssl
import threading
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar

from ide_agent_tabs.messaging.store import mail_to
from ide_agent_tabs.net import hubsync, tls, wire
from ide_agent_tabs.net.beacon import HubWatch, MemberBeacon, MemberCheck, build_member, parse_datagram
from ide_agent_tabs.net.cli import Services
from ide_agent_tabs.net.cli import main as net_main
from ide_agent_tabs.net.client import NetClientError, load_identity, peer_cert_path, send_request
from ide_agent_tabs.net.dispatch import NetDispatcher
from ide_agent_tabs.net.hubjoin import HubJoiner
from ide_agent_tabs.net.hubstate import (
    MAX_EPOCH_JUMP,
    MAX_PENDING,
    MAX_REMOVED,
    HubState,
    HubStateError,
    Pending,
    add_pending,
    adopt_key,
    drop_pending,
    forget_member,
    hub_state_path,
    read_hub_state,
    record_removed,
    rotate_key,
    write_hub_state,
)
from ide_agent_tabs.net.hubsync import HubRefusal, HubSync, announce_removal, flush_pending, introduce_member, leave_hub, push_key
from ide_agent_tabs.net.identity import format_digest
from ide_agent_tabs.net.listener import TrustCache
from ide_agent_tabs.net.pairing import MemberRef, RemovedRef, private_source
from ide_agent_tabs.net.peers import remove_peer, save_peer, update_address
from ide_agent_tabs.net.policy import Hub, Policy, grant_for, load_policy, save_policy
from ide_agent_tabs.net.store import NetStore
from net_support import LOOPBACK, FakeIngress, RunningListener, cert_of, create_home, device_of, make_root
from test_net_dispatch import NOW, FakeService, write_presence
from test_net_join import HUB_ID, NEEDS, JoinServer, dial, reset, set_name

KEY = "ab" * 32
ID_1, ID_2, ID_3, ID_4, ID_5 = (format_digest(bytes([number]) * 32) for number in range(1, 6))
OTHER_HUB_ID = "fedcba9876543210fedcba9876543210"
NAMES = {"a": "alpha", "b": "bravo", "c": "charlie", "d": "delta"}
ADDRESSES = {"a": "192.168.1.31", "b": "192.168.1.32", "c": "192.168.1.33", "d": "192.168.1.34"}


def refused(request_id: str = "r1") -> dict[str, Any]:
    return wire.refused_reply(request_id)


class Mesh:
    def __init__(self, test: unittest.TestCase, homes: dict[str, str]) -> None:
        self.homes = homes
        self.by_id = {device_of(home): home for home in homes.values()}
        self.offline: set[str] = set()
        self.calls: list[tuple[str, str, str]] = []
        self.notices: dict[str, list[str]] = {home: [] for home in homes.values()}
        self.bumps: dict[str, int] = {home: 0 for home in homes.values()}
        self.test = test
        self.strict = False

    def name_of(self, device: str) -> str:
        return next(name for name, home in self.homes.items() if device_of(home) == device)

    def go_offline(self, *names: str) -> None:
        self.offline.update(device_of(self.homes[name]) for name in names)

    def go_online(self, *names: str) -> None:
        self.offline.difference_update(device_of(self.homes[name]) for name in names)

    def sync(self, home: str) -> HubSync:
        def bump() -> None:
            self.bumps[home] += 1

        return HubSync(
            home,
            lambda _text: None,
            self.notices[home].append,
            bump,
            request=self.request_from(home),
            spawn=lambda task: task(),
            allow_address=private_source if self.strict else lambda _address: True,
        )

    def request_from(self, sender_home: str) -> Callable[..., dict[str, Any]]:
        def request(home: str, to: str, op: str, body: dict[str, Any], **_options: Any) -> dict[str, Any]:
            assert home == sender_home
            sender = device_of(home)
            self.calls.append((NAMES[self.name_of(sender)], NAMES[self.name_of(to)] if to in self.by_id else to, op))
            if grant_for(load_policy(home), to) is None:
                raise NetClientError("the peer is not trusted on this machine")
            if to in self.offline or to not in self.by_id:
                raise NetClientError("cannot reach the peer")
            parsed = wire.parse_request({"id": "r1", "op": op, "body": dict(body)})
            target = self.by_id[to]
            grant = grant_for(load_policy(target), sender)
            if grant is None:
                raise NetClientError("TLS failed; the peer may not trust this machine")
            try:
                reply = self.sync(target).handle(grant, parsed.body)  # pyright: ignore[reportArgumentType]
            except HubRefusal:
                return refused()
            return wire.ok_reply("r1", reply)

        return request


def version_of(home: str) -> tuple[int, str]:
    state = read_hub_state(home)
    assert state is not None
    return state.version()


@NEEDS
class HubCase(unittest.TestCase):
    homes: dict[str, str]

    @classmethod
    def setUpClass(cls) -> None:
        root = make_root(cls)
        cls.homes = {name: create_home(root, name) for name in NAMES}
        for name, home in cls.homes.items():
            set_name(home, NAMES[name])

    def setUp(self) -> None:
        for home in self.homes.values():
            reset(home)
        self.a, self.b, self.c, self.d = (self.homes[name] for name in "abcd")

    def form_hub(self, *names: str, key: str = KEY, epoch: int = 0) -> None:
        for name in names:
            save_policy(self.homes[name], Policy((), 0, Hub(HUB_ID, "home")))
            write_hub_state(self.homes[name], HubState(HUB_ID, key, (), epoch))
        for name in names:
            for other in names:
                if other != name:
                    pem = cert_of(self.homes[other]).read_text(encoding="ascii")
                    save_peer(self.homes[name], device_of(self.homes[other]), pem, ADDRESSES[other], NAMES[other], member=True)

    def mesh(self, *names: str) -> Mesh:
        return Mesh(self, {name: self.homes[name] for name in (names or tuple(NAMES))})

    def labels(self, home: str) -> list[str]:
        return sorted(p.label for p in load_policy(home).peers if p.member)

    def knows(self, home: str, other: str) -> bool:
        return grant_for(load_policy(home), device_of(self.homes[other])) is not None


class IntroductionTest(HubCase):
    def test_a_new_member_is_introduced_to_every_other_member_and_the_joiner_pins_the_member_list(self) -> None:
        self.form_hub("a", "b")
        rotate_key(self.a)
        mesh = self.mesh()

        def introduce(device: str) -> None:
            introduce_member(self.a, device, mesh.request_from(self.a))

        handler = HubJoiner(self.a, introduce=introduce)
        server = JoinServer(self, self.a, handler=handler)
        dialed = dial(server, self.c)
        self.assertEqual((dialed.outcome, server.wait_result().outcome), ("paired", "paired"))
        self.assertEqual(mesh.calls, [("alpha", "bravo", "introduce")])
        on_b = grant_for(load_policy(self.b), device_of(self.c))
        assert on_b is not None
        self.assertEqual((on_b.label, on_b.address, on_b.member), ("charlie", "127.0.0.1", True))
        self.assertEqual(mesh.notices[self.b], ["The hub member alpha introduced charlie. charlie joined from 127.0.0.1."])
        pinned = grant_for(load_policy(self.c), device_of(self.b))
        assert pinned is not None
        self.assertEqual((pinned.label, pinned.address, pinned.member), ("bravo", ADDRESSES["b"], True))
        self.assertTrue(peer_cert_path(self.c, device_of(self.b)).is_file())
        self.assertEqual(self.labels(self.c), ["alpha", "bravo"])
        self.assertEqual(version_of(self.c), version_of(self.a))
        self.assertEqual(version_of(self.c)[0], 1)

    def test_a_joiner_that_receives_the_hub_introduces_nobody(self) -> None:
        self.form_hub("a", "b")
        by_sender: list[str] = []
        by_joiner: list[str] = []
        server = JoinServer(self, self.c, handler=HubJoiner(self.c, introduce=by_joiner.append))
        dialed = dial(server, self.a, handler=HubJoiner(self.a, introduce=by_sender.append))
        self.assertEqual((dialed.outcome, server.wait_result().outcome), ("paired", "paired"))
        self.assertEqual((by_sender, by_joiner), ([device_of(self.c)], []))

    def introduce_c(self, mesh: Mesh) -> hubsync.Report:
        save_peer(self.a, device_of(self.c), cert_of(self.c).read_text(encoding="ascii"), ADDRESSES["c"], "charlie", member=True)
        return introduce_member(self.a, device_of(self.c), mesh.request_from(self.a))

    def test_the_introduction_reaches_every_other_member_and_never_the_newcomer(self) -> None:
        self.form_hub("a", "b", "d")
        mesh = self.mesh()
        report = self.introduce_c(mesh)
        self.assertEqual(sorted(report.told), ["bravo", "delta"])
        self.assertEqual(sorted(mesh.calls), [("alpha", "bravo", "introduce"), ("alpha", "delta", "introduce")])
        for home in (self.b, self.d):
            self.assertTrue(self.knows(home, "c"))
            state = read_hub_state(home)
            assert state is not None
            self.assertEqual([m.device_id for m in state.members], [device_of(self.c)])

    def test_an_introduction_from_a_device_the_receiver_does_not_pin_is_refused(self) -> None:
        self.form_hub("a", "b")
        mesh = self.mesh()
        save_policy(self.d, Policy((), 0, Hub(HUB_ID, "home")))
        save_peer(self.d, device_of(self.b), cert_of(self.b).read_text(encoding="ascii"), ADDRESSES["b"], "bravo", member=True)
        body = hubsync.introduce_body(
            HUB_ID,
            MemberRef(device_of(self.c), "charlie", ADDRESSES["c"], 47829, cert_of(self.c).read_text(encoding="ascii")),
        )
        with self.assertRaises(NetClientError):
            mesh.request_from(self.d)(self.d, device_of(self.b), "introduce", body)
        self.assertFalse(self.knows(self.b, "c"))

    def handle(self, receiver: str, sender: str, body: wire.HubBody) -> dict[str, Any]:
        grant = grant_for(load_policy(self.homes[receiver]), device_of(self.homes[sender]))
        assert grant is not None
        mesh = self.mesh()
        mesh.strict = True
        return mesh.sync(self.homes[receiver]).handle(grant, body)

    def introduce_body(self, hub: str = HUB_ID, **changes: Any) -> wire.IntroduceBody:
        fields: dict[str, Any] = {
            "hub": hub,
            "device_id": device_of(self.c),
            "label": "charlie",
            "address": ADDRESSES["c"],
            "port": 47829,
            "pem": cert_of(self.c).read_text(encoding="ascii"),
        }
        fields.update(changes)
        return wire.IntroduceBody(**fields)

    def refusal(self, receiver: str, sender: str, body: wire.HubBody) -> str:
        with self.assertRaises(HubRefusal) as caught:
            self.handle(receiver, sender, body)
        return caught.exception.reason

    def test_introductions_with_a_wrong_hub_a_bad_certificate_or_a_public_address_are_refused(self) -> None:
        self.form_hub("a", "b")
        self.assertEqual(self.refusal("b", "a", self.introduce_body(hub=OTHER_HUB_ID)), "wrong-hub")
        other_pem = cert_of(self.d).read_text(encoding="ascii")
        self.assertEqual(self.refusal("b", "a", self.introduce_body(pem=other_pem)), "invalid-member")
        self.assertEqual(self.refusal("b", "a", self.introduce_body(pem="not a certificate")), "invalid-member")
        self.assertEqual(self.refusal("b", "a", self.introduce_body(address="8.8.8.8")), "address-not-private")
        self.assertFalse(self.knows(self.b, "c"))

    def test_a_peer_that_is_not_a_member_cannot_introduce(self) -> None:
        self.form_hub("a", "b")
        save_peer(self.b, device_of(self.d), cert_of(self.d).read_text(encoding="ascii"), ADDRESSES["d"], "delta")
        self.assertEqual(self.refusal("b", "d", self.introduce_body()), "not-a-member")
        self.assertFalse(self.knows(self.b, "c"))

    def test_an_introduction_does_not_turn_an_existing_non_member_peer_into_a_member(self) -> None:
        self.form_hub("a", "b")
        save_peer(self.b, device_of(self.c), cert_of(self.c).read_text(encoding="ascii"), ADDRESSES["c"], "charlie")
        self.assertEqual(self.refusal("b", "a", self.introduce_body()), "not-a-member-peer")
        known = grant_for(load_policy(self.b), device_of(self.c))
        assert known is not None
        self.assertFalse(known.member)

    def test_introducing_a_known_member_or_this_device_changes_nothing_and_gives_no_notice(self) -> None:
        self.form_hub("a", "b", "c")
        mesh = self.mesh()
        sync = mesh.sync(self.b)
        grant = grant_for(load_policy(self.b), device_of(self.a))
        assert grant is not None
        self.assertEqual(sync.handle(grant, self.introduce_body()), {"added": False})
        self.assertEqual(
            sync.handle(grant, self.introduce_body(device_id=device_of(self.b), pem=cert_of(self.b).read_text(encoding="ascii"))),
            {"added": False},
        )
        self.assertEqual(mesh.notices[self.b], [])

    def test_a_label_that_another_peer_holds_gets_a_new_one(self) -> None:
        self.form_hub("a", "b")
        save_peer(self.b, device_of(self.d), cert_of(self.d).read_text(encoding="ascii"), ADDRESSES["d"], "charlie", member=True)
        self.assertEqual(self.handle("b", "a", self.introduce_body()), {"added": True})
        added = grant_for(load_policy(self.b), device_of(self.c))
        assert added is not None
        self.assertEqual(added.label, "charlie-2")

    def test_a_member_that_is_offline_gets_the_introduction_when_it_returns(self) -> None:
        self.form_hub("a", "b")
        mesh = self.mesh()
        mesh.go_offline("b")
        report = self.introduce_c(mesh)
        self.assertEqual((report.told, report.queued), ([], ["bravo"]))
        state = read_hub_state(self.a)
        assert state is not None
        self.assertEqual([(p.to, p.op, p.device) for p in state.pending], [(device_of(self.b), "introduce", device_of(self.c))])
        self.assertFalse(self.knows(self.b, "c"))
        self.assertEqual(flush_pending(self.a, mesh.request_from(self.a)), 0)
        mesh.go_online("b")
        self.assertEqual(flush_pending(self.a, mesh.request_from(self.a)), 1)
        self.assertTrue(self.knows(self.b, "c"))
        state = read_hub_state(self.a)
        assert state is not None
        self.assertEqual(state.pending, ())


class RevokeTest(HubCase):
    def revoke_d(self, mesh: Mesh) -> hubsync.Report:
        remove_peer(self.a, device_of(self.d))
        return announce_removal(self.a, device_of(self.d), mesh.request_from(self.a))

    def test_a_revoke_removes_the_device_on_every_member_and_replaces_the_key_everywhere(self) -> None:
        self.form_hub("a", "b", "c", "d")
        mesh = self.mesh()
        report = self.revoke_d(mesh)
        self.assertEqual(sorted(report.told), ["bravo", "charlie"])
        for home in (self.b, self.c):
            self.assertFalse(self.knows(home, "d"))
            self.assertFalse(peer_cert_path(home, device_of(self.d)).exists())
        self.assertEqual((mesh.bumps[self.b], mesh.bumps[self.c]), (1, 1))
        self.assertEqual(version_of(self.a), version_of(self.b))
        self.assertEqual(version_of(self.a), version_of(self.c))
        self.assertEqual(version_of(self.a)[0], 1)
        self.assertNotEqual(version_of(self.a)[1], KEY)
        self.assertNotIn("delta", [name for _sender, name, _op in mesh.calls])
        self.assertTrue(self.knows(self.d, "a"))
        self.assertEqual(version_of(self.d), (0, KEY))
        self.assertEqual(sorted(mesh.notices[self.b]), ["The hub member alpha removed delta from the hub."])

    def test_the_revoked_device_is_refused_on_every_member(self) -> None:
        self.form_hub("a", "b", "c", "d")
        self.revoke_d(self.mesh())
        for home in (self.b, self.c):
            Path(home, "config.json").write_text(json.dumps({"network": {"network": "lan", "bind": "192.168.1.10"}}))
            dispatcher = NetDispatcher(home, FakeService, lambda: NOW, lambda _text: None, alive=lambda _pid: True)  # pyright: ignore[reportArgumentType]
            request = {"id": "r1", "op": "open", "body": {"agent": "claude", "path": str(Path(home).resolve()), "task": "go"}}
            self.assertEqual(dispatcher.handle(device_of(self.d), request), refused())
            audit = [json.loads(line) for line in Path(home, "net", "audit.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual((audit[-1]["decision"], audit[-1]["reason"]), ("refused", "unknown-peer"))

    def test_a_removal_is_accepted_only_from_a_pinned_member_of_the_same_hub(self) -> None:
        self.form_hub("a", "b", "c")
        mesh = self.mesh()
        body = {"hub": HUB_ID, "device": device_of(self.c), "rotate": False}
        save_peer(self.b, device_of(self.d), cert_of(self.d).read_text(encoding="ascii"), ADDRESSES["d"], "delta")
        save_policy(self.d, Policy((), 0, Hub(HUB_ID, "home")))
        save_peer(self.d, device_of(self.b), cert_of(self.b).read_text(encoding="ascii"), ADDRESSES["b"], "bravo", member=True)
        self.assertEqual(mesh.request_from(self.d)(self.d, device_of(self.b), "remove", body), refused())
        self.assertTrue(self.knows(self.b, "c"))
        legacy = grant_for(load_policy(self.b), device_of(self.d))
        assert legacy is not None
        with self.assertRaises(HubRefusal) as caught:
            mesh.sync(self.b).handle(legacy, wire.RemoveBody(HUB_ID, device_of(self.c), False))
        self.assertEqual(caught.exception.reason, "not-a-member")
        member = grant_for(load_policy(self.b), device_of(self.a))
        assert member is not None
        with self.assertRaises(HubRefusal) as caught:
            mesh.sync(self.b).handle(member, wire.RemoveBody(OTHER_HUB_ID, device_of(self.c), False))
        self.assertEqual(caught.exception.reason, "wrong-hub")
        self.assertTrue(self.knows(self.b, "c"))

    def test_a_message_cannot_remove_the_receiver_itself(self) -> None:
        self.form_hub("a", "b")
        mesh = self.mesh()
        reply = mesh.request_from(self.a)(self.a, device_of(self.b), "remove", {"hub": HUB_ID, "device": device_of(self.b), "rotate": True})
        self.assertEqual(reply, refused())
        self.assertTrue(self.knows(self.b, "a"))
        self.assertEqual(version_of(self.b), (0, KEY))

    def test_only_member_peers_are_removed_and_an_unknown_device_is_a_no_op(self) -> None:
        self.form_hub("a", "b")
        save_peer(self.b, device_of(self.c), cert_of(self.c).read_text(encoding="ascii"), ADDRESSES["c"], "charlie")
        sync = self.mesh().sync(self.b)
        sender = grant_for(load_policy(self.b), device_of(self.a))
        assert sender is not None
        with self.assertRaises(HubRefusal) as caught:
            sync.handle(sender, wire.RemoveBody(HUB_ID, device_of(self.c), False))
        self.assertEqual(caught.exception.reason, "not-a-member-peer")
        self.assertEqual(sync.handle(sender, wire.RemoveBody(HUB_ID, device_of(self.d), False)), {"removed": False})
        self.assertTrue(self.knows(self.b, "c"))

    def test_a_member_that_is_offline_learns_the_removal_and_the_key_when_it_returns(self) -> None:
        self.form_hub("a", "b", "c", "d")
        mesh = self.mesh()
        mesh.go_offline("c")
        report = self.revoke_d(mesh)
        self.assertEqual((sorted(report.told), report.queued), (["bravo"], ["charlie"]))
        state = read_hub_state(self.a)
        assert state is not None
        self.assertEqual(sorted((p.op, p.device) for p in state.pending), sorted([("remove", device_of(self.d)), ("rekey", "")]))
        self.assertTrue(self.knows(self.c, "d"))
        mesh.go_online("c")
        self.assertEqual(flush_pending(self.a, mesh.request_from(self.a)), 2)
        self.assertFalse(self.knows(self.c, "d"))
        self.assertEqual(version_of(self.c), version_of(self.a))
        state = read_hub_state(self.a)
        assert state is not None
        self.assertEqual(state.pending, ())

    def test_queued_changes_for_a_device_that_left_or_refuses_them_or_is_stale_are_dropped(self) -> None:
        self.form_hub("a", "b", "c")
        mesh = self.mesh()
        old = Pending(device_of(self.b), "rekey", "", 0)
        gone = Pending(device_of(self.d), "rekey", "", hubsync.now_ms())
        unknown_subject = Pending(device_of(self.b), "introduce", device_of(self.d), hubsync.now_ms())
        add_pending(self.a, [old, gone, unknown_subject])
        self.assertEqual(flush_pending(self.a, mesh.request_from(self.a)), 0)
        state = read_hub_state(self.a)
        assert state is not None
        self.assertEqual(state.pending, ())
        refusing = Pending(device_of(self.b), "remove", device_of(self.c), hubsync.now_ms())
        add_pending(self.a, [refusing])
        self.assertEqual(flush_pending(self.a, lambda *_args, **_kwargs: refused()), 0)
        state = read_hub_state(self.a)
        assert state is not None
        self.assertEqual(state.pending, ())

    def test_net_revoke_removes_the_member_locally_and_forwards_the_removal_and_the_new_key(self) -> None:
        self.form_hub("a", "b", "c", "d")
        mesh = self.mesh()
        out: list[str] = []
        services = Services(
            home=self.a,
            out=out.append,
            err=out.append,
            member_request=mesh.request_from(self.a),
            is_remote_origin=lambda _home: False,
            notify=lambda _text: None,
        )
        self.assertEqual(net_main(["revoke", "delta"], services), 0, out)
        self.assertFalse(self.knows(self.a, "d"))
        self.assertFalse(self.knows(self.b, "d"))
        self.assertFalse(self.knows(self.c, "d"))
        self.assertEqual(version_of(self.a), version_of(self.c))
        self.assertTrue(any("hub key was replaced" in line for line in out), out)
        self.assertTrue(any(line.startswith("Told: bravo, charlie") for line in out), out)
        store = NetStore(self.a)
        try:
            self.assertEqual(store.generation(), 1)
        finally:
            store.close()

    def test_revoking_a_peer_that_is_not_a_member_sends_nothing(self) -> None:
        self.form_hub("a", "b")
        save_peer(self.a, device_of(self.d), cert_of(self.d).read_text(encoding="ascii"), ADDRESSES["d"], "delta")
        mesh = self.mesh()
        out: list[str] = []
        services = Services(
            home=self.a, out=out.append, err=out.append, member_request=mesh.request_from(self.a), is_remote_origin=lambda _home: False
        )
        self.assertEqual(net_main(["revoke", "delta"], services), 0, out)
        self.assertEqual(mesh.calls, [])
        self.assertEqual(version_of(self.a), (0, KEY))


class RotationTest(HubCase):
    def beacon_for(self, home: str, key: str) -> MemberBeacon:
        parsed = parse_datagram(build_member(key, HUB_ID, device_of(home), 1_800_000_000))
        assert isinstance(parsed, MemberBeacon)
        return parsed

    def test_a_beacon_signed_with_the_old_key_is_ignored_after_a_rotation(self) -> None:
        self.form_hub("a", "b", "c")
        check = MemberCheck(HubWatch(self.b), lambda: 1_800_000_000)
        self.assertTrue(check.check(self.beacon_for(self.a, KEY)).ok)
        remove_peer(self.a, device_of(self.c))
        announce_removal(self.a, device_of(self.c), self.mesh().request_from(self.a))
        new_key = version_of(self.b)[1]
        self.assertNotEqual(new_key, KEY)
        later = MemberCheck(HubWatch(self.b), lambda: 1_800_000_000)
        old = later.check(self.beacon_for(self.a, KEY))
        self.assertFalse(old.ok)
        self.assertIn("MAC", old.reason)
        self.assertTrue(later.check(self.beacon_for(self.a, new_key)).ok)

    def test_a_watch_that_already_read_the_old_key_reads_the_new_one(self) -> None:
        self.form_hub("a", "b")
        watch = HubWatch(self.b)
        before = watch()
        assert before is not None
        self.assertEqual(before.key, KEY)
        rotate_key(self.b)
        after = watch()
        assert after is not None
        self.assertNotEqual(after.key, KEY)

    def test_a_lower_or_equal_version_never_replaces_the_key(self) -> None:
        self.form_hub("a", "b", epoch=3)
        sync = self.mesh().sync(self.b)
        sender = grant_for(load_policy(self.b), device_of(self.a))
        assert sender is not None
        for epoch, key in ((2, "ff" * 32), (3, "00" * 32), (3, KEY)):
            self.assertEqual(sync.handle(sender, wire.RekeyBody(HUB_ID, epoch, key)), {"epoch": 3, "key": KEY})
        self.assertEqual(sync.handle(sender, wire.RekeyBody(HUB_ID, 3, "cd" * 32)), {"epoch": 3, "key": "cd" * 32})
        self.assertEqual(sync.handle(sender, wire.RekeyBody(HUB_ID, 4, "01" * 32)), {"epoch": 4, "key": "01" * 32})

    def test_a_key_for_another_hub_or_from_a_non_member_is_refused(self) -> None:
        self.form_hub("a", "b")
        save_peer(self.b, device_of(self.d), cert_of(self.d).read_text(encoding="ascii"), ADDRESSES["d"], "delta")
        sync = self.mesh().sync(self.b)
        member = grant_for(load_policy(self.b), device_of(self.a))
        legacy = grant_for(load_policy(self.b), device_of(self.d))
        assert member is not None and legacy is not None
        with self.assertRaises(HubRefusal) as caught:
            sync.handle(member, wire.RekeyBody(OTHER_HUB_ID, 9, "11" * 32))
        self.assertEqual(caught.exception.reason, "wrong-hub")
        with self.assertRaises(HubRefusal) as caught:
            sync.handle(legacy, wire.RekeyBody(HUB_ID, 9, "11" * 32))
        self.assertEqual(caught.exception.reason, "not-a-member")
        self.assertEqual(version_of(self.b), (0, KEY))

    def test_members_that_rotate_at_the_same_time_converge_on_the_highest_version(self) -> None:
        self.form_hub("a", "b", "c")
        rotate_key(self.a)
        rotate_key(self.b)
        self.assertNotEqual(version_of(self.a), version_of(self.b))
        highest = max(version_of(self.a), version_of(self.b))
        push_key(self.a, self.mesh().request_from(self.a))
        self.assertEqual({version_of(self.a), version_of(self.b), version_of(self.c)}, {highest})

    def test_a_key_that_reaches_a_member_clears_its_queued_key_change(self) -> None:
        self.form_hub("a", "b")
        add_pending(self.a, [Pending(device_of(self.b), "rekey", "", 1_800_000_000_000)])
        push_key(self.a, self.mesh().request_from(self.a))
        state = read_hub_state(self.a)
        assert state is not None
        self.assertEqual(state.pending, ())

    def test_the_epoch_only_goes_up(self) -> None:
        self.form_hub("a", "b")
        first = rotate_key(self.a)
        second = rotate_key(self.a)
        assert first is not None and second is not None
        self.assertEqual((first.epoch, second.epoch), (1, 2))
        self.assertNotEqual(first.key, second.key)
        self.assertIsNone(adopt_key(self.d, HUB_ID, 5, "22" * 32))
        updated = adopt_key(self.a, HUB_ID, 1, "ff" * 32)
        assert updated is not None
        self.assertEqual(updated.epoch, 2)
        wrong = adopt_key(self.a, OTHER_HUB_ID, 9, "ff" * 32)
        assert wrong is not None
        self.assertEqual(wrong.epoch, 2)


class LeaveTest(HubCase):
    def lowest_of(self, names: str) -> str:
        return min(names, key=lambda name: device_of(self.homes[name]))

    def test_leaving_tells_every_member_and_the_one_with_the_lowest_device_id_replaces_the_key(self) -> None:
        self.form_hub("a", "b", "c", "d")
        mesh = self.mesh()
        report = leave_hub(self.d, mesh.request_from(self.d))
        rotator = self.lowest_of("abc")
        self.assertEqual(report.rotator, NAMES[rotator])
        self.assertIn(NAMES[rotator], report.told)
        self.assertEqual(sorted(report.told + report.queued + report.refused), ["alpha", "bravo", "charlie"])
        for home in (self.a, self.b, self.c):
            self.assertFalse(self.knows(home, "d"))
        self.assertEqual({version_of(self.a), version_of(self.b), version_of(self.c)}, {version_of(self.a)})
        self.assertEqual(version_of(self.a)[0], 1)
        self.assertNotEqual(version_of(self.a)[1], KEY)
        self.assertEqual(version_of(self.d), (0, KEY))
        self.assertEqual(mesh.calls[0], ("delta", NAMES[rotator], "remove"))
        for name in "abc":
            self.assertTrue(any("delta" in text for text in mesh.notices[self.homes[name]]), name)

    def test_the_next_member_replaces_the_key_when_the_lowest_is_offline(self) -> None:
        self.form_hub("a", "b", "c", "d")
        mesh = self.mesh()
        ordered = sorted("abc", key=lambda name: device_of(self.homes[name]))
        mesh.go_offline(ordered[0])
        report = leave_hub(self.d, mesh.request_from(self.d))
        self.assertEqual(report.rotator, NAMES[ordered[1]])
        self.assertIn(NAMES[ordered[0]], report.queued)
        self.assertTrue(self.knows(self.homes[ordered[0]], "d"))
        rotator_home = self.homes[ordered[1]]
        state = read_hub_state(rotator_home)
        assert state is not None
        self.assertIn(("rekey", device_of(self.homes[ordered[0]])), [(p.op, p.to) for p in state.pending])
        self.assertIn(("remove", device_of(self.d)), [(p.op, p.device) for p in state.pending])
        mesh.go_online(ordered[0])
        flush_pending(rotator_home, mesh.request_from(rotator_home))
        self.assertFalse(self.knows(self.homes[ordered[0]], "d"))
        self.assertEqual(version_of(self.homes[ordered[0]]), version_of(rotator_home))

    def test_with_no_member_reachable_nothing_is_replaced_and_the_report_says_so(self) -> None:
        self.form_hub("a", "b", "d")
        mesh = self.mesh()
        mesh.go_offline("a", "b")
        report = leave_hub(self.d, mesh.request_from(self.d))
        self.assertIsNone(report.rotator)
        self.assertEqual(sorted(report.queued), ["alpha", "bravo"])
        self.assertEqual(version_of(self.a), (0, KEY))

    def test_net_hub_leave_tells_the_members_before_it_clears_the_local_state(self) -> None:
        self.form_hub("a", "b", "c", "d")
        mesh = self.mesh()
        out: list[str] = []
        services = Services(
            home=self.d, out=out.append, err=out.append, member_request=mesh.request_from(self.d), is_remote_origin=lambda _home: False
        )
        self.assertEqual(net_main(["hub", "leave"], services), 0, out)
        self.assertIsNone(load_policy(self.d).hub)
        self.assertEqual(load_policy(self.d).peers, ())
        self.assertIsNone(read_hub_state(self.d))
        for home in (self.a, self.b, self.c):
            self.assertFalse(self.knows(home, "d"))
        rotator = NAMES[self.lowest_of("abc")]
        self.assertTrue(any(f"{rotator} replaces the hub key" in line for line in out), out)
        self.assertEqual(version_of(self.a)[0], 1)

    def test_a_leave_with_no_answer_says_the_key_is_not_replaced_yet(self) -> None:
        self.form_hub("a", "d")
        mesh = self.mesh()
        mesh.go_offline("a")
        out: list[str] = []
        services = Services(
            home=self.d, out=out.append, err=out.append, member_request=mesh.request_from(self.d), is_remote_origin=lambda _home: False
        )
        self.assertEqual(net_main(["hub", "leave"], services), 0, out)
        self.assertTrue(any("not replaced yet" in line for line in out), out)
        self.assertIsNone(load_policy(self.d).hub)


class LiveTlsTest(HubCase):
    def setUp(self) -> None:
        super().setUp()
        self.form_hub("a", "b")
        save_peer(self.a, device_of(self.c), cert_of(self.c).read_text(encoding="ascii"), ADDRESSES["c"], "charlie", member=True)
        for home in (self.a, self.b):
            Path(home, "config.json").write_text(json.dumps({"network": {"network": "lan", "bind": "192.168.1.10", "deviceName": "x"}}))
        write_presence(self.b, "local-1", "claude", "idle", "desk")
        save_peer(self.b, device_of(self.a), cert_of(self.a).read_text(encoding="ascii"), LOOPBACK, "alpha", member=True)
        self.dispatcher = NetDispatcher(self.b, FakeService, lambda: NOW, lambda _text: None, alive=lambda _pid: True)  # pyright: ignore[reportArgumentType]
        self.ingress = FakeIngress(self.b, lambda peer, request: self.dispatcher.handle(peer, request))
        self.addCleanup(self.ingress.close)
        self.running = RunningListener(self.b, self.ingress.port).start()
        self.addCleanup(self.running.stop)

    def request(self, home: str, to: str, op: str, body: dict[str, Any], **options: Any) -> dict[str, Any]:
        return send_request(home, to, op, body, port=self.running.port, address=LOOPBACK, **options)

    def test_an_introduction_travels_over_pinned_mutual_tls_and_reaches_the_sessions(self) -> None:
        report = introduce_member(self.a, device_of(self.c), self.request)
        self.assertEqual((report.told, report.queued, report.refused), (["bravo"], [], []))
        self.assertTrue(self.knows(self.b, "c"))
        texts = [m["text"] for m in mail_to(self.b, "local-1")]
        self.assertEqual(sum("charlie joined from 192.168.1.33" in text for text in texts), 1)

    def test_a_revoke_travels_over_pinned_mutual_tls_and_the_key_follows(self) -> None:
        save_peer(self.b, device_of(self.c), cert_of(self.c).read_text(encoding="ascii"), ADDRESSES["c"], "charlie", member=True)
        remove_peer(self.a, device_of(self.c))
        report = announce_removal(self.a, device_of(self.c), self.request)
        self.assertEqual(report.told, ["bravo"])
        self.assertFalse(self.knows(self.b, "c"))
        self.assertEqual(version_of(self.a), version_of(self.b))
        self.assertEqual(version_of(self.a)[0], 1)
        unknown = self.dispatcher.handle(device_of(self.c), {"id": "r2", "op": "inventory", "body": {}})
        self.assertEqual(unknown, refused("r2"))

    def test_a_device_that_the_receiver_does_not_pin_cannot_get_a_hub_request_through(self) -> None:
        save_policy(self.d, Policy((), 0, Hub(HUB_ID, "home")))
        save_peer(self.d, device_of(self.b), cert_of(self.b).read_text(encoding="ascii"), LOOPBACK, "bravo", member=True)
        with self.assertRaises(NetClientError):
            self.request(self.d, device_of(self.b), "rekey", {"hub": HUB_ID, "epoch": 5, "key": "33" * 32})
        self.assertEqual(version_of(self.b), (0, KEY))


class StateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.home = make_root(self)

    def test_the_epoch_and_the_pending_changes_round_trip(self) -> None:
        state = HubState(
            HUB_ID, KEY, (), 4, (Pending(ID_1, "introduce", ID_2, 5), Pending(ID_2, "rekey", "", 6), Pending(ID_1, "remove", ID_2, 7))
        )
        write_hub_state(self.home, state)
        self.assertEqual(read_hub_state(self.home), state)
        self.assertEqual(
            sorted(json.loads(hub_state_path(self.home).read_text(encoding="utf-8"))),
            ["epoch", "hub", "key", "members", "pending", "removed"],
        )

    def test_the_removed_devices_and_the_epoch_of_a_key_change_round_trip(self) -> None:
        state = HubState(HUB_ID, KEY, (), 4, (Pending(ID_1, "rekey", "", 6, 4),), (RemovedRef(ID_2, 9), RemovedRef(ID_3, 10)))
        write_hub_state(self.home, state)
        self.assertEqual(read_hub_state(self.home), state)

    def test_a_file_without_an_epoch_or_pending_changes_reads_with_defaults(self) -> None:
        write_hub_state(self.home, HubState(HUB_ID, KEY))
        path = hub_state_path(self.home)
        body = json.loads(path.read_text(encoding="utf-8"))
        del body["epoch"], body["pending"]
        path.write_text(json.dumps(body), encoding="utf-8")
        self.assertEqual(read_hub_state(self.home), HubState(HUB_ID, KEY))

    def test_a_damaged_epoch_or_pending_entry_is_refused(self) -> None:
        write_hub_state(self.home, HubState(HUB_ID, KEY))
        path = hub_state_path(self.home)
        good = json.loads(path.read_text(encoding="utf-8"))
        for changes in (
            {"epoch": -1},
            {"epoch": True},
            {"epoch": 2**31},
            {"pending": {}},
            {"pending": [{"to": "x"}]},
            {"pending": [{"to": ID_1, "op": "bogus", "device": "", "atMs": 1}]},
            {"pending": [{"to": ID_1, "op": "remove", "device": "x", "atMs": 1}]},
            {"pending": [{"to": "x", "op": "remove", "device": ID_2, "atMs": 1}]},
            {"pending": [{"to": ID_1, "op": "rekey", "device": "", "atMs": 1, "epoch": 2**31}]},
            {"removed": {}},
            {"removed": [{"deviceId": "x", "atMs": 1}]},
            {"removed": [{"deviceId": ID_1}]},
            {"removed": [{"deviceId": ID_1, "atMs": 1}] * (MAX_REMOVED + 1)},
        ):
            path.write_text(json.dumps({**good, **changes}), encoding="utf-8")
            with self.assertRaises(HubStateError, msg=str(changes)):
                read_hub_state(self.home)

    def test_the_same_pending_task_is_kept_once(self) -> None:
        write_hub_state(self.home, HubState(HUB_ID, KEY))
        add_pending(self.home, [Pending(ID_1, "rekey", "", 1)])
        add_pending(self.home, [Pending(ID_1, "rekey", "", 2)])
        state = read_hub_state(self.home)
        assert state is not None
        self.assertEqual(len(state.pending), 1)

    def test_forgetting_a_member_drops_its_tasks_but_keeps_the_notice_that_it_was_removed(self) -> None:
        a, b, c = ID_1, ID_2, ID_3
        write_hub_state(self.home, HubState(HUB_ID, KEY))
        tasks = [Pending(b, "rekey", "", 1), Pending(a, "introduce", b, 1), Pending(a, "remove", b, 1), Pending(a, "rekey", "", 1)]
        add_pending(self.home, [*tasks, Pending(c, "rekey", "", 1)])
        forget_member(self.home, b)
        state = read_hub_state(self.home)
        assert state is not None
        self.assertEqual(sorted((p.to, p.op) for p in state.pending), sorted([(a, "remove"), (a, "rekey"), (c, "rekey")]))

    def test_no_hub_file_means_no_update(self) -> None:
        self.assertIsNone(rotate_key(self.home))
        self.assertIsNone(adopt_key(self.home, HUB_ID, 1, KEY))


class WireTest(unittest.TestCase):
    MEMBER: ClassVar[dict[str, Any]] = {
        "deviceId": ID_1,
        "label": "alpha",
        "address": "192.168.1.5",
        "port": 47829,
        "certificatePem": "pem",
    }

    def parse(self, op: str, body: dict[str, Any]) -> wire.Request:
        return wire.parse_request({"id": "r1", "op": op, "body": body})

    def test_the_hub_requests_parse(self) -> None:
        introduce = self.parse("introduce", {"hub": HUB_ID, "member": self.MEMBER}).body
        self.assertEqual(introduce, wire.IntroduceBody(HUB_ID, ID_1, "alpha", "192.168.1.5", 47829, "pem"))
        self.assertEqual(self.parse("remove", {"hub": HUB_ID, "device": ID_1, "rotate": True}).body, wire.RemoveBody(HUB_ID, ID_1, True))
        self.assertEqual(self.parse("rekey", {"hub": HUB_ID, "epoch": 3, "key": KEY}).body, wire.RekeyBody(HUB_ID, 3, KEY))

    def test_malformed_hub_requests_are_refused(self) -> None:
        bad: list[tuple[str, dict[str, Any]]] = [
            ("introduce", {"hub": HUB_ID}),
            ("introduce", {"hub": "short", "member": self.MEMBER}),
            ("introduce", {"hub": HUB_ID, "member": {**self.MEMBER, "extra": 1}}),
            ("introduce", {"hub": HUB_ID, "member": {**self.MEMBER, "address": "999.1.1.1x"}}),
            ("introduce", {"hub": HUB_ID, "member": {**self.MEMBER, "port": True}}),
            ("introduce", {"hub": HUB_ID, "member": {**self.MEMBER, "port": 0}}),
            ("introduce", {"hub": HUB_ID, "member": {**self.MEMBER, "label": "Alpha"}}),
            ("introduce", {"hub": HUB_ID, "member": {**self.MEMBER, "certificatePem": "x" * 9000}}),
            ("remove", {"hub": HUB_ID, "device": ID_1}),
            ("remove", {"hub": HUB_ID, "device": ID_1, "rotate": 1}),
            ("remove", {"hub": HUB_ID, "device": "a b", "rotate": False}),
            ("remove", {"hub": HUB_ID, "device": "x", "rotate": False}),
            ("remove", {"hub": HUB_ID, "device": "x", "rotate": True}),
            ("remove", {"hub": HUB_ID, "device": ID_1.lower(), "rotate": False}),
            ("remove", {"hub": HUB_ID, "device": ID_1.replace("-", ""), "rotate": False}),
            ("remove", {"hub": HUB_ID, "device": 7, "rotate": False}),
            ("introduce", {"hub": HUB_ID, "member": {**self.MEMBER, "deviceId": "x"}}),
            ("rekey", {"hub": HUB_ID, "epoch": True, "key": KEY}),
            ("rekey", {"hub": HUB_ID, "epoch": -1, "key": KEY}),
            ("rekey", {"hub": HUB_ID, "epoch": 2**31, "key": KEY}),
            ("rekey", {"hub": HUB_ID, "epoch": 1, "key": "AB" * 32}),
            ("rekey", {"hub": HUB_ID, "epoch": 1, "key": KEY, "x": 1}),
        ]
        for op, body in bad:
            with self.assertRaises(wire.WireError, msg=f"{op} {body}"):
                self.parse(op, body)


def far_id(number: int) -> str:
    return format_digest(number.to_bytes(32, "big"))


class RemovalRecordTest(HubCase):
    def introduce_d_body(self) -> wire.IntroduceBody:
        return wire.IntroduceBody(HUB_ID, device_of(self.d), "delta", ADDRESSES["d"], 47829, cert_of(self.d).read_text(encoding="ascii"))

    def handle(self, receiver: str, sender: str, body: wire.HubBody) -> dict[str, Any]:
        grant = grant_for(load_policy(self.homes[receiver]), device_of(self.homes[sender]))
        assert grant is not None
        return self.mesh().sync(self.homes[receiver]).handle(grant, body)

    def removed_on(self, home: str) -> list[str]:
        state = read_hub_state(home)
        assert state is not None
        return [r.device_id for r in state.removed]

    def test_a_removal_leaves_a_record_and_a_late_introduction_of_that_device_is_refused(self) -> None:
        self.form_hub("a", "b", "d")
        self.assertEqual(self.handle("b", "a", wire.RemoveBody(HUB_ID, device_of(self.d), False)), {"removed": True})
        self.assertEqual(self.removed_on(self.b), [device_of(self.d)])
        with self.assertRaises(HubRefusal) as caught:
            self.handle("b", "a", self.introduce_d_body())
        self.assertEqual(caught.exception.reason, "removed-member")
        self.assertFalse(self.knows(self.b, "d"))

    def test_a_removal_of_a_device_the_member_never_knew_still_leaves_a_record(self) -> None:
        self.form_hub("a", "b")
        self.assertEqual(self.handle("b", "a", wire.RemoveBody(HUB_ID, device_of(self.d), False)), {"removed": False})
        self.assertEqual(self.removed_on(self.b), [device_of(self.d)])
        with self.assertRaises(HubRefusal) as caught:
            self.handle("b", "a", self.introduce_d_body())
        self.assertEqual(caught.exception.reason, "removed-member")

    def test_net_revoke_records_the_removal_on_the_revoker(self) -> None:
        self.form_hub("a", "b", "d")
        out: list[str] = []
        services = Services(
            home=self.a,
            out=out.append,
            err=out.append,
            member_request=self.mesh().request_from(self.a),
            is_remote_origin=lambda _home: False,
        )
        self.assertEqual(net_main(["revoke", "delta"], services), 0, out)
        self.assertEqual(self.removed_on(self.a), [device_of(self.d)])
        self.assertEqual(self.removed_on(self.b), [device_of(self.d)])

    def test_an_explicit_join_with_the_device_clears_the_record(self) -> None:
        self.form_hub("a")
        record_removed(self.a, device_of(self.d))
        server = JoinServer(self, self.a)
        dialed = dial(server, self.d)
        self.assertEqual((dialed.outcome, server.wait_result().outcome), ("paired", "paired"))
        self.assertEqual(self.removed_on(self.a), [])
        self.assertTrue(self.knows(self.a, "d"))

    def test_the_hub_message_hands_the_record_to_the_joiner_and_the_joiner_drops_a_removed_member(self) -> None:
        self.form_hub("a", "b", "d")
        record_removed(self.a, device_of(self.d))
        server = JoinServer(self, self.a)
        dialed = dial(server, self.c)
        self.assertEqual((dialed.outcome, server.wait_result().outcome), ("paired", "paired"))
        self.assertEqual(self.labels(self.c), ["alpha", "bravo"])
        self.assertFalse(self.knows(self.c, "d"))
        self.assertEqual(self.removed_on(self.c), [device_of(self.d)])

    def test_a_record_the_joiner_already_holds_also_drops_a_member_a_lagging_member_still_lists(self) -> None:
        self.form_hub("a", "b", "d")
        save_policy(self.c, Policy((), 0, Hub(HUB_ID, "home")))
        write_hub_state(self.c, HubState(HUB_ID, KEY, (), 0, (), (RemovedRef(device_of(self.d), 1),)))
        server = JoinServer(self, self.a)
        dialed = dial(server, self.c)
        self.assertEqual((dialed.outcome, server.wait_result().outcome), ("paired", "paired"))
        self.assertEqual(self.labels(self.c), ["alpha", "bravo"])
        self.assertEqual(self.removed_on(self.c), [device_of(self.d)])

    def test_the_record_stays_bounded_and_keeps_the_newest_entries(self) -> None:
        home = make_root(self)
        write_hub_state(home, HubState(HUB_ID, KEY))
        for number in range(1, MAX_REMOVED + 6):
            record_removed(home, far_id(number))
        state = read_hub_state(home)
        assert state is not None
        ids = [r.device_id for r in state.removed]
        self.assertEqual(len(ids), MAX_REMOVED)
        self.assertNotIn(far_id(1), ids)
        self.assertEqual(ids[-1], far_id(MAX_REMOVED + 5))
        record_removed(home, far_id(10))
        again = read_hub_state(home)
        assert again is not None
        self.assertEqual([r.device_id for r in again.removed][-1], far_id(10))
        self.assertEqual(len(again.removed), MAX_REMOVED)


class DamagedHubFileTest(HubCase):
    def damage(self, home: str, **changes: Any) -> None:
        path = hub_state_path(home)
        body = json.loads(path.read_text(encoding="utf-8"))
        body.update(changes)
        path.write_text(json.dumps(body), encoding="utf-8")

    def test_a_queued_entry_with_an_id_the_wire_would_refuse_is_a_hub_state_error(self) -> None:
        self.form_hub("a", "b")
        self.damage(self.a, pending=[{"to": device_of(self.b), "op": "remove", "device": "x", "atMs": 1}])
        with self.assertRaises(HubStateError):
            read_hub_state(self.a)

    def test_the_beacon_watch_reads_a_damaged_file_as_no_view_and_names_the_problem(self) -> None:
        self.form_hub("a", "b")
        self.damage(self.a, pending=[{"to": device_of(self.b), "op": "remove", "device": "x", "atMs": 1}])
        watch = HubWatch(self.a)
        self.assertIsNone(watch())
        self.assertIsNotNone(watch.problem)
        write_hub_state(self.a, HubState(HUB_ID, KEY))
        view = watch()
        assert view is not None
        self.assertIsNone(watch.problem)

    def test_flush_pending_reports_a_damaged_file_as_a_hub_state_error(self) -> None:
        self.form_hub("a", "b")
        self.damage(self.a, pending=[{"to": "x", "op": "remove", "device": "x", "atMs": 1}])
        with self.assertRaises(HubStateError):
            flush_pending(self.a, self.mesh().request_from(self.a))

    def test_a_revoke_still_tells_the_members_when_the_hub_file_is_damaged(self) -> None:
        self.form_hub("a", "b", "c", "d")
        self.damage(self.a, pending=[{"to": device_of(self.b), "op": "remove", "device": "x", "atMs": 1}])
        mesh = self.mesh()
        out: list[str] = []
        services = Services(
            home=self.a, out=out.append, err=out.append, member_request=mesh.request_from(self.a), is_remote_origin=lambda _home: False
        )
        self.assertEqual(net_main(["revoke", "delta"], services), 0, out)
        self.assertFalse(self.knows(self.a, "d"))
        self.assertFalse(self.knows(self.b, "d"))
        self.assertFalse(self.knows(self.c, "d"))
        self.assertTrue(any("hub state file is not usable" in line for line in out), out)
        self.assertFalse(any("hub key was replaced" in line for line in out), out)

    def test_announcing_a_removal_with_a_damaged_file_still_sends_it_and_reports_the_problem(self) -> None:
        self.form_hub("a", "b", "d")
        self.damage(self.a, members=[{"deviceId": "x"}])
        remove_peer(self.a, device_of(self.d))
        report = announce_removal(self.a, device_of(self.d), self.mesh().request_from(self.a))
        self.assertEqual(report.told, ["bravo"])
        self.assertIsNotNone(report.state_problem)
        self.assertFalse(self.knows(self.b, "d"))


class RotationWorkerTest(HubCase):
    def sync_on_b(self) -> tuple[HubSync, list[Any]]:
        spawned: list[Any] = []
        mesh = self.mesh()
        sync = HubSync(
            self.b,
            lambda _text: None,
            lambda _text: None,
            lambda: None,
            request=mesh.request_from(self.b),
            spawn=spawned.append,
            allow_address=lambda _address: True,
        )
        return sync, spawned

    def from_a(self, sync: HubSync, body: wire.HubBody) -> dict[str, Any]:
        grant = grant_for(load_policy(self.b), device_of(self.a))
        assert grant is not None
        return sync.handle(grant, body)

    def test_rotating_removals_share_one_worker(self) -> None:
        self.form_hub("a", "b", "c", "d")
        sync, spawned = self.sync_on_b()
        self.assertEqual(self.from_a(sync, wire.RemoveBody(HUB_ID, device_of(self.c), True)), {"removed": True})
        self.assertEqual(self.from_a(sync, wire.RemoveBody(HUB_ID, device_of(self.d), True)), {"removed": True})
        self.assertEqual(len(spawned), 1)
        self.assertEqual(version_of(self.b)[0], 0)
        spawned[0]()
        self.assertEqual(version_of(self.b)[0], 2)

    def test_a_removal_that_arrives_after_the_worker_finished_starts_the_next_one(self) -> None:
        self.form_hub("a", "b", "c", "d")
        sync, spawned = self.sync_on_b()
        self.from_a(sync, wire.RemoveBody(HUB_ID, device_of(self.c), True))
        spawned.pop()()
        self.from_a(sync, wire.RemoveBody(HUB_ID, device_of(self.d), True))
        self.assertEqual(len(spawned), 1)
        spawned.pop()()
        self.assertEqual(version_of(self.b)[0], 2)

    def test_a_rotating_removal_of_a_device_that_is_not_a_member_is_refused_and_rotates_nothing(self) -> None:
        self.form_hub("a", "b")
        sync, spawned = self.sync_on_b()
        with self.assertRaises(HubRefusal) as caught:
            self.from_a(sync, wire.RemoveBody(HUB_ID, device_of(self.d), True))
        self.assertEqual(caught.exception.reason, "unknown-member")
        self.assertEqual(spawned, [])
        self.assertEqual(version_of(self.b), (0, KEY))


class PendingQueueTest(HubCase):
    def fill(self, home: str, op: str, count: int) -> None:
        recipients = [far_id(number) for number in range(1, 17)]
        subjects = [far_id(number) for number in range(101, 117)]
        tasks = [Pending(recipients[n // 16], op, subjects[n % 16], 1) for n in range(count)]
        add_pending(home, tasks)

    def pending_of(self, home: str) -> tuple[Pending, ...]:
        state = read_hub_state(home)
        assert state is not None
        return state.pending

    def test_a_removal_evicts_the_oldest_other_task_when_the_queue_is_full_and_is_never_evicted(self) -> None:
        home = make_root(self)
        write_hub_state(home, HubState(HUB_ID, KEY))
        self.fill(home, "introduce", MAX_PENDING)
        self.assertEqual(len(self.pending_of(home)), MAX_PENDING)
        removal = Pending(far_id(40), "remove", far_id(41), 2)
        add_pending(home, [removal])
        queue = self.pending_of(home)
        self.assertEqual(len(queue), MAX_PENDING)
        self.assertIn(removal, queue)
        self.assertEqual(sum(1 for task in queue if task.op == "introduce"), MAX_PENDING - 1)

    def test_a_full_queue_refuses_a_new_task_that_is_not_a_removal(self) -> None:
        home = make_root(self)
        write_hub_state(home, HubState(HUB_ID, KEY))
        self.fill(home, "remove", MAX_PENDING)
        before = self.pending_of(home)
        add_pending(home, [Pending(far_id(40), "introduce", far_id(41), 2), Pending(far_id(40), "rekey", "", 2, 1)])
        self.assertEqual(self.pending_of(home), before)

    def test_a_full_queue_of_removals_keeps_every_one_of_them(self) -> None:
        home = make_root(self)
        write_hub_state(home, HubState(HUB_ID, KEY))
        self.fill(home, "remove", MAX_PENDING)
        before = self.pending_of(home)
        add_pending(home, [Pending(far_id(40), "remove", far_id(41), 2)])
        self.assertEqual(self.pending_of(home), before)

    def test_the_same_removal_is_queued_once(self) -> None:
        home = make_root(self)
        write_hub_state(home, HubState(HUB_ID, KEY))
        add_pending(home, [Pending(ID_1, "remove", ID_2, 1), Pending(ID_1, "remove", ID_2, 2)])
        self.assertEqual(len(self.pending_of(home)), 1)

    def test_a_newer_key_change_replaces_an_older_one_for_the_same_member_and_an_older_one_is_ignored(self) -> None:
        home = make_root(self)
        write_hub_state(home, HubState(HUB_ID, KEY))
        add_pending(home, [Pending(ID_1, "rekey", "", 1, 1)])
        add_pending(home, [Pending(ID_1, "rekey", "", 2, 2), Pending(ID_2, "rekey", "", 2, 1)])
        self.assertEqual([(p.to, p.epoch) for p in self.pending_of(home)], [(ID_1, 2), (ID_2, 1)])
        add_pending(home, [Pending(ID_1, "rekey", "", 3, 1)])
        self.assertEqual([(p.to, p.epoch) for p in self.pending_of(home)], [(ID_1, 2), (ID_2, 1)])

    def test_dropping_a_sent_key_change_leaves_a_newer_queued_one(self) -> None:
        home = make_root(self)
        write_hub_state(home, HubState(HUB_ID, KEY))
        add_pending(home, [Pending(ID_1, "rekey", "", 2, 2)])
        drop_pending(home, [Pending(ID_1, "rekey", "", 0, 1)])
        self.assertEqual([(p.to, p.epoch) for p in self.pending_of(home)], [(ID_1, 2)])
        drop_pending(home, [Pending(ID_1, "rekey", "", 0, 2)])
        self.assertEqual(self.pending_of(home), ())

    def test_a_key_change_queued_while_another_is_being_sent_survives_the_flush(self) -> None:
        self.form_hub("a", "b")
        mesh = self.mesh()
        add_pending(self.a, [Pending(device_of(self.b), "rekey", "", hubsync.now_ms(), 0)])
        send = mesh.request_from(self.a)

        def request(home: str, to: str, op: str, body: dict[str, Any], **options: Any) -> dict[str, Any]:
            reply = send(home, to, op, body, **options)
            add_pending(self.a, [Pending(device_of(self.b), "rekey", "", hubsync.now_ms(), 1)])
            return reply

        self.assertEqual(flush_pending(self.a, request), 1)
        self.assertEqual([(p.op, p.epoch) for p in self.pending_of(self.a)], [("rekey", 1)])

    def test_a_key_change_queued_while_the_key_is_pushed_survives_the_push(self) -> None:
        self.form_hub("a", "b")
        send = self.mesh().request_from(self.a)

        def request(home: str, to: str, op: str, body: dict[str, Any], **options: Any) -> dict[str, Any]:
            reply = send(home, to, op, body, **options)
            add_pending(self.a, [Pending(device_of(self.b), "rekey", "", hubsync.now_ms(), 5)])
            return reply

        push_key(self.a, request)
        self.assertEqual([(p.op, p.epoch) for p in self.pending_of(self.a)], [("rekey", 5)])


class EpochBoundTest(HubCase):
    def sync_and_sender(self) -> tuple[HubSync, Any]:
        sender = grant_for(load_policy(self.b), device_of(self.a))
        assert sender is not None
        return self.mesh().sync(self.b), sender

    def test_a_rekey_far_above_the_local_epoch_is_refused_and_the_epoch_can_still_advance(self) -> None:
        self.form_hub("a", "b", epoch=3)
        sync, sender = self.sync_and_sender()
        with self.assertRaises(HubRefusal) as caught:
            sync.handle(sender, wire.RekeyBody(HUB_ID, wire.MAX_EPOCH, "ee" * 32))
        self.assertEqual(caught.exception.reason, "epoch-too-far")
        with self.assertRaises(HubRefusal):
            sync.handle(sender, wire.RekeyBody(HUB_ID, 3 + MAX_EPOCH_JUMP + 1, "ee" * 32))
        self.assertEqual(version_of(self.b), (3, KEY))
        self.assertEqual(
            sync.handle(sender, wire.RekeyBody(HUB_ID, 3 + MAX_EPOCH_JUMP, "ee" * 32)), {"epoch": 3 + MAX_EPOCH_JUMP, "key": "ee" * 32}
        )
        rotated = rotate_key(self.b)
        assert rotated is not None
        self.assertEqual(rotated.epoch, 3 + MAX_EPOCH_JUMP + 1)

    def test_a_member_cannot_raise_the_epoch_through_the_reply_to_a_key_push(self) -> None:
        self.form_hub("a", "b")

        def request(home: str, to: str, op: str, body: dict[str, Any], **_options: Any) -> dict[str, Any]:
            return wire.ok_reply("r1", {"epoch": wire.MAX_EPOCH, "key": "ee" * 32})

        push_key(self.a, request)
        self.assertEqual(version_of(self.a), (0, KEY))
        self.assertIsNone(adopt_key(self.d, HUB_ID, 1, KEY))
        updated = adopt_key(self.a, HUB_ID, MAX_EPOCH_JUMP, "ee" * 32)
        assert updated is not None
        self.assertEqual(updated.epoch, MAX_EPOCH_JUMP)
        ignored = adopt_key(self.a, HUB_ID, wire.MAX_EPOCH, "dd" * 32)
        assert ignored is not None
        self.assertEqual(ignored.epoch, MAX_EPOCH_JUMP)


class ThreadSafetyTest(unittest.TestCase):
    def test_concurrent_pending_additions_all_land(self) -> None:
        home = make_root(self)
        write_hub_state(home, HubState(HUB_ID, KEY))
        subjects = (ID_2, ID_3, ID_4, ID_5, format_digest(bytes([9]) * 32))
        threads = [threading.Thread(target=add_pending, args=(home, [Pending(ID_1, "introduce", subject, 1)])) for subject in subjects]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20)
        state = read_hub_state(home)
        assert state is not None
        self.assertEqual(len(state.pending), 5)


if __name__ == "__main__":
    unittest.main()


class PinnedCertificatesTest(HubCase):
    def connects(self, server: str, client: str, pinned_by_server: list[str]) -> bool:
        pem = lambda home: cert_of(home).read_text(encoding="ascii")  # noqa: E731
        ours, theirs = load_identity(server), load_identity(client)
        server_context = tls.server_context(ours.cert, ours.key, lambda: ours.passphrase, [pem(h) for h in pinned_by_server])
        client_context = tls.client_context(theirs.cert, theirs.key, lambda: theirs.passphrase, [pem(server)])
        listener = socket.socket()
        listener.bind((LOOPBACK, 0))
        listener.listen(1)
        self.addCleanup(listener.close)
        accepted: list[bool] = []

        def serve() -> None:
            conn, _address = listener.accept()
            try:
                with server_context.wrap_socket(conn, server_side=True):
                    accepted.append(True)
            except (ssl.SSLError, OSError):
                accepted.append(False)

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            with socket.create_connection(listener.getsockname(), timeout=5) as raw, client_context.wrap_socket(raw, server_hostname=None):
                pass
        except (ssl.SSLError, OSError):
            pass
        thread.join(10)
        return accepted == [True]

    def test_every_pinned_certificate_connects_even_though_all_certificates_share_one_subject(self) -> None:
        for order in (["a", "c"], ["c", "a"]):
            pinned = [self.homes[name] for name in order]
            self.assertTrue(self.connects(self.b, self.a, pinned), order)
            self.assertTrue(self.connects(self.b, self.c, pinned), order)

    def test_a_certificate_that_is_not_pinned_is_refused_even_with_the_same_subject(self) -> None:
        self.assertFalse(self.connects(self.b, self.d, [self.a, self.c]))
        self.assertFalse(self.connects(self.b, self.d, [self.c, self.a]))


class AddressUpdateTest(HubCase):
    def test_a_changed_member_address_replaces_the_one_the_listener_accepts_and_the_hub_file_follows(self) -> None:
        self.form_hub("a", "b")
        write_hub_state(self.b, HubState(HUB_ID, KEY, (member_ref_of(self.a),)))
        identity = load_identity(self.b)
        cache = TrustCache(self.b, identity.cert, identity.key, identity.passphrase, lambda _text: None)
        self.assertEqual(cache.current().addresses, frozenset({ADDRESSES["a"]}))
        self.assertTrue(update_address(self.b, device_of(self.a), "192.168.1.177"))
        self.assertEqual(cache.current().addresses, frozenset({"192.168.1.177"}))
        self.assertEqual(cache.current().peers[device_of(self.a)], "192.168.1.177")
        state = read_hub_state(self.b)
        assert state is not None
        self.assertEqual([m.address for m in state.members], ["192.168.1.177"])
        self.assertFalse(update_address(self.b, device_of(self.a), "192.168.1.177"))


def member_ref_of(home: str) -> MemberRef:
    return MemberRef(device_of(home), "alpha", ADDRESSES["a"], 47829, cert_of(home).read_text(encoding="ascii"))

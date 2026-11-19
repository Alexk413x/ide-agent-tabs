from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from collections.abc import Callable, Collection, Mapping
from typing import Any
from unittest import mock

from ide_agent_tabs.messaging.db import MailError
from ide_agent_tabs.messaging.sessions import update_presence
from ide_agent_tabs.messaging.store import mail_to
from ide_agent_tabs.net import dispatch, policy, wire
from ide_agent_tabs.net.audit import audit_path
from ide_agent_tabs.net.dispatch import NetDispatcher, folder_problem, session_generation
from ide_agent_tabs.net.origin import Caller, OriginChecker
from ide_agent_tabs.net.policy import FolderGrant, PeerGrant, Policy
from ide_agent_tabs.net.remote_launch import FIRST_TURN
from ide_agent_tabs.net.store import Launch, NetStore, Reservation
from support import temp_home

NOW = 1_800_000_000_000
MINUTE = 60_000
HOUR = 60 * MINUTE
PEER = "AAAAAAA-BBBBBBB-CCCCCCC-DDDDDDD-EEEEEEE-FFFFFFF-GGGGGGG-HHHHHHH"
OTHER = "BBBBBBB-CCCCCCC-DDDDDDD-EEEEEEE-FFFFFFF-GGGGGGG-HHHHHHH-AAAAAAA"
TASK = "TASK-TEXT-SENTINEL rotate the signing keys"
HELP = (
    "Options:\n  --permission-mode <mode>   Permission mode to use for the session\n"
    '                             (choices: "acceptEdits", "manual", "plan")\n  -p, --print   Print\n'
)


class FakeService:
    def __init__(self) -> None:
        self.opened: list[dict[str, Any]] = []
        self.closed: list[str | None] = []
        self.live: list[str] = []
        self.fail_open: Exception | None = None
        self.fail_close: Exception | None = None
        self.fail_list: Exception | None = None
        self.reply_id = True
        self.on_open: Callable[[], None] | None = None

    def open_tab(self, given: Mapping[str, Any]) -> dict[str, Any]:
        if self.on_open is not None:
            self.on_open()
        if self.fail_open is not None:
            raise self.fail_open
        self.opened.append(dict(given))
        tab = f"tab-{len(self.opened)}"
        self.live.append(tab)
        return {"id": tab, "ide": "fake"} if self.reply_id else {"pending": True}

    def close_tab(self, tab_id: str | None = None) -> dict[str, Any]:
        if self.fail_close is not None:
            raise self.fail_close
        self.closed.append(tab_id)
        if tab_id in self.live:
            self.live.remove(tab_id)
        return {"id": tab_id, "closed": True}

    def list_tabs(self, ide: str | None = None) -> dict[str, Any]:
        if self.fail_list is not None:
            raise self.fail_list
        return {"tabs": [{"id": t} for t in self.live]}


class Clock:
    def __init__(self) -> None:
        self.at = NOW

    def __call__(self) -> int:
        return self.at


class HooksStore(NetStore):
    after_reserve: Any = None

    def reserve_open(self, *args: Any, **kwargs: Any) -> Reservation | None:
        reserved = super().reserve_open(*args, **kwargs)
        if self.after_reserve is not None:
            self.after_reserve()
        return reserved


def write_policy(home: str, peers: Collection[PeerGrant], generation: int = 0) -> None:
    policy.save_policy(home, Policy(tuple(peers), generation))


def write_config(home: str, **network: Any) -> None:
    with open(os.path.join(home, "config.json"), "w", encoding="utf-8") as f:
        json.dump({"network": network}, f)


def folder_of(path: str, alias: str = "proj") -> FolderGrant:
    status = os.stat(path)
    return FolderGrant(alias, path, status.st_dev, status.st_ino)


def write_presence(home: str, session_id: str, agent: str = "claude", state: str = "idle", name: str | None = None) -> None:
    value: dict[str, Any] = {
        "id": session_id,
        "agent": agent,
        "path": os.path.join(home, "somewhere", "private-folder"),
        "pid": os.getpid(),
        "startedAt": "2027-01-15T07:00:00.000Z",
        "state": state,
    }
    if name is not None:
        value["nativeName"] = name
    update_presence(home, session_id, lambda _: value)


class DispatchCase(unittest.TestCase):
    def setUp(self) -> None:
        self.home = temp_home(self)
        self.folder = os.path.join(self.home, "work", "proj")
        os.makedirs(self.folder)
        self.service = FakeService()
        self.clock = Clock()
        self.logs: list[str] = []
        self.help = HELP
        self.grant = self.peer_grant()
        write_config(self.home, network="lan", bind="192.168.1.10")
        write_policy(self.home, [self.grant])
        self.dispatcher = self.make_dispatcher()

    def peer_grant(self, **changes: Any) -> PeerGrant:
        fields: dict[str, Any] = {
            "device_id": PEER,
            "label": "laptop",
            "address": "192.168.1.20",
            "folders": (folder_of(self.folder),),
            "agents": ("claude", "codex"),
            "models": ("sonnet",),
            "max_tabs": 3,
            "opens_per_hour": 10,
            "messages_per_minute": 10,
        }
        fields.update(changes)
        return PeerGrant(**fields)

    def make_dispatcher(self, store: NetStore | None = None) -> NetDispatcher:
        return NetDispatcher(
            self.home,
            lambda: self.service,
            self.clock,
            self.logs.append,
            store=store,
            claude_help=lambda: self.help,
            alive=lambda _pid: True,
        )

    def use_store(self, store_type: type[NetStore] = NetStore) -> NetStore:
        store = store_type(self.home)
        self.addCleanup(store.close)
        self.dispatcher = self.make_dispatcher(store)
        return store

    def open_request(self, request_id: str = "r1", **body: Any) -> dict[str, Any]:
        fields: dict[str, Any] = {"agent": "claude", "folder": "proj", "task": TASK}
        fields.update(body)
        return {"id": request_id, "op": "open", "body": fields}

    def handle(self, request: object, peer: str = PEER) -> dict[str, Any]:
        return self.dispatcher.handle(peer, request)

    def audit_lines(self) -> list[dict[str, Any]]:
        with open(audit_path(self.home), encoding="utf-8") as f:
            return [json.loads(line) for line in f.read().splitlines()]

    def last_audit(self) -> dict[str, Any]:
        return self.audit_lines()[-1]

    def assertRefused(self, reply: dict[str, Any], request_id: str, reason: str | None = None) -> None:
        self.assertEqual(reply, {"re": request_id, "ok": False, "refused": True})
        if reason is not None:
            self.assertEqual(self.last_audit()["reason"], reason)

    def assertOk(self, reply: dict[str, Any], request_id: str, body: dict[str, Any]) -> None:
        self.assertEqual(reply, {"re": request_id, "ok": True, "body": body})

    def open_ok(self, request_id: str = "r1", **body: Any) -> str:
        reply = self.handle(self.open_request(request_id, **body))
        self.assertTrue(reply["ok"], self.last_audit())
        return str(reply["body"]["tab"])


class GateTest(DispatchCase):
    def test_network_off_refuses_everything(self) -> None:
        write_config(self.home, network="off")
        self.assertRefused(self.handle(self.open_request()), "r1", "network-off")
        self.assertEqual(self.last_audit()["decision"], "refused")
        self.assertEqual(self.service.opened, [])

    def test_missing_network_section_is_off(self) -> None:
        os.remove(os.path.join(self.home, "config.json"))
        self.assertRefused(self.handle({"id": "r1", "op": "inventory", "body": {}}), "r1", "network-off")

    def test_unknown_peer_is_refused(self) -> None:
        self.assertRefused(self.handle(self.open_request(), peer=OTHER), "r1", "unknown-peer")
        self.assertEqual(self.service.opened, [])

    def test_a_peer_with_no_grants_cannot_open(self) -> None:
        write_policy(self.home, [self.peer_grant(folders=(), agents=())])
        self.assertRefused(self.handle(self.open_request()), "r1", "agent-not-granted")

    def test_an_invalid_policy_file_refuses(self) -> None:
        with open(policy.policy_path(self.home), "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertRefused(self.handle(self.open_request()), "r1", "policy-invalid")

    def test_a_malformed_request_is_refused_with_its_id_when_it_has_one(self) -> None:
        self.assertRefused(self.handle({"id": "r9", "op": "open", "body": {"agent": "claude"}}), "r9", "bad-request")
        self.assertRefused(self.handle({"id": "r9", "op": "launch", "body": {}}), "r9", "bad-request")
        self.assertRefused(self.handle({"id": "bad id!", "op": "inventory", "body": {}}), "invalid", "bad-request")
        self.assertRefused(self.handle("not an object"), "invalid", "bad-request")
        self.assertIsNone(self.last_audit()["id"])

    def test_an_internal_failure_is_the_generic_refusal(self) -> None:
        self.service.fail_list = RuntimeError("secret path C:/Users/someone")
        reply = self.handle(self.open_request())
        self.assertRefused(reply, "r1", "internal")
        self.assertNotIn("secret", json.dumps(reply) + json.dumps(self.last_audit()))
        self.assertEqual(self.service.opened, [])


class OpenTest(DispatchCase):
    def test_an_open_launches_the_receiver_built_plan_and_queues_the_task(self) -> None:
        reply = self.handle(self.open_request())
        self.assertOk(reply, "r1", {"tab": "tab-1"})
        self.assertEqual(
            self.service.opened,
            [
                {
                    "path": self.folder,
                    "agent": "claude",
                    "args": ["--permission-mode", "manual"],
                    "prompt": FIRST_TURN.format(label="laptop"),
                }
            ],
        )
        mail = mail_to(self.home, "tab-1")
        self.assertEqual(len(mail), 1)
        self.assertEqual(mail[0]["text"], TASK)
        self.assertEqual(mail[0]["from"], {"id": "laptop-remote", "agent": "remote", "path": ""})
        audit = self.last_audit()
        self.assertEqual((audit["decision"], audit["outcome"], audit["reason"], audit["tab"]), ("admitted", "completed", "ok", "tab-1"))
        self.assertEqual((audit["peer"], audit["id"], audit["op"], audit["generation"]), (PEER, "r1", "open", 0))

    def test_codex_plan_and_a_granted_model(self) -> None:
        reply = self.handle(self.open_request(agent="codex", model="sonnet"))
        self.assertTrue(reply["ok"])
        opened = self.service.opened[0]
        self.assertEqual(opened["agent"], "codex")
        self.assertEqual(opened["model"], "sonnet")
        self.assertEqual(opened["args"][-2:], ["-C", self.folder])
        self.assertNotIn("env", opened)

    def test_the_task_text_never_reaches_the_audit_or_the_log(self) -> None:
        self.handle(self.open_request())
        self.handle(self.open_request("r2", task=TASK + " again", agent="codex"))
        with open(audit_path(self.home), encoding="utf-8") as f:
            self.assertNotIn("SENTINEL", f.read())
        self.assertFalse(any("SENTINEL" in line for line in self.logs))

    def test_the_tab_is_recorded_as_remote_origin(self) -> None:
        tab = self.open_ok()
        self.assertEqual(self.dispatcher.is_remote_origin(tab), PEER)
        self.assertIsNone(self.dispatcher.is_remote_origin("someone-else"))

    def watch_launches(self) -> list[tuple[list[Launch], bool]]:
        seen: list[tuple[list[Launch], bool]] = []
        observer = NetStore(self.home)
        self.addCleanup(observer.close)
        checker = OriginChecker(self.home, self.clock)
        self.addCleanup(checker.close)
        session = Caller("s-0123456789ab", os.path.join(self.folder, "sub"))

        def look() -> None:
            seen.append((observer.inflight_launches(NOW), checker.is_remote(session)))

        self.service.on_open = look
        return seen

    def test_the_launch_is_recorded_while_the_tab_opens_and_cleared_after_the_tab_is_recorded(self) -> None:
        seen = self.watch_launches()
        tab = self.open_ok()
        self.assertEqual(seen, [([Launch(PEER, "r1", os.path.realpath(self.folder), NOW)], True)])
        store = NetStore(self.home)
        self.addCleanup(store.close)
        self.assertEqual(store.inflight_launches(NOW), [])
        self.assertEqual(store.remote_tab_peer(tab), PEER)
        checker = OriginChecker(self.home, self.clock)
        self.addCleanup(checker.close)
        self.assertTrue(checker.is_remote(Caller(tab, self.folder)))
        self.assertFalse(checker.is_remote(Caller("s-0123456789ab", self.folder)))

    def test_the_launch_is_cleared_when_the_tab_fails_to_open(self) -> None:
        seen = self.watch_launches()
        self.service.fail_open = RuntimeError("terminal exploded")
        self.assertRefused(self.handle(self.open_request()), "r1", "launch-failed")
        self.assertEqual([[x.request_id for x in launches] for launches, _ in seen], [["r1"]])
        store = NetStore(self.home)
        self.addCleanup(store.close)
        self.assertEqual(store.inflight_launches(NOW), [])

    def test_the_launch_is_cleared_when_the_launcher_returns_no_tab_id(self) -> None:
        self.service.reply_id = False
        self.assertRefused(self.handle(self.open_request()), "r1", "launch-failed")
        store = NetStore(self.home)
        self.addCleanup(store.close)
        self.assertEqual(store.inflight_launches(NOW), [])

    def test_remote_origin_without_a_database_creates_nothing(self) -> None:
        self.assertIsNone(self.dispatcher.is_remote_origin("tab-1"))
        self.assertFalse(os.path.exists(os.path.join(self.home, "net", "net.db")))

    def test_an_agent_the_peer_may_not_use_is_refused(self) -> None:
        write_policy(self.home, [self.peer_grant(agents=("codex",))])
        self.assertRefused(self.handle(self.open_request(agent="claude")), "r1", "agent-not-granted")
        self.assertEqual(self.service.opened, [])

    def test_an_unknown_folder_alias_is_refused(self) -> None:
        self.assertRefused(self.handle(self.open_request(folder="elsewhere")), "r1", "folder-not-granted")

    def test_a_model_that_is_not_granted_is_refused(self) -> None:
        self.assertRefused(self.handle(self.open_request(model="opus")), "r1", "model-not-granted")
        write_policy(self.home, [self.peer_grant(models=())])
        self.assertRefused(self.handle(self.open_request("r2", model="sonnet")), "r2", "model-not-granted")
        self.assertEqual(self.service.opened, [])

    def test_a_changed_folder_identity_is_refused(self) -> None:
        real = folder_of(self.folder)
        write_policy(self.home, [self.peer_grant(folders=(FolderGrant("proj", real.path, real.dev, real.ino + 1),))])
        self.assertRefused(self.handle(self.open_request()), "r1", "folder-identity")
        write_policy(self.home, [self.peer_grant(folders=(FolderGrant("proj", real.path, real.dev + 1, real.ino),))])
        self.assertRefused(self.handle(self.open_request("r2")), "r2", "folder-identity")
        self.assertEqual(self.service.opened, [])

    def test_a_missing_folder_is_refused(self) -> None:
        os.rmdir(self.folder)
        self.assertRefused(self.handle(self.open_request()), "r1", "folder-missing")

    def test_a_folder_swapped_for_a_file_is_refused(self) -> None:
        file = os.path.join(self.home, "work", "afile")
        with open(file, "w", encoding="utf-8") as f:
            f.write("x")
        status = os.stat(file)
        write_policy(self.home, [self.peer_grant(folders=(FolderGrant("proj", file, status.st_dev, status.st_ino),))])
        self.assertRefused(self.handle(self.open_request()), "r1", "folder-identity")

    def test_a_junction_or_symlink_in_a_parent_component_is_refused(self) -> None:
        real_parent = os.path.join(self.home, "real")
        os.makedirs(os.path.join(real_parent, "inner"))
        link = os.path.join(self.home, "link")
        if sys.platform == "win32":
            done = subprocess.run(["cmd", "/c", "mklink", "/J", link, real_parent], capture_output=True, check=False)
            if done.returncode != 0:
                self.skipTest("cannot create a junction")
        else:
            try:
                os.symlink(real_parent, link)
            except OSError:
                self.skipTest("cannot create a symlink")
        through = os.path.join(link, "inner")
        self.assertTrue(os.path.isdir(through))
        write_policy(self.home, [self.peer_grant(folders=(folder_of(through),))])
        self.assertRefused(self.handle(self.open_request()), "r1", "folder-link")
        self.assertEqual(self.service.opened, [])
        self.assertEqual(folder_problem(folder_of(through)), "link")
        self.assertIsNone(folder_problem(folder_of(os.path.join(real_parent, "inner"))))

    def test_a_unc_folder_is_refused(self) -> None:
        self.assertEqual(folder_problem(FolderGrant("proj", "\\\\server\\share\\proj", 1, 1)), "not-local")
        self.assertEqual(folder_problem(FolderGrant("proj", "//server/share/proj", 1, 1)), "not-local")

    def test_claude_is_refused_when_the_help_lists_no_safe_mode(self) -> None:
        self.help = 'Options:\n  --permission-mode <mode>   Mode (choices: "plan", "auto")\n'
        self.assertRefused(self.handle(self.open_request()), "r1", "launch-plan")
        self.assertEqual(self.service.opened, [])
        self.assertTrue(self.handle(self.open_request("r2", agent="codex"))["ok"])

    def test_a_failed_launch_is_refused_and_recorded(self) -> None:
        self.service.fail_open = RuntimeError("terminal exploded")
        self.assertRefused(self.handle(self.open_request()), "r1", "launch-failed")
        self.assertEqual((self.last_audit()["decision"], self.last_audit()["outcome"]), ("admitted", "failed"))
        self.service.fail_open = None
        self.assertRefused(self.handle(self.open_request()), "r1", "replay-refused")
        self.assertEqual(self.service.opened, [])
        self.assertTrue(self.handle(self.open_request("r2"))["ok"])

    def test_a_launch_that_returns_no_tab_id_is_refused(self) -> None:
        self.service.reply_id = False
        self.assertRefused(self.handle(self.open_request()), "r1", "launch-failed")

    def test_a_task_the_mailbox_refuses_is_audited_and_refused(self) -> None:
        original = dispatch.send_message

        def refuse(*_args: Any, **_kwargs: Any) -> Any:
            raise MailError("full")

        dispatch.send_message = refuse
        self.addCleanup(setattr, dispatch, "send_message", original)
        self.assertRefused(self.handle(self.open_request()), "r1", "task-not-delivered")
        self.assertEqual((self.last_audit()["outcome"], self.last_audit()["tab"]), ("failed", "tab-1"))
        self.assertEqual(self.handle(self.open_request())["body"], {"tab": "tab-1"})

    def test_a_queued_task_waits_for_a_session_that_has_not_registered(self) -> None:
        self.open_ok()
        self.assertEqual([m["text"] for m in mail_to(self.home, "tab-1")], [TASK])


class LimitTest(DispatchCase):
    def test_a_peer_may_hold_only_max_tabs(self) -> None:
        write_policy(self.home, [self.peer_grant(max_tabs=2)])
        self.open_ok("r1")
        self.open_ok("r2")
        self.assertRefused(self.handle(self.open_request("r3")), "r3", "limit")
        self.assertEqual(len(self.service.opened), 2)

    def test_a_closed_tab_frees_a_slot(self) -> None:
        write_policy(self.home, [self.peer_grant(max_tabs=1)])
        tab = self.open_ok("r1")
        self.assertRefused(self.handle(self.open_request("r2")), "r2", "limit")
        self.assertTrue(self.handle({"id": "c1", "op": "close", "body": {"tab": tab}})["ok"])
        self.assertTrue(self.handle(self.open_request("r3"))["ok"])

    def test_the_global_cap_counts_every_peer(self) -> None:
        write_config(self.home, network="lan", bind="192.168.1.10", maxRemoteTabs=1)
        write_policy(self.home, [self.grant, self.peer_grant(device_id=OTHER, label="desk")])
        self.open_ok("r1")
        self.assertRefused(self.handle(self.open_request("r2"), peer=OTHER), "r2", "limit")

    def test_opens_per_hour(self) -> None:
        write_policy(self.home, [self.peer_grant(max_tabs=10, opens_per_hour=2)])
        self.open_ok("r1")
        self.open_ok("r2")
        self.assertRefused(self.handle(self.open_request("r3")), "r3", "limit")
        self.clock.at += HOUR + 1
        self.assertTrue(self.handle(self.open_request("r4"))["ok"])

    def test_a_refused_open_does_not_use_up_the_hourly_budget(self) -> None:
        write_policy(self.home, [self.peer_grant(max_tabs=10, opens_per_hour=1)])
        self.assertRefused(self.handle(self.open_request("r1", agent="agy")), "r1")
        self.assertTrue(self.handle(self.open_request("r2"))["ok"])


class ReplayTest(DispatchCase):
    def test_a_repeated_open_returns_the_stored_tab_without_a_second_launch(self) -> None:
        first = self.handle(self.open_request())
        again = self.handle(self.open_request())
        self.assertEqual(first, again)
        self.assertEqual(len(self.service.opened), 1)
        self.assertEqual(len(mail_to(self.home, "tab-1")), 1)
        self.assertEqual((self.last_audit()["decision"], self.last_audit()["outcome"]), ("admitted", "replayed"))

    def test_the_same_id_with_other_content_is_refused(self) -> None:
        self.open_ok()
        self.assertRefused(self.handle(self.open_request(task="something else")), "r1", "id-conflict")
        self.assertEqual(len(self.service.opened), 1)

    def test_a_replay_survives_a_new_dispatcher(self) -> None:
        self.open_ok()
        self.dispatcher = self.make_dispatcher()
        self.assertOk(self.handle(self.open_request()), "r1", {"tab": "tab-1"})
        self.assertEqual(len(self.service.opened), 1)

    def test_a_replay_of_a_request_still_in_flight_is_refused(self) -> None:
        store = self.use_store()
        store.reserve(PEER, "r1", "digest", "open", 0, NOW)
        self.assertRefused(self.handle(self.open_request()), "r1")

    def test_a_replay_still_needs_a_grant(self) -> None:
        self.open_ok()
        write_policy(self.home, [])
        self.assertRefused(self.handle(self.open_request()), "r1", "unknown-peer")

    def test_a_dispatched_record_left_by_a_crash_is_never_launched_again(self) -> None:
        seeded = NetStore(self.home)
        seeded.reserve(PEER, "r1", "x", "open", 0, NOW)
        self.assertTrue(seeded.dispatch_handoff(PEER, "r1", 0, lambda: True))
        seeded.close()
        self.assertTrue(self.handle(self.open_request("r2"))["ok"])
        check = NetStore(self.home)
        self.addCleanup(check.close)
        found = check.get_request(PEER, "r1")
        assert found is not None
        self.assertEqual(found.state, "uncertain")
        self.assertEqual(len(self.service.opened), 1)


class HandoffTest(DispatchCase):
    def test_a_generation_bump_between_reserve_and_handoff_launches_nothing(self) -> None:
        store = self.use_store(HooksStore)
        assert isinstance(store, HooksStore)
        store.after_reserve = store.bump_generation
        self.assertRefused(self.handle(self.open_request()), "r1", "superseded")
        self.assertEqual(self.service.opened, [])
        self.assertEqual(self.last_audit()["decision"], "admitted")
        found = store.get_request(PEER, "r1")
        assert found is not None
        self.assertEqual(found.state, "refused")

    def test_a_revoked_grant_between_reserve_and_handoff_launches_nothing(self) -> None:
        store = self.use_store(HooksStore)
        assert isinstance(store, HooksStore)
        store.after_reserve = lambda: write_policy(self.home, [])
        self.assertRefused(self.handle(self.open_request()), "r1", "superseded")
        self.assertEqual(self.service.opened, [])

    def test_a_policy_generation_bump_between_reserve_and_handoff_launches_nothing(self) -> None:
        store = self.use_store(HooksStore)
        assert isinstance(store, HooksStore)
        store.after_reserve = lambda: write_policy(self.home, [self.grant], generation=1)
        self.assertRefused(self.handle(self.open_request()), "r1", "superseded")
        self.assertEqual(self.service.opened, [])

    def test_a_folder_swapped_between_reserve_and_handoff_launches_nothing(self) -> None:
        store = self.use_store(HooksStore)
        assert isinstance(store, HooksStore)
        store.after_reserve = lambda: os.rmdir(self.folder)
        self.assertRefused(self.handle(self.open_request()), "r1", "superseded")
        self.assertEqual(self.service.opened, [])

    def test_a_dispatched_reservation_survives_a_later_revoke_and_completes(self) -> None:
        store = self.use_store()
        tab = self.open_ok()
        store.bump_generation()
        found = store.get_request(PEER, "r1")
        assert found is not None
        self.assertEqual((found.state, found.tab_id), ("completed", tab))


class CloseTest(DispatchCase):
    def close_request(self, tab: str, request_id: str = "c1") -> dict[str, Any]:
        return {"id": request_id, "op": "close", "body": {"tab": tab}}

    def test_a_peer_closes_a_tab_it_opened(self) -> None:
        tab = self.open_ok()
        reply = self.handle(self.close_request(tab))
        self.assertOk(reply, "c1", {"closed": True})
        self.assertEqual(self.service.closed, [tab])
        audit = self.last_audit()
        self.assertEqual((audit["op"], audit["decision"], audit["outcome"], audit["tab"]), ("close", "admitted", "completed", tab))

    def test_a_repeated_close_closes_once(self) -> None:
        tab = self.open_ok()
        self.handle(self.close_request(tab))
        self.assertOk(self.handle(self.close_request(tab)), "c1", {"closed": True})
        self.assertEqual(self.service.closed, [tab])

    def test_a_tab_another_peer_opened_is_refused(self) -> None:
        write_policy(self.home, [self.grant, self.peer_grant(device_id=OTHER, label="desk")])
        tab = self.open_ok()
        self.assertRefused(self.handle(self.close_request(tab), peer=OTHER), "c1", "not-opened-by-peer")
        self.assertEqual(self.service.closed, [])

    def test_a_tab_nobody_remote_opened_is_refused(self) -> None:
        self.assertRefused(self.handle(self.close_request("local-tab")), "c1", "not-opened-by-peer")
        self.assertEqual(self.service.closed, [])

    def test_a_failed_close_is_refused(self) -> None:
        tab = self.open_ok()
        self.service.fail_close = RuntimeError("no such tab")
        self.assertRefused(self.handle(self.close_request(tab)), "c1", "close-failed")

    def test_a_revoked_peer_cannot_close(self) -> None:
        tab = self.open_ok()
        write_policy(self.home, [])
        self.assertRefused(self.handle(self.close_request(tab)), "c1", "unknown-peer")
        self.assertEqual(self.service.closed, [])


class SendTest(DispatchCase):
    def send_request(self, to: str, text: str = "hello", request_id: str = "s1", **extra: Any) -> dict[str, Any]:
        return {"id": request_id, "op": "send", "body": {"to": to, "text": text, **extra}}

    def test_a_session_outside_the_send_list_is_refused(self) -> None:
        write_presence(self.home, "local-1")
        self.assertRefused(self.handle(self.send_request("local-1")), "s1", "target-not-granted")
        self.assertEqual(mail_to(self.home, "local-1"), [])
        self.assertEqual(self.last_audit()["decision"], "refused")

    def test_a_granted_live_session_gets_the_message(self) -> None:
        write_presence(self.home, "local-1")
        write_policy(self.home, [self.peer_grant(send=("local-1",))])
        reply = self.handle(self.send_request("local-1", "ping"))
        self.assertTrue(reply["ok"])
        mail = mail_to(self.home, "local-1")
        self.assertEqual([(m["text"], m["from"]) for m in mail], [("ping", {"id": "laptop-remote", "agent": "remote", "path": ""})])
        self.assertEqual(reply["body"], {"id": mail[0]["id"]})
        self.assertEqual(self.last_audit()["outcome"], "completed")

    def test_a_granted_session_that_is_not_live_is_refused(self) -> None:
        write_policy(self.home, [self.peer_grant(send=("local-1",))])
        self.assertRefused(self.handle(self.send_request("local-1")), "s1", "no-session")

    def test_a_tab_the_peer_opened_may_be_messaged_before_it_registers(self) -> None:
        tab = self.open_ok()
        self.assertTrue(self.handle(self.send_request(tab, "more detail"))["ok"])
        self.assertEqual([m["text"] for m in mail_to(self.home, tab)], [TASK, "more detail"])

    def test_a_tab_another_peer_opened_is_not_a_target(self) -> None:
        write_policy(self.home, [self.grant, self.peer_grant(device_id=OTHER, label="desk")])
        tab = self.open_ok()
        self.assertRefused(self.handle(self.send_request(tab), peer=OTHER), "s1", "target-not-granted")

    def test_a_reply_grant_allows_one_conversation(self) -> None:
        write_presence(self.home, "local-1")
        store = self.use_store()
        generation = session_generation({"startedAt": "2027-01-15T07:00:00.000Z"})
        assert generation is not None
        store.add_reply_grant("local-1", generation, PEER, "remote-sess", "m-0123456789abcdef", NOW)
        reply = self.handle(self.send_request("local-1", "answer", replyTo="m-0123456789abcdef"))
        self.assertTrue(reply["ok"])
        mail = mail_to(self.home, "local-1")
        self.assertEqual(mail[0]["from"]["id"], "remote-sess@laptop")
        self.assertEqual(mail[0]["replyTo"], "m-0123456789abcdef")

    def test_a_reply_without_a_grant_is_refused(self) -> None:
        write_presence(self.home, "local-1")
        store = self.use_store()
        store.add_reply_grant("local-1", "2027-01-15T07:00:00.000Z", PEER, "remote-sess", "m-0123456789abcdef", NOW)
        self.assertRefused(self.handle(self.send_request("local-1", replyTo="m-fedcba9876543210")), "s1", "target-not-granted")
        self.assertRefused(self.handle(self.send_request("local-1", "no reply to", "s2")), "s2", "target-not-granted")
        write_presence(self.home, "local-2")
        self.assertRefused(
            self.handle(self.send_request("local-2", "wrong", "s3", replyTo="m-0123456789abcdef")), "s3", "target-not-granted"
        )

    def test_a_reply_grant_is_tied_to_the_peer_the_session_generation_and_the_day(self) -> None:
        write_presence(self.home, "local-1")
        store = self.use_store()
        store.add_reply_grant("local-1", "2027-01-15T07:00:00.000Z", OTHER, "remote-sess", "m-0123456789abcdef", NOW)
        store.add_reply_grant("local-1", "an older generation", PEER, "remote-sess", "m-1111111111111111", NOW)
        store.add_reply_grant("local-1", "2027-01-15T07:00:00.000Z", PEER, "remote-sess", "m-2222222222222222", NOW)
        self.assertRefused(self.handle(self.send_request("local-1", replyTo="m-0123456789abcdef")), "s1", "target-not-granted")
        self.assertRefused(self.handle(self.send_request("local-1", "x", "s2", replyTo="m-1111111111111111")), "s2", "target-not-granted")
        self.clock.at += 24 * HOUR + 1
        self.assertRefused(self.handle(self.send_request("local-1", "y", "s3", replyTo="m-2222222222222222")), "s3", "target-not-granted")

    def test_a_reply_grant_never_allows_open_close_or_inventory_beyond_the_peers_own(self) -> None:
        write_presence(self.home, "local-1")
        store = self.use_store()
        store.add_reply_grant("local-1", "2027-01-15T07:00:00.000Z", PEER, "remote-sess", "m-0123456789abcdef", NOW)
        self.assertEqual(self.handle({"id": "i1", "op": "inventory", "body": {}})["body"], {"sessions": []})
        self.assertRefused(self.handle({"id": "c1", "op": "close", "body": {"tab": "local-1"}}), "c1", "not-opened-by-peer")

    def test_messages_per_minute(self) -> None:
        write_presence(self.home, "local-1")
        write_policy(self.home, [self.peer_grant(send=("local-1",), messages_per_minute=2)])
        self.assertTrue(self.handle(self.send_request("local-1", "a", "s1"))["ok"])
        self.assertTrue(self.handle(self.send_request("local-1", "b", "s2"))["ok"])
        self.assertRefused(self.handle(self.send_request("local-1", "c", "s3")), "s3", "rate-limit")
        self.clock.at += MINUTE
        self.assertTrue(self.handle(self.send_request("local-1", "d", "s4"))["ok"])
        self.assertEqual([m["text"] for m in mail_to(self.home, "local-1")], ["a", "b", "d"])

    def test_a_repeated_send_returns_the_first_reply_without_another_message(self) -> None:
        write_presence(self.home, "local-1")
        write_policy(self.home, [self.peer_grant(send=("local-1",))])
        first = self.handle(self.send_request("local-1", "once"))
        again = self.handle(self.send_request("local-1", "once"))
        self.assertEqual(first, again)
        self.assertEqual(len(mail_to(self.home, "local-1")), 1)
        self.assertEqual(self.last_audit()["outcome"], "replayed")
        self.assertRefused(self.handle(self.send_request("local-1", "different")), "s1", "id-conflict")

    def test_the_message_text_never_reaches_the_audit(self) -> None:
        write_presence(self.home, "local-1")
        write_policy(self.home, [self.peer_grant(send=("local-1",))])
        self.handle(self.send_request("local-1", "SEND-TEXT-SENTINEL"))
        with open(audit_path(self.home), encoding="utf-8") as f:
            self.assertNotIn("SENTINEL", f.read())


class InventoryTest(DispatchCase):
    def inventory(self) -> list[dict[str, str]]:
        reply = self.handle({"id": "i1", "op": "inventory", "body": {}})
        self.assertTrue(reply["ok"], self.last_audit())
        return list(reply["body"]["sessions"])

    def test_only_granted_and_opened_sessions_are_listed(self) -> None:
        write_presence(self.home, "granted", name="Planner")
        write_presence(self.home, "private-one")
        write_presence(self.home, "someone-elses")
        write_policy(self.home, [self.peer_grant(send=("granted",)), self.peer_grant(device_id=OTHER, label="desk")])
        tab = self.open_ok()
        write_presence(self.home, tab, agent="codex", state="busy")
        rows = {r["id"]: r for r in self.inventory()}
        self.assertEqual(set(rows), {"granted", tab})
        self.assertEqual(rows["granted"]["agent"], "claude")
        self.assertEqual(rows["granted"]["status"], "idle")
        self.assertEqual(rows[tab]["status"], "busy")
        self.assertEqual(rows[tab]["agent"], "codex")

    def test_rows_match_the_wire_contract_and_hold_no_path(self) -> None:
        write_presence(self.home, "granted", name="Planner\nHeading")
        write_policy(self.home, [self.peer_grant(send=("granted",))])
        reply = self.handle({"id": "i1", "op": "inventory", "body": {}})
        sessions = wire.parse_inventory(reply["body"])
        self.assertEqual([s.id for s in sessions], ["granted"])
        self.assertEqual(set(reply["body"]["sessions"][0]), {"id", "name", "agent", "status"})
        self.assertNotIn("private-folder", json.dumps(reply))
        self.assertNotIn("\n", sessions[0].name)
        self.assertLessEqual(len(sessions[0].name), wire.MAX_NAME_CHARS)

    def test_a_session_whose_agent_the_wire_cannot_express_is_skipped(self) -> None:
        write_presence(self.home, "granted", agent="unknown-agent")
        write_policy(self.home, [self.peer_grant(send=("granted",))])
        self.assertEqual(self.inventory(), [])

    def test_a_dead_session_is_not_listed_and_its_file_stays(self) -> None:
        write_presence(self.home, "granted")
        write_policy(self.home, [self.peer_grant(send=("granted",))])
        self.dispatcher = NetDispatcher(
            self.home, lambda: self.service, self.clock, self.logs.append, claude_help=lambda: self.help, alive=lambda _p: False
        )
        self.assertEqual(self.inventory(), [])
        self.assertTrue(os.path.exists(os.path.join(self.home, "sessions", "granted.json")))

    def test_an_empty_inventory(self) -> None:
        self.assertEqual(self.handle({"id": "i1", "op": "inventory", "body": {}})["body"], {"sessions": []})
        self.assertEqual((self.last_audit()["op"], self.last_audit()["decision"]), ("inventory", "admitted"))


HUB = policy.Hub("0123456789abcdef0123456789abcdef", "home")


class MemberCase(DispatchCase):
    def setUp(self) -> None:
        super().setUp()
        self.base = os.path.join(self.home, "members")
        self.inside = os.path.join(self.base, "proj")
        os.makedirs(self.inside)
        self.set_member()

    def set_member(self, folders: Collection[str] | None = None, generation: int = 0, **changes: Any) -> None:
        write_config(
            self.home,
            network="lan",
            bind="192.168.1.10",
            memberFolders=list([self.base] if folders is None else folders),
        )
        member = PeerGrant(device_id=PEER, label="laptop", address="192.168.1.20", member=True, **changes)
        policy.save_policy(self.home, Policy((member,), generation, HUB))

    def member_open(self, path: str, request_id: str = "r1", **body: Any) -> dict[str, Any]:
        fields: dict[str, Any] = {"agent": "claude", "path": path, "task": TASK}
        fields.update(body)
        return {"id": request_id, "op": "open", "body": fields}

    def sessions(self) -> list[dict[str, Any]]:
        reply = self.handle({"id": "i1", "op": "inventory", "body": {}})
        self.assertTrue(reply["ok"], self.last_audit())
        return list(reply["body"]["sessions"])


class MemberInventoryTest(MemberCase):
    def test_every_live_session_is_listed_with_no_send_grant(self) -> None:
        write_presence(self.home, "one", name="Planner")
        write_presence(self.home, "two", agent="codex", state="busy")
        reply = self.handle({"id": "i1", "op": "inventory", "body": {}})
        self.assertEqual({s["id"] for s in reply["body"]["sessions"]}, {"one", "two"})
        self.assertEqual(wire.parse_inventory(reply["body"])[0].id, "one")

    def test_the_reply_carries_the_member_folders(self) -> None:
        other = os.path.join(self.home, "elsewhere")
        os.makedirs(other)
        self.set_member([self.base, other])
        reply = self.handle({"id": "i1", "op": "inventory", "body": {}})
        self.assertEqual(reply["body"]["folders"], [self.base, other])
        self.assertEqual(wire.parse_inventory_folders(reply["body"]), [self.base, other])

    def test_an_empty_member_folder_list_is_an_empty_array(self) -> None:
        self.set_member([])
        self.assertEqual(self.handle({"id": "i1", "op": "inventory", "body": {}})["body"], {"sessions": [], "folders": []})

    def test_a_folder_the_wire_cannot_carry_is_left_out(self) -> None:
        self.set_member([self.base, os.path.join(self.base, "a" * (wire.MAX_PATH_CHARS + 1))])
        self.assertEqual(self.handle({"id": "i1", "op": "inventory", "body": {}})["body"]["folders"], [self.base])

    def test_a_non_member_reply_has_no_folders_and_no_ungranted_sessions(self) -> None:
        write_presence(self.home, "one")
        write_policy(self.home, [self.peer_grant()])
        write_config(self.home, network="lan", bind="192.168.1.10", memberFolders=[self.base])
        self.assertEqual(self.handle({"id": "i1", "op": "inventory", "body": {}})["body"], {"sessions": []})


class MemberSendTest(MemberCase):
    def send_request(self, to: str, text: str = "hello", request_id: str = "s1", **extra: Any) -> dict[str, Any]:
        return {"id": request_id, "op": "send", "body": {"to": to, "text": text, **extra}}

    def test_a_member_messages_any_live_session(self) -> None:
        write_presence(self.home, "local-1")
        reply = self.handle(self.send_request("local-1", "ping"))
        self.assertTrue(reply["ok"], self.last_audit())
        mail = mail_to(self.home, "local-1")
        self.assertEqual([(m["text"], m["from"]) for m in mail], [("ping", {"id": "laptop-remote", "agent": "remote", "path": ""})])

    def test_a_session_that_is_not_live_is_refused(self) -> None:
        self.assertRefused(self.handle(self.send_request("local-1")), "s1", "no-session")

    def test_the_message_rate_limit_still_applies(self) -> None:
        write_presence(self.home, "local-1")
        self.set_member(messages_per_minute=2)
        self.assertTrue(self.handle(self.send_request("local-1", "a", "s1"))["ok"])
        self.assertTrue(self.handle(self.send_request("local-1", "b", "s2"))["ok"])
        self.assertRefused(self.handle(self.send_request("local-1", "c", "s3")), "s3", "rate-limit")

    def test_a_reply_keeps_the_remote_session_in_the_sender(self) -> None:
        write_presence(self.home, "local-1")
        store = self.use_store()
        generation = session_generation({"startedAt": "2027-01-15T07:00:00.000Z"})
        assert generation is not None
        store.add_reply_grant("local-1", generation, PEER, "remote-sess", "m-0123456789abcdef", NOW)
        self.assertTrue(self.handle(self.send_request("local-1", "answer", replyTo="m-0123456789abcdef"))["ok"])
        self.assertEqual(mail_to(self.home, "local-1")[0]["from"]["id"], "remote-sess@laptop")

    def test_a_non_member_still_needs_a_send_grant(self) -> None:
        write_presence(self.home, "local-1")
        write_policy(self.home, [self.peer_grant()])
        self.assertRefused(self.handle(self.send_request("local-1")), "s1", "target-not-granted")


class MemberOpenTest(MemberCase):
    def test_an_open_inside_a_member_folder_launches_with_the_receivers_plan(self) -> None:
        self.assertEqual(self.handle(self.member_open(self.inside)), {"re": "r1", "ok": True, "body": {"tab": "tab-1"}})
        self.assertEqual(
            self.service.opened,
            [
                {
                    "path": self.inside,
                    "agent": "claude",
                    "args": ["--permission-mode", "manual"],
                    "prompt": FIRST_TURN.format(label="laptop"),
                }
            ],
        )
        self.assertEqual([m["text"] for m in mail_to(self.home, "tab-1")], [TASK])
        self.assertEqual(self.dispatcher.is_remote_origin("tab-1"), PEER)

    def test_the_member_folder_itself_and_a_deeper_folder_are_inside(self) -> None:
        deeper = os.path.join(self.inside, "a", "b")
        os.makedirs(deeper)
        self.set_member(max_tabs=3)
        self.assertTrue(self.handle(self.member_open(self.base, "r1"))["ok"])
        self.assertTrue(self.handle(self.member_open(deeper, "r2"))["ok"])
        self.assertEqual([o["path"] for o in self.service.opened], [self.base, deeper])

    def test_codex_and_any_model_that_passes_the_model_check_are_accepted(self) -> None:
        self.set_member(max_tabs=3)
        reply = self.handle(self.member_open(self.inside, "r1", agent="codex", model="gpt-5.4:high/x@y+z"))
        self.assertTrue(reply["ok"], self.last_audit())
        opened = self.service.opened[0]
        self.assertEqual((opened["agent"], opened["model"]), ("codex", "gpt-5.4:high/x@y+z"))
        self.assertEqual(opened["args"][-2:], ["-C", self.inside])
        self.assertTrue(self.handle(self.member_open(self.inside, "r2", model="opus"))["ok"])
        self.assertEqual(self.service.opened[1]["model"], "opus")

    def test_a_model_the_policy_check_refuses_is_refused(self) -> None:
        self.assertRefused(self.handle(self.member_open(self.inside, model="m" * 129)), "r1", "model-invalid")
        self.assertEqual(self.service.opened, [])

    def test_agents_outside_claude_and_codex_are_refused_by_the_wire(self) -> None:
        self.assertRefused(self.handle(self.member_open(self.inside, agent="agy")), "r1", "bad-request")

    def test_a_member_cannot_name_a_permission_mode(self) -> None:
        extras: list[dict[str, Any]] = [
            {"args": ["--permission-mode", "bypassPermissions"]},
            {"permissionMode": "bypassPermissions"},
            {"permission_mode": "bypassPermissions"},
            {"env": {"X": "1"}},
        ]
        for extra in extras:
            with self.subTest(extra):
                self.assertRefused(self.handle(self.member_open(self.inside, "r1", **extra)), "r1", "bad-request")
        self.assertEqual(self.service.opened, [])
        self.assertTrue(self.handle(self.member_open(self.inside, "r2"))["ok"])
        self.assertEqual(self.service.opened[0]["args"], ["--permission-mode", "manual"])

    def test_a_path_outside_the_member_folders_is_refused(self) -> None:
        outside = os.path.join(self.home, "outside")
        os.makedirs(outside)
        self.assertRefused(self.handle(self.member_open(outside)), "r1", "folder-not-allowed")
        self.assertEqual(self.service.opened, [])
        self.assertEqual(self.last_audit()["decision"], "refused")

    def test_an_empty_member_folder_list_allows_nothing(self) -> None:
        self.set_member([])
        self.assertRefused(self.handle(self.member_open(self.inside)), "r1", "folder-not-allowed")

    def test_a_sibling_that_shares_a_prefix_is_outside(self) -> None:
        sibling = self.inside + "-other"
        os.makedirs(sibling)
        self.set_member([self.inside])
        self.assertRefused(self.handle(self.member_open(sibling)), "r1", "folder-not-allowed")

    def test_dot_dot_components_cannot_leave_the_list(self) -> None:
        outside = os.path.join(self.home, "outside")
        os.makedirs(outside)
        sneaky = os.path.join(self.inside, "..", "..", "outside")
        self.assertRefused(self.handle(self.member_open(sneaky)), "r1", "folder-not-allowed")

    def test_a_link_that_leads_out_of_the_list_is_outside(self) -> None:
        target = os.path.join(self.home, "outside")
        os.makedirs(target)
        link = os.path.join(self.inside, "link")
        if sys.platform == "win32":
            done = subprocess.run(["cmd", "/c", "mklink", "/J", link, target], capture_output=True, check=False)
            if done.returncode != 0:
                self.skipTest("cannot create a junction")
        else:
            try:
                os.symlink(target, link)
            except OSError:
                self.skipTest("cannot create a symlink")
        self.assertRefused(self.handle(self.member_open(link)), "r1", "folder-not-allowed")

    def test_a_link_between_the_list_and_the_folder_is_refused_by_the_folder_checks(self) -> None:
        real = os.path.join(self.inside, "real")
        os.makedirs(os.path.join(real, "inner"))
        link = os.path.join(self.inside, "link")
        if sys.platform == "win32":
            done = subprocess.run(["cmd", "/c", "mklink", "/J", link, real], capture_output=True, check=False)
            if done.returncode != 0:
                self.skipTest("cannot create a junction")
        else:
            try:
                os.symlink(real, link)
            except OSError:
                self.skipTest("cannot create a symlink")
        self.assertRefused(self.handle(self.member_open(os.path.join(link, "inner"))), "r1", "folder-link")
        self.assertEqual(self.service.opened, [])

    def test_a_missing_folder_inside_the_list_is_refused(self) -> None:
        self.assertRefused(self.handle(self.member_open(os.path.join(self.inside, "nope"))), "r1", "folder-missing")

    def test_a_file_inside_the_list_is_refused(self) -> None:
        file = os.path.join(self.inside, "afile")
        with open(file, "w", encoding="utf-8") as f:
            f.write("x")
        self.assertRefused(self.handle(self.member_open(file)), "r1")
        self.assertEqual(self.service.opened, [])

    @unittest.skipUnless(sys.platform == "win32", "Windows paths ignore case")
    def test_the_comparison_ignores_case_on_windows(self) -> None:
        self.assertTrue(self.handle(self.member_open(self.inside.upper()))["ok"], self.last_audit())

    def test_the_alias_form_needs_a_path_from_a_member(self) -> None:
        self.assertRefused(self.handle(self.open_request()), "r1", "path-required")

    def test_a_non_member_cannot_send_a_path(self) -> None:
        write_policy(self.home, [self.peer_grant()])
        self.assertRefused(self.handle(self.member_open(self.folder)), "r1", "path-not-granted")
        self.assertEqual(self.service.opened, [])

    def test_a_relative_unc_or_empty_path_is_refused_by_the_wire(self) -> None:
        for bad in ("proj", "..\\proj", "\\\\server\\share\\proj", "//server/share/proj", "", "C:proj", self.inside + "\n"):
            with self.subTest(bad):
                self.assertRefused(self.handle(self.member_open(bad)), "r1", "bad-request")

    def test_both_a_folder_and_a_path_are_refused_by_the_wire(self) -> None:
        self.assertRefused(self.handle(self.member_open(self.inside, folder="proj")), "r1", "bad-request")

    def test_a_member_may_hold_only_max_tabs_and_opens_per_hour(self) -> None:
        self.set_member(max_tabs=1, opens_per_hour=5)
        self.open_member("r1")
        self.assertRefused(self.handle(self.member_open(self.inside, "r2")), "r2", "limit")

    def open_member(self, request_id: str) -> str:
        reply = self.handle(self.member_open(self.inside, request_id))
        self.assertTrue(reply["ok"], self.last_audit())
        return str(reply["body"]["tab"])

    def test_a_member_closes_only_a_tab_it_opened(self) -> None:
        tab = self.open_member("r1")
        self.assertTrue(self.handle({"id": "c1", "op": "close", "body": {"tab": tab}})["ok"])
        self.assertEqual(self.service.closed, [tab])
        self.assertRefused(self.handle({"id": "c2", "op": "close", "body": {"tab": "local-tab"}}), "c2", "not-opened-by-peer")

    def test_a_folder_removed_from_the_list_between_reserve_and_handoff_launches_nothing(self) -> None:
        store = self.use_store(HooksStore)
        assert isinstance(store, HooksStore)
        store.after_reserve = lambda: self.set_member([])
        self.assertRefused(self.handle(self.member_open(self.inside)), "r1", "superseded")
        self.assertEqual(self.service.opened, [])

    def test_a_member_flag_dropped_between_reserve_and_handoff_launches_nothing(self) -> None:
        store = self.use_store(HooksStore)
        assert isinstance(store, HooksStore)
        store.after_reserve = lambda: write_policy(self.home, [self.peer_grant()])
        self.assertRefused(self.handle(self.member_open(self.inside)), "r1", "superseded")
        self.assertEqual(self.service.opened, [])

    def test_a_policy_generation_bump_between_reserve_and_handoff_launches_nothing(self) -> None:
        store = self.use_store(HooksStore)
        assert isinstance(store, HooksStore)
        store.after_reserve = lambda: self.set_member(generation=1)
        self.assertRefused(self.handle(self.member_open(self.inside)), "r1", "superseded")
        self.assertEqual(self.service.opened, [])


class MemberNoticeTest(MemberCase):
    def setUp(self) -> None:
        super().setUp()
        self.outside = os.path.join(self.home, "outside", "deep")
        os.makedirs(self.outside)
        write_presence(self.home, "local-1")
        write_presence(self.home, "local-2")

    def refuse(self, request_id: str = "r1", path: str | None = None) -> None:
        self.assertRefused(self.handle(self.member_open(path or self.outside, request_id)), request_id, "folder-not-allowed")

    def test_every_local_session_gets_a_notice_with_the_folder_and_the_command(self) -> None:
        self.refuse()
        for session in ("local-1", "local-2"):
            mail = mail_to(self.home, session)
            self.assertEqual(len(mail), 1)
            text = mail[0]["text"]
            self.assertIn(self.outside, text)
            self.assertIn(f'agent-tabs net folders add "{self.outside}"', text)
            self.assertTrue(text.startswith(f"laptop asked to open a tab in {self.outside}"), text)
            self.assertIn("ask your user", text)
            self.assertEqual(mail[0]["from"]["agent"], "remote")
        self.assertNotIn("SENTINEL", json.dumps(mail_to(self.home, "local-1")))

    def assertGenericNotice(self, session: str = "local-1") -> None:
        mail = mail_to(self.home, session)
        self.assertEqual(len(mail), 1)
        self.assertEqual(
            mail[0]["text"],
            "laptop asked to open a tab outside network.memberFolders; this machine refused. Run agent-tabs net folders to see the list.",
        )

    def test_a_path_with_shell_syntax_gets_a_notice_without_the_path_or_the_command(self) -> None:
        hostile = os.path.join(self.outside, '"; rm -rf ~; "')
        self.refuse("r1", hostile)
        self.assertGenericNotice()
        self.assertGenericNotice("local-2")
        self.assertNotIn("rm -rf", json.dumps(mail_to(self.home, "local-1")))

    def test_a_path_that_does_not_exist_gets_the_notice_without_the_path_or_the_command(self) -> None:
        self.refuse("r1", os.path.join(self.outside, "not-there"))
        self.assertGenericNotice()

    def test_an_existing_folder_with_a_shell_character_gets_the_notice_without_the_path(self) -> None:
        self.set_member(opens_per_hour=100)
        for index, char in enumerate(";$&%'`|<>"):
            folder = os.path.join(self.outside, f"a{char}b")
            try:
                os.makedirs(folder)
            except OSError:
                continue
            with self.subTest(char):
                self.clock.at += HOUR + 1
                self.refuse(f"r{index}", folder)
                self.assertGenericNotice()
                self.assertNotIn(f"a{char}b", " ".join(m["text"] for m in mail_to(self.home, "local-1")))

    def test_a_path_with_a_control_character_is_refused_by_the_wire_and_sends_no_notice(self) -> None:
        self.assertRefused(self.handle(self.member_open(self.outside + "\n; rm -rf ~")), "r1", "bad-request")
        self.assertEqual(mail_to(self.home, "local-1"), [])

    def test_the_label_goes_through_the_display_escaping(self) -> None:
        with mock.patch.object(wire, "escape_display", return_value="ESCAPED") as shown:
            self.refuse("r1", os.path.join(self.outside, "not-there"))
        shown.assert_called_with("laptop")
        self.assertTrue(mail_to(self.home, "local-1")[0]["text"].startswith("ESCAPED asked"))

    def test_the_notice_never_carries_the_task_text(self) -> None:
        self.refuse()
        self.assertNotIn("SENTINEL", mail_to(self.home, "local-1")[0]["text"])

    def test_a_repeat_of_the_same_refusal_sends_no_second_notice_within_an_hour(self) -> None:
        with mock.patch.object(dispatch, "send_message", wraps=dispatch.send_message) as sent:
            self.refuse("r1")
            self.refuse("r2")
            self.assertEqual(sent.call_count, 2)
            self.clock.at += HOUR + 1
            self.refuse("r3")
            self.assertEqual(sent.call_count, 4)
        self.assertEqual(len(mail_to(self.home, "local-1")), 1)

    def test_refusals_for_different_folders_send_one_notice_per_peer_per_hour(self) -> None:
        self.refuse("r1")
        other = os.path.join(self.home, "outside", "second")
        os.makedirs(other)
        self.refuse("r2", other)
        self.assertEqual(len(mail_to(self.home, "local-1")), 1)
        self.clock.at += HOUR + 1
        self.refuse("r3", other)
        self.assertEqual(len(mail_to(self.home, "local-1")), 2)

    def test_a_flood_of_invented_paths_reaches_the_sessions_once_and_builds_no_further_text(self) -> None:
        self.set_member(opens_per_hour=100)
        with mock.patch.object(dispatch, "send_message", wraps=dispatch.send_message) as sent:
            for number in range(40):
                self.refuse(f"r{number}", os.path.join(self.outside, f"invented-{number}"))
            self.assertEqual(sent.call_count, 2)
        self.assertEqual(len(mail_to(self.home, "local-1")), 1)

    def test_the_notice_limit_is_kept_per_peer(self) -> None:
        self.refuse("r1")
        self.dispatcher._notice_sessions = mock.Mock()  # type: ignore[method-assign]  # pyright: ignore[reportPrivateUsage]
        self.dispatcher._notify_folder_refused(PeerGrant(device_id=OTHER, label="desk", address="192.168.1.21", member=True), self.outside)  # pyright: ignore[reportPrivateUsage]
        self.dispatcher._notice_sessions.assert_called_once()  # pyright: ignore[reportPrivateUsage]

    def test_refused_opens_count_against_the_peers_open_limit(self) -> None:
        self.set_member(opens_per_hour=3)
        for number in range(3):
            self.refuse(f"r{number}", os.path.join(self.outside, f"invented-{number}"))
        self.assertRefused(self.handle(self.member_open(self.outside, "r3")), "r3", "limit")
        self.assertRefused(self.handle(self.member_open(self.inside, "r4")), "r4", "limit")
        self.assertEqual(self.service.opened, [])
        self.clock.at += HOUR + 1
        self.assertTrue(self.handle(self.member_open(self.inside, "r5"))["ok"])

    def test_a_tab_a_peer_opened_gets_no_notice(self) -> None:
        tab = self.handle(self.member_open(self.inside, "r0"))["body"]["tab"]
        write_presence(self.home, tab)
        self.refuse("r1")
        self.assertEqual([m["text"] for m in mail_to(self.home, tab)], [TASK])

    def test_a_refusal_with_no_live_session_still_refuses(self) -> None:
        os.remove(os.path.join(self.home, "sessions", "local-1.json"))
        os.remove(os.path.join(self.home, "sessions", "local-2.json"))
        self.refuse()

    def test_a_failing_mailbox_does_not_change_the_refusal(self) -> None:
        with mock.patch("ide_agent_tabs.net.dispatch.send_message", side_effect=MailError("full")):
            self.refuse()
        self.assertEqual(mail_to(self.home, "local-1"), [])

    def test_an_open_inside_the_list_sends_no_notice(self) -> None:
        self.assertTrue(self.handle(self.member_open(self.inside))["ok"])
        self.assertEqual(mail_to(self.home, "local-1"), [])

    def test_a_non_member_refusal_sends_no_notice(self) -> None:
        write_policy(self.home, [self.peer_grant()])
        self.assertRefused(self.handle(self.open_request(folder="elsewhere")), "r1", "folder-not-granted")
        self.assertEqual(mail_to(self.home, "local-1"), [])


class AuditFailureTest(DispatchCase):
    def break_audit(self) -> None:
        os.makedirs(audit_path(self.home))

    def test_an_unwritable_audit_refuses_every_request_before_it_acts(self) -> None:
        self.break_audit()
        reply = self.handle(self.open_request())
        self.assertEqual(reply, {"re": "r1", "ok": False, "refused": True})
        self.assertEqual(self.service.opened, [])
        self.assertTrue(any("audit" in line for line in self.logs))
        self.assertEqual(self.handle({"id": "i1", "op": "inventory", "body": {}}), {"re": "i1", "ok": False, "refused": True})

    def test_a_read_only_request_is_refused_when_its_line_cannot_be_written(self) -> None:
        self.dispatcher.handle(PEER, {"id": "i0", "op": "inventory", "body": {}})
        original = self.dispatcher._audit.write  # pyright: ignore[reportPrivateUsage]
        from ide_agent_tabs.net.audit import AuditError

        def fail(_record: Any) -> None:
            raise AuditError("disk full")

        self.dispatcher._audit.write = fail  # type: ignore[method-assign]  # pyright: ignore[reportPrivateUsage]
        self.addCleanup(setattr, self.dispatcher._audit, "write", original)  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(self.handle({"id": "i1", "op": "inventory", "body": {}}), {"re": "i1", "ok": False, "refused": True})

    def test_a_launch_that_already_happened_keeps_its_reply_when_the_closing_line_fails(self) -> None:
        from ide_agent_tabs.net.audit import AuditError

        def fail(_record: Any) -> None:
            raise AuditError("disk full")

        self.dispatcher._audit.write = fail  # type: ignore[method-assign]  # pyright: ignore[reportPrivateUsage]
        self.assertOk(self.handle(self.open_request()), "r1", {"tab": "tab-1"})
        self.assertEqual(len(self.service.opened), 1)


if __name__ == "__main__":
    unittest.main()

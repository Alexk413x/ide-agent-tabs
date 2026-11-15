from __future__ import annotations

import json
import os
import unittest
from collections.abc import Collection
from typing import Any

from ide_agent_tabs.messaging import store as mail
from ide_agent_tabs.messaging.sessions import update_presence
from ide_agent_tabs.net.origin import REMOTE_ORIGIN_REFUSAL, Caller, OriginChecker
from ide_agent_tabs.net.store import NetStore, NetStoreError, db_path
from ide_agent_tabs.shared.ancestry import Lookup, ProcessInfo
from outbound_support import OTHER, PEER, NetCase, write_config

PRESENCE_START = "2027-01-15T07:00:00.000Z"


def lookup_of(chain: Collection[ProcessInfo]) -> Lookup:
    table = {p.pid: p for p in chain}
    return lambda pid: table.get(pid)


class OriginTest(NetCase):
    def checker(self, chain: Collection[ProcessInfo] = (), **over: Any) -> OriginChecker:
        fields: dict[str, Any] = {"lookup": lambda: lookup_of(chain)}
        fields.update(over)
        checker = OriginChecker(self.home, self.clock, **fields)
        self.addCleanup(checker.close)
        return checker

    def presence(self, session_id: str, **fields: Any) -> None:
        value = {
            "id": session_id,
            "agent": "claude",
            "path": self.folder,
            "pid": 900,
            "startedAt": PRESENCE_START,
            "state": "idle",
            **fields,
        }
        update_presence(self.home, session_id, lambda _: value)

    def test_a_session_is_local_while_no_network_database_exists(self) -> None:
        self.assertFalse(os.path.exists(db_path(self.home)))
        self.assertFalse(self.checker().is_remote(Caller("tab-a", self.folder, "tab-a")))
        self.assertFalse(os.path.exists(db_path(self.home)), "the check creates nothing")

    def test_a_tab_in_remote_tabs_is_remote_origin(self) -> None:
        self.record_remote_tab("tab-a")
        checker = self.checker()
        self.assertTrue(checker.is_remote(Caller("tab-a", self.folder)))
        self.assertFalse(checker.is_remote(Caller("tab-z", self.folder)))

    def test_a_completed_remote_open_in_the_launch_record_is_remote_origin(self) -> None:
        store = self.store()
        store.reserve_open(PEER, "r1", "digest", store.generation(), self.clock(), 3, 4, 10, [])
        store.dispatch_handoff(PEER, "r1", store.generation(), lambda: True)
        store.complete(PEER, "r1", "tab-launched")
        self.assertTrue(self.checker().is_remote(Caller("tab-launched", self.folder)))

    def test_a_session_that_inherited_the_tab_id_of_a_remote_tab_is_remote_origin(self) -> None:
        self.record_remote_tab("tab-r")
        nested = Caller("s-0123456789ab", self.folder, "tab-r")
        self.assertTrue(self.checker().is_remote(nested))
        self.assertFalse(self.checker().is_remote(Caller("s-0123456789ab", self.folder, "tab-local")))

    def test_a_session_descended_from_the_process_of_a_remote_tab_is_remote_origin(self) -> None:
        self.record_remote_tab("tab-r")
        self.presence("tab-r", agentPid=200, agentPidStart=5_000)
        chain = [ProcessInfo(100, 150, "node", 6_000), ProcessInfo(150, 200, "bash", 5_500), ProcessInfo(200, 1, "claude", 5_000)]
        caller = Caller("s-0123456789ab", self.folder, None, 100)
        self.assertTrue(self.checker(chain).is_remote(caller))

    def test_the_descent_also_follows_the_pid_a_shared_server_records_for_a_tab(self) -> None:
        self.record_remote_tab("tab-r")
        self.presence("tab-r", pid=200, pidStart=5_000)
        chain = [ProcessInfo(100, 200, "bash", 5_500), ProcessInfo(200, 1, "claude", 5_000)]
        self.assertTrue(self.checker(chain).is_remote(Caller("s-0123456789ab", self.folder, None, 100)))

    def test_a_reused_pid_that_started_at_another_time_is_not_an_ancestor(self) -> None:
        self.record_remote_tab("tab-r")
        self.presence("tab-r", agentPid=200, agentPidStart=5_000)
        chain = [ProcessInfo(100, 200, "bash", 90_000), ProcessInfo(200, 1, "claude", 80_000)]
        self.assertFalse(self.checker(chain).is_remote(Caller("s-0123456789ab", self.folder, None, 100)))

    def test_descent_from_a_local_tab_is_not_remote(self) -> None:
        self.record_remote_tab("tab-r")
        self.presence("tab-l", agentPid=300, agentPidStart=5_000)
        chain = [ProcessInfo(100, 300, "bash", 5_500), ProcessInfo(300, 1, "claude", 5_000)]
        self.assertFalse(self.checker(chain).is_remote(Caller("s-0123456789ab", self.folder, None, 100)))

    def test_an_unreadable_launch_record_counts_as_remote(self) -> None:
        self.record_remote_tab("tab-r")

        def broken(_home: str) -> NetStore:
            raise NetStoreError("busy")

        self.assertTrue(self.checker(open_store=broken).is_remote(Caller("tab-a", self.folder)))

    def test_a_local_verdict_is_checked_again_at_once(self) -> None:
        checker = self.checker()
        caller = Caller("tab-a", self.folder)
        self.store()
        self.assertFalse(checker.is_remote(caller))
        self.record_remote_tab("tab-a")
        self.assertTrue(checker.is_remote(caller))

    def launching(self, folder: str, request_id: str = "r1") -> None:
        self.store().begin_launch(PEER, request_id, folder, self.clock())

    def test_a_session_in_the_folder_of_an_inflight_launch_is_remote_origin(self) -> None:
        self.launching(self.folder)
        checker = self.checker()
        self.assertTrue(checker.is_remote(Caller("s-0123456789ab", self.folder)))
        self.assertTrue(checker.is_remote(Caller("s-0123456789cd", os.path.join(self.folder, "sub", "deeper"))))

    def test_a_session_beside_or_above_the_launch_folder_is_local(self) -> None:
        self.launching(os.path.join(self.folder, "proj"))
        checker = self.checker()
        self.assertFalse(checker.is_remote(Caller("s-0123456789ab", self.folder)))
        self.assertFalse(checker.is_remote(Caller("s-0123456789cd", os.path.join(self.folder, "proj2"))))
        self.assertFalse(checker.is_remote(Caller("s-0123456789ef", os.path.join(self.home, "elsewhere"))))

    def test_a_session_stays_remote_by_its_tab_record_after_the_launch_ends(self) -> None:
        self.launching(self.folder)
        caller = Caller("tab-a", self.folder)
        self.assertTrue(self.checker().is_remote(caller))
        self.record_remote_tab("tab-a")
        self.store().end_launch(PEER, "r1")
        self.assertTrue(self.checker().is_remote(caller))
        self.assertFalse(self.checker().is_remote(Caller("tab-z", self.folder)))

    def test_an_inflight_launch_older_than_two_minutes_is_ignored(self) -> None:
        self.launching(self.folder)
        caller = Caller("s-0123456789ab", self.folder)
        self.clock.at += 120_000
        self.assertTrue(self.checker().is_remote(caller))
        self.clock.at += 1
        self.assertFalse(self.checker().is_remote(caller))

    def test_a_session_without_a_known_folder_is_not_matched_to_a_launch(self) -> None:
        self.launching(self.folder)
        self.assertFalse(self.checker().is_remote(Caller("s-0123456789ab", "")))

    @unittest.skipUnless(os.name == "nt", "path case is only ignored on Windows")
    def test_the_folder_match_ignores_case_on_windows(self) -> None:
        self.launching(self.folder.upper())
        self.assertTrue(self.checker().is_remote(Caller("s-0123456789ab", self.folder.lower())))


class ToolRefusalTest(NetCase):
    def setUp(self) -> None:
        super().setUp()
        self.local = self.make_session("tab-b", 2)

    def refused(self, name: str, arguments: dict[str, Any], messaging: Any = None) -> None:
        failed, text = self.call(name, arguments, messaging)
        self.assertTrue(failed, text)
        self.assertEqual(text, REMOTE_ORIGIN_REFUSAL)

    def test_a_local_session_may_use_every_tool(self) -> None:
        failed, text = self.call("open_tab", {"path": self.folder})
        self.assertFalse(failed, text)
        failed, text = self.call("handoff", {"path": self.folder, "brief": "carry on"})
        self.assertFalse(failed, text)
        failed, text = self.call("resume_tab", {"id": "closed-1"})
        self.assertFalse(failed, text)
        self.assertEqual((len(self.service.opened), len(self.handoffs.started), len(self.resumes.resumed)), (1, 1, 1))

    def test_a_remote_origin_session_is_refused_open_tab_in_both_forms(self) -> None:
        self.record_remote_tab("tab-a")
        self.refused("open_tab", {"path": self.folder})
        self.refused("open_tab", {"host": "laptop", "path": "proj", "agent": "claude", "prompt": "go"})
        self.assertEqual((self.service.opened, self.request.calls), ([], []))

    def test_a_remote_origin_session_is_refused_resume_tab_and_handoff(self) -> None:
        self.record_remote_tab("tab-a")
        self.refused("resume_tab", {"id": "closed-1"})
        self.refused("handoff", {"path": self.folder, "brief": "carry on"})
        self.assertEqual((self.resumes.resumed, self.handoffs.started), ([], []))

    def test_a_remote_origin_session_is_refused_a_send_to_a_peer_and_start_lan_listener(self) -> None:
        self.record_remote_tab("tab-a")
        self.refused("send_message", {"to": "tab-r@laptop", "text": "x"})
        self.refused("agent_tabs_mod", {"op": "send", "to": "tab-r@laptop", "text": "x"})
        self.refused("start_lan_listener", {})
        self.assertEqual(self.request.calls, [])

    def test_a_remote_origin_session_may_still_message_a_local_session(self) -> None:
        self.record_remote_tab("tab-a")
        failed, text = self.call("send_message", {"to": "tab-b", "text": "local only"})
        self.assertFalse(failed, text)
        self.assertEqual([m["text"] for m in mail.peek_unread(self.home, "tab-b")], ["local only"])
        failed, text = self.call("list_sessions", {})
        self.assertFalse(failed, text)
        self.assertEqual([r["id"] for r in json.loads(text)["sessions"]], ["tab-a", "tab-b"])

    def test_the_refusal_names_no_peer_and_no_tab(self) -> None:
        self.record_remote_tab("tab-a", OTHER)
        failed, text = self.call("open_tab", {"path": self.folder})
        self.assertTrue(failed)
        self.assertEqual(text, REMOTE_ORIGIN_REFUSAL)
        self.assertNotIn(OTHER, text)
        self.assertNotIn("tab-a", text)

    def test_the_refusals_hold_after_the_network_is_turned_off(self) -> None:
        self.record_remote_tab("tab-a")
        write_config(self.home, network="off")
        self.refused("open_tab", {"path": self.folder})
        self.refused("handoff", {"path": self.folder, "brief": "x"})


if __name__ == "__main__":
    unittest.main()

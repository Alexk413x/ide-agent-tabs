from __future__ import annotations

import json
import threading
import unittest
from typing import Any

from ide_agent_tabs.clock import iso, now_ms
from ide_agent_tabs.messaging import sessions, store
from ide_agent_tabs.messaging.db import MailError
from ide_agent_tabs.messaging.history import Who, history
from ide_agent_tabs.messaging.hook import run_hook
from ide_agent_tabs.messaging.messaging import MOD_DELIVERY_NOTE, Messaging, MessagingDeps
from ide_agent_tabs.scheduler import Scheduler
from support import temp_home


class FakeHosts:
    def __init__(self, host: str | None = None) -> None:
        self.host = host
        self.typed: list[tuple[str, str, str]] = []

    def find_host(self, tab_id: str) -> str | None:
        return self.host

    def type_into(self, tab_id: str, host: str, text: str) -> dict[str, Any]:
        self.typed.append((tab_id, host, text))
        return {"ok": True}

    def describe_host(self, host: str) -> str | None:
        return None


class Clock:
    def __init__(self, start: float) -> None:
        self.at = start

    def __call__(self) -> float:
        return self.at


def session(
    test: unittest.TestCase,
    home: str,
    tab_id: str | None,
    agent: str,
    pid: int,
    hosts: FakeHosts | None = None,
    **over: Any,
) -> Messaging:
    scheduler = Scheduler()
    test.addCleanup(scheduler.stop)
    env = {} if tab_id is None else {"IDE_AGENT_TABS_ID": tab_id, "IDE_AGENT_TABS_AGENT": agent}
    deps = {
        "home": home,
        "env": env,
        "pid": pid,
        "cwd": f"/w/{tab_id or 'x'}",
        "hosts": hosts if hosts is not None else FakeHosts(),
        "is_alive": lambda _pid: True,
        "scheduler": scheduler,
        **over,
    }
    messaging = Messaging(MessagingDeps(**deps))
    test.addCleanup(messaging.stop_sync)
    messaging.start()
    # start() queues a session scan; a send that joins a scan begun before the peer registered would not see the peer.
    scanned = threading.Event()
    scheduler.soon(scanned.set)
    scanned.wait(10)
    return messaging


def patch(home: str, tab_id: str, **fields: Any) -> None:
    sessions.update_presence(home, tab_id, lambda p: p if p is None else {**p, **fields})


def presence(home: str, tab_id: str) -> dict[str, Any]:
    return sessions.read_presence(home, tab_id) or {}


def idle(home: str, tab_id: str, at: float | None = None) -> None:
    patch(home, tab_id, state="idle", stateAt=iso(int(now_ms() - 10_000 if at is None else at)), inputIdle=True)


def deliver(home: str, to: str, text: str) -> str:
    out = {"from": {"id": "codex-1a2b", "agent": "codex", "path": "/w"}, "to": to, "text": text}
    return store.send_message(home, out)["id"]


def texts(messages: list[dict[str, Any]]) -> list[str]:
    return [m["text"] for m in messages]


def read_ids(home: str, tab_id: str) -> list[str]:
    return [m["id"] for m in history(home, Who(tab_id, [])) if m["direction"] == "received" and m.get("status") == "read"]


class PresenceTest(unittest.TestCase):
    def test_the_mod_claims_a_tab_and_reports_its_state_and_native_name(self) -> None:
        home = temp_home(self)
        claude = session(self, home, "tab-c", "claude", 1)
        reply = claude.mod_presence({"driver": True, "nativeName": "plugins-fa [6a3948]", "state": "busy"})
        self.assertEqual(reply, {"id": "tab-c", "tab": True, "driver": True})
        p = presence(home, "tab-c")
        self.assertEqual([p["driver"], p["nativeName"], p["state"]], ["mod", "plugins-fa [6a3948]", "busy"])
        self.assertTrue(sessions.is_mod_driven(p, now_ms()))
        self.assertEqual(claude.mod_presence({"driver": False}), {"id": "tab-c", "tab": True, "driver": False})
        released = presence(home, "tab-c")
        self.assertNotIn("driver", released)
        self.assertNotIn("modBeat", released)
        self.assertNotIn("nativeName", released)

    def test_the_mod_beat_is_the_clock_time_and_a_repeat_call_keeps_the_claim(self) -> None:
        home = temp_home(self)
        clock = Clock(1_790_000_000_000)
        claude = session(self, home, "tab-c", "claude", 1, now=clock)
        claude.mod_presence({"driver": True})
        self.assertEqual(presence(home, "tab-c")["modBeat"], 1_790_000_000_000)
        clock.at += 30_000
        claude.mod_presence({"state": "idle"})
        p = presence(home, "tab-c")
        self.assertEqual([p["driver"], p["modBeat"], p["state"], p["stateAt"]], ["mod", 1_790_000_030_000, "idle", iso(1_790_000_030_000)])

    def test_a_state_equal_to_the_current_one_keeps_its_time(self) -> None:
        home = temp_home(self)
        clock = Clock(1_790_000_000_000)
        claude = session(self, home, "tab-c", "claude", 1, now=clock)
        claude.mod_presence({"state": "busy"})
        clock.at += 5_000
        claude.mod_presence({"state": "busy"})
        self.assertEqual(presence(home, "tab-c")["stateAt"], iso(1_790_000_000_000))

    def test_a_session_outside_a_tab_never_claims_the_driver(self) -> None:
        home = temp_home(self)
        loose = session(self, home, None, "", 1, random_id=lambda: "s-000000000001")
        reply = loose.mod_presence({"driver": True, "state": "idle"})
        self.assertIs(reply["driver"], False)
        self.assertIs(reply["tab"], False)
        self.assertNotIn("driver", presence(home, "s-000000000001"))

    def test_a_mod_driven_recipient_is_never_typed_into_and_a_stale_mod_beat_falls_back_to_the_wake_line(self) -> None:
        home = temp_home(self)
        hosts = FakeHosts("fake-term")
        codex = session(self, home, "tab-x", "codex", 1, hosts)
        claude = session(self, home, "tab-c", "claude", 2)
        claude.mod_presence({"driver": True, "state": "idle"})
        idle(home, "tab-c")
        sent = codex.send({"to": "tab-c", "text": "hi"})
        self.assertEqual([sent["delivery"], sent["note"]], ["queued", MOD_DELIVERY_NOTE])
        self.assertEqual(hosts.typed, [])
        patch(home, "tab-c", modBeat=now_ms() - sessions.MOD_STALE_MS - 1)
        idle(home, "tab-c")
        self.assertEqual(codex.send({"to": "tab-c", "text": "again"})["delivery"], "woken")
        self.assertEqual([t[0] for t in hosts.typed], ["tab-c"])

    def test_the_classic_hooks_do_nothing_for_a_mod_driven_session_and_resume_with_no_driver_or_a_stale_beat(self) -> None:
        home = temp_home(self)
        claude = session(self, home, "tab-c", "claude", 1)
        claude.mod_presence({"driver": True, "state": "idle"})
        deliver(home, "tab-c", "hello")
        self.assertIsNone(run_hook("claude", "Stop", {}, home, "tab-c"))
        self.assertIsNone(run_hook("claude", "UserPromptSubmit", {}, home, "tab-c"))
        self.assertEqual(presence(home, "tab-c")["state"], "idle")

        patch(home, "tab-c", modBeat=now_ms() - sessions.MOD_STALE_MS - 1)
        stale = run_hook("claude", "Stop", {}, home, "tab-c")
        self.assertRegex(json.dumps(stale), "Agent Tabs kept this turn open")

        claude.mod_presence({"driver": False})
        run_hook("claude", "UserPromptSubmit", {}, home, "tab-c")
        self.assertEqual(presence(home, "tab-c")["state"], "busy")

    def test_a_server_that_replaces_a_dead_one_drops_the_driver_its_mod_left(self) -> None:
        home = temp_home(self)
        first_alive = [True]

        def alive(pid: int) -> bool:
            return pid != 1 or first_alive[0]

        first = session(self, home, "tab-c", "claude", 1, is_alive=alive)
        first.mod_presence({"driver": True, "nativeName": "old [1]"})
        first.stop_heartbeat()
        first_alive[0] = False
        session(self, home, "tab-c", "claude", 2, is_alive=alive)
        p = presence(home, "tab-c")
        self.assertEqual([p["pid"], p.get("driver"), p.get("nativeName")], [2, None, None])

    def test_a_mod_presence_call_that_beats_the_client_name_still_records_the_session_as_claude(self) -> None:
        home = temp_home(self)
        m = session(self, home, "tab-r", "${IDE_AGENT_TABS_AGENT}", 7)
        self.assertEqual(presence(home, "tab-r")["agent"], "unknown")
        m.mod_presence({"driver": True, "nativeName": "plugins-fa [6a3948]", "state": "idle"})
        self.assertEqual(presence(home, "tab-r")["agent"], "claude")

    def test_the_mod_records_the_model_and_effort_and_bad_values_are_refused(self) -> None:
        home = temp_home(self)
        claude = session(self, home, "tab-c", "claude", 1)
        claude.mod_presence({"model": "claude-opus-5-5", "effort": "xhigh"})
        p = presence(home, "tab-c")
        self.assertEqual([p["model"], p["effort"]], ["claude-opus-5-5", "xhigh"])
        with self.assertRaisesRegex(MailError, "model must be one printable line"):
            claude.mod_presence({"model": "a\nb"})
        with self.assertRaisesRegex(MailError, "effort must be"):
            claude.mod_presence({"effort": "very high"})

    def test_the_mod_refuses_a_native_name_that_is_not_one_printable_line_and_an_owner_that_is_not_a_session_id(self) -> None:
        home = temp_home(self)
        claude = session(self, home, "tab-c", "claude", 1)
        with self.assertRaisesRegex(MailError, "nativeName must be one printable line"):
            claude.mod_presence({"nativeName": "a\nb"})
        with self.assertRaisesRegex(MailError, "nativeName must be one printable line"):
            claude.mod_presence({"nativeName": "x" * 129})
        with self.assertRaisesRegex(MailError, "not a session id: ../x"):
            claude.mod_presence({"owner": "../x"})
        claude.mod_presence({"owner": "9f8e7d6c-0000-4000-8000-000000000000"})
        self.assertEqual(presence(home, "tab-c")["owner"], "9f8e7d6c-0000-4000-8000-000000000000")

    def test_a_native_name_sent_with_the_release_is_not_kept(self) -> None:
        home = temp_home(self)
        claude = session(self, home, "tab-c", "claude", 1)
        claude.mod_presence({"driver": True, "nativeName": "kept [1]"})
        claude.mod_presence({"driver": False, "nativeName": "dropped [2]"})
        self.assertNotIn("nativeName", presence(home, "tab-c"))


class MailTest(unittest.TestCase):
    def test_take_claims_mail_at_least_once_release_and_a_stale_claim_return_it_ack_marks_it_read(self) -> None:
        home = temp_home(self)
        clock = Clock(now_ms())
        claude = session(self, home, "tab-c", "claude", 1, now=clock)
        deliver(home, "tab-c", "one")
        deliver(home, "tab-c", "two")

        first = claude.mod_take()
        self.assertEqual(len(first["messages"]), 2)
        self.assertRegex(first["notice"], "not from your user")
        self.assertEqual(store.peek_unread(home, "tab-c"), [])
        self.assertEqual(claude.mod_settle(first["claim"], "release"), {"claim": first["claim"], "released": 2})
        self.assertEqual(len(store.peek_unread(home, "tab-c")), 2)

        both = claude.mod_take()
        self.assertEqual(texts(both["messages"]), ["one", "two"])
        self.assertEqual(claude.mod_settle(both["claim"], "ack"), {"claim": both["claim"], "read": 2})
        self.assertEqual(len(read_ids(home, "tab-c")), 2)
        with self.assertRaisesRegex(MailError, "no open claim"):
            claude.mod_settle(both["claim"], "ack")
        self.assertEqual(claude.mod_take(), {"claim": None, "messages": []})

        deliver(home, "tab-c", "three")
        lost = claude.mod_take()
        clock.at += store.CLAIM_TIMEOUT_MS + 1_000
        again = claude.mod_take()
        self.assertEqual(texts(again["messages"]), ["three"])
        self.assertNotEqual(again["claim"], lost["claim"])
        self.assertEqual(store.peek_unread(home, "tab-c", clock.at), [])

        clock.at += store.CLAIM_TIMEOUT_MS + 1_000
        self.assertEqual(len(store.peek_unread(home, "tab-c", clock.at)), 1)
        self.assertEqual(texts(claude.read()["messages"]), ["three"])

    def test_unread_counts_waiting_mail_and_names_its_senders_until_it_is_claimed(self) -> None:
        home = temp_home(self)
        claude = session(self, home, "tab-c", "claude", 1)
        self.assertEqual(claude.mod_unread(), {"count": 0, "senders": []})
        deliver(home, "tab-c", "for claude")
        self.assertEqual(claude.mod_unread(), {"count": 1, "senders": ["codex-1a2b"]})
        taken = claude.mod_take()
        self.assertEqual(texts(taken["messages"]), ["for claude"])
        self.assertEqual(claude.mod_unread(), {"count": 0, "senders": []})
        self.assertEqual(claude.mod_settle(taken["claim"], "ack"), {"claim": taken["claim"], "read": 1})
        self.assertEqual(claude.mod_unread(), {"count": 0, "senders": []})
        with self.assertRaisesRegex(MailError, "no open claim"):
            claude.mod_settle("c-0000", "release")

    def test_settling_a_claim_of_another_session_moves_nothing(self) -> None:
        home = temp_home(self)
        claude = session(self, home, "tab-c", "claude", 1)
        other = session(self, home, "tab-d", "claude", 2)
        deliver(home, "tab-c", "mine")
        claim = claude.mod_take()["claim"]
        with self.assertRaisesRegex(MailError, "no open claim"):
            other.mod_settle(claim, "ack")
        self.assertEqual(claude.mod_settle(claim, "release"), {"claim": claim, "released": 1})

    def test_an_ack_resets_the_turn_end_count_and_a_release_keeps_it(self) -> None:
        home = temp_home(self)
        claude = session(self, home, "tab-c", "claude", 1)
        deliver(home, "tab-c", "one")
        patch(home, "tab-c", nudges=2)
        claim = claude.mod_take()["claim"]
        claude.mod_settle(claim, "release")
        self.assertEqual(presence(home, "tab-c")["nudges"], 2)
        claude.mod_settle(claude.mod_take()["claim"], "ack")
        self.assertEqual(presence(home, "tab-c")["nudges"], 0)

    def test_one_take_claims_every_waiting_message_up_to_max_read_chars_at_least_one_and_leaves_the_rest(self) -> None:
        home = temp_home(self)
        m = session(self, home, "tab-t", "claude", 9)
        for i in range(12):
            deliver(home, "tab-t", f"short {i}")
        every = m.mod_take()
        self.assertEqual(len(every["messages"]), 12)
        self.assertNotIn("remaining", every)
        for i in range(3):
            deliver(home, "tab-t", str(i) * store.MAX_TEXT_CHARS)
        capped = m.mod_take()
        self.assertEqual(len(capped["messages"]), max(1, store.MAX_READ_CHARS // store.MAX_TEXT_CHARS))
        self.assertEqual(capped["remaining"], 3 - len(capped["messages"]))

    def test_a_take_shows_each_message_without_its_delivery_fields(self) -> None:
        home = temp_home(self)
        claude = session(self, home, "tab-c", "claude", 1)
        first = deliver(home, "tab-c", "question")
        store.send_message(
            home,
            {"from": {"id": "codex-1a2b", "agent": "codex", "path": "/w"}, "to": "tab-c", "text": "answer", "replyTo": first},
        )
        shown = claude.mod_take()["messages"]
        self.assertEqual([sorted(m) for m in shown], [["from", "id", "sentAt", "text"], ["from", "id", "replyTo", "sentAt", "text"]])


class LogAndHistoryTest(unittest.TestCase):
    def test_a_logged_native_message_reaches_the_history_of_the_session_and_of_its_peer(self) -> None:
        home = temp_home(self)
        claude = session(self, home, "tab-c", "claude", 1)
        claude.mod_presence({"driver": True, "nativeName": "plugins-fa [6a3948]"})
        logged = claude.mod_log({"direction": "sent", "peer": "docs-9b [11aa22]", "text": "ping", "delivery": "delivered"})
        shown = claude.mod_history(Who("tab-c", []))
        self.assertEqual([shown["total"], shown["older"]], [1, 0])
        message = shown["messages"][0]
        self.assertEqual(
            [message["id"], message["direction"], message["route"], message["from"]["name"], message["to"]["name"], message["textLength"]],
            [logged["id"], "sent", "native", "plugins-fa [6a3948]", "docs-9b [11aa22]", 4],
        )
        by_peer = claude.mod_history(Who(None, ["docs-9b [11aa22]"]))
        self.assertEqual([m["direction"] for m in by_peer["messages"]], ["received"])
        by_name = claude.mod_history(Who(None, ["plugins-fa [6a3948]"]))
        self.assertEqual([m["direction"] for m in by_name["messages"]], ["sent"])

    def test_history_and_message_refuse_an_id_that_is_not_a_session_id(self) -> None:
        home = temp_home(self)
        claude = session(self, home, "tab-c", "claude", 1)
        with self.assertRaisesRegex(MailError, "not a session id"):
            claude.mod_message(Who("../x", []), "m-0123456789abcdef")
        with self.assertRaisesRegex(MailError, "needs session or names"):
            claude.mod_message(Who(None, []), "m-0123456789abcdef")


if __name__ == "__main__":
    unittest.main()

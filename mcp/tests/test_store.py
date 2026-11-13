from __future__ import annotations

import subprocess
import sys
import threading
import time
import unittest
from typing import Any
from unittest import mock

from ide_agent_tabs.clock import iso, parse_iso
from ide_agent_tabs.messaging import store, wake
from ide_agent_tabs.messaging.db import MailError, query
from support import SRC, js_fixtures, out, temp_home, texts

T0 = parse_iso("2026-10-07T12:00:00Z") or 0
MINUTE = 60_000
INF = float("inf")


class SendTest(unittest.TestCase):
    def test_refuses_text_over_the_cap_and_a_recipient_that_is_not_a_session_id(self) -> None:
        home = temp_home(self)
        with self.assertRaisesRegex(MailError, "exceeds 32000 characters"):
            store.send_message(home, out("tab-b", "x" * (store.MAX_TEXT_CHARS + 1)), T0)
        with self.assertRaisesRegex(MailError, "exceeds 32000 characters"):
            store.send_message(home, out("tab-b", "\U0001f600" * 16_001), T0)
        with self.assertRaisesRegex(MailError, "not a session id"):
            store.send_message(home, out("../x", "hi"), T0)
        store.send_message(home, out("tab-b", "x" * store.MAX_TEXT_CHARS), T0)
        self.assertEqual(len(store.peek_unread(home, "tab-b", T0)[0]["text"]), store.MAX_TEXT_CHARS)

    def test_rate_limit_counts_the_last_minute_and_a_refused_send_uses_no_slot(self) -> None:
        home = temp_home(self)
        for i in range(store.MAX_SENT_PER_MINUTE):
            store.send_message(home, out(f"tab-{i % 3}", f"m{i}"), T0 + i)
        with self.assertRaisesRegex(MailError, "20 messages in the last minute"):
            store.send_message(home, out("tab-b", "one more"), T0 + MINUTE - 1)
        store.send_message(home, out("tab-b", "one more"), T0 + MINUTE)
        store.send_message(home, out("tab-b", "from z", "tab-z"), T0 + 31)

    def test_the_same_text_to_the_same_recipient_within_dedupe_ms_returns_the_first_id(self) -> None:
        home = temp_home(self)
        first = store.send_message(home, out("tab-b", "review this"), T0)
        self.assertEqual(
            store.send_message(home, out("tab-b", "review this"), T0 + store.DEDUPE_MS - 1), {"id": first["id"], "duplicate": True}
        )
        self.assertNotEqual(store.send_message(home, out("tab-b", "review this", replyTo="m-0000000000000001"), T0 + 2)["id"], first["id"])
        self.assertNotEqual(store.send_message(home, out("tab-c", "review this"), T0 + 3)["id"], first["id"])
        later = store.send_message(home, out("tab-b", "review this"), T0 + store.DEDUPE_MS)
        self.assertNotEqual(later["id"], first["id"])
        self.assertNotIn("duplicate", later)

    def test_send_digest_matches_the_node_digests(self) -> None:
        for case in js_fixtures()["digests"]:
            with self.subTest(case=case):
                self.assertEqual(store.send_digest(case["to"], case["text"], case.get("replyTo")), case["digest"])

    def test_unread_cap_and_reading_frees_a_slot(self) -> None:
        home = temp_home(self)
        for i in range(store.MAX_UNREAD):
            store.send_message(home, out("tab-b", f"m{i}", f"s-{i:012d}"), T0)
        with self.assertRaisesRegex(MailError, "tab-b already has 50 unread messages"):
            store.send_message(home, out("tab-b", "full"), T0)
        store.take_batch(home, "tab-b", count=1, now=T0)
        store.send_message(home, out("tab-b", "fits"), T0)

    def test_lone_surrogates_are_stored_replaced_like_node(self) -> None:
        home = temp_home(self)
        store.send_message(home, out("tab-b", "a\ud800b \U0001f600"), T0)
        self.assertEqual(texts(store.peek_unread(home, "tab-b", T0)), ["a�b \U0001f600"])


class ReadTest(unittest.TestCase):
    def test_a_read_takes_at_most_the_char_cap_at_least_one_message_and_counts_the_rest(self) -> None:
        home = temp_home(self)
        for i in range(3):
            store.send_message(home, out("tab-b", str(i) * store.MAX_TEXT_CHARS), T0 + i)
        first = store.take_batch(home, "tab-b", chars=store.MAX_READ_CHARS, now=T0 + 10)
        self.assertEqual([len(first["messages"]), first["remaining"], first["unreadable"]], [1, 2, 0])
        self.assertEqual(first["messages"][0]["text"][0], "0")
        self.assertEqual(len(store.peek_unread(home, "tab-b", T0 + 10)), 2)

    def test_a_filtered_take_leaves_the_skipped_messages_unread_in_order(self) -> None:
        home = temp_home(self)
        asked = store.send_message(home, out("tab-b", "question", "tab-b-peer"), T0)
        store.send_message(home, out("tab-a", "unrelated", "tab-c"), T0 + 1)
        store.send_message(home, out("tab-a", "answer", "tab-b", replyTo=asked["id"]), T0 + 2)
        store.send_message(home, out("tab-a", "other answer", "tab-c", replyTo=asked["id"]), T0 + 3)
        self.assertTrue(store.has_unread(home, "tab-a", {"from": "tab-b"}, T0 + 4))
        self.assertFalse(store.has_unread(home, "tab-a", {"from": "tab-z"}, T0 + 4))
        taken = store.take_batch(home, "tab-a", {"from": "tab-b", "replyTo": asked["id"]}, now=T0 + 4)
        self.assertEqual(texts(taken["messages"]), ["answer"])
        self.assertEqual(texts(store.take_batch(home, "tab-a", {"replyTo": asked["id"]}, now=T0 + 5)["messages"]), ["other answer"])
        self.assertEqual(texts(store.peek_unread(home, "tab-a", T0 + 6)), ["unrelated"])

    def test_message_shape(self) -> None:
        home = temp_home(self)
        sent = store.send_message(home, out("tab-b", "hi", replyTo="m-0000000000000001"), T0)
        self.assertEqual(
            store.peek_unread(home, "tab-b", T0),
            [
                {
                    "id": sent["id"],
                    "from": {"id": "tab-a", "agent": "codex", "path": "/w/tab-a"},
                    "to": "tab-b",
                    "text": "hi",
                    "replyTo": "m-0000000000000001",
                    "sentAt": iso(T0),
                }
            ],
        )

    def test_claims_hold_until_acked_or_released_and_a_stale_claim_returns_them(self) -> None:
        home = temp_home(self)
        store.send_message(home, out("tab-c", "one"), T0)
        store.send_message(home, out("tab-c", "two"), T0 + 1)
        first = store.claim_batch(home, "tab-c", INF, store.MAX_READ_CHARS, T0 + 2)
        self.assertRegex(first["claim"], r"^c-[0-9a-f]{16}$")
        self.assertEqual(texts(first["messages"]), ["one", "two"])
        self.assertEqual(store.peek_unread(home, "tab-c", T0 + 3), [])
        self.assertEqual(store.take_batch(home, "tab-c", now=T0 + 3)["messages"], [])
        self.assertEqual(store.settle_claim(home, "tab-c", first["claim"], "release", T0 + 4), 2)
        self.assertEqual(store.settle_claim(home, "tab-c", first["claim"], "release", T0 + 4), 0)
        second = store.claim_batch(home, "tab-c", INF, store.MAX_READ_CHARS, T0 + 5)
        self.assertEqual(store.settle_claim(home, "tab-c", second["claim"], "ack", T0 + 6), 2)
        self.assertIsNone(store.claim_batch(home, "tab-c", INF, store.MAX_READ_CHARS, T0 + 7)["claim"])
        self.assertEqual(store.settle_claim(home, "tab-c", "not-a-claim", "ack"), 0)
        store.send_message(home, out("tab-c", "three"), T0 + 8)
        lost = store.claim_batch(home, "tab-c", INF, store.MAX_READ_CHARS, T0 + 9)
        self.assertEqual(store.peek_unread(home, "tab-c", T0 + 9 + store.CLAIM_TIMEOUT_MS - 1), [])
        self.assertEqual(texts(store.peek_unread(home, "tab-c", T0 + 9 + store.CLAIM_TIMEOUT_MS)), ["three"])
        again = store.claim_batch(home, "tab-c", INF, store.MAX_READ_CHARS, T0 + 9 + store.CLAIM_TIMEOUT_MS)
        self.assertEqual(texts(again["messages"]), ["three"])
        self.assertNotEqual(again["claim"], lost["claim"])
        self.assertEqual(store.settle_claim(home, "tab-c", lost["claim"], "ack", T0 + 9 + store.CLAIM_TIMEOUT_MS), 0)
        self.assertEqual(texts(store.peek_unread(home, "tab-c", T0 - 2 * MINUTE)), ["three"])

    def test_a_read_claims_instead_of_marking_read_and_the_next_call_settles_it(self) -> None:
        home = temp_home(self)
        store.send_message(home, out("tab-b", "one"), T0)
        batch = store.take_batch(home, "tab-b", now=T0 + 1)
        self.assertRegex(batch["claim"], r"^t-[0-9a-f]{16}$")
        self.assertEqual([r["state"] for r in store.stored_for(home, "tab-b", [], T0 + 1)], ["held"])
        self.assertEqual([r["state"] for r in store.stored_for(home, "tab-b", [], T0 + store.TOOL_CLAIM_TIMEOUT_MS + 1)], ["unread"])
        self.assertEqual(store.peek_unread(home, "tab-b", T0 + 2), [])
        self.assertFalse(store.has_unread(home, "tab-b", None, T0 + 2))
        self.assertEqual(store.unread_summary(home, "tab-b", T0 + 2), {"count": 0, "senders": []})
        self.assertEqual(store.settle_read_claims(home, "tab-b", [batch["claim"]], "ack", T0 + 3), 1)
        self.assertEqual([r["state"] for r in store.stored_for(home, "tab-b", [], T0 + store.TOOL_CLAIM_TIMEOUT_MS + 1)], ["read"])
        self.assertEqual(store.settle_read_claims(home, "tab-b", [batch["claim"]], "ack", T0 + 4), 0)
        self.assertEqual(store.take_batch(home, "tab-b", now=T0 + store.TOOL_CLAIM_TIMEOUT_MS * 2)["messages"], [])

    def test_a_result_that_never_arrives_returns_the_messages_after_the_tool_claim_timeout(self) -> None:
        home = temp_home(self)
        store.send_message(home, out("tab-b", "lost"), T0)
        lost = store.take_batch(home, "tab-b", now=T0 + 1)
        later = T0 + 1 + store.TOOL_CLAIM_TIMEOUT_MS
        self.assertGreater(store.TOOL_CLAIM_TIMEOUT_MS, store.CLAIM_TIMEOUT_MS)
        self.assertEqual(store.peek_unread(home, "tab-b", later - 1), [])
        self.assertEqual(store.has_unread(home, "tab-b", None, later - 1), False)
        self.assertEqual(texts(store.peek_unread(home, "tab-b", later)), ["lost"])
        self.assertEqual(store.unread_summary(home, "tab-b", later)["count"], 1)
        again = store.take_batch(home, "tab-b", now=later)
        self.assertEqual(texts(again["messages"]), ["lost"])
        self.assertNotEqual(again["claim"], lost["claim"])
        self.assertEqual(store.settle_read_claims(home, "tab-b", [lost["claim"]], "ack", later), 0)
        self.assertEqual(store.settle_read_claims(home, "tab-b", [again["claim"]], "ack", later), 1)

    def test_a_release_returns_a_claim_to_unread_and_only_the_named_session_and_claim_move(self) -> None:
        home = temp_home(self)
        store.send_message(home, out("tab-b", "keep me"), T0)
        store.send_message(home, out("tab-c", "other"), T0)
        batch = store.take_batch(home, "tab-b", now=T0)
        other = store.take_batch(home, "tab-c", now=T0)
        self.assertEqual(store.settle_read_claims(home, "tab-c", [batch["claim"]], "ack", T0), 0)
        self.assertEqual(store.settle_read_claims(home, "tab-b", [batch["claim"]], "release", T0), 1)
        self.assertEqual(texts(store.peek_unread(home, "tab-b", T0)), ["keep me"])
        self.assertEqual(store.settle_read_claims(home, "tab-b", ["t-nothex", "c-0000000000000000"], "ack", T0), 0)
        self.assertEqual(store.settle_read_claims(home, "tab-c", None, "ack", T0), 1)
        self.assertEqual(other["claim"][:2], "t-")

    def test_tool_claims_and_mod_claims_never_settle_each_other(self) -> None:
        home = temp_home(self)
        store.send_message(home, out("tab-b", "for the tool"), T0)
        tool = store.take_batch(home, "tab-b", now=T0)
        store.send_message(home, out("tab-b", "for the mod"), T0 + 1)
        mod = store.claim_batch(home, "tab-b", INF, store.MAX_READ_CHARS, T0 + 2)
        self.assertRegex(mod["claim"], r"^c-")
        self.assertEqual(store.settle_read_claims(home, "tab-b", [mod["claim"]], "ack", T0 + 3), 0)
        self.assertEqual(store.settle_read_claims(home, "tab-b", None, "ack", T0 + 3), 1)
        self.assertEqual(store.settle_claim(home, "tab-b", tool["claim"], "ack", T0 + 3), 0)
        self.assertEqual(store.settle_claim(home, "tab-b", mod["claim"], "release", T0 + 3), 1)
        self.assertEqual(texts(store.peek_unread(home, "tab-b", T0 + 3)), ["for the mod"])

    def test_a_mod_claim_still_times_out_after_two_minutes_beside_a_tool_claim(self) -> None:
        home = temp_home(self)
        store.send_message(home, out("tab-b", "mod"), T0)
        store.claim_batch(home, "tab-b", INF, store.MAX_READ_CHARS, T0)
        store.send_message(home, out("tab-b", "tool"), T0)
        store.take_batch(home, "tab-b", {"from": "tab-a"}, count=1, now=T0)
        self.assertEqual(texts(store.peek_unread(home, "tab-b", T0 + store.CLAIM_TIMEOUT_MS)), ["mod"])

    def test_two_reads_at_once_never_hand_out_a_message_twice(self) -> None:
        home = temp_home(self)
        for i in range(30):
            store.send_message(home, out("tab-b", f"m{i}", f"s-{i:012d}"), T0)
        got: list[list[str]] = []
        lock = threading.Lock()

        def read() -> None:
            while True:
                batch = store.take_batch(home, "tab-b", count=3, now=T0 + 1)
                if not batch["messages"]:
                    return
                with lock:
                    got.append(texts(batch["messages"]))

        threads = [threading.Thread(target=read) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        flat = sorted(t for batch in got for t in batch)
        self.assertEqual(flat, sorted(f"m{i}" for i in range(30)))

    def test_a_held_message_no_longer_matches_a_reply_filter(self) -> None:
        home = temp_home(self)
        asked = store.send_message(home, out("tab-a", "question", "tab-b"), T0)
        store.send_message(home, out("tab-b", "answer", "tab-a", replyTo=asked["id"]), T0)
        store.take_batch(home, "tab-b", {"replyTo": asked["id"]}, now=T0)
        self.assertFalse(store.has_unread(home, "tab-b", {"replyTo": asked["id"]}, T0))

    def test_unread_summary_counts_and_names_senders_newest_first(self) -> None:
        home = temp_home(self)
        self.assertEqual(store.unread_summary(home, "tab-c", T0), {"count": 0, "senders": []})
        store.send_message(home, out("tab-c", "1", "tab-a"), T0)
        store.send_message(home, out("tab-c", "2", "tab-b"), T0 + 1)
        store.send_message(home, out("tab-c", "3", "tab-a"), T0 + 2)
        store.send_message(home, out("tab-x", "other", "tab-d"), T0 + 3)
        self.assertEqual(store.unread_summary(home, "tab-c", T0 + 4), {"count": 3, "senders": ["tab-a", "tab-b"]})


class HistoryTest(unittest.TestCase):
    def test_history_rows_join_delivery_and_state_and_native_rows_by_owner_or_name(self) -> None:
        home = temp_home(self)
        sent = store.send_message(home, out("tab-b", "hello", toName="plugins-fa [6a3948]"), T0)
        store.set_delivery(home, sent["id"], "woken")
        store.log_native(
            home,
            {
                "id": store.new_message_id(),
                "owner": "tab-b",
                "direction": "sent",
                "from": {"id": "tab-b", "name": "plugins-fa [6a3948]"},
                "to": {"name": "docs-9b [11aa22]"},
                "text": "native",
                "sentAt": iso(T0 + 1),
            },
        )
        mail, native = store.stored_for(home, "tab-b", [])
        self.assertEqual(
            [mail["id"], mail["route"], mail["delivery"], mail["state"], mail["from"]["id"], mail["to"]["id"], mail["to"]["name"]],
            [sent["id"], "agent-tabs", "woken", "unread", "tab-a", "tab-b", "plugins-fa [6a3948]"],
        )
        self.assertEqual(
            [native["route"], native["owner"], native["direction"], native["to"]["name"]], ["native", "tab-b", "sent", "docs-9b [11aa22]"]
        )
        self.assertEqual(texts(store.stored_for(home, None, ["docs-9b [11aa22]"])), ["native"])
        self.assertEqual(store.stored_for(home, None, ["nobody"]), [])

    def test_mail_to_lists_unread_and_read(self) -> None:
        home = temp_home(self)
        ask = store.send_message(home, out("tab-new", "taking over", "tab-old"), T0)
        store.send_message(home, out("tab-new", "stopped", "tab-old", replyTo=ask["id"]), T0 + 1)
        store.take_batch(home, "tab-new", count=1, now=T0 + 2)
        self.assertEqual(
            [[m["text"], m.get("replyTo")] for m in store.mail_to(home, "tab-new")], [["taking over", None], ["stopped", ask["id"]]]
        )

    def test_cleanup_keeps_7_days(self) -> None:
        home = temp_home(self)
        old = T0 - store.KEEP_MS - 1
        store.send_message(home, out("live", "old read"), old)
        store.settle_read_claims(home, "live", [store.take_batch(home, "live", now=old)["claim"]], "ack", old)
        store.send_message(home, out("live", "old unread"), old + 1)
        store.send_message(home, out("gone", "old unread", "tab-q"), old)
        store.send_message(home, out("gone-recent", "new unread", "tab-q"), T0)
        for text, at in (("old native", old), ("new native", T0)):
            store.log_native(
                home,
                {
                    "id": store.new_message_id(),
                    "owner": "live",
                    "direction": "received",
                    "from": {"name": "p"},
                    "to": {"id": "live"},
                    "text": text,
                    "sentAt": iso(at),
                },
            )
        store.clean_store(home, {"live"}, T0)
        left = query(home, lambda db: [f"{r['to_id']}: {r['text']}" for r in db.all("SELECT to_id, text FROM messages ORDER BY seq")])
        self.assertEqual(left, ["live: old unread", "gone-recent: new unread", "live: new native"])


class WaitTest(unittest.TestCase):
    def test_a_waiter_wakes_at_once_on_a_send_from_this_process(self) -> None:
        home = temp_home(self)
        store.peek_unread(home, "tab-b")
        found: list[Any] = []
        waiter = threading.Thread(target=lambda: found.append(store.wait_for_message(home, "tab-b", None, 5_000)))
        waiter.start()
        time.sleep(0.05)
        started = time.monotonic()
        store.send_message(home, out("tab-b", "local"))
        waiter.join()
        self.assertEqual(found[0]["text"], "local")
        self.assertLess(time.monotonic() - started, 0.5)

    def test_a_cancelled_wait_returns_nothing_and_a_timeout_returns_nothing(self) -> None:
        home = temp_home(self)
        cancel = threading.Event()
        cancel.set()
        store.send_message(home, out("tab-b", "keep"))
        self.assertIsNone(store.wait_for_message(home, "tab-b", None, 1_000, cancel))
        self.assertEqual(texts(store.peek_unread(home, "tab-b")), ["keep"])
        started = time.monotonic()
        self.assertIsNone(store.wait_for_message(home, "tab-c", None, 300))
        self.assertGreaterEqual(time.monotonic() - started, 0.29)

    def test_a_filtered_wait_takes_only_the_match(self) -> None:
        home = temp_home(self)
        store.send_message(home, out("tab-b", "other", "tab-x"))
        store.send_message(home, out("tab-b", "wanted", "tab-y"))
        found = store.wait_for_message(home, "tab-b", {"from": "tab-y"}, 1_000)
        assert found is not None
        self.assertEqual(found["text"], "wanted")
        self.assertEqual(texts(store.peek_unread(home, "tab-b")), ["other"])

    def send_from_another_process(self, home: str, text: str) -> threading.Thread:
        script = (
            "import sys; sys.path.insert(0, sys.argv[1]); from ide_agent_tabs.messaging import store; "
            "store.send_message(sys.argv[2], {'from': {'id': 'tab-z', 'agent': 'codex', 'path': '/z'}, 'to': 'tab-b', 'text': sys.argv[3]})"
        )

        def run() -> None:
            time.sleep(0.2)
            subprocess.run([sys.executable, "-I", "-S", "-c", script, SRC, home, text], check=True, timeout=60)

        sender = threading.Thread(target=run)
        sender.start()
        return sender

    def test_a_waiter_wakes_on_a_send_from_another_process(self) -> None:
        home = temp_home(self)
        store.peek_unread(home, "tab-b")
        sender = self.send_from_another_process(home, "from afar")
        with mock.patch.object(store, "RECHECK_MS", 60_000):
            found = store.wait_for_message(home, "tab-b", None, 20_000)
        sender.join()
        assert found is not None
        self.assertEqual(found["text"], "from afar")

    def test_a_waiter_whose_watch_misses_still_finds_the_message_by_polling(self) -> None:
        home = temp_home(self)
        store.peek_unread(home, "tab-b")
        sender = self.send_from_another_process(home, "polled")
        with mock.patch.object(wake, "on_wake", lambda *_a: lambda: None), mock.patch.object(store, "RECHECK_MS", 100):
            found = store.wait_for_message(home, "tab-b", None, 20_000)
        sender.join()
        assert found is not None
        self.assertEqual(found["text"], "polled")


if __name__ == "__main__":
    unittest.main()

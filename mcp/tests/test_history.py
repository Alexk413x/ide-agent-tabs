from __future__ import annotations

import threading
import unittest
from typing import Any

from ide_agent_tabs.clock import iso, now_ms
from ide_agent_tabs.jsjson import stringify, utf16_len
from ide_agent_tabs.messaging import history, store
from ide_agent_tabs.messaging.db import MailError
from ide_agent_tabs.messaging.history import HISTORY_REPLY_CHARS, PIECE_CHARS, PREVIEW_CHARS, Who, text_piece
from ide_agent_tabs.messaging.messaging import Messaging, MessagingDeps
from ide_agent_tabs.scheduler import Scheduler
from support import temp_home

NATIVE_PEER = "docs-9b [11aa22]"
NATIVE_SELF = "plugins-fa [6a3948]"


class FakeHosts:
    def find_host(self, tab_id: str) -> str | None:
        return None

    def type_into(self, tab_id: str, host: str, text: str) -> dict[str, Any]:
        return {"ok": True}

    def describe_host(self, host: str) -> str | None:
        return None


class Clock:
    def __init__(self, start: float) -> None:
        self.at = start

    def __call__(self) -> float:
        return self.at


def session(test: unittest.TestCase, home: str, tab_id: str, agent: str, pid: int, clock: Clock | None = None) -> Messaging:
    scheduler = Scheduler()
    test.addCleanup(scheduler.stop)
    deps = MessagingDeps(
        home=home,
        env={"IDE_AGENT_TABS_ID": tab_id, "IDE_AGENT_TABS_AGENT": agent},
        pid=pid,
        cwd=f"/w/{tab_id}",
        hosts=FakeHosts(),
        is_alive=lambda _pid: True,
        now=clock,
        scheduler=scheduler,
    )
    messaging = Messaging(deps)
    test.addCleanup(messaging.stop_sync)
    messaging.start()
    # start() queues a session scan; a send that joins a scan begun before the peer registered would not see the peer.
    scanned = threading.Event()
    scheduler.soon(scanned.set)
    scanned.wait(10)
    return messaging


def unread_ids(home: str, tab_id: str) -> list[str]:
    return [m["id"] for m in store.peek_unread(home, tab_id)]


def peer_key(item: dict[str, Any]) -> str:
    return item["peer"].get("id") or item["peer"].get("name")


def json_length(value: Any) -> int:
    return utf16_len(stringify(value))


class SentHistoryTest(unittest.TestCase):
    def test_every_send_is_in_the_senders_history_with_its_delivery(self) -> None:
        home = temp_home(self)
        a = session(self, home, "tab-a", "codex", 1)
        session(self, home, "tab-b", "claude", 2)
        sent = a.send({"to": "tab-b", "text": "review x.ts"})
        record = a.mod_history(Who("tab-a", []))["messages"][0]
        self.assertEqual(
            {
                "id": record["id"],
                "route": record["route"],
                "from": record["from"]["id"],
                "to": record["to"]["id"],
                "text": record["text"],
                "delivery": record["delivery"],
                "direction": record["direction"],
            },
            {
                "id": sent["id"],
                "route": "agent-tabs",
                "from": "tab-a",
                "to": "tab-b",
                "text": "review x.ts",
                "delivery": "queued",
                "direction": "sent",
            },
        )

    def test_history_merges_sent_received_and_native_traffic_oldest_first_and_marks_nothing_read(self) -> None:
        home = temp_home(self)
        clock = Clock(now_ms() - 10 * 60_000)
        a = session(self, home, "tab-a", "codex", 1, clock)
        b = session(self, home, "tab-b", "claude", 2, clock)
        b.mod_presence({"driver": True, "nativeName": NATIVE_SELF, "state": "busy"})
        b.mod_log({"direction": "received", "peer": NATIVE_PEER, "text": "native hello", "at": clock.at - 60_000})
        first = a.send({"to": "tab-b", "text": "from codex"})
        clock.at += 60_000
        reply = b.send({"to": "tab-a", "text": "from claude", "replyTo": first["id"]})
        clock.at += 60_000
        b.mod_log({"direction": "sent", "peer": NATIVE_PEER, "text": "native reply", "delivery": "delivered"})

        unread_b = unread_ids(home, "tab-b")
        unread_a = unread_ids(home, "tab-a")
        messages = b.mod_history(Who("tab-b", [NATIVE_SELF]))["messages"]
        self.assertEqual(
            [[m["direction"], peer_key(m), m["text"], m["route"]] for m in messages],
            [
                ["received", NATIVE_PEER, "native hello", "native"],
                ["received", "tab-a", "from codex", "agent-tabs"],
                ["sent", "tab-a", "from claude", "agent-tabs"],
                ["sent", NATIVE_PEER, "native reply", "native"],
            ],
        )
        self.assertEqual(messages[1]["status"], "unread")
        self.assertEqual(messages[2]["replyTo"], first["id"])
        self.assertEqual([messages[2]["delivery"], messages[2]["status"]], ["queued", "unread"])
        self.assertEqual(messages[3]["delivery"], "delivered")
        self.assertEqual(len({m["id"] for m in messages}), 4)
        self.assertIn(reply["id"], [m["id"] for m in messages])

        from_codex = a.mod_history(Who("tab-a", []))["messages"]
        self.assertEqual([[m["direction"], m["text"]] for m in from_codex], [["sent", "from codex"], ["received", "from claude"]])
        native_peer = b.mod_history(Who(None, [NATIVE_PEER]))["messages"]
        self.assertEqual([[m["direction"], m["text"]] for m in native_peer], [["sent", "native hello"], ["received", "native reply"]])

        self.assertEqual(unread_ids(home, "tab-b"), unread_b)
        self.assertEqual(unread_ids(home, "tab-a"), unread_a)

    def test_a_native_message_logged_by_two_sessions_within_two_minutes_shows_once(self) -> None:
        home = temp_home(self)
        clock = Clock(now_ms())
        b = session(self, home, "tab-b", "claude", 2, clock)
        c = session(self, home, "tab-c", "claude", 3, clock)
        b.mod_log({"direction": "sent", "peer": NATIVE_PEER, "text": "same words"})
        clock.at += 30_000
        c.mod_log({"direction": "sent", "peer": NATIVE_PEER, "text": "same words"})
        shown = b.mod_history(Who(None, [NATIVE_PEER]))
        self.assertEqual(shown["total"], 1)
        clock.at += 200_000
        c.mod_log({"direction": "sent", "peer": NATIVE_PEER, "text": "same words"})
        self.assertEqual(b.mod_history(Who(None, [NATIVE_PEER]))["total"], 2)


class LogTest(unittest.TestCase):
    def test_log_refuses_a_bad_peer_or_direction_and_history_needs_a_session_or_a_name(self) -> None:
        home = temp_home(self)
        b = session(self, home, "tab-b", "claude", 2)
        with self.assertRaisesRegex(MailError, "peer must be one printable line"):
            b.mod_log({"direction": "sent", "peer": "a\nb", "text": "x"})
        with self.assertRaisesRegex(MailError, "direction"):
            b.mod_log({"direction": "sideways", "peer": "p", "text": "x"})
        with self.assertRaisesRegex(MailError, "needs session or names"):
            b.mod_history(Who(None, []))
        with self.assertRaisesRegex(MailError, "not a session id"):
            b.mod_history(Who("../x", []))

    def test_history_drops_names_that_are_not_one_printable_line(self) -> None:
        home = temp_home(self)
        b = session(self, home, "tab-b", "claude", 2)
        b.mod_log({"direction": "sent", "peer": NATIVE_PEER, "text": "kept"})
        self.assertEqual(b.mod_history(Who(None, [NATIVE_PEER, "a\nb"]))["total"], 1)
        with self.assertRaisesRegex(MailError, "needs session or names"):
            b.mod_history(Who(None, ["a\nb"]))

    def test_cleanup_drops_logged_native_traffic_after_seven_days_like_read_mail(self) -> None:
        home = temp_home(self)
        b = session(self, home, "tab-b", "claude", 2)
        old = now_ms() - store.KEEP_MS - 60_000
        b.mod_log({"direction": "sent", "peer": "p", "text": "old sent", "at": old})
        b.mod_log({"direction": "received", "peer": "p", "text": "old received", "at": old})
        b.mod_log({"direction": "sent", "peer": "p", "text": "new"})
        store.clean_store(home, {"tab-b"})
        self.assertEqual([m["text"] for m in b.mod_history(Who("tab-b", []))["messages"]], ["new"])

    def test_a_valid_message_id_is_kept_and_logging_it_twice_stores_it_once(self) -> None:
        home = temp_home(self)
        b = session(self, home, "tab-b", "claude", 2)
        wanted = "m-0123456789abcdef"
        self.assertEqual(b.mod_log({"direction": "sent", "peer": "p", "text": "first", "id": wanted}), {"id": wanted})
        b.mod_log({"direction": "sent", "peer": "p", "text": "second", "id": wanted})
        self.assertEqual([m["text"] for m in b.mod_history(Who("tab-b", []))["messages"]], ["first"])
        made = b.mod_log({"direction": "sent", "peer": "p", "text": "third", "id": "not an id"})["id"]
        self.assertRegex(made, r"m-[0-9a-f]{16}")

    def test_log_cuts_the_text_and_the_delivery_note_to_their_caps(self) -> None:
        home = temp_home(self)
        b = session(self, home, "tab-b", "claude", 2)
        logged = b.mod_log({"direction": "sent", "peer": "p", "text": "x" * (store.MAX_TEXT_CHARS + 5), "delivery": "d" * 300})
        message = b.mod_message(Who("tab-b", []), logged["id"])
        self.assertEqual(message["total"], store.MAX_TEXT_CHARS)
        self.assertEqual(len(message["message"]["delivery"]), 200)

    def test_log_uses_the_clock_when_the_time_is_missing_or_not_finite(self) -> None:
        home = temp_home(self)
        clock = Clock(1_700_000_000_000)
        b = session(self, home, "tab-b", "claude", 2, clock)
        b.mod_log({"direction": "sent", "peer": "p", "text": "no time"})
        b.mod_log({"direction": "sent", "peer": "p", "text": "nan time", "at": float("nan")})
        shown = b.mod_history(Who("tab-b", []))["messages"]
        self.assertEqual([m["at"] for m in shown], [iso(1_700_000_000_000)] * 2)
        with self.assertRaisesRegex(MailError, "Invalid time value"):
            b.mod_log({"direction": "sent", "peer": "p", "text": "far", "at": 1e300})


class CountsTest(unittest.TestCase):
    def test_counts_match_the_history_each_session_shows_and_grow_with_new_traffic(self) -> None:
        home = temp_home(self)
        a = session(self, home, "tab-a", "codex", 1)
        b = session(self, home, "tab-b", "claude", 2)
        b.mod_presence({"driver": True, "nativeName": NATIVE_SELF, "state": "busy"})
        a.send({"to": "tab-b", "text": "one"})
        b.send({"to": "tab-a", "text": "two"})
        b.mod_log({"direction": "sent", "peer": NATIVE_PEER, "text": "native", "delivery": "delivered"})
        whos = [Who("tab-a", []), Who("tab-b", [NATIVE_SELF]), Who(None, [NATIVE_PEER]), Who(None, [])]
        counts = b.mod_counts(whos)["counts"]
        shown = [b.mod_history(who)["total"] for who in whos[:3]]
        self.assertEqual(counts, [*shown, None])
        self.assertEqual(counts, [2, 3, 1, None])

        a.send({"to": "tab-b", "text": "three"})
        self.assertEqual(b.mod_counts(whos)["counts"], [3, 4, 1, None])

    def test_counts_give_none_for_an_invalid_id_without_names(self) -> None:
        home = temp_home(self)
        b = session(self, home, "tab-b", "claude", 2)
        b.mod_log({"direction": "sent", "peer": NATIVE_PEER, "text": "native"})
        counts = b.mod_counts([Who("../x", []), Who("../x", [NATIVE_PEER]), Who("tab-b", [])])["counts"]
        self.assertEqual(counts, [None, 1, 1])


class LongHistoryTest(unittest.TestCase):
    def test_a_long_history_fits_one_reply_with_its_full_total_previews_of_the_newest_and_each_message_whole_on_request(self) -> None:
        home = temp_home(self)
        clock = Clock(now_ms() - 60 * 60_000)
        b = session(self, home, "tab-b", "claude", 2, clock)
        for i in range(200):
            clock.at += 1_000
            b.mod_log(
                {
                    "direction": "sent" if i % 2 else "received",
                    "peer": NATIVE_PEER,
                    "text": f"{i} {'y' * 3_000}",
                    "at": clock.at,
                }
            )
        who = Who("tab-b", [])
        reply = b.mod_history(who)
        self.assertLess(json_length(reply), HISTORY_REPLY_CHARS)
        self.assertEqual(reply["total"], 200)
        self.assertEqual(b.mod_counts([who])["counts"], [200])
        self.assertTrue(0 < len(reply["messages"]) < 200)
        self.assertEqual(reply["messages"][-1]["text"].split(" ")[0], "199")
        self.assertTrue(all(len(m["text"]) == PREVIEW_CHARS and m["textLength"] > PREVIEW_CHARS for m in reply["messages"]))
        first = reply["messages"][0]
        whole = b.mod_message(who, first["id"])
        self.assertEqual(len(whole["text"]), first["textLength"])
        self.assertEqual([whole["offset"], whole["total"], whole["message"]["text"]], [0, first["textLength"], ""])
        self.assertEqual(b.mod_message(who, "m-0000000000000000"), {"message": None})

    def test_a_message_whose_json_outgrows_one_reply_comes_back_in_pieces_that_each_fit(self) -> None:
        home = temp_home(self)
        b = session(self, home, "tab-b", "claude", 2)
        text = "".join('"' if i % 7 == 0 else "\n" if i % 5 == 0 else chr(97 + i % 26) for i in range(store.MAX_TEXT_CHARS))
        message_id = b.mod_log({"direction": "sent", "peer": NATIVE_PEER, "text": text})["id"]
        who = Who("tab-b", [])
        offset = 0
        joined = ""
        pieces = 0
        while offset < len(text):
            reply = b.mod_message(who, message_id, offset)
            self.assertLess(json_length(reply), HISTORY_REPLY_CHARS, f"piece {pieces}")
            self.assertEqual(reply["offset"], offset)
            self.assertEqual(reply["total"], store.MAX_TEXT_CHARS)
            joined += reply["text"]
            offset += len(reply["text"])
            pieces += 1
        self.assertEqual(joined, text)
        self.assertGreaterEqual(pieces, 2)

    def test_a_message_offset_is_clamped_into_the_text(self) -> None:
        home = temp_home(self)
        b = session(self, home, "tab-b", "claude", 2)
        message_id = b.mod_log({"direction": "sent", "peer": NATIVE_PEER, "text": "abcdef"})["id"]
        who = Who("tab-b", [])
        self.assertEqual([b.mod_message(who, message_id, -5)[k] for k in ("offset", "text")], [0, "abcdef"])
        self.assertEqual([b.mod_message(who, message_id, 2.9)[k] for k in ("offset", "text")], [2, "cdef"])
        self.assertEqual([b.mod_message(who, message_id, 99)[k] for k in ("offset", "text")], [6, ""])

    def test_history_pages_older_batches_by_a_before_cursor_and_the_totals_add_up(self) -> None:
        home = temp_home(self)
        clock = Clock(now_ms() - 60 * 60_000)
        b = session(self, home, "tab-b", "claude", 2, clock)
        for i in range(300):
            clock.at += 1_000
            b.mod_log({"direction": "sent", "peer": NATIVE_PEER, "text": f"{i} {'z' * 600}", "at": clock.at})
        who = Who("tab-b", [])
        seen: list[str] = []
        before: str | None = None
        batches = 0
        while True:
            reply = b.mod_history(who, before)
            self.assertEqual(reply["total"], 300)
            seen[:0] = [m["text"].split(" ")[0] for m in reply["messages"]]
            batches += 1
            self.assertEqual(reply["older"], 300 - len(seen))
            if reply["older"] == 0:
                break
            before = reply["messages"][0]["id"]
        self.assertGreaterEqual(batches, 3)
        self.assertEqual(seen, [str(i) for i in range(300)])


class HelperTest(unittest.TestCase):
    def test_a_piece_never_splits_a_surrogate_pair_and_shrinks_until_its_json_fits(self) -> None:
        emoji = "\U0001f600" * 10
        piece = text_piece(emoji, 0, 9)
        self.assertEqual(utf16_len(piece) % 2, 0)
        self.assertLessEqual(json_length(text_piece('"' * 100, 0, 50)), 50)
        self.assertEqual(text_piece("abc", 3), "")
        self.assertEqual(PIECE_CHARS, 30_000)

    def test_a_piece_counts_its_offset_and_length_in_utf16_units(self) -> None:
        emoji = "\U0001f600" * 4
        self.assertEqual(text_piece(emoji, 2, 4), "\U0001f600")
        self.assertEqual(text_piece("x" + emoji, 1, 100), emoji)

    def test_older_than_uses_the_cursor_position_or_falls_back_to_its_time(self) -> None:
        items = [{"id": f"m-{i:016x}", "at": iso(1_000 * i)} for i in range(1, 6)]
        self.assertEqual(history.older_than(items, None), items)
        self.assertEqual(history.older_than(items, items[3]["id"]), items[:3])
        self.assertEqual(history.older_than(items, iso(3_500)), items[:3])

    def test_previews_stop_at_the_batch_size(self) -> None:
        items = [{"id": f"m-{i:016x}", "at": iso(i), "text": f"t{i}"} for i in range(history.HISTORY_BATCH + 10)]
        shown = history.previews(items)
        self.assertEqual(len(shown), history.HISTORY_BATCH)
        self.assertEqual(shown[-1]["text"], items[-1]["text"])
        self.assertEqual(shown[0]["textLength"], utf16_len(shown[0]["text"]))

    def test_history_keeps_only_the_newest_history_max_items(self) -> None:
        home = temp_home(self)
        start = now_ms() - 3_600_000
        for i in range(history.HISTORY_MAX + 3):
            record = {
                "id": f"m-{i:016x}",
                "owner": "tab-b",
                "direction": "sent",
                "from": {"id": "tab-b", "agent": "claude", "path": "/w"},
                "to": {"name": "p"},
                "text": str(i),
                "sentAt": iso(start + i),
            }
            store.log_native(home, record)
        shown = history.history(home, Who("tab-b", []))
        self.assertEqual(len(shown), history.HISTORY_MAX)
        self.assertEqual(shown[-1]["text"], str(history.HISTORY_MAX + 2))
        self.assertEqual(shown[0]["text"], "3")


if __name__ == "__main__":
    unittest.main()

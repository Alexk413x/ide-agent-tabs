from __future__ import annotations

import os
import sys
import time
import unittest
from typing import Any

from ide_agent_tabs.messaging import db, store
from support import Worker, node_worker, percentile, py_worker, require_node, start_together, temp_home

# The limits of the Node stress test in mcp/test/mailboxStress.test.ts.
BURST_SEND_MS = 600
BURST_LOOP_DELAY_MS = 750


def report(label: str, values: list[float]) -> None:
    if values:
        sys.stderr.write(
            f"\n  {label}: n {len(values)}, p50 {percentile(values, 50):.1f} ms, p99 {percentile(values, 99):.1f} ms, max {max(values):.1f} ms"
        )


class MixedStressTest(unittest.TestCase):
    def setUp(self) -> None:
        require_node(self)

    def worker(self, i: int, mode: str, args: dict[str, Any]) -> Worker:
        return node_worker(self, mode, args, "stress") if i % 2 == 0 else py_worker(self, mode, args)

    def test_12_node_and_python_processes_messaging_each_other_lose_nothing_and_read_nothing_twice(self) -> None:
        home = temp_home(self, "iat-mixed-")
        ids = [f"p-{i:02d}" for i in range(12)]
        workers = [
            self.worker(
                i, "mesh", {"home": home, "me": me, "peers": [ids[(i + 1 + k) % 12] for k in range(11)], "count": 15, "expected": 15}
            )
            for i, me in enumerate(ids)
        ]
        start_together(workers)
        results = [w.result() for w in workers]
        sent = [m for r in results for m in r["sent"]]
        read = [{**m, "by": ids[i]} for i, r in enumerate(results) for m in r["read"]]
        self.assertEqual(len(sent), 12 * 15)
        self.assertEqual(len(read), len(sent), "no message is lost")
        self.assertEqual(len({m["id"] for m in read}), len(read), "no message is read twice")
        to = {m["id"]: m["to"] for m in sent}
        for m in read:
            self.assertEqual(to[m["id"]], m["by"])
        self.assertEqual(db.query(home, lambda d: d.one("SELECT count(*) FROM messages WHERE state <> 'read'")[0]), 0)  # type: ignore[index]
        report("send plus read, node", [p for i, r in enumerate(results) if i % 2 == 0 for p in r["pairs"]])
        report("send plus read, python", [p for i, r in enumerate(results) if i % 2 == 1 for p in r["pairs"]])

    def test_six_mixed_readers_of_one_mailbox_take_disjoint_messages(self) -> None:
        home = temp_home(self, "iat-mixed-")
        stop = os.path.join(home, "stop")
        readers = [self.worker(i, "readLoop", {"home": home, "target": "tab-t", "stop": stop}) for i in range(6)]
        senders = [
            self.worker(
                i,
                "send",
                {"home": home, "from": sender, "targets": ["tab-t"], "texts": [f"{sender} {k}" for k in range(store.MAX_SENT_PER_MINUTE)]},
            )
            for i, sender in enumerate(("tab-s1", "tab-s2"))
        ]
        start_together(readers + senders)
        sent = [r for w in senders for r in w.result()["results"]]
        self.assertEqual([r for r in sent if "error" in r], [])
        with open(stop, "w", encoding="utf-8"):
            pass
        taken = [i for w in readers for i in w.result()["ids"]]
        self.assertEqual(len(set(taken)), len(taken), "no two readers took one message")
        self.assertEqual(set(taken), {r["id"] for r in sent})

    def test_the_rate_limit_holds_across_a_node_and_a_python_process_sending_as_one_session(self) -> None:
        home = temp_home(self, "iat-mixed-")
        workers = [
            self.worker(
                i, "send", {"home": home, "from": "tab-a", "targets": ["tab-b", "tab-c", "tab-d"], "texts": [f"{p} {k}" for k in range(15)]}
            )
            for i, p in enumerate("ab")
        ]
        start_together(workers)
        results = [r for w in workers for r in w.result()["results"]]
        self.assertEqual(sum("id" in r for r in results), store.MAX_SENT_PER_MINUTE)
        self.assertEqual(sum("20 messages in the last minute" in r.get("error", "") for r in results), 30 - store.MAX_SENT_PER_MINUTE)

    def test_four_mixed_processes_sending_the_same_text_store_it_once(self) -> None:
        home = temp_home(self, "iat-mixed-")
        workers = [
            self.worker(i, "send", {"home": home, "from": "tab-a", "targets": ["tab-b"], "texts": ["the same request"]}) for i in range(4)
        ]
        start_together(workers)
        results = [r for w in workers for r in w.result()["results"]]
        self.assertEqual(len({r["id"] for r in results}), 1)
        self.assertEqual(sum(bool(r.get("duplicate")) for r in results), 3)
        self.assertEqual(len(store.peek_unread(home, "tab-b")), 1)

    def test_60_sends_from_six_mixed_processes_stop_at_the_unread_cap(self) -> None:
        home = temp_home(self, "iat-mixed-")
        workers = [
            self.worker(i, "send", {"home": home, "from": f"tab-s{i}", "targets": ["tab-b"], "texts": [f"{i}.{k}" for k in range(10)]})
            for i in range(6)
        ]
        start_together(workers)
        results = [r for w in workers for r in w.result()["results"]]
        self.assertEqual(sum("id" in r for r in results), store.MAX_UNREAD)
        self.assertEqual(sum("already has 50 unread messages" in r.get("error", "") for r in results), 10)

    def test_mixed_claimers_and_readers_share_a_mailbox_without_taking_one_message_twice(self) -> None:
        home = temp_home(self, "iat-mixed-")
        stop = os.path.join(home, "stop")
        sent = [
            store.send_message(home, {"from": {"id": f"s-{i:012d}", "agent": "codex", "path": "/"}, "to": "tab-c", "text": f"m{i}"})["id"]
            for i in range(40)
        ]
        db.close_db(home)
        workers = [
            node_worker(self, "claimLoop", {"home": home, "target": "tab-c", "stop": stop}, "stress"),
            py_worker(self, "readLoop", {"home": home, "target": "tab-c", "stop": stop}),
            py_worker(self, "claimLoop", {"home": home, "target": "tab-c", "stop": stop}),
            node_worker(self, "readLoop", {"home": home, "target": "tab-c", "stop": stop}, "stress"),
        ]
        start_together(workers)
        with open(stop, "w", encoding="utf-8"):
            pass
        taken = [i for w in workers for i in w.result()["ids"]]
        self.assertEqual(len(set(taken)), len(taken))
        self.assertEqual(set(taken), set(sent))

    def test_a_python_process_killed_inside_a_write_leaves_no_row_and_node_sends_within_a_second(self) -> None:
        home = temp_home(self, "iat-mixed-")
        store.peek_unread(home, "tab-b")
        db.close_db(home)
        holder = py_worker(self, "holdLock", {"home": home})
        holder.line("locked")
        holder.close()
        started = time.monotonic()
        sender = node_worker(self, "send", {"home": home, "from": "tab-a", "targets": ["tab-b"], "texts": ["after the crash"]}, "stress")
        start_together([sender])
        result = sender.result()["results"][0]
        self.assertIn("id", result)
        self.assertLess(result["ms"], 1_000)
        self.assertLess(time.monotonic() - started, 15)
        self.assertEqual([m["text"] for m in store.peek_unread(home, "tab-b")], ["after the crash"])

    def test_eight_mixed_processes_opening_a_fresh_home_migrate_it_once(self) -> None:
        home = temp_home(self, "iat-mixed-")
        os.makedirs(os.path.join(home, "mail", "tab-b", "new"))
        workers = [self.worker(i, "open", {"home": home}) for i in range(8)]
        start_together(workers)
        self.assertEqual([w.result()["version"] for w in workers], [db.SCHEMA_VERSION] * 8)
        self.assertFalse(os.path.exists(os.path.join(home, "mail")))
        store.send_message(home, {"from": {"id": "tab-a", "agent": "codex", "path": "/"}, "to": "tab-b", "text": "works"})
        self.assertEqual(len(store.peek_unread(home, "tab-b")), 1)

    def check_shared(self, server_kind: str) -> None:
        home = temp_home(self, "iat-mixed-")
        sessions = [f"sh-{i:02d}" for i in range(32)]
        internal = 5
        external = [
            self.worker(
                k,
                "send",
                {
                    "home": home,
                    "from": f"ext-{k}",
                    "targets": [sessions[(k * 20 + m) % 32] for m in range(20)],
                    "texts": [f"ext {k}.{m}" for m in range(20)],
                },
            )
            for k in range(8)
        ]
        args = {"home": home, "sessions": sessions, "internal": internal, "expected": internal + 5}
        server = node_worker(self, "shared", args, "stress") if server_kind == "node" else py_worker(self, "shared", args)
        start_together([server, *external])
        external_sends = [r for w in external for r in w.result()["results"]]
        shared = server.result()
        self.assertEqual([r for r in external_sends if "error" in r], [])
        received = [m["id"] for box in shared["received"].values() for m in box]
        self.assertEqual(len(received), 32 * (internal + 5), "no message is lost")
        self.assertEqual(len(set(received)), len(received), "no message is read twice")
        self.assertLessEqual({r["id"] for r in external_sends}, set(received))
        report(f"{server_kind} shared-server sends", shared["sends"])
        report("external sends", [r["ms"] for r in external_sends])
        sys.stderr.write(f"\n  {server_kind} loop delay: p99 {shared['loopP99Ms']:.1f} ms, max {shared['loopMaxMs']:.1f} ms\n")
        self.assertLess(percentile(shared["sends"], 50), BURST_SEND_MS)
        self.assertLess(percentile([r["ms"] for r in external_sends], 50), BURST_SEND_MS)
        self.assertLess(shared["loopP99Ms"], BURST_LOOP_DELAY_MS)

    def test_a_python_process_serving_32_sessions_stays_responsive_while_8_mixed_processes_send(self) -> None:
        self.check_shared("python")

    def test_a_node_process_serving_32_sessions_stays_responsive_while_8_mixed_processes_send(self) -> None:
        self.check_shared("node")


if __name__ == "__main__":
    unittest.main()

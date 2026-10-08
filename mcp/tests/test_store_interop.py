from __future__ import annotations

import time
import unittest
from typing import Any, Callable

from ide_agent_tabs.clock import iso, parse_iso
from support import Worker, node_worker, py_worker, require_node, temp_home

T0 = parse_iso("2026-10-07T12:00:00Z") or 0
Spawn = Callable[[unittest.TestCase, str, "dict[str, Any]"], Worker]
SIDES: tuple[tuple[str, Spawn], ...] = (("node", node_worker), ("python", py_worker))


def out(to: str, text: str, sender: str = "tab-a", **over: Any) -> dict[str, Any]:
    return {"from": {"id": sender, "agent": "codex", "path": f"/w/{sender}"}, "to": to, "text": text, **over}


class StoreInteropTest(unittest.TestCase):
    def setUp(self) -> None:
        require_node(self)

    def ops(self, spawn: Spawn, home: str, *ops: dict[str, Any]) -> list[Any]:
        results = spawn(self, "store", {"home": home, "ops": list(ops)}).result()["results"]
        for o, r in zip(ops, results):
            if "error" in r and not o.get("mayFail"):
                self.fail(f"{o['op']} failed: {r['error']}")
        return [r.get("ok", r.get("error")) for r in results]

    def test_both_builds_create_the_same_schema_and_open_each_others_database(self) -> None:
        schemas = {}
        for name, spawn in SIDES:
            home = temp_home(self)
            schemas[name] = self.ops(spawn, home, {"op": "schema"})[0]
            other = py_worker if spawn is node_worker else node_worker
            self.ops(other, home, {"op": "send", "out": out("tab-b", f"into a {name} store")})
            self.assertEqual(self.ops(spawn, home, {"op": "peek", "id": "tab-b"})[0][0]["text"], f"into a {name} store")
        self.assertEqual(schemas["python"], schemas["node"])
        self.assertEqual(schemas["node"]["userVersion"], 1)
        self.assertEqual(schemas["node"]["journalMode"], "wal")

    def test_messages_cross_both_ways_with_the_same_shape(self) -> None:
        home = temp_home(self)
        for sender_name, sender in SIDES:
            for reader_name, reader in SIDES:
                text = f'{sender_name} to {reader_name}: caf\u00e9 \U0001f600 "quoted"\n'
                to = f"tab-{reader_name}-{sender_name}"
                sent = self.ops(sender, home, {"op": "send", "out": out(to, text, replyTo="m-0123456789abcdef", toName="peer"), "now": T0})[
                    0
                ]
                peeked = self.ops(reader, home, {"op": "peek", "id": to, "now": T0})[0]
                taken = self.ops(reader, home, {"op": "take", "id": to, "now": T0})[0]
                expected = {
                    "id": sent["id"],
                    "from": {"id": "tab-a", "agent": "codex", "path": "/w/tab-a"},
                    "to": to,
                    "text": text,
                    "replyTo": "m-0123456789abcdef",
                    "sentAt": iso(T0),
                }
                self.assertEqual(peeked, [expected])
                self.assertEqual(taken["messages"], [expected])
                self.assertEqual(self.ops(sender, home, {"op": "peek", "id": to, "now": T0})[0], [])
        node_rows = self.ops(node_worker, home, {"op": "rows"})[0]
        python_rows = self.ops(py_worker, home, {"op": "rows"})[0]
        self.assertEqual(node_rows, python_rows)
        by_sender = {r["to_id"].split("-")[-1]: r for r in node_rows}
        for column in ("sent_at", "sent_ms", "state", "state_ms", "route", "to_name", "reply_to"):
            self.assertEqual(by_sender["node"][column], by_sender["python"][column], column)

    def test_dedupe_digest_and_limits_hold_across_builds(self) -> None:
        home = temp_home(self)
        for text in ("same text", "caf\u00e9 \U0001f600 \u2028", "lone \ud800 surrogate"):
            first = self.ops(py_worker, home, {"op": "send", "out": out("tab-b", text), "now": T0})[0]
            second = self.ops(node_worker, home, {"op": "send", "out": out("tab-b", text), "now": T0 + 1})[0]
            self.assertEqual(second, {"id": first["id"], "duplicate": True}, text)
            third = self.ops(py_worker, home, {"op": "send", "out": out("tab-b", text), "now": T0 + 2})[0]
            self.assertEqual(third, {"id": first["id"], "duplicate": True}, text)
        stored = [m["text"] for m in self.ops(node_worker, home, {"op": "peek", "id": "tab-b", "now": T0})[0]]
        self.assertEqual(stored[2], "lone \ufffd surrogate")
        sends = [{"op": "send", "out": out(f"tab-{i}", f"n{i}", "tab-r"), "now": T0 + i, "mayFail": True} for i in range(10)]
        self.ops(node_worker, home, *sends)
        more = [{"op": "send", "out": out(f"tab-{i}", f"p{i}", "tab-r"), "now": T0 + 10 + i, "mayFail": True} for i in range(11)]
        results = self.ops(py_worker, home, *more)
        self.assertEqual(sum(isinstance(r, dict) for r in results), 10)
        self.assertRegex(results[-1], "20 messages in the last minute")

    def test_claims_from_one_build_settle_in_the_other(self) -> None:
        home = temp_home(self)
        self.ops(
            py_worker,
            home,
            {"op": "send", "out": out("tab-c", "one"), "now": T0},
            {"op": "send", "out": out("tab-c", "two"), "now": T0 + 1},
        )
        claimed = self.ops(node_worker, home, {"op": "claim", "id": "tab-c", "now": T0 + 2})[0]
        self.assertEqual([m["text"] for m in claimed["messages"]], ["one", "two"])
        self.assertEqual(self.ops(py_worker, home, {"op": "peek", "id": "tab-c", "now": T0 + 3})[0], [])
        self.assertEqual(
            self.ops(py_worker, home, {"op": "settle", "id": "tab-c", "claim": claimed["claim"], "how": "release", "now": T0 + 4})[0], 2
        )
        again = self.ops(py_worker, home, {"op": "claim", "id": "tab-c", "now": T0 + 5})[0]
        self.assertEqual(
            self.ops(node_worker, home, {"op": "settle", "id": "tab-c", "claim": again["claim"], "how": "ack", "now": T0 + 6})[0], 2
        )
        self.assertEqual(self.ops(node_worker, home, {"op": "summary", "id": "tab-c", "now": T0 + 7})[0], {"count": 0, "senders": []})

    def test_history_and_native_log_read_the_same_from_both_builds(self) -> None:
        home = temp_home(self)
        record = {
            "id": "m-00000000000000aa",
            "owner": "tab-b",
            "direction": "sent",
            "from": {"id": "tab-b", "name": "plugins-fa [6a3948]"},
            "to": {"name": "docs-9b [11aa22]"},
            "text": "native",
            "sentAt": iso(T0 + 1),
        }
        self.ops(
            node_worker,
            home,
            {"op": "send", "out": out("tab-b", "hello", toName="plugins-fa [6a3948]"), "now": T0},
            {"op": "logNative", "record": record},
        )
        self.ops(py_worker, home, {"op": "logNative", "record": {**record, "id": "m-00000000000000bb", "text": "native 2"}})
        queries = (
            {"op": "stored", "id": "tab-b", "names": []},
            {"op": "stored", "id": None, "names": ["docs-9b [11aa22]"]},
            {"op": "mailTo", "id": "tab-b"},
        )
        self.assertEqual(self.ops(node_worker, home, *queries), self.ops(py_worker, home, *queries))

    def test_presence_files_are_byte_identical_and_read_by_both(self) -> None:
        presence = {
            "id": "tab-p",
            "agent": "codex",
            "path": "C:\\work\\caf\u00e9",
            "pid": 1,
            "startedAt": iso(T0),
            "state": "idle",
            "reminded": [],
            "inputIdle": True,
            "beatMs": 60000,
        }
        texts = {}
        for name, spawn in SIDES:
            home = temp_home(self)
            self.ops(spawn, home, {"op": "presenceWrite", "id": "tab-p", "presence": presence})
            texts[name] = self.ops(spawn, home, {"op": "presenceText", "id": "tab-p"})[0]
            other = py_worker if spawn is node_worker else node_worker
            self.assertEqual(self.ops(other, home, {"op": "presenceRead", "id": "tab-p"})[0], presence)
        self.assertEqual(texts["python"], texts["node"])

    def test_a_waiter_in_one_build_wakes_on_a_send_from_the_other(self) -> None:
        latencies: dict[str, float] = {}
        for waiter_name, spawn in SIDES:
            home = temp_home(self)
            waiter = spawn(self, "waitOnce", {"home": home, "id": "tab-w", "timeoutMs": 10_000})
            send_args = {"home": home, "from": "tab-a", "to": "tab-w", "rounds": 1, "delayMs": 0, "tag": "x"}
            sender = py_worker(self, "wakeSend", send_args) if spawn is node_worker else node_worker(self, "wakeSend", send_args, "stress")
            waiter.line("ready")
            sender.line("ready")
            waiter.send()
            time.sleep(0.5)
            sender.send()
            committed = int(sender.line("sent ")[5:])
            result = waiter.result()
            self.assertEqual(result["message"]["text"], "wake x 0")
            latencies[waiter_name] = result["returned"] - committed
        self.assertLess(latencies["node"], 500, latencies)
        self.assertLess(latencies["python"], 500, latencies)


if __name__ == "__main__":
    unittest.main()

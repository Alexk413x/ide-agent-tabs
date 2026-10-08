from __future__ import annotations

import json
import os
import time
import unittest
from typing import Any

from ide_agent_tabs.clock import iso
from ide_agent_tabs.messaging import sessions
from support import js_fixtures, temp_home


class ParseTest(unittest.TestCase):
    def test_parse_presence_matches_node_including_key_order(self) -> None:
        for case in js_fixtures()["presence"]:
            with self.subTest(text=case["text"]):
                parsed = sessions.parse_presence(case["text"])
                self.assertEqual(parsed, case["parsed"])
                if parsed is not None:
                    self.assertEqual(list(parsed), list(case["parsed"]))

    def test_effective_state_matches_node(self) -> None:
        for case in js_fixtures()["effectiveState"]:
            with self.subTest(case=case):
                self.assertEqual(sessions.effective_state(case["presence"], case["now"]), case["state"])

    def test_agent_from_client(self) -> None:
        self.assertEqual(sessions.agent_from_client("codex-mcp-client"), "codex")
        self.assertEqual(sessions.agent_from_client("claude-code"), "claude")
        self.assertEqual(sessions.agent_from_client("Antigravity CLI"), "agy")
        self.assertEqual(sessions.agent_from_client("pi"), "pi")
        self.assertEqual(sessions.agent_from_client("my tool!"), "mytool")
        self.assertEqual(sessions.agent_from_client(None), "unknown")

    def test_is_mod_driven_only_while_the_beat_is_fresh(self) -> None:
        p = {"id": "x", "driver": "mod", "modBeat": 1_000_000}
        self.assertTrue(sessions.is_mod_driven(p, 1_000_000 + sessions.MOD_STALE_MS - 1))
        self.assertFalse(sessions.is_mod_driven(p, 1_000_000 + sessions.MOD_STALE_MS))
        self.assertFalse(sessions.is_mod_driven({"id": "x", "modBeat": 1}, 1))


class PresenceFileTest(unittest.TestCase):
    def presence(self, **over: Any) -> dict[str, Any]:
        return {"id": "tab-1", "agent": "codex", "path": "/w", "pid": os.getpid(), "startedAt": iso(0), **over}

    def test_update_writes_two_space_json_and_removes_on_none(self) -> None:
        home = temp_home(self)
        written = sessions.update_presence(home, "tab-1", lambda _: self.presence(state="idle"))
        with open(sessions.presence_path(home, "tab-1"), encoding="utf-8") as f:
            text = f.read()
        self.assertEqual(text, json.dumps(written, indent=2, separators=(",", ": ")) + "\n")
        self.assertEqual(sessions.read_presence(home, "tab-1"), written)
        self.assertIsNone(sessions.update_presence(home, "tab-1", lambda _: None))
        self.assertFalse(os.path.exists(sessions.presence_path(home, "tab-1")))

    def test_an_unchanged_result_writes_nothing(self) -> None:
        home = temp_home(self)
        sessions.update_presence(home, "tab-1", lambda _: self.presence())
        path = sessions.presence_path(home, "tab-1")
        old = time.time() - 100
        os.utime(path, (old, old))
        sessions.update_presence(home, "tab-1", lambda current: current)
        self.assertAlmostEqual(os.stat(path).st_mtime, old, delta=1)

    def test_with_state_keeps_key_order_and_stamps_the_time(self) -> None:
        p = sessions.with_state(self.presence(state="idle"), "busy", 1_000, nudges=2)
        self.assertEqual(list(p), ["id", "agent", "path", "pid", "startedAt", "state", "stateAt", "nudges"])
        self.assertEqual(p["stateAt"], "1970-01-01T00:00:01.000Z")

    def test_live_sessions_drop_dead_silent_and_stale_files(self) -> None:
        home = temp_home(self)
        now = time.time() * 1000
        sessions.update_presence(home, "alive", lambda _: self.presence(id="alive", state="busy", stateAt=iso(int(now))))
        sessions.update_presence(home, "dead", lambda _: self.presence(id="dead", pid=999_999))
        sessions.update_presence(home, "silent", lambda _: self.presence(id="silent", beatMs=1_000))
        old = time.time() - 10
        os.utime(sessions.presence_path(home, "silent"), (old, old))
        sessions.update_presence(home, "stub", lambda _: {"id": "stub"})
        ended: list[str] = []
        live = sessions.live_sessions(home, alive=lambda pid: pid == os.getpid(), now=now, ended=lambda p, at: ended.append(p["id"]))
        self.assertEqual([p["id"] for p in live], ["alive"])
        self.assertEqual(live[0]["state"], "busy")
        self.assertEqual(sorted(ended), ["dead", "silent"])
        self.assertEqual(sorted(os.listdir(os.path.join(home, sessions.SESSIONS_DIR))), ["alive.json", "stub.json"])


if __name__ == "__main__":
    unittest.main()

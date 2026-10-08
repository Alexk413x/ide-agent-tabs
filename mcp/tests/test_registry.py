from __future__ import annotations

import json
import os
import time
import unittest
from typing import Any

from ide_agent_tabs.registry import Registry, read_registry
from support import temp_home


def entry(**over: Any) -> str:
    return json.dumps(
        {
            "protocol": 1,
            "ide": "jetbrains",
            "product": "Android Studio",
            "version": "2026.2.2",
            "pid": 12345,
            "url": "http://127.0.0.1:63342/ide-agent-tabs/",
            "token": "a" * 64,
            **over,
        }
    )


class RegistryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.home = temp_home(self, "iat-reg-")
        self.dir = os.path.join(self.home, "endpoints")
        os.mkdir(self.dir)
        self.now = time.time() * 1000

    def write(self, name: str, text: str, age_ms: float = 0) -> str:
        file = os.path.join(self.dir, name)
        with open(file, "w", encoding="utf-8") as f:
            f.write(text)
        at = (self.now - age_ms) / 1000
        os.utime(file, (at, at))
        return file

    def test_reading_the_registry_drops_and_deletes_dead_entries_and_keeps_live_ones(self) -> None:
        dead = self.write("jetbrains-1.json", entry(pid=1))
        self.write("jetbrains-2.json", entry(pid=2))
        future = self.write("future-3.json", entry(pid=3, protocol=9))
        self.write("broken-4.json", "{")
        self.write("jetbrains-5.json.tmp", entry(pid=5))
        registry = read_registry(self.home, lambda pid: pid == 2)
        self.assertEqual([e.id for e in registry.endpoints], ["jetbrains-2"])
        self.assertEqual(len(registry.warnings), 1)
        self.assertFalse(os.path.exists(dead))
        self.assertTrue(os.path.exists(future), "an entry from a newer protocol is left for its IDE")
        self.assertEqual(read_registry(os.path.join(self.home, "absent")), Registry([], []))

    def test_an_endpoint_that_stopped_beating_is_dropped_and_deleted_even_when_its_pid_is_alive(self) -> None:
        silent = self.write("jetbrains-1.json", entry(beatMs=60_000), 5 * 60_000 + 5_000)
        self.write("jetbrains-2.json", entry(beatMs=60_000), 4 * 60_000)
        old = self.write("jetbrains-3.json", entry(), 24 * 3_600_000)
        registry = read_registry(self.home, lambda _pid: True, self.now)
        self.assertEqual([(e.id, e.beat_ms) for e in registry.endpoints], [("jetbrains-2", 60_000), ("jetbrains-3", None)])
        self.assertFalse(os.path.exists(silent))
        self.assertTrue(os.path.exists(old), "an entry without beatMs lives by its pid")

    def test_a_fresh_beat_still_needs_a_live_pid_and_a_bad_beat_ms_falls_back_to_the_pid_rule(self) -> None:
        fresh = self.write("jetbrains-1.json", entry(pid=1, beatMs=60_000))
        self.write("jetbrains-2.json", entry(pid=2, beatMs="x"), 24 * 3_600_000)
        registry = read_registry(self.home, lambda pid: pid == 2, self.now)
        self.assertEqual([e.id for e in registry.endpoints], ["jetbrains-2"])
        self.assertFalse(os.path.exists(fresh))


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import os
import threading
import unittest
from typing import Any

import support
from ide_agent_tabs import profiles
from ide_agent_tabs.clock import iso, now_ms
from ide_agent_tabs.jev.ledger import append_ledger, ledger_path, summarize_ledger
from ide_agent_tabs.jsjson import stringify
from ide_agent_tabs.retention import RETENTION_MS, prune_handoffs, prune_old_files, prune_tab_settings
from support import temp_home

DAY_MS = 24 * 60 * 60 * 1000
NOW = float(now_ms())


def write(path: str, text: str, age_ms: float = 0) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    if age_ms:
        old = (NOW - age_ms) / 1000
        os.utime(path, (old, old))
    return path


def record(handoff_id: str, age_ms: float, **over: Any) -> dict[str, Any]:
    created = int(NOW - age_ms)
    return {
        "id": handoff_id,
        "brief": "b",
        "oldSession": "old",
        "oldTab": "tab-old",
        "newTab": "tab-new",
        "closeAfter": True,
        "createdAt": iso(created),
        "confirmBy": iso(created + 600_000),
        **over,
    }


class HandoffRetentionTest(unittest.TestCase):
    def put(self, home: str, handoff_id: str, age_days: float, **over: Any) -> tuple[str, str]:
        folder = os.path.join(home, "handoffs")
        brief = write(os.path.join(folder, f"{handoff_id}.md"), "brief", age_days * DAY_MS)
        # The record file is fresh while its own createdAt is old, so the record's time is what counts.
        data = write(os.path.join(folder, f"{handoff_id}.json"), stringify(record(handoff_id, age_days * DAY_MS, **over), 2))
        return brief, data

    def test_old_finished_records_go_with_their_briefs_and_the_rest_stay(self) -> None:
        home = temp_home(self)
        confirmed = self.put(home, "h-000000000001", 60, confirmedAt=iso(int(NOW - 31 * DAY_MS)))
        never_closed = self.put(home, "h-000000000002", 31, closeAfter=False)
        waiting = self.put(home, "h-000000000003", 90)
        # The 30 days run from the confirmation, not from the creation.
        young = self.put(home, "h-000000000004", 60, confirmedAt=iso(int(NOW - 29 * DAY_MS)))
        prune_handoffs(home, NOW)
        for file in (*confirmed, *never_closed):
            self.assertFalse(os.path.exists(file), file)
        for file in (*waiting, *young):
            self.assertTrue(os.path.exists(file), file)

    def test_a_brief_without_a_record_goes_by_file_age(self) -> None:
        home = temp_home(self)
        folder = os.path.join(home, "handoffs")
        old = write(os.path.join(folder, "h-0000000000aa.md"), "x", 40 * DAY_MS)
        fresh = write(os.path.join(folder, "h-0000000000bb.md"), "x", DAY_MS)
        other = write(os.path.join(folder, "notes.txt"), "x", 400 * DAY_MS)
        prune_handoffs(home, NOW)
        self.assertEqual((os.path.exists(old), os.path.exists(fresh), os.path.exists(other)), (False, True, True))

    def test_an_unreadable_record_goes_by_file_age(self) -> None:
        home = temp_home(self)
        folder = os.path.join(home, "handoffs")
        old = write(os.path.join(folder, "h-0000000000cc.json"), "{", 40 * DAY_MS)
        fresh = write(os.path.join(folder, "h-0000000000dd.json"), "{", DAY_MS)
        prune_handoffs(home, NOW)
        self.assertEqual((os.path.exists(old), os.path.exists(fresh)), (False, True))

    def test_a_missing_folder_is_fine(self) -> None:
        prune_handoffs(temp_home(self), NOW)


class TabSettingsRetentionTest(unittest.TestCase):
    def test_old_merged_files_go_and_the_base_file_and_young_ones_stay(self) -> None:
        home = temp_home(self)
        folder = os.path.join(home, "mcp")
        base = write(os.path.join(folder, "claude-tab-settings.json"), "{}", 400 * DAY_MS)
        old = write(os.path.join(folder, "claude-tab-settings-0123456789abcdef.json"), "{}", 31 * DAY_MS)
        young = write(os.path.join(folder, "claude-tab-settings-fedcba9876543210.json"), "{}", 29 * DAY_MS)
        other = write(os.path.join(folder, "claude-tab-settings-notahash.json"), "{}", 400 * DAY_MS)
        prune_tab_settings(home, NOW)
        self.assertEqual([os.path.exists(f) for f in (base, old, young, other)], [True, False, True, True])

    def test_while_a_claude_session_is_live_nothing_goes_and_the_30_days_start_again(self) -> None:
        home = temp_home(self)
        old = write(os.path.join(home, "mcp", "claude-tab-settings-0123456789abcdef.json"), "{}", 400 * DAY_MS)
        prune_tab_settings(home, NOW, claude_live=True)
        self.assertTrue(os.path.exists(old))
        prune_tab_settings(home, NOW + 29 * DAY_MS)
        self.assertTrue(os.path.exists(old))
        prune_tab_settings(home, NOW + 31 * DAY_MS)
        self.assertFalse(os.path.exists(old))

    def test_reusing_a_merged_file_makes_it_new(self) -> None:
        home = temp_home(self)
        folder = os.path.join(home, "mcp")
        ours = write(os.path.join(folder, "claude-tab-settings.json"), stringify({"hooks": {"Stop": []}}))
        theirs = write(os.path.join(home, "user.json"), stringify({"model": "x"}))
        merged = profiles.merged_claude_settings(ours, theirs, None)
        assert merged is not None
        old = (NOW - 40 * DAY_MS) / 1000
        os.utime(merged, (old, old))
        self.assertEqual(profiles.merged_claude_settings(ours, theirs, None), merged)
        prune_tab_settings(home, NOW)
        self.assertTrue(os.path.exists(merged))
        self.assertGreater(os.stat(merged).st_mtime, old + 39 * DAY_MS / 1000)


def entry(age_ms: float, **over: Any) -> dict[str, Any]:
    return {"at": iso(int(NOW - age_ms)), "tool": "jev_ask", "ok": True, "model": "m", "input_tokens": 100, **over}


class LedgerRetentionTest(unittest.TestCase):
    def lines(self, home: str) -> list[str]:
        with open(ledger_path(home), encoding="utf-8") as f:
            return f.read().splitlines()

    def test_trim_drops_old_entries_keeps_the_rest_byte_for_byte_and_leaves_status_alone(self) -> None:
        home = temp_home(self)
        kept = [stringify(entry(RETENTION_MS - DAY_MS)), "not json", stringify(entry(1000))]
        for e in (entry(RETENTION_MS + DAY_MS), entry(100 * DAY_MS, ok=False)):
            append_ledger(home, e)
        with open(ledger_path(home), "a", encoding="utf-8", newline="") as f:
            f.write("\n".join(kept) + "\n")
        before = summarize_ledger(home, 1.0, NOW)
        prune_old_files(home, NOW)
        self.assertEqual(self.lines(home), kept)
        self.assertEqual(summarize_ledger(home, 1.0, NOW), before)
        self.assertEqual(os.listdir(os.path.dirname(ledger_path(home))), ["ledger.jsonl"])

    def test_trim_leaves_a_ledger_with_nothing_old_untouched(self) -> None:
        home = temp_home(self)
        append_ledger(home, entry(1000))
        before = os.stat(ledger_path(home)).st_mtime_ns
        prune_old_files(home, NOW)
        self.assertEqual(os.stat(ledger_path(home)).st_mtime_ns, before)

    def test_appends_racing_a_trim_are_not_lost(self) -> None:
        home = temp_home(self)
        for _ in range(50):
            append_ledger(home, entry(RETENTION_MS + DAY_MS))
        added = [entry(1000, tool=f"t{i}") for i in range(40)]
        failures: list[BaseException] = []

        def appender() -> None:
            try:
                for e in added:
                    append_ledger(home, e)
            except BaseException as e:  # noqa: BLE001
                failures.append(e)

        thread = threading.Thread(target=appender)
        thread.start()
        while thread.is_alive():
            prune_old_files(home, NOW)
        thread.join()
        prune_old_files(home, NOW)
        self.assertEqual(failures, [])
        self.assertEqual(self.lines(home), [stringify(e) for e in added])


class HourlyCleanTest(unittest.TestCase):
    def test_the_hourly_clean_prunes(self) -> None:
        home = temp_home(self)
        old = write(os.path.join(home, "mcp", "claude-tab-settings-0123456789abcdef.json"), "{}", 31 * DAY_MS)
        messaging = support.session(self, home, "tab-aaaa-1", "codex", 101, start=False, now=support.Clock(NOW))
        messaging._clean()
        self.assertFalse(os.path.exists(old))

    def test_the_hourly_clean_keeps_merged_settings_while_a_claude_session_is_live(self) -> None:
        home = temp_home(self)
        old = write(os.path.join(home, "mcp", "claude-tab-settings-0123456789abcdef.json"), "{}", 31 * DAY_MS)
        messaging = support.session(self, home, "tab-aaaa-1", "claude", 101, now=support.Clock(NOW))
        messaging._clean()
        self.assertTrue(os.path.exists(old))


if __name__ == "__main__":
    unittest.main()

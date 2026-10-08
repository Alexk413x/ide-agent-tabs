from __future__ import annotations

import json
import os
import re
import unittest
from typing import Any

from ide_agent_tabs import closed
from ide_agent_tabs.clock import iso, parse_iso
from ide_agent_tabs.profiles import resolve_settings
from ide_agent_tabs.resume import CHEAP_NOTE, ResumeDeps, Resumes
from ide_agent_tabs.service import ToolError
from support import temp_home

CLAUDE_ID = "0b5d2c1e-1111-4222-8333-444455556666"
CODEX_ID = "01a10626-3892-7963-938c-a326a5769d94"
NOW = parse_iso("2026-10-04T12:00:00.000Z") or 0
MINUTE = 60_000


def dirs_in(root: str) -> closed.TranscriptDirs:
    return closed.TranscriptDirs(os.path.join(root, "claude"), os.path.join(root, "codex"))


def write_claude(dirs: closed.TranscriptDirs, folder: str, session_id: str, turns: list[dict[str, Any]]) -> None:
    target = os.path.join(dirs.claude, "projects", re.sub(r"[^A-Za-z0-9]", "-", folder))
    os.makedirs(target, exist_ok=True)
    lines = [json.dumps({"type": "user", "message": {"role": "user", "content": "secret user prompt"}})]
    for t in turns:
        message = {"model": "claude-opus-5-5", "role": "assistant", "content": [{"type": "text", "text": t["text"]}], "usage": t["usage"]}
        lines.append(json.dumps({"type": "assistant", "isSidechain": False, "message": message}))
    lines.append(
        json.dumps(
            {
                "type": "assistant",
                "isSidechain": True,
                "message": {"content": [{"type": "text", "text": "sub"}], "usage": {"input_tokens": 9}},
            }
        )
    )
    with open(os.path.join(target, f"{session_id}.jsonl"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def presence(**over: Any) -> dict[str, Any]:
    return {
        "id": "tab-claude-1",
        "agent": "claude",
        "path": "/work/app",
        "pid": 4242,
        "startedAt": "2026-10-04T10:00:00.000Z",
        "owner": CLAUDE_ID,
        "model": "claude-opus-5-5",
        "effort": "high",
        "product": "IntelliJ IDEA",
        "host": "jetbrains-1",
        "nativeName": "parser-fix",
        **over,
    }


class ClosedRecordTest(unittest.TestCase):
    def test_a_claude_session_records_its_size_cache_and_preview(self) -> None:
        home = temp_home(self)
        dirs = dirs_in(temp_home(self))
        usage = {
            "input_tokens": 5,
            "cache_creation_input_tokens": 100,
            "cache_read_input_tokens": 19_895,
            "cache_creation": {"ephemeral_1h_input_tokens": 100},
        }
        write_claude(
            dirs, "/work/app", CLAUDE_ID, [{"text": "First.", "usage": {"input_tokens": 1}}, {"text": "\n  Done.  \nmore", "usage": usage}]
        )
        record = closed.record_ended(home, presence(via="ori"), NOW, dirs)
        self.assertEqual(
            record,
            {
                "id": CLAUDE_ID,
                "agent": "claude",
                "label": "Claude Code",
                "name": "parser-fix",
                "folder": "/work/app",
                "product": "IntelliJ IDEA",
                "host": "jetbrains-1",
                "model": "claude-opus-5-5",
                "effort": "high",
                "harness": "Claude Code via OpenRouter",
                "via": "ori",
                "startedAt": "2026-10-04T10:00:00.000Z",
                "endedAt": "2026-10-04T12:00:00.000Z",
                "tokens": 20_000,
                "cache": "1h",
                "preview": "Done.",
                "tab": "tab-claude-1",
            },
        )
        with open(closed.closed_path(home, CLAUDE_ID), encoding="utf-8") as f:
            self.assertEqual(json.load(f), record)

    def test_a_claude_session_without_a_transcript_is_not_recorded(self) -> None:
        self.assertIsNone(closed.record_ended(temp_home(self), presence(), NOW, dirs_in(temp_home(self))))

    def test_a_codex_session_reads_the_newest_rollout(self) -> None:
        dirs = dirs_in(temp_home(self))
        day = os.path.join(dirs.codex, "sessions", "2026", "10", "04")
        os.makedirs(day)
        lines = [
            {"payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "All set."}]}},
            {"payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 1234}}}},
        ]
        with open(os.path.join(day, f"rollout-2026-10-04T10-00-00-{CODEX_ID}.jsonl"), "w", encoding="utf-8") as f:
            f.write("\n".join(json.dumps(line) for line in lines))
        usage = closed.codex_usage(dirs, CODEX_ID)
        self.assertEqual((usage.found, usage.tokens, usage.preview), (True, 1234, "All set."))
        record = closed.closed_record(presence(agent="codex", owner="x", threadId=CODEX_ID), NOW, dirs)
        assert record is not None
        self.assertEqual((record["id"], record["tokens"], record["harness"]), (CODEX_ID, 1234, "Codex"))

    def test_read_closed_drops_old_and_sorts_newest_first(self) -> None:
        home = temp_home(self)
        os.makedirs(os.path.join(home, closed.CLOSED_DIR))
        for session_id, ended in (
            ("b", NOW - MINUTE),
            ("a", NOW - MINUTE),
            ("c", NOW - 2 * MINUTE),
            ("old", NOW - closed.CLOSED_KEEP_MS - 1),
        ):
            with open(closed.closed_path(home, session_id), "w", encoding="utf-8") as f:
                json.dump({"id": session_id, "agent": "claude", "folder": "/w", "endedAt": iso(ended)}, f)
        self.assertEqual([r["id"] for r in closed.read_closed(home, NOW)], ["a", "b", "c"])


class ResumesTest(unittest.TestCase):
    def setup(self, config: str | None = None, **record: Any) -> tuple[Resumes, list[dict[str, Any]]]:
        home = temp_home(self)
        os.makedirs(os.path.join(home, closed.CLOSED_DIR))
        saved = {
            "id": CLAUDE_ID,
            "agent": "claude",
            "folder": "/work/app",
            "endedAt": iso(NOW - 2 * MINUTE),
            "tokens": 20_000,
            "cache": "5m",
            "model": "claude-opus-5-5",
            "host": "jetbrains-1",
            "product": "IntelliJ IDEA",
            **record,
        }
        with open(closed.closed_path(home, saved["id"]), "w", encoding="utf-8") as f:
            json.dump(saved, f)
        opened: list[dict[str, Any]] = []

        def open_tab(given: dict[str, Any]) -> dict[str, Any]:
            opened.append(given)
            return {"id": "tab-9", "ide": given.get("ide", "fake"), "product": "IntelliJ IDEA"}

        deps = ResumeDeps(
            home=home,
            settings=lambda: resolve_settings(None, config),
            open_tab=open_tab,
            live_host=lambda host, product: host,
            live=list,
            now=lambda: NOW,
        )
        return Resumes(deps), opened

    def test_a_cheap_session_reopens_with_the_resume_args(self) -> None:
        resumes, opened = self.setup()
        result = resumes.resume({"id": CLAUDE_ID[:6]})
        self.assertEqual(
            opened,
            [{"path": "/work/app", "agent": "claude", "args": ["--resume", CLAUDE_ID], "model": "claude-opus-5-5", "ide": "jetbrains-1"}],
        )
        self.assertEqual(result["cost"], CHEAP_NOTE)
        self.assertEqual(
            list(result), ["resumed", "id", "agent", "folder", "tokens", "size", "age", "endedAt", "tab", "ide", "product", "model", "cost"]
        )

    def test_an_expensive_session_asks_first(self) -> None:
        resumes, opened = self.setup(tokens=80_000, endedAt=iso(NOW - 30 * MINUTE))
        result = resumes.resume({"id": CLAUDE_ID})
        self.assertEqual(opened, [])
        self.assertEqual(result["needsConfirm"], True)
        self.assertEqual(result["reasons"], ["it ended past the 5-minute prompt cache window", "it holds over 50,000 tokens"])
        confirmed = resumes.resume({"id": CLAUDE_ID, "confirm": True})
        self.assertEqual(confirmed["cost"], "the user confirmed: the full history, 80k tokens, is re-read at full input price")

    def test_resume_off_unknown_and_unsupported_sessions_are_errors(self) -> None:
        resumes, _ = self.setup('{"allowResume": false}')
        with self.assertRaisesRegex(ToolError, "resuming closed sessions is off"):
            resumes.resume({"id": CLAUDE_ID})
        resumes, _ = self.setup(agent="pi")
        with self.assertRaisesRegex(ToolError, "Pi has no resume option that Agent Tabs knows, so session 0b5d2c1e can't be reopened"):
            resumes.resume({"id": CLAUDE_ID})
        with self.assertRaisesRegex(ToolError, "no closed session with id zzzz in the last 7 days"):
            resumes.resume({"id": "zzzz"})

    def test_list_shows_the_listing_and_rows(self) -> None:
        resumes, _ = self.setup()
        listed = resumes.list()
        self.assertTrue(listed["listing"].startswith("  NAME"))
        self.assertEqual(listed["sessions"][0]["name"], "claude-0b5d")
        self.assertEqual(listed["sessions"][0]["ended"], "2m ago")
        self.assertTrue(listed["sessions"][0]["resumable"])


if __name__ == "__main__":
    unittest.main()

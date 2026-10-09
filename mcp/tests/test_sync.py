from __future__ import annotations

import json
import os
import sys
import time
import unittest

from config_support import context, fake_cli, make_source, read_json, read_text, system_env, write
from ide_agent_tabs.register import config_file
from ide_agent_tabs.server_copy import current_build, read_python
from ide_agent_tabs.sync import (
    LOCK_STALE_MS,
    next_sync_state,
    parse_sync_state,
    publish_jetbrains,
    read_bundle,
    read_sync_state,
    repository_dir,
    sync_hook,
    sync_install,
    sync_status,
    try_lock,
)
from ide_agent_tabs.sync_cli import run
from support import temp_home


class Home:
    def __init__(self, test: unittest.TestCase, vscode: str = "0.9.0", jetbrains: str = "0.9.1") -> None:
        self.root = temp_home(test, "iat-sync-")
        self.bin = os.path.join(self.root, "bin")
        self.user = os.path.join(self.root, "user")
        os.makedirs(self.bin)
        self.bundle = os.path.join(self.root, "plugin", "dist", "ide")
        self.write_bundle(vscode, jetbrains)
        self.env = system_env({"PATH": self.bin, "PI_CODING_AGENT_DIR": os.path.join(self.user, ".pi")})
        self.ctx = context(make_source(self.root), self.user, self.env)

    def write_bundle(self, vscode: str, jetbrains: str) -> None:
        write(os.path.join(self.bundle, "versions.json"), json.dumps({"vscode": vscode, "jetbrains": jetbrains}))
        write(os.path.join(self.bundle, "ide-agent-tabs.vsix"), "vsix")
        write(os.path.join(self.bundle, "ide-agent-tabs-jetbrains.zip"), f"zip {jetbrains}")

    def editor(self, name: str, installed: list[str]) -> None:
        fake_cli(self.bin, name, "editor")
        write(os.path.join(self.bin, f"{name}.json"), json.dumps(installed))

    def extensions(self, name: str) -> list[str]:
        return read_json(os.path.join(self.bin, f"{name}.json"))


class SyncHook(unittest.TestCase):
    def test_the_hook_updates_only_older_extensions_then_stays_quiet(self) -> None:
        h = Home(self)
        h.editor("code", ["alexk413x.ide-agent-tabs@0.8.0", "other.ext@1.0.0"])
        h.editor("cursor", ["alexk413x.ide-agent-tabs@0.9.5"])
        h.editor("windsurf", ["other.ext@1.0.0"])
        os.makedirs(repository_dir(h.ctx.home))
        message = sync_hook(h.ctx, h.bundle)
        self.assertEqual(
            message,
            "Agent Tabs: updated the VS Code extension to 0.9.0 in code (reload their windows); "
            "the JetBrains plugin 0.9.1 is ready in each JetBrains IDE's plugin updates.",
        )
        self.assertEqual(h.extensions("code"), ["alexk413x.ide-agent-tabs@9.9.9"])
        self.assertEqual(h.extensions("cursor"), ["alexk413x.ide-agent-tabs@0.9.5"])
        self.assertEqual(h.extensions("windsurf"), ["other.ext@1.0.0"])
        state = read_sync_state(h.ctx.home)
        assert state is not None
        self.assertEqual((state["vscode"], state["jetbrains"], state["failures"]), ("0.9.0", "0.9.1", 0))
        self.assertTrue(os.path.isfile(os.path.join(repository_dir(h.ctx.home), "ide-agent-tabs-0.9.1.zip")))
        self.assertIsNone(sync_hook(h.ctx, h.bundle))
        self.assertIsNotNone(current_build(h.ctx.home), "Claude Code tabs run their hooks from the copy")

    def test_the_hook_writes_the_claude_tab_settings_and_restores_them_when_they_change(self) -> None:
        h = Home(self)
        sync_hook(h.ctx, h.bundle)
        settings_file = os.path.join(h.ctx.home, "mcp", "claude-tab-settings.json")
        settings = read_json(settings_file)
        python = sys.executable.replace("\\", "/") if sys.platform == "win32" else sys.executable
        hook = os.path.join(h.ctx.home, "mcp", "py", "launch", "agent_hook.py")
        hook = hook.replace("\\", "/") if sys.platform == "win32" else hook
        self.assertEqual(sorted(settings), ["hooks"])
        self.assertEqual(
            settings["hooks"]["UserPromptSubmit"],
            [{"hooks": [{"type": "command", "command": python, "args": ["-I", "-S", hook, "claude", "UserPromptSubmit"], "timeout": 5}]}],
        )
        self.assertTrue(os.path.isfile(hook))
        before = read_text(settings_file)
        self.assertIsNone(sync_hook(h.ctx, h.bundle))
        write(settings_file, "{}")
        sync_hook(h.ctx, h.bundle)
        self.assertEqual(read_text(settings_file), before)
        os.remove(settings_file)
        sync_hook(h.ctx, h.bundle)
        self.assertEqual(read_text(settings_file), before)

    def test_a_failed_editor_is_logged_and_retried_up_to_three_times(self) -> None:
        h = Home(self)
        failing = "@exit /b 1\r\n" if sys.platform == "win32" else "#!/bin/sh\nexit 1\n"
        write(os.path.join(h.bin, "code.cmd" if sys.platform == "win32" else "code"), failing)
        if sys.platform != "win32":
            os.chmod(os.path.join(h.bin, "code"), 0o755)
        for attempt in (1, 2, 3):
            sync_hook(h.ctx, h.bundle)
            state = read_sync_state(h.ctx.home)
            assert state is not None
            self.assertEqual(state["failures"], attempt)
        self.assertIsNone(sync_hook(h.ctx, h.bundle))
        self.assertIn("code: ", read_text(os.path.join(h.ctx.home, "sync.log")))

    def test_the_hook_refreshes_an_existing_copy_and_moves_node_registrations(self) -> None:
        h = Home(self)
        fake_cli(h.bin, "pi")
        old = os.path.join(h.ctx.home, "mcp", "mcp-server.mjs")
        write(old, "// 0.8.0\n")
        pi = config_file("pi", h.env, h.user)
        write(pi, json.dumps({"mcpServers": {"ide-agent-tabs": {"command": "node", "args": [old.replace("\\", "/")]}}}))
        write(
            os.path.join(h.ctx.home, "synced.json"),
            json.dumps({"vscode": "0.9.0", "jetbrains": "0.9.1", "syncedAt": "", "failures": 0, "server": "abc"}),
        )
        self.assertIsNone(sync_hook(h.ctx, h.bundle))
        build = current_build(h.ctx.home)
        self.assertIsNotNone(build)
        self.assertEqual(read_python(h.ctx.home), sys.executable)
        entry = read_json(pi)["mcpServers"]["ide-agent-tabs"]
        self.assertEqual(entry["command"], sys.executable.replace("\\", "/") if sys.platform == "win32" else sys.executable)
        state = read_sync_state(h.ctx.home)
        assert state is not None
        self.assertEqual((state["server"], state["python"]), (build, sys.executable))
        self.assertFalse(os.path.exists(old))
        self.assertEqual(read_json(os.path.join(h.ctx.home, "mcp", "version.json")), {"version": h.ctx.source.version})
        before = read_text(os.path.join(h.ctx.home, "synced.json"))
        self.assertIsNone(sync_hook(h.ctx, h.bundle))
        self.assertEqual(read_text(os.path.join(h.ctx.home, "synced.json")), before, "nothing to do writes nothing")

    def test_the_node_copy_stays_until_every_registration_moved_off_it(self) -> None:
        h = Home(self)
        fake_cli(h.bin, "pi")
        folder = os.path.join(h.ctx.home, "mcp")
        old = [
            os.path.join(folder, *n.split("/"))
            for n in ("mcp-server.mjs", "agent-hook.mjs", "THIRD_PARTY_NOTICES.txt", "launch/agent-launch.sh")
        ]
        for file in old:
            write(file, "0.8.0\n")
        write(os.path.join(folder, "version.json"), '{"version":"0.8.0"}\n')
        pi = config_file("pi", h.env, h.user)
        write(pi, "{")
        self.assertIsNone(sync_hook(h.ctx, h.bundle))
        self.assertTrue(all(os.path.exists(f) for f in old))
        self.assertIn("migration: pi: ", read_text(os.path.join(h.ctx.home, "sync.log")))
        write(pi, json.dumps({"mcpServers": {"ide-agent-tabs": {"command": "node", "args": [old[0].replace("\\", "/")]}}}))
        self.assertIsNone(sync_hook(h.ctx, h.bundle))
        self.assertEqual([f for f in old if os.path.exists(f)], [])
        self.assertFalse(os.path.exists(os.path.join(folder, "launch")))
        self.assertEqual(read_json(os.path.join(folder, "version.json")), {"version": h.ctx.source.version})
        self.assertIsNotNone(current_build(h.ctx.home))

    def test_a_fresh_lock_blocks_a_second_sync_and_a_stale_one_is_taken_over(self) -> None:
        h = Home(self)
        lock = os.path.join(h.ctx.home, "sync.lock")
        self.assertTrue(try_lock(lock))
        self.assertFalse(try_lock(lock))
        self.assertIsNone(sync_hook(h.ctx, h.bundle))
        self.assertTrue(try_lock(lock, LOCK_STALE_MS, int(time.time() * 1000) + LOCK_STALE_MS + 60_000))


class Pieces(unittest.TestCase):
    def test_sync_state_parses_defensively_and_counts_failures(self) -> None:
        self.assertIsNone(parse_sync_state("{"))
        self.assertIsNone(parse_sync_state('{"vscode": 1}'))
        state = parse_sync_state('{"vscode": "1", "jetbrains": "2", "failures": "x", "server": "s", "python": "p"}')
        self.assertEqual(state, {"vscode": "1", "jetbrains": "2", "syncedAt": "", "failures": 0, "server": "s", "python": "p"})
        nxt = next_sync_state(state, {"vscode": "1", "jetbrains": "2"}, True, 0)
        self.assertEqual(
            nxt, {"vscode": "1", "jetbrains": "2", "syncedAt": "1970-01-01T00:00:00.000Z", "failures": 1, "server": "s", "python": "p"}
        )
        self.assertEqual(next_sync_state(nxt, {"vscode": "3", "jetbrains": "2"}, True, 0)["failures"], 1)

    def test_a_bundle_without_valid_versions_is_refused(self) -> None:
        h = Home(self, vscode="bad version!")
        with self.assertRaisesRegex(ValueError, "no valid vscode version"):
            read_bundle(h.bundle)

    def test_jetbrains_publishing_never_downgrades(self) -> None:
        h = Home(self, jetbrains="0.9.1")
        repo = repository_dir(h.ctx.home)
        first = publish_jetbrains(read_bundle(h.bundle), repo, h.ctx.platform)
        self.assertTrue(first["changed"])
        self.assertIn('version="0.9.1"', read_text(os.path.join(repo, "updatePlugins.xml")))
        self.assertFalse(publish_jetbrains(read_bundle(h.bundle), repo, h.ctx.platform)["changed"])
        h.write_bundle("0.9.0", "0.8.0")
        older = publish_jetbrains(read_bundle(h.bundle), repo, h.ctx.platform)
        self.assertEqual((older["version"], older["changed"]), ("0.9.1", False))
        h.write_bundle("0.9.0", "0.10.0")
        self.assertTrue(publish_jetbrains(read_bundle(h.bundle), repo, h.ctx.platform)["changed"])
        self.assertEqual(sorted(n for n in os.listdir(repo) if n.endswith(".zip")), ["ide-agent-tabs-0.10.0.zip"])

    def test_install_and_status_report_each_editor(self) -> None:
        h = Home(self)
        h.editor("code", [])
        report = sync_install(h.ctx, h.bundle, ["code", "nope"], True)
        self.assertEqual(report["errors"], ["nope: not found"])
        self.assertEqual([(e["cli"], e["ok"]) for e in report["vscode"]["editors"]], [("code", True), ("nope", False)])
        self.assertEqual(report["jetbrains"]["version"], "0.9.1")
        status = sync_status(h.ctx, h.bundle)
        self.assertEqual([(e["cli"], e["installed"]) for e in status["editors"]], [("code", "9.9.9")])
        self.assertEqual(status["jetbrains"]["version"], "0.9.1")
        self.assertTrue(status["jetbrains"]["url"].startswith("file:///"))


class Cli(unittest.TestCase):
    def test_modes_print_json_and_bad_arguments_print_usage(self) -> None:
        h = Home(self)
        out: list[str] = []
        err: list[str] = []
        self.assertEqual(run(["--agents"], h.ctx, h.bundle, out.append, err.append), 0)
        report = json.loads(out[-1])
        self.assertEqual(report["server"]["exists"], False)
        self.assertEqual(len(report["agents"]), 10)
        self.assertEqual(run(["--register"], h.ctx, h.bundle, out.append, err.append), 2)
        self.assertIn("Usage: agent-tabs sync-ides <--hook", err[-1])
        self.assertEqual(run(["--register", "claude"], h.ctx, h.bundle, out.append, err.append), 1)
        self.assertEqual(run(["--hook"], h.ctx, h.bundle, out.append, err.append), 0)
        self.assertTrue(os.path.isfile(os.path.join(h.ctx.home, "detected.json")))


if __name__ == "__main__":
    unittest.main()

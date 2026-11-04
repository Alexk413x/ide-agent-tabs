from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import unittest

from config_support import read_json, write
from ide_agent_tabs.agent_config import HookTarget, copilot_hooks
from ide_agent_tabs.copilot_config import desired_hooks, has_hooks, merge_hooks, server_entry
from ide_agent_tabs.register import agents_report, migrate_registrations, register_agents, unregister_agents
from support import ROOT, temp_home
from test_register import Setup


class CopilotConfig(unittest.TestCase):
    def test_direct_exec_keeps_windows_paths_and_home_without_shell_quoting(self) -> None:
        target = HookTarget(r"C:\Program Files\Python\python.exe", r"C:\Agent Tabs\mcp\py\launch\agent_hook.py", "win32")
        home = r"C:\Agent Tabs\100% & data"
        root = desired_hooks(target, home)
        for event, handlers in root["hooks"].items():
            self.assertEqual(
                handlers,
                [
                    {
                        "type": "command",
                        "exec": target.python,
                        "args": ["-I", "-S", target.path, "copilot", event],
                        "timeoutSec": 5,
                        "env": {"IDE_AGENT_TABS_HOME": home},
                    }
                ],
            )
        self.assertTrue(has_hooks(root, target, home))
        self.assertFalse(has_hooks(root, target, "another home"))
        self.assertEqual(server_entry([target.python, "-I", "-S", "server.py"], home)["env"]["IDE_AGENT_TABS_HOME"], home)

    def test_register_update_and_unregister_keep_foreign_handlers_and_metadata(self) -> None:
        s = Setup(self, installed=("copilot",))
        foreign = {"type": "command", "exec": "my-validator", "args": ["check"]}
        original = {"version": 1, "description": "my hooks", "hooks": {"agentStop": [foreign], "sessionEnd": [foreign]}}
        write(s.hooks("copilot"), json.dumps(original))
        self.assertEqual(register_agents(s.ctx, ["copilot"])["errors"], [])
        first = read_json(s.hooks("copilot"))
        self.assertEqual(first["hooks"]["agentStop"][0], foreign)
        self.assertEqual(first["hooks"]["sessionEnd"], [foreign])
        self.assertEqual(register_agents(s.ctx, ["copilot"])["errors"], [])
        self.assertEqual(read_json(s.hooks("copilot")), first)
        self.assertEqual(migrate_registrations(s.ctx), ([], []))
        self.assertEqual(unregister_agents(s.ctx, ["copilot"])["errors"], [])
        self.assertEqual(read_json(s.hooks("copilot")), original)

    def test_migration_repairs_home_propagation_in_both_server_and_hooks(self) -> None:
        s = Setup(self, installed=("copilot",))
        self.assertEqual(register_agents(s.ctx, ["copilot"])["errors"], [])
        config = read_json(s.file("copilot"))
        del config["mcpServers"]["ide-agent-tabs"]["env"]["IDE_AGENT_TABS_HOME"]
        write(s.file("copilot"), json.dumps(config))
        from ide_agent_tabs.register import hook_target

        write(s.hooks("copilot"), json.dumps(copilot_hooks(hook_target(s.ctx))))
        status = next(a for a in agents_report(s.ctx)["agents"] if a["agent"] == "copilot")
        self.assertFalse(status["stable"])
        self.assertFalse(status["hooks"])
        self.assertEqual(migrate_registrations(s.ctx), (["copilot"], []))
        self.assertEqual(migrate_registrations(s.ctx), ([], []))
        self.assertEqual(read_json(s.file("copilot"))["mcpServers"]["ide-agent-tabs"]["env"]["IDE_AGENT_TABS_HOME"], s.ctx.home)

    def test_malformed_hook_documents_fail_without_replacing_the_file(self) -> None:
        s = Setup(self, installed=("copilot",))
        for value in ({"version": 2, "hooks": {}}, {"version": 1, "hooks": []}, {"hooks": {"agentStop": {}}}):
            text = json.dumps(value)
            write(s.hooks("copilot"), text)
            self.assertTrue(register_agents(s.ctx, ["copilot"])["errors"])
            with open(s.hooks("copilot"), encoding="utf-8") as f:
                self.assertEqual(f.read(), text)

    def test_replacing_legacy_handlers_keeps_other_events(self) -> None:
        target = HookTarget("python", "/home/mcp/py/launch/agent_hook.py", "linux")
        legacy = {"type": "command", "exec": "node", "args": ["/home/mcp/agent-hook.mjs", "copilot", "agentStop"]}
        foreign = {"type": "command", "command": "echo audit"}
        root = {"version": 1, "hooks": {"agentStop": [legacy, foreign]}}
        merged = merge_hooks(root, "hooks.json", target, "/home")
        self.assertEqual(merged["hooks"]["agentStop"][0], foreign)
        self.assertEqual(len(merged["hooks"]["agentStop"]), 2)
        self.assertEqual(root["hooks"]["agentStop"], [legacy, foreign])


class CopilotReleaseSetup(unittest.TestCase):
    def test_standalone_setup_and_hooks_work_from_a_flattened_release_without_claude(self) -> None:
        root = temp_home(self, "iat-copilot release-")
        plugin = os.path.join(ROOT, "claude-plugin")
        for folder in ("mcp", os.path.join("dist", "launch")):
            shutil.copytree(os.path.join(plugin, folder), os.path.join(root, folder), ignore=shutil.ignore_patterns("__pycache__"))
        s = Setup(self, installed=("copilot",))
        home = os.path.join(root, "custom data")
        env = {**os.environ, **s.env, "IDE_AGENT_TABS_HOME": home}
        launch = os.path.join(root, "mcp", "launch", "agent_tabs.py")
        done = subprocess.run(
            [sys.executable, "-I", "-S", launch, "sync-ides", "--register", "copilot"],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
        report = json.loads(done.stdout)
        self.assertEqual(report["errors"], [])
        entry = read_json(s.file("copilot"))["mcpServers"]["ide-agent-tabs"]
        self.assertEqual(entry["env"]["IDE_AGENT_TABS_HOME"], home)
        self.assertTrue(os.path.isfile(entry["args"][-1]))
        hook = read_json(s.hooks("copilot"))["hooks"]["sessionStart"][0]
        # Match Copilot's hook-specific env override even when the caller uses a different data folder.
        hook_env = {**env, "IDE_AGENT_TABS_HOME": os.path.join(root, "wrong home"), **hook["env"], "IDE_AGENT_TABS_ID": "release-tab"}
        done = subprocess.run(
            [hook["exec"], *hook["args"]],
            input='{"sessionId":"release-session"}',
            env=hook_env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        from ide_agent_tabs.messaging.sessions import read_presence

        presence = read_presence(home, "release-tab")
        self.assertIsNotNone(presence)
        assert presence is not None
        self.assertEqual(presence["owner"], "release-session")
        self.assertFalse(os.path.exists(os.path.join(s.user, ".claude")))


if __name__ == "__main__":
    unittest.main()

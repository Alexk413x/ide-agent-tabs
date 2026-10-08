from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from typing import Any

from ide_agent_tabs.launch_plan import LaunchRequest, plan_launch
from ide_agent_tabs.profiles import (
    BUILTIN_PROFILES,
    CLAUDE_TAB_SETTINGS_FILE,
    AgentProfile,
    ConfigError,
    check_env,
    claude_tab_settings,
    launch_of,
)
from ide_agent_tabs.request import validate_open
from ide_agent_tabs.terminals.processes import terminal_environment
from support import temp_home

DIR = os.path.realpath(tempfile.gettempdir())


class ProfileTest(unittest.TestCase):
    def test_caller_env_wins_over_profile_env(self) -> None:
        profile = AgentProfile("p", "P", "p", env={"A": "profile", "B": "profile"})
        launch = launch_of(profile, None, [], {"A": "caller", "C": "caller"})
        self.assertEqual(launch.env, {"A": "caller", "B": "profile", "C": "caller"})

    def test_reserved_names_are_refused_in_env(self) -> None:
        for name in ("IDE_AGENT_TABS_ID", "ide_agent_tabs_agent", "JEDITERM_SOURCE", "JEDITERM_SOURCE_ARGS"):
            with self.subTest(name=name), self.assertRaisesRegex(ConfigError, name):
                check_env({name: "x"}, "env")
        for name in ("", " ", "A B", "A\tB", "A B"):
            with self.subTest(name=name), self.assertRaises(ConfigError):
                check_env({name: "x"}, "env")
        for env in ({"A": "x" * 30_001}, {"A": "a\0b"}, {f"V{i}": "" for i in range(65)}):
            with self.subTest(size=len(env)), self.assertRaises(ConfigError):
                check_env(env, "env")
        check_env({"CLAUDE_CODE_USE_BEDROCK": "1"}, "env")

    def test_a_model_for_a_profile_without_a_model_flag_is_an_error_never_ignored(self) -> None:
        base = LaunchRequest(args=[], env={}, launch_via="direct", ori=None, platform="linux")
        bare = AgentProfile("bare", "Bare", "bare")
        with self.assertRaisesRegex(ConfigError, "bare has no model option"):
            plan_launch(bare, base._replace(model="x"))
        claude = next(p for p in BUILTIN_PROFILES if p.name == "claude")
        with self.assertRaisesRegex(ValueError, "model"):
            plan_launch(claude, base._replace(model="two words"))


class ClaudeTabSettingsTest(unittest.TestCase):
    def test_a_claude_command_gets_the_settings_file_after_its_profile_args(self) -> None:
        base = LaunchRequest(args=[], env={}, launch_via="direct", ori=None, platform="linux", prompt="hi", claude_settings="/h/s.json")
        claude = next(p for p in BUILTIN_PROFILES if p.name == "claude")
        plan = plan_launch(claude, base._replace(model="opus", args=["--resume", "abc"]))
        self.assertEqual(plan.launch.args, ["--settings", "/h/s.json", "--model", "opus", "--resume", "abc"])
        bypass = claude._replace(args=("--permission-mode", "bypassPermissions"))
        self.assertEqual(plan_launch(bypass, base).launch.args, ["--permission-mode", "bypassPermissions", "--settings", "/h/s.json"])
        for command in ("/usr/local/bin/claude", r"C:\Users\a b\.local\bin\claude.exe", "CLAUDE.CMD"):
            with self.subTest(command=command):
                self.assertEqual(plan_launch(claude._replace(command=command), base).launch.args, ["--settings", "/h/s.json"])
        ori = plan_launch(claude, base._replace(launch_via="ori", ori={"agents": ["claude"]}))
        self.assertEqual((ori.via, ori.launch.args), ("ori", ["claude", "--settings", "/h/s.json"]))

    def test_other_agents_a_profile_with_its_own_settings_and_a_missing_file_get_none(self) -> None:
        base = LaunchRequest(args=[], env={}, launch_via="direct", ori=None, platform="linux", claude_settings="/h/s.json")
        claude = next(p for p in BUILTIN_PROFILES if p.name == "claude")
        for name in ("gemini", "copilot", "pi"):
            profile = next(p for p in BUILTIN_PROFILES if p.name == name)
            self.assertNotIn("--settings", plan_launch(profile, base).launch.args, name)
        self.assertEqual(plan_launch(claude._replace(command="claude-wrapper"), base).launch.args, [])
        self.assertEqual(plan_launch(claude._replace(args=("--settings=x.json",)), base).launch.args, ["--settings=x.json"])
        self.assertEqual(plan_launch(claude, base._replace(args=["--settings", "y"])).launch.args, ["--settings", "y"])
        self.assertEqual(plan_launch(claude, base._replace(claude_settings=None)).launch.args, [])

    def test_the_settings_path_needs_a_recorded_interpreter_and_the_file(self) -> None:
        home = temp_home(self)
        windows = sys.platform == "win32"
        settings = os.path.join(home, "mcp", CLAUDE_TAB_SETTINGS_FILE)
        os.makedirs(os.path.join(home, "mcp"))
        self.assertIsNone(claude_tab_settings(home, windows))
        with open(settings, "w", encoding="utf-8") as f:
            f.write("{}")
        self.assertIsNone(claude_tab_settings(home, windows))
        with open(os.path.join(home, "mcp", "python.json"), "w", encoding="utf-8") as f:
            json.dump({"python": sys.executable}, f)
        self.assertEqual(claude_tab_settings(home, windows), settings)
        os.remove(settings)
        self.assertIsNone(claude_tab_settings(home, windows))

    def test_a_terminal_started_from_a_tab_drops_the_variable_that_quiets_the_plugin_hooks(self) -> None:
        self.assertEqual(terminal_environment({"IDE_AGENT_TABS_HOOKS": "1", "KEEP": "1"}), {"KEEP": "1"})


class OpenRequestTest(unittest.TestCase):
    def test_open_requests_follow_the_ide_rules(self) -> None:
        ok = validate_open({"path": DIR, "prompt": "  ", "args": ["--yolo"], "env": {"A": "b"}, "agent": "codex"})
        self.assertEqual((ok.path, ok.agent, ok.args, ok.env, ok.prompt), (os.path.normpath(DIR), "codex", ["--yolo"], {"A": "b"}, None))
        self.assertEqual(validate_open({"path": DIR, "prompt": "hi"}).prompt, "hi")

        bad: list[dict[str, Any]] = [
            {"path": ""},
            {"path": "relative/dir"},
            {"path": os.path.join(DIR, "surely-absent-iat-dir")},
            {"path": DIR, "prompt": "x" * 30_001},
            {"path": DIR, "prompt": "a\0b"},
            {"path": DIR, "args": ["a"] * 65},
            {"path": DIR, "args": ["x" * 30_001]},
            {"path": DIR, "args": ["a\0"]},
            {"path": DIR, "env": {"IDE_AGENT_TABS_ID": "x"}},
            {"path": DIR, "agent": " "},
            {"path": DIR, "ide": ""},
            {"path": DIR, "focus": "yes"},
            {"path": DIR, "via": "teleport"},
        ]
        for given in bad:
            with self.subTest(given=str(given)[:80]), self.assertRaises(ConfigError):
                validate_open(given)

    def test_focus_passes_through_only_when_the_caller_gives_it(self) -> None:
        self.assertIsNone(validate_open({"path": DIR}).focus)
        self.assertIs(validate_open({"path": DIR, "focus": True}).focus, True)
        self.assertIs(validate_open({"path": DIR, "focus": False}).focus, False)


if __name__ == "__main__":
    unittest.main()

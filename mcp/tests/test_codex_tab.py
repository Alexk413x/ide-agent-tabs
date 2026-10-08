from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import unittest
from typing import Any

from ide_agent_tabs.mcp_tools import HOOK_TOOL, SERVER_NAME
from ide_agent_tabs.messaging.hook import HOOK_EVENTS
from ide_agent_tabs.profiles import BUILTIN_PROFILES, CODEX_TAB_ARGS
from support import ROOT, temp_home

HOOK_TIMEOUT_S = 10
INTERRUPT_TIMEOUT_S = 3


def hook_input(event: str) -> dict[str, str]:
    return {"event": event, "session_id": "${session_id}", "turn_id": "${turn_id}"}


HOOKS: list[tuple[str, str, dict[str, str], int]] = [
    ("UserPromptSubmit", "user_prompt_submit", hook_input("UserPromptSubmit"), HOOK_TIMEOUT_S),
    ("PostToolUse", "post_tool_use", hook_input("PostToolUse"), HOOK_TIMEOUT_S),
    ("PermissionRequest", "permission_request", hook_input("PermissionRequest"), HOOK_TIMEOUT_S),
    ("Stop", "stop", hook_input("Stop"), HOOK_TIMEOUT_S),
    ("Interrupt", "interrupt", hook_input("Interrupt"), INTERRUPT_TIMEOUT_S),
]

# Codex's hook_key for the -c layer: its synthetic source path resolved against "/" or "C:\".
SESSION_FLAG_SOURCES = ["/<session-flags>/config.toml", "C:\\<session-flags>\\config.toml"]


def trusted_hash(key: str, given: dict[str, str], timeout: int) -> str:
    # codex-rs hooks discovery: hook_hash, then config version_for_toml (SHA-256 of key-sorted compact JSON).
    identity: dict[str, Any] = {
        "event_name": key,
        "hooks": [{"type": "mcp_tool", "server": SERVER_NAME, "tool": HOOK_TOOL, "input": given, "timeout": timeout}],
    }
    text = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def table(given: dict[str, str]) -> str:
    return "{ " + ", ".join(f"{k} = '{v}'" for k, v in given.items()) + " }"


class CodexTabTest(unittest.TestCase):
    def test_the_codex_profile_launches_in_process_with_its_own_server_and_trusted_hooks(self) -> None:
        codex = next(p for p in BUILTIN_PROFILES if p.name == "codex")
        self.assertEqual(codex.args, CODEX_TAB_ARGS)
        self.assertEqual(CODEX_TAB_ARGS[0], "--no-daemon")

        flags = CODEX_TAB_ARGS[1:]
        self.assertTrue(all((i % 2 == 0) == (a == "-c") for i, a in enumerate(flags)))
        overrides = [a for i, a in enumerate(flags) if i % 2 == 1]
        self.assertRegex(
            overrides[0],
            r"\Amcp_servers\.ide-agent-tabs=\{ command = 'python3', args = \['-I', '-S', '-c', '''[^\n]+'''\], "
            r"env_vars = \['IDE_AGENT_TABS_ID', 'IDE_AGENT_TABS_AGENT', 'IDE_AGENT_TABS_HOME'\], tool_timeout_sec = 660 \}\Z",
        )

        hooks = [
            f"hooks.{event}=[{{ hooks = [{{ type = 'mcp_tool', server = '{SERVER_NAME}', tool = '{HOOK_TOOL}', "
            f"input = {table(given)}, timeout = {timeout} }}] }}]"
            for event, _, given, timeout in HOOKS
        ]
        self.assertEqual(overrides[1:-1], hooks)

        state = [
            f"'{source}:{key}:0:0' = {{ trusted_hash = '{trusted_hash(key, given, timeout)}' }}"
            for _, key, given, timeout in HOOKS
            for source in SESSION_FLAG_SOURCES
        ]
        self.assertEqual(overrides[-1], "hooks.state={ " + ", ".join(state) + " }")

    def test_the_hooked_events_are_the_ones_the_hook_tool_handles(self) -> None:
        self.assertEqual(sorted(event for event, _, _, _ in HOOKS), sorted(HOOK_EVENTS["codex"]))

    def test_the_server_one_liner_runs_the_shared_copy_from_the_agent_tabs_home(self) -> None:
        match = re.search(r"'''(.+)'''", CODEX_TAB_ARGS[2])
        assert match is not None
        code = match.group(1)
        root = temp_home(self, "iat-codex-tab-")
        user = os.path.join(root, "user")

        def stub(home: str) -> str:
            return os.path.join(home, "mcp", "py", "launch", "mcp_server.py")

        for home in (os.path.join(root, "custom"), os.path.join(user, ".ide-agent-tabs")):
            os.makedirs(os.path.dirname(stub(home)))
            with open(stub(home), "w", encoding="utf-8") as f:
                f.write("import sys\nsys.stdout.write(__file__)\n")

        def run(home_var: str) -> str:
            env = {**os.environ, "HOME": user, "USERPROFILE": user, "IDE_AGENT_TABS_HOME": home_var}
            result = subprocess.run(
                [sys.executable, "-I", "-S", "-c", code], env=env, capture_output=True, check=False, encoding="utf-8", timeout=60
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout

        self.assertEqual(run(os.path.join(root, "custom")), stub(os.path.join(root, "custom")))
        self.assertEqual(run(""), stub(os.path.join(user, ".ide-agent-tabs")))

    def test_no_argument_can_be_changed_by_windows_power_shell_5_1_or_cmd_exe_on_its_way_to_codex(self) -> None:
        for arg in CODEX_TAB_ARGS:
            with self.subTest(arg=arg[:60]):
                self.assertNotRegex(arg, r"[\"%!\n]")
                self.assertFalse(arg.endswith("\\"))
                if re.search(r"[&|<>^()]", arg):
                    self.assertRegex(arg, r"\s", "an argument with a cmd.exe metacharacter must be quoted, so it needs a space")

    def test_the_vs_code_and_jetbrains_profiles_hold_the_same_codex_arguments(self) -> None:
        with open(os.path.join(ROOT, "vscode", "src", "profiles.ts"), encoding="utf-8") as f:
            vscode = f.read()
        kotlin_file = os.path.join(ROOT, "jetbrains", "src", "main", "kotlin", "dev", "alexk", "ideagenttabs", "AgentProfiles.kt")
        with open(kotlin_file, encoding="utf-8") as f:
            kotlin = f.read()
        for arg in CODEX_TAB_ARGS:
            literal = json.dumps(arg, ensure_ascii=False)
            with self.subTest(arg=arg[:60]):
                self.assertIn(literal, vscode, "vscode/src/profiles.ts lacks the argument")
                self.assertIn(literal.replace("$", "\\$"), kotlin, "AgentProfiles.kt lacks the argument")


if __name__ == "__main__":
    unittest.main()

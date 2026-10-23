from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import unittest
from typing import Any

import source_values
from ide_agent_tabs.launch_plan import LaunchRequest, plan_launch
from ide_agent_tabs.mcp_tools import HOOK_TOOL, SERVER_NAME
from ide_agent_tabs.messaging.hook import HOOK_EVENTS
from ide_agent_tabs.profiles import BUILTIN_PROFILES, CODEX_TAB_ARGS, codex_http, codex_http_arg, with_codex_http
from ide_agent_tabs.shared.headers import option_env
from support import temp_home

WINDOWS = sys.platform == "win32"
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
        self.assertEqual(source_values.typescript().value("CODEX_TAB_ARGS"), list(CODEX_TAB_ARGS))
        self.assertEqual(source_values.kotlin().value("CODEX_TAB_ARGS"), list(CODEX_TAB_ARGS))


class CodexHttpTest(unittest.TestCase):
    def home(self, port: int | None = 47828, stub: bool = True, python: str | None = None) -> str:
        home = temp_home(self, "iat-codex-http-")
        mcp = os.path.join(home, "mcp")
        os.makedirs(os.path.join(mcp, "py", "launch"))
        if python is None:
            python = os.path.join(home, "python.exe")
            with open(python, "w", encoding="utf-8") as f:
                f.write("")
        with open(os.path.join(mcp, "python.json"), "w", encoding="utf-8") as f:
            json.dump({"python": python}, f)
        if port is not None:
            with open(os.path.join(mcp, "port.json"), "w", encoding="utf-8") as f:
                json.dump({"port": port}, f)
        if stub:
            with open(os.path.join(mcp, "py", "launch", "headers.py"), "w", encoding="utf-8") as f:
                f.write("")
        return home

    def test_a_tab_uses_the_shared_server_when_the_copy_the_port_and_the_interpreter_are_recorded(self) -> None:
        home = self.home(port=47831)
        http = codex_http(home, WINDOWS, "tab-1", "codex")
        assert http is not None
        self.assertEqual((http.port, http.tab, http.agent, http.home), (47831, "tab-1", "codex", home))
        arg = codex_http_arg(http)
        self.assertTrue(arg.startswith("mcp_servers.ide-agent-tabs={ url = 'http://127.0.0.1:47831/mcp', http_headers_helper = '"))
        self.assertIn(" -I -S ", arg)
        self.assertTrue(
            arg.endswith(f"--tab tab-1 --agent codex --port 47831 --home {home}', startup_timeout_sec = 30, tool_timeout_sec = 660 }}")
        )

    def test_a_tab_stays_on_the_stdio_server_without_a_port_a_headers_stub_or_a_safe_path(self) -> None:
        self.assertIsNone(codex_http(self.home(port=None), WINDOWS, "tab-1", "codex"))
        self.assertIsNone(codex_http(self.home(stub=False), WINDOWS, "tab-1", "codex"))
        self.assertIsNone(codex_http(self.home(port=47828), WINDOWS, "tab 1", "codex"))
        spaced = self.home(python=os.path.join(temp_home(self, "iat-sp ace-"), "python.exe"))
        self.assertIsNone(codex_http(spaced, WINDOWS, "tab-1", "codex"))

    def test_only_the_server_argument_changes_and_nothing_in_it_needs_quoting_through_a_shell(self) -> None:
        http = codex_http(self.home(), WINDOWS, "tab-1", "codex-ollama")
        assert http is not None
        swapped = with_codex_http(CODEX_TAB_ARGS, http)
        assert swapped is not None
        self.assertEqual([a for a, b in zip(swapped, CODEX_TAB_ARGS) if a != b], [codex_http_arg(http)])
        self.assertIsNone(with_codex_http(CODEX_TAB_ARGS, None))
        self.assertIsNone(with_codex_http(("--other",), http))
        self.assertNotRegex(codex_http_arg(http), r"[\"%!\n]")

    def test_the_local_codex_tabs_use_the_shared_server_as_codex_does(self) -> None:
        home = self.home()
        for name, provider in (("codex-lmstudio", "lmstudio"), ("codex-ollama", "ollama")):
            with self.subTest(name=name):
                profile = next(p for p in BUILTIN_PROFILES if p.name == name)
                http = codex_http(home, WINDOWS, "tab-1", name)
                assert http is not None
                request = LaunchRequest(args=[], env={}, launch_via="direct", ori=None, platform="linux", model="qwen3", http=http)
                args = plan_launch(profile, request).launch.args
                self.assertIn(codex_http_arg(http), args)
                self.assertTrue(
                    codex_http_arg(http).endswith(
                        f"--agent {name} --port 47828 --home {home}', startup_timeout_sec = 30, tool_timeout_sec = 660 }}"
                    )
                )
                self.assertEqual(args[-5:], ["--oss", "--local-provider", provider, "-m", "qwen3"])

    def test_the_helper_takes_the_tab_the_agent_the_port_and_the_home_as_arguments(self) -> None:
        got = option_env(["--tab", "t1", "--agent", "codex", "--port", "47831", "--home", "/h", "--other", "x", "--dangling"])
        self.assertEqual(
            got,
            {
                "IDE_AGENT_TABS_ID": "t1",
                "IDE_AGENT_TABS_AGENT": "codex",
                "CLAUDE_PLUGIN_OPTION_SERVER_PORT": "47831",
                "IDE_AGENT_TABS_HOME": "/h",
            },
        )
        self.assertEqual(option_env([]), {})


if __name__ == "__main__":
    unittest.main()

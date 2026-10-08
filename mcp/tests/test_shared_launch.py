from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import unittest

from ide_agent_tabs.shared.client import probe, stop_server
from ide_agent_tabs.shared.state import read_token
from shared_support import free_port, reset_store_mode, serve
from store_host import StoreHost
from support import ROOT, temp_home
from test_hook import git_bash

PLUGIN = os.path.join(ROOT, "claude-plugin")
PLACEHOLDER = re.compile(r"\$\{([^}]*)\}")


def mcp_entry() -> dict[str, object]:
    with open(os.path.join(PLUGIN, ".mcp.json"), encoding="utf-8") as f:
        return json.load(f)["mcpServers"]["ide-agent-tabs"]


def clean_env(home: str, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("IDE_AGENT_TABS", "CLAUDE_PLUGIN", "CLAUDE_CODE_MCP"))}
    env["IDE_AGENT_TABS_HOME"] = home
    env.update(extra)
    return env


class McpJsonTest(unittest.TestCase):
    def test_the_entry_keeps_the_shape_sentinel_swarm_copies(self) -> None:
        entry = mcp_entry()
        self.assertEqual(entry["type"], "http")
        self.assertEqual(entry["url"], "http://127.0.0.1:${user_config.server_port}/mcp")
        self.assertEqual(entry["timeout"], 300000)
        helper = str(entry["headersHelper"])
        self.assertEqual(set(PLACEHOLDER.findall(helper)), {"CLAUDE_PLUGIN_ROOT"})
        self.assertNotIn("node", helper.split())
        self.assertEqual([part.split()[0] for part in helper.split("||")], ["py", "python3", "python"])

    def test_the_helper_command_runs_in_the_shell_claude_code_uses_and_prints_the_token(self) -> None:
        home = temp_home(self, "iat-launch-")
        server = serve(self, home, StoreHost(home), version="99.0.0")
        command = str(mcp_entry()["headersHelper"]).replace("${CLAUDE_PLUGIN_ROOT}", PLUGIN.replace("\\", "/"))
        # Claude Code hands cmd.exe the command line as is, which list2cmdline's quoting would change.
        shell: str | list[str] = f'cmd.exe /d /s /c "{command}"' if sys.platform == "win32" else ["/bin/sh", "-c", command]
        env = clean_env(home, CLAUDE_CODE_MCP_SERVER_URL=f"http://127.0.0.1:{server.port}/mcp")
        done = subprocess.run(shell, env=env, capture_output=True, timeout=60, check=False)
        headers = json.loads(done.stdout.decode("utf-8").strip().splitlines()[-1])
        self.assertEqual(headers["Authorization"], f"Bearer {server.token}")
        self.assertRegex(headers["X-Agent-Tabs-Pid"], r"^\d+$")


def server_hook_commands() -> dict[str, str]:
    with open(os.path.join(PLUGIN, "hooks", "hooks.json"), encoding="utf-8") as f:
        hooks = json.load(f)["hooks"]
    return {event: hooks[event][0]["hooks"][0]["command"] for event in ("SessionStart", "SessionEnd")}


def shells() -> list[tuple[str, list[str]]]:
    found: list[tuple[str, list[str]]] = []
    posix = git_bash() if sys.platform == "win32" else "/bin/sh"
    if posix is not None:
        found.append(("sh", [posix, "-c"]))
    if sys.platform == "win32":
        for name in ("powershell", "pwsh"):
            path = shutil.which(name)
            if path is not None:
                found.append((name, [path, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command"]))
    return found


class ServerHookShellTest(unittest.TestCase):
    def test_the_server_hooks_come_first_and_run_the_python_hook(self) -> None:
        commands = server_hook_commands()
        self.assertEqual(commands["SessionStart"], 'set -- server SessionStart; . "${CLAUDE_PLUGIN_ROOT}/mcp/launch/server-hook.ps1"')
        self.assertEqual(commands["SessionEnd"], 'set -- server SessionEnd; . "${CLAUDE_PLUGIN_ROOT}/mcp/launch/server-hook.ps1"')

    def test_the_hook_commands_start_the_server_and_end_a_session_in_every_shell(self) -> None:
        root = PLUGIN.replace("\\", "/")
        commands = {event: command.replace("${CLAUDE_PLUGIN_ROOT}", root) for event, command in server_hook_commands().items()}
        found = shells()
        if not found:
            self.skipTest("no shell")
        for name, shell in found:
            with self.subTest(shell=name):
                home = temp_home(self, "iat-launch-")
                port = free_port()
                self.addCleanup(lambda home=home, port=port: stop_server(home, port))
                env = clean_env(home, CLAUDE_PLUGIN_ROOT=PLUGIN.replace("\\", "/"), CLAUDE_PLUGIN_OPTION_SERVER_PORT=str(port))
                done = subprocess.run(
                    [*shell, commands["SessionStart"]], input=b"{}", env=env, capture_output=True, timeout=60, check=False
                )
                self.assertEqual((done.returncode, done.stdout), (0, b""), done.stderr)
                self.assertEqual(probe(port).kind, "ours")
                self.assertIsNotNone(read_token(home))
                env["CLAUDE_PID"] = "1"
                end = commands["SessionEnd"]
                done = subprocess.run([*shell, end], input=b'{"reason":"other"}', env=env, capture_output=True, timeout=60, check=False)
                self.assertEqual((done.returncode, done.stdout), (0, b""), done.stderr)


def tearDownModule() -> None:
    reset_store_mode()


if __name__ == "__main__":
    unittest.main()

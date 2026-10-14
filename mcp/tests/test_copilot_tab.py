from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import unittest
from typing import Any

from ide_agent_tabs.launch_plan import LaunchRequest, plan_launch
from ide_agent_tabs.messaging.hook import run_hook
from ide_agent_tabs.messaging.sessions import read_presence, tab_owner, update_presence
from ide_agent_tabs.profiles import (
    BUILTIN_PROFILES,
    CODEX_TAB_ARGS,
    CWD_ENV,
    CopilotHttp,
    copilot_http,
    cwd_header,
    is_copilot_command,
    with_copilot_http,
)
from ide_agent_tabs.server_copy import copilot_config, copilot_config_port, write_copilot_config
from ide_agent_tabs.shared.hub import Identity, session_key
from shared_support import PROTOCOL, McpHttp, request, serve
from store_host import StoreHost
from support import ROOT, temp_home
from test_service import Setup

WINDOWS = sys.platform == "win32"
COPILOT = next(p for p in BUILTIN_PROFILES if p.name == "copilot")
CODEX = next(p for p in BUILTIN_PROFILES if p.name == "codex")
TOKEN = "ab" * 32
HOOK_SCRIPT = os.path.join(ROOT, "claude-plugin", "mcp", "launch", "agent_hook.py")


def _write(path: str, value: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(value, f)


class CopilotHttpTest(unittest.TestCase):
    def home(self, port: int | None = 47951, config_port: int | None = 47951, prefix: str = "iat-copilot-http-") -> str:
        home = temp_home(self, prefix)
        if port is not None:
            _write(os.path.join(home, "mcp", "port.json"), {"port": port})
        if config_port is not None:
            write_copilot_config(home, config_port, TOKEN)
        return home

    def test_a_tab_uses_the_shared_server_when_the_port_the_config_and_a_running_server_agree(self) -> None:
        home = self.home()
        asked: list[int] = []
        http = copilot_http(home, WINDOWS, lambda port: asked.append(port) or True)
        assert http is not None
        self.assertEqual(asked, [47951])
        self.assertEqual(http.port, 47951)
        expected = os.path.join(home, "mcp", "copilot-http.json")
        self.assertEqual(http.config, expected.replace("\\", "/") if WINDOWS else expected)

    def test_a_tab_stays_on_the_stdio_server_without_a_server_a_port_a_matching_config_or_a_safe_path(self) -> None:
        self.assertIsNone(copilot_http(self.home(), WINDOWS, lambda _p: False))
        self.assertIsNone(copilot_http(self.home(port=None), WINDOWS, lambda _p: True))
        self.assertIsNone(copilot_http(self.home(config_port=None), WINDOWS, lambda _p: True))
        self.assertIsNone(copilot_http(self.home(config_port=47952), WINDOWS, lambda _p: True))
        self.assertIsNone(copilot_http(self.home(prefix="iat-copilot sp ace-"), WINDOWS, lambda _p: True))

    def test_the_health_check_asks_the_recorded_port_and_refuses_a_closed_one(self) -> None:
        server = serve(self, temp_home(self, "iat-copilot-srv-"), StoreHost(temp_home(self, "iat-copilot-store-")))
        self.assertIsNotNone(copilot_http(server.home, WINDOWS))
        server.stop("test")
        self.assertIsNone(copilot_http(server.home, WINDOWS))

    def test_the_option_names_the_config_file_and_passes_the_folder_in_the_environment(self) -> None:
        http = CopilotHttp("C:/h/mcp/copilot-http.json", 47951)
        folder = "C:\\work\\my repo\\100%"
        plan = plan_launch(COPILOT, LaunchRequest([], {"X": "1"}, "direct", None, "win32", prompt="hi", copilot=http, cwd=folder))
        launch = plan.launch
        self.assertEqual(launch.args, ["--additional-mcp-config", "@C:/h/mcp/copilot-http.json", "-i"])
        self.assertEqual(launch.env, {"X": "1", CWD_ENV: "C:\\work\\my repo\\100%25"})
        self.assertEqual(launch.prompt, "hi")
        for arg in launch.args:
            self.assertNotRegex(arg, r"[\s\"%]")
        without = plan_launch(COPILOT, LaunchRequest([], {}, "direct", None, "win32", cwd=folder)).launch
        self.assertEqual((without.args, without.env), ([], {}))

    def test_only_a_copilot_command_takes_the_option_and_codex_keeps_its_arguments(self) -> None:
        http = CopilotHttp("/h/mcp/copilot-http.json", 47951)
        for command in ("copilot", "COPILOT.CMD", "C:\\npm\\copilot.ps1", "/usr/local/bin/copilot"):
            self.assertTrue(is_copilot_command(command), command)
        for command in ("codex", "copilot-x", "notcopilot"):
            self.assertFalse(is_copilot_command(command), command)
        codex = plan_launch(CODEX, LaunchRequest([], {}, "direct", None, "linux", copilot=http, cwd="/w")).launch
        self.assertEqual((codex.args, codex.env), (list(CODEX_TAB_ARGS), {}))
        self.assertEqual(with_copilot_http("copilot", ["--a"], {}, None, "/w"), (("--a",), {}))
        self.assertEqual(with_copilot_http("copilot", [], {}, http, None), (("--additional-mcp-config", "@/h/mcp/copilot-http.json"), {}))

    def test_the_folder_header_decodes_back_to_the_folder(self) -> None:
        from ide_agent_tabs.shared.front import percent_decoded

        for folder in ("/home/a b/c", "C:\\Users\\Zoë\\${HOME}\\%x", "/tmp/日本"):
            encoded = cwd_header(folder)
            self.assertNotIn("$", encoded)
            self.assertTrue(encoded.isascii())
            self.assertEqual(percent_decoded(encoded), folder)

    def test_the_config_file_holds_the_token_the_url_and_the_tab_variables_and_is_private(self) -> None:
        home = self.home(port=None, config_port=None)
        write_copilot_config(home, 47951, TOKEN)
        path = os.path.join(home, "mcp", "copilot-http.json")
        with open(path, encoding="utf-8") as f:
            written = json.load(f)
        self.assertEqual(written, copilot_config(47951, TOKEN))
        entry = written["mcpServers"]["ide-agent-tabs"]
        self.assertEqual((entry["type"], entry["url"], entry["tools"]), ("http", "http://127.0.0.1:47951/mcp", ["*"]))
        self.assertEqual(
            entry["headers"],
            {
                "Authorization": f"Bearer {TOKEN}",
                "X-Agent-Tabs-Tab": "${IDE_AGENT_TABS_ID}",
                "X-Agent-Tabs-Agent": "${IDE_AGENT_TABS_AGENT}",
                "X-Agent-Tabs-Cwd": "${IDE_AGENT_TABS_CWD}",
            },
        )
        if not WINDOWS:
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        write_copilot_config(home, 47952, "cd" * 32)
        self.assertEqual(copilot_config_port(home), 47952)
        with open(path, encoding="utf-8") as f:
            self.assertIn("Bearer " + "cd" * 32, f.read())

    def test_a_terminal_tab_connects_while_the_server_runs_and_falls_back_after_it_stops(self) -> None:
        s = Setup(self)
        server = serve(self, s.home, StoreHost(s.home))
        s.service.open_tab({"path": s.work, "agent": "copilot", "ide": "fake-term"})
        spec = s.terminal.opened[-1][0]
        config = os.path.join(s.home, "mcp", "copilot-http.json")
        self.assertEqual(spec.args, ["--additional-mcp-config", f"@{config}"])
        self.assertEqual(spec.env, {CWD_ENV: cwd_header(s.work)})
        server.stop("test")
        s.service.open_tab({"path": s.work, "agent": "copilot", "ide": "fake-term"})
        self.assertEqual((s.terminal.opened[-1][0].args, s.terminal.opened[-1][0].env), ([], {}))

    def test_the_server_writes_the_config_when_it_claims_its_port(self) -> None:
        home = temp_home(self, "iat-copilot-srv-")
        server = serve(self, home, StoreHost(home))
        self.assertEqual(copilot_config_port(home), server.port)
        with open(os.path.join(home, "mcp", "copilot-http.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f), copilot_config(server.port, server.token))


def _modern(port: int, token: str, method: str, params: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    meta = {"io.modelcontextprotocol/protocolVersion": PROTOCOL, "io.modelcontextprotocol/clientCapabilities": {}}
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": {**params, "_meta": meta}})
    sent = {
        "Authorization": f"Bearer {token}",
        "Accept": "text/event-stream, application/json",
        "Content-Type": "application/json",
        "mcp-protocol-version": PROTOCOL,
        "mcp-method": method,
        **headers,
    }
    if method == "tools/call":
        sent["mcp-name"] = params["name"]
    return request(port, "/mcp", headers=sent, body=body).json


class CopilotSessionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.home = temp_home(self, "iat-copilot-session-")
        self.dead: set[int] = set()
        self.host = StoreHost(self.home, alive=lambda pid: pid not in self.dead and pid > 0)
        self.server = serve(self, self.home, self.host, alive=lambda pid: pid not in self.dead)

    def call(self, tab: str, cwd: str | None = None, agent: str = "copilot") -> dict[str, Any]:
        headers = {"X-Agent-Tabs-Tab": tab, "X-Agent-Tabs-Agent": agent}
        if cwd is not None:
            headers["X-Agent-Tabs-Cwd"] = cwd_header(cwd)
        return _modern(self.server.port, self.server.token, "tools/call", {"name": "list_sessions", "arguments": {}}, headers)

    def test_a_client_without_roots_gets_no_roots_request_and_its_folder_comes_from_the_header(self) -> None:
        folder = temp_home(self, "iat-copilot-cwd ")
        result = self.call("tab-cop-1", folder)["result"]
        self.assertEqual(result["resultType"], "complete")
        me = next(s for s in json.loads(result["content"][0]["text"])["sessions"] if s["self"])
        self.assertEqual(me["id"], "tab-cop-1")
        self.assertEqual(os.path.normcase(os.path.realpath(me["path"])), os.path.normcase(os.path.realpath(folder)))
        unset = self.call("tab-cop-2", None)["result"]
        self.assertEqual(unset["resultType"], "complete")
        me = next(s for s in json.loads(unset["content"][0]["text"])["sessions"] if s["self"])
        self.assertEqual(me["path"], os.path.expanduser("~"))

    def test_a_client_with_roots_is_still_asked_for_them(self) -> None:
        client = McpHttp(self.server.port, self.server.token, {"X-Agent-Tabs-Tab": "tab-cop-roots"})
        first = client.rpc("tools/call", {"name": "list_sessions", "arguments": {}})
        self.assertEqual(first.json["result"]["resultType"], "input_required")

    def test_the_tool_list_follows_the_agent_header(self) -> None:
        def names(agent: str | None) -> list[str]:
            headers = {} if agent is None else {"X-Agent-Tabs-Agent": agent}
            return [t["name"] for t in _modern(self.server.port, self.server.token, "tools/list", {}, headers)["result"]["tools"]]

        self.assertNotIn("agent_tabs_mod", names("copilot"))
        self.assertIn("list_sessions", names("copilot"))
        self.assertIn("agent_tabs_mod", names("claude"))
        self.assertIn("agent_tabs_mod", names(None))

    def test_a_tab_alone_keys_the_session_and_its_recorded_copilot_process_owns_it(self) -> None:
        self.assertEqual(session_key(Identity(tab="tab-x")), "tab:tab-x")
        self.assertEqual(session_key(Identity(client="c", tab="tab-x")), "client:c")
        owner = os.getppid()
        update_presence(self.home, "tab-owned", lambda _c: {"id": "tab-owned", "agentPid": owner, "agentPidStart": 99})
        self.assertEqual(tab_owner(self.home, "tab-owned"), (owner, 99))
        self.assertEqual(self.call("tab-owned")["result"]["resultType"], "complete")
        binding = self.host.bound[-1].binding
        self.assertEqual((binding.pid, binding.pid_start, binding.tab, binding.agent), (owner, 99, "tab-owned", "copilot"))
        self.assertEqual(self.server.hub.live_sessions(), 1)
        self.dead.add(owner)
        self.assertEqual(self.server.hub.sweep(), 1)
        self.assertEqual(self.host.ended, ["tab-owned"])
        self.assertEqual(self.server.hub.size, 0)

    def test_without_a_record_or_with_a_dead_process_the_session_stays_on_the_server_pid(self) -> None:
        self.assertEqual(self.call("tab-unowned")["result"]["resultType"], "complete")
        self.assertEqual(self.host.bound[-1].binding.pid, os.getpid())
        update_presence(self.home, "tab-dead", lambda _c: {"id": "tab-dead", "agentPid": 999_999_991})
        self.dead.add(999_999_991)
        self.assertEqual(self.call("tab-dead")["result"]["resultType"], "complete")
        self.assertEqual(self.host.bound[-1].binding.pid, os.getpid())
        self.assertEqual(self.server.hub.live_sessions(), 0)
        self.assertEqual(self.server.hub.sweep(), 0)


class CopilotHookTest(unittest.TestCase):
    def test_the_start_hook_records_the_copilot_process_and_later_hooks_keep_it(self) -> None:
        home = temp_home(self, "iat-copilot-hook-")
        run_hook("copilot", "sessionStart", {"sessionId": "s1"}, home, "tab-h", agent_process=(4321, 77))
        self.assertEqual(tab_owner(home, "tab-h"), (4321, 77))
        run_hook("copilot", "userPromptSubmitted", {"sessionId": "s1"}, home, "tab-h", agent_process=(9, 9))
        run_hook("copilot", "postToolUse", {"sessionId": "s1"}, home, "tab-h")
        self.assertEqual(tab_owner(home, "tab-h"), (4321, 77))
        run_hook("copilot", "sessionStart", {"sessionId": "s2"}, home, "tab-h2")
        self.assertIsNone(tab_owner(home, "tab-h2"))
        presence = read_presence(home, "tab-h2")
        assert presence is not None
        self.assertNotIn("agentPid", presence)

    def test_the_hook_script_finds_the_process_that_ran_it_only_for_the_start_event(self) -> None:
        home = temp_home(self, "iat-copilot-hook-")
        clean = {k: v for k, v in os.environ.items() if not k.startswith("IDE_AGENT_TABS")}
        for tab, event in (("tab-s1", "sessionStart"), ("tab-s2", "postToolUse")):
            env = {**clean, "IDE_AGENT_TABS_HOME": home, "IDE_AGENT_TABS_ID": tab}
            subprocess.run([sys.executable, "-I", "-S", HOOK_SCRIPT, "copilot", event], input=b"{}", env=env, timeout=30, check=True)
        owner = tab_owner(home, "tab-s1")
        assert owner is not None
        if sys.prefix == sys.base_prefix:
            self.assertEqual(owner[0], os.getpid())
        self.assertIsNone(tab_owner(home, "tab-s2"))
        self.assertIsNotNone(read_presence(home, "tab-s2"))

    def test_a_child_copilot_started_inside_a_busy_turn_does_not_take_the_tab_over(self) -> None:
        home = temp_home(self, "iat-copilot-hook-")
        run_hook("copilot", "sessionStart", {"sessionId": "parent"}, home, "tab-p", agent_process=(100, 1))
        run_hook("copilot", "userPromptSubmitted", {"sessionId": "parent"}, home, "tab-p")
        run_hook("copilot", "sessionStart", {"sessionId": "child"}, home, "tab-p", agent_process=(200, 2))
        self.assertEqual(tab_owner(home, "tab-p"), (100, 1))


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from ide_agent_tabs.liveness import pid_alive
from ide_agent_tabs.shared.client import ensure_server, probe, server_command, start_server, stop_server
from ide_agent_tabs.shared.handover import claim_port
from ide_agent_tabs.shared.headers import helper_headers, launcher_path
from ide_agent_tabs.shared.start_hook import run as run_hook
from ide_agent_tabs.shared.state import ServerState, read_state, read_token, state_path, write_state
from shared_support import McpHttp, free_port, pid_headers, reset_store_mode, serve
from store_host import StoreHost
from support import MCP, NODE_080, ROOT, TESTS, require_node, temp_home

PLUGIN = os.path.join(ROOT, "claude-plugin")
LAUNCHER = launcher_path(PLUGIN)
AS_BUILD = os.path.join(TESTS, "shared_server_as.py")
NODE_SERVER = os.path.join(NODE_080, "shared-server.mjs")
NODE_HELPER = os.path.join(NODE_080, "headers.mjs")
START_WAIT_MS = 20_000


def wait_until(check: Any, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.05)
    return bool(check())


def stop_after(test: unittest.TestCase, home: str, port: int) -> None:
    test.addCleanup(lambda: stop_server(home, port))


class StartedServerTest(unittest.TestCase):
    def started(self) -> tuple[str, int, Any]:
        home = temp_home(self, "iat-srv-")
        port = free_port()
        stop_after(self, home, port)
        ensured = ensure_server(server_command(LAUNCHER, port), port, home, "0", START_WAIT_MS, {**os.environ, "IDE_AGENT_TABS_HOME": home})
        return home, port, ensured

    def test_the_first_caller_starts_the_server_and_gets_its_token_and_later_callers_reuse_it(self) -> None:
        home, port, ensured = self.started()
        self.assertIsNone(ensured.problem)
        self.assertTrue(ensured.started)
        self.assertRegex(ensured.token or "", r"^[0-9a-f]{64}$")
        self.assertEqual(ensured.token, read_token(home))
        again = ensure_server(server_command(LAUNCHER, port), port, home, "0")
        self.assertFalse(again.started)
        self.assertEqual(again.health["pid"], ensured.health["pid"])  # type: ignore[index]
        self.assertEqual(McpHttp(port, ensured.token).rpc("server/discover").status, 200)

    def test_the_headers_helper_sends_the_token_a_client_id_and_the_agent_pid(self) -> None:
        home, port, ensured = self.started()
        env = {
            "IDE_AGENT_TABS_HOME": home,
            "CLAUDE_CODE_MCP_SERVER_URL": f"http://127.0.0.1:{port}/mcp",
            "IDE_AGENT_TABS_ID": "tab-x1",
            "IDE_AGENT_TABS_AGENT": "claude",
        }
        headers = helper_headers(env, PLUGIN, 60)
        self.assertEqual(headers["Authorization"], f"Bearer {ensured.token}")
        self.assertRegex(headers["X-Agent-Tabs-Client"], r"^[0-9a-f]{24}$")
        self.assertEqual((headers["X-Agent-Tabs-Tab"], headers["X-Agent-Tabs-Agent"]), ("tab-x1", "claude"))
        self.assertRegex(headers["X-Agent-Tabs-Pid"], r"^\d+$")
        unset = helper_headers({**env, "IDE_AGENT_TABS_ID": "${IDE_AGENT_TABS_ID}", "IDE_AGENT_TABS_AGENT": "bad name"}, PLUGIN)
        self.assertNotIn("X-Agent-Tabs-Tab", unset)
        self.assertNotIn("X-Agent-Tabs-Agent", unset)
        self.assertNotIn("X-Agent-Tabs-Tab", helper_headers({**env, "IDE_AGENT_TABS_ID": "s-0123456789ab"}, PLUGIN))

    def test_the_helper_script_run_through_a_shell_names_the_process_above_the_shell(self) -> None:
        home, port, ensured = self.started()
        script = os.path.join(PLUGIN, "mcp", "launch", "headers.py")
        env = {**os.environ, "IDE_AGENT_TABS_HOME": home, "CLAUDE_CODE_MCP_SERVER_URL": f"http://127.0.0.1:{port}/mcp"}
        for name in ("IDE_AGENT_TABS_ID", "IDE_AGENT_TABS_AGENT"):
            env.pop(name, None)
        quoted = (
            subprocess.list2cmdline([sys.executable, "-I", "-S", script])
            if sys.platform == "win32"
            else f'"{sys.executable}" -I -S "{script}"'
        )
        shell = ["cmd.exe", "/d", "/s", "/c", quoted] if sys.platform == "win32" else ["/bin/sh", "-c", f"{quoted}; true"]
        out = subprocess.run(shell, env=env, capture_output=True, timeout=60, check=True)
        headers = json.loads(out.stdout.decode("utf-8"))
        self.assertEqual(headers["X-Agent-Tabs-Pid"], str(os.getpid()))
        self.assertEqual(headers["Authorization"], f"Bearer {ensured.token}")

    def test_server_stop_asks_the_server_named_in_the_state_file_to_exit(self) -> None:
        home, port, ensured = self.started()
        pid = int(ensured.health["pid"])  # type: ignore[index]
        self.assertTrue(stop_server(home, port)["stopped"])
        self.assertEqual(probe(port).kind, "free")
        self.assertIn("nothing listens", stop_server(home, port)["problem"])
        self.assertTrue(wait_until(lambda: not pid_alive(pid)))
        self.assertIsNone(read_state(home, port))

    def test_a_server_whose_state_file_is_gone_exits_within_5_seconds(self) -> None:
        home, port, ensured = self.started()
        os.remove(state_path(home, port))
        self.assertTrue(wait_until(lambda: probe(port).kind == "free", 8))
        self.assertTrue(wait_until(lambda: not pid_alive(int(ensured.health["pid"])), 5))  # type: ignore[index]

    def test_a_started_server_runs_in_the_home_folder_without_the_session_variables(self) -> None:
        home = temp_home(self, "iat-srv-")
        out = os.path.join(home, "seen.json")
        code = "import json, os, sys; json.dump([os.getcwd(), os.environ.get('IDE_AGENT_TABS_ID'), os.environ['IDE_AGENT_TABS_HOME']], open(sys.argv[1], 'w'))"
        env = {**os.environ, "IDE_AGENT_TABS_ID": "tab-parent"}
        self.assertIsNotNone(start_server([sys.executable, "-I", "-S", "-c", code, out], home, env))
        self.assertTrue(wait_until(lambda: os.path.exists(out) and os.path.getsize(out) > 0, 30))
        with open(out, encoding="utf-8") as f:
            cwd, tab, seen_home = json.load(f)
        self.assertEqual(os.path.normcase(os.path.realpath(cwd)), os.path.normcase(os.path.realpath(os.path.expanduser("~"))))
        self.assertIsNone(tab)
        self.assertEqual(seen_home, home)

    def test_a_second_server_of_the_same_build_leaves_the_port_and_exits(self) -> None:
        home, port, ensured = self.started()
        env = {**os.environ, "IDE_AGENT_TABS_HOME": home}
        second = subprocess.run(server_command(LAUNCHER, port), env=env, capture_output=True, timeout=60, check=False)
        self.assertEqual(second.returncode, 0)
        self.assertEqual(probe(port).health["pid"], ensured.health["pid"])  # type: ignore[index]


class _Hello(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Length", "5")
        self.end_headers()
        self.wfile.write(b"hello")

    def log_message(self, format: str, *args: Any) -> None:
        pass


class OtherProgramTest(unittest.TestCase):
    def test_a_caller_never_sends_the_token_to_a_port_another_program_holds(self) -> None:
        home = temp_home(self, "iat-srv-")
        other = HTTPServer(("127.0.0.1", free_port()), _Hello)
        threading.Thread(target=other.serve_forever, daemon=True).start()
        self.addCleanup(other.server_close)
        self.addCleanup(other.shutdown)
        port = other.server_address[1]
        ensured = ensure_server(server_command(LAUNCHER, port), port, home, "0", 500)
        self.assertIsNone(ensured.token)
        self.assertIn("another program; set the Agent Tabs server_port option", ensured.problem or "")
        headers = helper_headers(
            {"IDE_AGENT_TABS_HOME": home, "CLAUDE_CODE_MCP_SERVER_URL": f"http://127.0.0.1:{port}/mcp"}, PLUGIN, wait_ms=500
        )
        self.assertNotIn("Authorization", headers)
        self.assertEqual(
            run_hook("SessionStart", {"IDE_AGENT_TABS_HOME": home, "CLAUDE_PLUGIN_OPTION_SERVER_PORT": str(port)}, LAUNCHER),
            f"Agent Tabs: port {port} belongs to another program; set the Agent Tabs server_port option to a free port.",
        )


class HandoverTest(unittest.TestCase):
    def test_a_newer_build_takes_the_port_from_an_older_one_it_verifies_and_leaves_a_newer_or_unverified_one(self) -> None:
        home = temp_home(self, "iat-srv-")
        old = serve(self, home, StoreHost(home), version="0.0.1")
        port = old.port
        log: list[str] = []
        honest = read_state(home, port)
        assert honest is not None
        write_state(home, honest._replace(pid=os.getpid() + 1))
        self.assertIsNone(claim_port(port, home, "9.9.9", log.append, 300))
        self.assertIn("matches no state file", log[-1])
        write_state(home, honest)
        self.assertIsNone(claim_port(port, home, "0.0.1", log.append, 300))
        sock = claim_port(port, home, "9.9.9", log.append)
        self.assertIsNotNone(sock)
        assert sock is not None
        sock.close()
        self.assertTrue(old.stopped.wait(10))

    def test_a_session_keeps_its_id_across_a_handover(self) -> None:
        home = temp_home(self, "iat-srv-")
        old = serve(self, home, StoreHost(home), version="0.0.1")
        me = McpHttp(old.port, old.token, pid_headers(os.getpid(), 42)).self_id()
        new_host = StoreHost(home)
        port = old.port
        from ide_agent_tabs.shared.server import SharedServer

        newer = SharedServer(home, port, new_host, version="9.9.9", log=lambda _m: None)
        self.assertTrue(newer.claim())
        threading.Thread(target=newer.serve, daemon=True).start()
        self.addCleanup(newer.stop, "test over")
        self.assertEqual(newer.token, old.token)
        self.assertEqual(McpHttp(port, newer.token, {**pid_headers(os.getpid(), 42), "x-agent-tabs-client": "same-client"}).self_id(), me)


class NodeHandoverTest(unittest.TestCase):
    def setUp(self) -> None:
        require_node(self)
        self.node = shutil.which("node") or "node"

    def env(self, home: str) -> dict[str, str]:
        env = {**os.environ, "IDE_AGENT_TABS_HOME": home}
        for name in ("IDE_AGENT_TABS_ID", "IDE_AGENT_TABS_AGENT", "IDE_AGENT_TABS_MOD", "CLAUDE_PLUGIN_OPTION_SERVER_PORT"):
            env.pop(name, None)
        return env

    def node_server(self, home: str, port: int) -> subprocess.Popen[bytes]:
        child = subprocess.Popen(
            [self.node, NODE_SERVER, "--port", str(port)],
            cwd=MCP,
            env=self.env(home),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(lambda: child.kill() if child.poll() is None else None)
        return child

    def python_server(self, home: str, port: int, version: str) -> subprocess.Popen[bytes]:
        child = subprocess.Popen(
            [sys.executable, "-I", "-S", AS_BUILD, version, "--port", str(port)],
            env=self.env(home),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(lambda: child.kill() if child.poll() is None else None)
        return child

    def serving(self, port: int, version: str, pid: int) -> bool:
        found = probe(port)
        return found.health is not None and found.health.get("version") == version and found.health.get("pid") == pid

    def test_a_python_build_newer_than_node_0_8_0_takes_the_port_through_the_verified_handover(self) -> None:
        home = temp_home(self, "iat-handover-")
        port = free_port()
        stop_after(self, home, port)
        node = self.node_server(home, port)
        self.assertTrue(wait_until(lambda: self.serving(port, "0.8.0", node.pid) and read_state(home, port) is not None, 60))
        node_token = read_token(home)
        python = self.python_server(home, port, "0.9.0")
        self.assertTrue(wait_until(lambda: self.serving(port, "0.9.0", python.pid), 30))
        self.assertEqual(node.wait(10), 0)
        state = read_state(home, port)
        self.assertEqual((state.pid, state.version) if state else None, (python.pid, "0.9.0"))
        self.assertEqual(read_token(home), node_token, "the per-user token outlives the handover")
        self.assertEqual(McpHttp(port, node_token or "").rpc("server/discover").status, 200)

    def test_node_0_8_0_leaves_a_newer_python_server_and_its_helper_verifies_the_python_state_file(self) -> None:
        home = temp_home(self, "iat-handover-")
        port = free_port()
        stop_after(self, home, port)
        python = self.python_server(home, port, "0.9.0")
        self.assertTrue(wait_until(lambda: self.serving(port, "0.9.0", python.pid), 30))
        node = self.node_server(home, port)
        self.assertEqual(node.wait(60), 0)
        self.assertTrue(self.serving(port, "0.9.0", python.pid))
        env = {**self.env(home), "CLAUDE_CODE_MCP_SERVER_URL": f"http://127.0.0.1:{port}/mcp"}
        out = subprocess.run([self.node, NODE_HELPER], env=env, capture_output=True, timeout=60, check=True)
        self.assertEqual(json.loads(out.stdout)["Authorization"], f"Bearer {read_token(home)}")

    def test_the_helper_of_a_newer_build_starts_it_over_node_0_8_0_and_gets_the_token(self) -> None:
        home = temp_home(self, "iat-handover-")
        port = free_port()
        stop_after(self, home, port)
        node = self.node_server(home, port)
        self.assertTrue(wait_until(lambda: self.serving(port, "0.8.0", node.pid) and read_state(home, port) is not None, 60))
        command = [sys.executable, "-I", "-S", AS_BUILD, "0.9.0", "--port", str(port)]
        ensured = ensure_server(command, port, home, "0.9.0", START_WAIT_MS, self.env(home))
        self.assertIsNone(ensured.problem)
        self.assertTrue(ensured.started)
        self.assertEqual(ensured.health["version"] if ensured.health else None, "0.9.0")
        self.assertEqual(ensured.token, read_token(home))
        self.assertEqual(node.wait(10), 0)

    def test_a_python_build_of_the_same_version_leaves_node_alone(self) -> None:
        home = temp_home(self, "iat-handover-")
        port = free_port()
        stop_after(self, home, port)
        node = self.node_server(home, port)
        self.assertTrue(wait_until(lambda: self.serving(port, "0.8.0", node.pid) and read_state(home, port) is not None, 60))
        python = self.python_server(home, port, "0.8.0")
        self.assertEqual(python.wait(30), 0)
        self.assertTrue(self.serving(port, "0.8.0", node.pid))


class StartHookTest(unittest.TestCase):
    def test_session_start_starts_the_server_and_session_end_ends_that_process_sessions(self) -> None:
        home = temp_home(self, "iat-hook-")
        port = free_port()
        stop_after(self, home, port)
        env = {**os.environ, "IDE_AGENT_TABS_HOME": home, "CLAUDE_PLUGIN_OPTION_SERVER_PORT": str(port)}
        self.assertIsNone(run_hook("SessionStart", env, LAUNCHER))
        self.assertEqual(probe(port).kind, "ours")

    def test_session_end_posts_the_claude_pid_and_skips_a_clear(self) -> None:
        home = temp_home(self, "iat-hook-")
        server = serve(self, home, StoreHost(home))
        McpHttp(server.port, server.token, pid_headers(os.getppid(), 1)).self_id()
        env = {"IDE_AGENT_TABS_HOME": home, "CLAUDE_PLUGIN_OPTION_SERVER_PORT": str(server.port), "CLAUDE_PID": str(os.getppid())}
        run_hook("SessionEnd", env, LAUNCHER, b'{"reason":"clear"}')
        self.assertEqual(server.hub.size, 1)
        run_hook("SessionEnd", env, LAUNCHER, b'{"reason":"logout"}')
        self.assertEqual(server.hub.size, 0)

    def test_the_hook_script_exits_0_and_prints_nothing_when_all_is_well(self) -> None:
        home = temp_home(self, "iat-hook-")
        server = serve(self, home, StoreHost(home), version="99.0.0")
        script = os.path.join(PLUGIN, "mcp", "launch", "server_hook.py")
        env = {**os.environ, "IDE_AGENT_TABS_HOME": home, "CLAUDE_PLUGIN_OPTION_SERVER_PORT": str(server.port), "CLAUDE_PID": "1"}
        for event, stdin in (("SessionStart", b""), ("SessionEnd", b'{"reason":"other"}'), ("Bogus", b"")):
            done = subprocess.run(
                [sys.executable, "-I", "-S", script, event], input=stdin, env=env, capture_output=True, timeout=60, check=False
            )
            self.assertEqual((done.returncode, done.stdout, done.stderr), (0, b"", b""), event)

    def test_a_state_written_by_the_python_server_reads_back(self) -> None:
        home = temp_home(self, "iat-hook-")
        write_state(home, ServerState(123, 4567, "1.2.3", "2026-01-01T00:00:00.000Z", "t" * 64))
        self.assertEqual(read_state(home, 4567), ServerState(123, 4567, "1.2.3", "2026-01-01T00:00:00.000Z", "t" * 64))


def tearDownModule() -> None:
    reset_store_mode()


if __name__ == "__main__":
    unittest.main()

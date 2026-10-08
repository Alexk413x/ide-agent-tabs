from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler
from typing import Any

from ide_agent_tabs import server_cli
from ide_agent_tabs.shared import client
from ide_agent_tabs.shared.state import parse_port, read_state, read_token, state_path, token_path
from jev_support import QuietServer
from support import ROOT, require_node, temp_home

LAUNCHER = os.path.join(ROOT, "claude-plugin", "mcp", "launch", "agent_tabs.py")
NODE_SERVER = os.path.join(ROOT, "claude-plugin", "dist", "shared-server.mjs")
NODE_CLI = os.path.join(ROOT, "claude-plugin", "dist", "mcp-server.mjs")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run_cli(args: list[str], home: str) -> tuple[int, Any]:
    out: list[str] = []
    code = server_cli.main(args, {"IDE_AGENT_TABS_HOME": home}, out.append)
    return code, json.loads("".join(out))


class FakeServer:
    def __init__(self, health: Any, shutdown_status: int = 200) -> None:
        self.health = health
        self.shutdown_status = shutdown_status
        self.shutdowns: list[str | None] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                pass

            def reply(self, status: int, body: str) -> None:
                data = body.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                self.reply(200, fake.health if isinstance(fake.health, str) else json.dumps(fake.health))

            def do_POST(self) -> None:
                fake.shutdowns.append(self.headers.get("authorization"))
                self.reply(fake.shutdown_status, "{}")
                if fake.shutdown_status == 200:
                    threading.Thread(target=fake.close, daemon=True).start()

        self.server = QuietServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.closed = False

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.server.shutdown()
            self.server.server_close()


def write_state(home: str, port: int, pid: int, token: str = "s" * 8) -> None:
    os.makedirs(os.path.dirname(state_path(home, port)), exist_ok=True)
    with open(state_path(home, port), "w", encoding="utf-8") as f:
        json.dump({"pid": pid, "port": port, "version": "0.8.0", "startedAt": "x", "shutdownToken": token}, f)


class Ports(unittest.TestCase):
    def test_parse_port(self) -> None:
        for text, want in {
            "1": 1,
            " 80 ": 80,
            "65535": 65535,
            "65536": None,
            "0": None,
            "080": 80,
            "1e3": None,
            "": None,
            "１": None,
        }.items():
            self.assertEqual(parse_port(text), want, text)
        self.assertIsNone(parse_port(None))

    def test_bad_arguments_print_usage(self) -> None:
        home = temp_home(self)
        for args in ([], ["restart"], ["status", "--port"], ["status", "--port", "x"], ["stop", "extra"], ["status", "--port", "1", "x"]):
            self.assertEqual(run_cli(args, home), (2, {"error": "Usage: agent-tabs server status|stop [--port <port>]"}), args)


class AgainstFakes(unittest.TestCase):
    def test_nothing_listening(self) -> None:
        home = temp_home(self)
        port = free_port()
        started = time.monotonic()
        self.assertEqual(run_cli(["status", "--port", str(port)], home), (1, {"port": port, "running": False}))
        self.assertLess(time.monotonic() - started, 0.9)
        self.assertEqual(
            run_cli(["stop", "--port", str(port)], home),
            (1, {"port": port, "stopped": False, "problem": f"nothing listens on port {port}"}),
        )

    def test_another_program(self) -> None:
        fake = FakeServer("<html>")
        self.addCleanup(fake.close)
        home = temp_home(self)
        problem = f"port {fake.port} belongs to another program"
        self.assertEqual(
            run_cli(["status", "--port", str(fake.port)], home), (1, {"port": fake.port, "running": False, "problem": problem})
        )
        self.assertEqual(run_cli(["stop", "--port", str(fake.port)], home), (1, {"port": fake.port, "stopped": False, "problem": problem}))

    def test_status_and_stop_with_a_state_file(self) -> None:
        home = temp_home(self)
        fake = FakeServer({})
        self.addCleanup(fake.close)
        fake.health = {"service": "ide-agent-tabs", "version": "9.9.9", "pid": 4242, "port": fake.port, "2": "index key"}
        code, status = run_cli(["status", "--port", str(fake.port)], home)
        self.assertEqual(code, 0)
        self.assertEqual(list(status), ["2", "running", "service", "version", "pid", "port"])
        no_state = run_cli(["stop", "--port", str(fake.port)], home)
        self.assertEqual(no_state[1]["problem"], f"the server on port {fake.port} matches no state file in {home}")
        write_state(home, fake.port, 4242, "tok")
        self.assertEqual(run_cli(["stop", "--port", str(fake.port)], home), (0, {"port": fake.port, "stopped": True, "pid": 4242}))
        self.assertEqual(fake.shutdowns, ["Bearer tok"])

    def test_a_refused_stop(self) -> None:
        home = temp_home(self)
        fake = FakeServer({}, shutdown_status=403)
        self.addCleanup(fake.close)
        fake.health = {"service": "ide-agent-tabs", "version": "1", "pid": 7, "port": fake.port}
        write_state(home, fake.port, 7)
        self.assertEqual(
            run_cli(["stop", "--port", str(fake.port)], home),
            (1, {"port": fake.port, "stopped": False, "pid": 7, "problem": "the server refused to stop (status 403)"}),
        )

    def test_port_option_from_the_environment(self) -> None:
        home = temp_home(self)
        port = free_port()
        out: list[str] = []
        server_cli.main(["status"], {"IDE_AGENT_TABS_HOME": home, "CLAUDE_PLUGIN_OPTION_SERVER_PORT": str(port)}, out.append)
        self.assertEqual(json.loads("".join(out))["port"], port)


class AgainstNodeServer(unittest.TestCase):
    def test_python_cli_reads_and_stops_the_node_server(self) -> None:
        require_node(self)
        node = shutil.which("node")
        assert node is not None
        home = temp_home(self)
        port = free_port()
        env = {**os.environ, "IDE_AGENT_TABS_HOME": home}
        server = subprocess.Popen([node, NODE_SERVER, "--port", str(port)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: server.poll() is None and server.kill())
        deadline = time.monotonic() + 15
        while client.probe(port).kind != "ours" or read_state(home, port) is None:
            self.assertLess(time.monotonic(), deadline, "the Node shared server did not start")
            time.sleep(0.05)
        self.assertIsNotNone(read_token(home), token_path(home))

        def cli(command: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.run(command, env=env, capture_output=True, encoding="utf-8", timeout=60, check=False)

        node_status = cli([node, NODE_CLI, "server", "status", "--port", str(port)])
        py_status = cli([sys.executable, "-I", "-S", LAUNCHER, "server", "status", "--port", str(port)])
        self.assertEqual((py_status.returncode, py_status.stdout), (node_status.returncode, node_status.stdout))
        self.assertTrue(json.loads(py_status.stdout)["running"])
        stopped = cli([sys.executable, "-I", "-S", LAUNCHER, "server", "stop", "--port", str(port)])
        self.assertEqual(stopped.returncode, 0, stopped.stdout)
        self.assertEqual(json.loads(stopped.stdout), {"port": port, "stopped": True, "pid": server.pid})
        server.wait(timeout=15)


if __name__ == "__main__":
    unittest.main()

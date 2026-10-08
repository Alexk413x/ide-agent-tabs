from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import unittest
from typing import Any

from ide_agent_tabs.shared.client import probe, stop_server
from ide_agent_tabs.shared.state import read_token
from shared_support import McpHttp, free_port
from support import ROOT, TESTS, percentile, require_node, temp_home

AS_BUILD = os.path.join(TESTS, "shared_server_as.py")
NODE_STDIO = os.path.join(ROOT, "claude-plugin", "dist", "mcp-server.mjs")
HTTP_SESSIONS = 32
STDIO_SESSIONS = 8
ROUNDS = 10
DRAIN_S = 60.0
SESSION_ENV = ("IDE_AGENT_TABS_ID", "IDE_AGENT_TABS_AGENT", "IDE_AGENT_TABS_MOD", "CLAUDE_PLUGIN_ROOT", "CLAUDE_PLUGIN_OPTION_SERVER_PORT")


def clean_env(home: str, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in SESSION_ENV}
    env["IDE_AGENT_TABS_HOME"] = home
    env.update(extra)
    return env


class HttpSession:
    def __init__(self, port: int, token: str, tab: str, index: int, cwd: str) -> None:
        self.tab = tab
        identity = {
            "x-agent-tabs-client": f"stress{index:04d}",
            "x-agent-tabs-tab": tab,
            "x-agent-tabs-agent": "claude",
            "x-agent-tabs-pid": str(os.getpid()),
            "x-agent-tabs-pid-start": str(1_000_000 + index),
        }
        self.client = McpHttp(port, token, identity, cwd)

    def call(self, name: str, args: dict[str, Any] | None = None) -> Any:
        out = self.client.call(name, args)
        if out["isError"]:
            raise RuntimeError(f"{name}: {out['text']}")
        return out["json"] or {}

    def close(self) -> None:
        return None


class StdioSession:
    def __init__(self, node: str, home: str, tab: str) -> None:
        self.tab = tab
        self.child = subprocess.Popen(
            [node, NODE_STDIO],
            env=clean_env(home, IDE_AGENT_TABS_ID=tab, IDE_AGENT_TABS_AGENT="claude"),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self.next = 1
        self.request(
            {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "claude-code", "version": "2.1.293"}}, "initialize"
        )
        self.write({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def write(self, message: dict[str, Any]) -> None:
        assert self.child.stdin is not None
        self.child.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        self.child.stdin.flush()

    def request(self, params: dict[str, Any], method: str) -> dict[str, Any]:
        request_id = self.next
        self.next += 1
        self.write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        assert self.child.stdout is not None
        while True:
            line = self.child.stdout.readline()
            if not line:
                raise RuntimeError(f"{self.tab}: the stdio server exited")
            message = json.loads(line)
            if message.get("id") == request_id:
                if "error" in message:
                    raise RuntimeError(f"{method}: {message['error']['message']}")
                return message["result"]

    def call(self, name: str, args: dict[str, Any] | None = None) -> Any:
        result = self.request({"name": name, "arguments": args or {}}, "tools/call")
        text = result.get("content", [{}])[0].get("text", "")
        if result.get("isError"):
            raise RuntimeError(f"{name}: {text}")
        return json.loads(text) if text else {}

    def close(self) -> None:
        if self.child.stdin is not None:
            self.child.stdin.close()
        try:
            self.child.wait(10)
        except subprocess.TimeoutExpired:
            self.child.kill()
            self.child.wait()
        if self.child.stdout is not None:
            self.child.stdout.close()


def median(values: list[float]) -> float:
    return percentile(values, 50)


class SharedStressTest(unittest.TestCase):
    def setUp(self) -> None:
        require_node(self)
        if not os.path.exists(NODE_STDIO):
            self.skipTest("claude-plugin/dist/mcp-server.mjs is missing")

    def test_32_http_and_8_stdio_sessions_lose_nothing_and_read_nothing_twice(self) -> None:
        node = shutil.which("node") or "node"
        home = temp_home(self, "iat-stress-")
        port = free_port()
        server = subprocess.Popen(
            [sys.executable, "-I", "-S", AS_BUILD, "0.9.0", "--store", "--port", str(port)],
            env=clean_env(home),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(lambda: server.kill() if server.poll() is None else None)
        self.addCleanup(lambda: stop_server(home, port))
        deadline = time.monotonic() + 30
        while probe(port).kind != "ours" and time.monotonic() < deadline:
            time.sleep(0.05)
        token = read_token(home)
        assert token is not None
        sessions: list[Any] = [HttpSession(port, token, f"stress-http-{i:03d}", i, home) for i in range(HTTP_SESSIONS)]
        opened: list[Any] = []
        openers = [
            threading.Thread(target=lambda i=i: opened.append(StdioSession(node, home, f"stress-stdio-{i:03d}")))
            for i in range(STDIO_SESSIONS)
        ]
        for t in openers:
            t.start()
        for t in openers:
            t.join(120)
        sessions += sorted(opened, key=lambda s: s.tab)
        for s in sessions:
            self.addCleanup(s.close)
        self.assertEqual(len(sessions), HTTP_SESSIONS + STDIO_SESSIONS)
        for s in sessions:
            s.call("list_sessions")
        ids = [s.tab for s in sessions]
        timings: dict[str, list[float]] = {"list_sessions": [], "send_message": [], "read_messages": []}
        sent: dict[str, str] = {}
        read: list[tuple[str, str]] = []
        failed: list[str] = []
        lock = threading.Lock()

        def timed(op: str, work: Any) -> Any:
            start = time.perf_counter()
            try:
                out = work()
            except Exception as e:  # noqa: BLE001 - every failure is counted and reported
                with lock:
                    failed.append(f"{op}: {e}")
                return None
            with lock:
                timings[op].append((time.perf_counter() - start) * 1000)
            return out

        def exchange(i: int, s: Any) -> None:
            for r in range(ROUNDS):
                timed("list_sessions", lambda: s.call("list_sessions"))
                to = ids[(i + 1 + r % (len(ids) - 1)) % len(ids)]
                got = timed("send_message", lambda to=to, r=r: s.call("send_message", {"to": to, "text": f"round {r} from {s.tab}"}))
                if got and got.get("id"):
                    with lock:
                        sent[got["id"]] = to
                batch = timed("read_messages", lambda: s.call("read_messages"))
                with lock:
                    read.extend((m["id"], s.tab) for m in (batch or {}).get("messages", []))

        workers = [threading.Thread(target=exchange, args=(i, s)) for i, s in enumerate(sessions)]
        started = time.perf_counter()
        for t in workers:
            t.start()
        for t in workers:
            t.join(600)
        elapsed = time.perf_counter() - started
        deadline = time.monotonic() + DRAIN_S
        while len(read) < len(sent) and time.monotonic() < deadline:
            for s in sessions:
                batch = s.call("read_messages")
                read.extend((m["id"], s.tab) for m in batch.get("messages", []))
        seen: set[str] = set()
        twice = 0
        for message_id, _ in read:
            twice += message_id in seen
            seen.add(message_id)
        wrong = sum(1 for message_id, by in read if sent.get(message_id) != by)
        lost = [m for m in sent if m not in seen]
        sys.stderr.write(
            f"\n  32 HTTP + 8 stdio: {elapsed:.1f} s, send p50 {median(timings['send_message']):.1f} ms "
            f"(p95 {percentile(timings['send_message'], 95):.1f}), read p50 {median(timings['read_messages']):.1f} ms, "
            f"list p50 {median(timings['list_sessions']):.1f} ms; failed {len(failed)}, lost {len(lost)}, read twice {twice}, wrong reader {wrong}"
        )
        self.assertEqual(failed, [])
        self.assertEqual(len(sent), len(sessions) * ROUNDS)
        self.assertEqual((len(lost), twice, wrong), (0, 0, 0))


if __name__ == "__main__":
    unittest.main()

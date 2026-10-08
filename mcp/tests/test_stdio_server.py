from __future__ import annotations

import io
import json
import threading
import time
import unittest
from typing import Any

from ide_agent_tabs.mcp_tools import ToolDeps, Tools, tools_for
from ide_agent_tabs.stdio_server import LATEST_PROTOCOL_VERSION, Output, StdioServer
from ide_agent_tabs.tool_input import compile_schema, issues_text, parse_args


class Capture(io.RawIOBase):
    def __init__(self) -> None:
        self.lines: list[dict[str, Any]] = []
        self.cond = threading.Condition()

    def writable(self) -> bool:
        return True

    def write(self, data: Any) -> int:
        with self.cond:
            for line in bytes(data).decode("utf-8").splitlines():
                self.lines.append(json.loads(line))
            self.cond.notify_all()
        return len(data)

    def wait_for(self, test: Any, timeout: float = 10.0) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout
        with self.cond:
            while True:
                found = next((m for m in self.lines if test(m)), None)
                left = deadline - time.monotonic()
                if found is not None or left <= 0:
                    return found
                self.cond.wait(left)


class FakeService:
    def __init__(self) -> None:
        self.opened: list[dict[str, Any]] = []

    def open_tab(self, given: dict[str, Any], wait: str = "full", on_progress: Any = None) -> dict[str, Any]:
        self.opened.append({**given, "wait": wait})
        if on_progress is not None:
            on_progress(5_400, 40_000, "Waiting for the IDE")
        return {"id": "tab-1", "ide": "vscode-1"}

    def list_agents(self) -> dict[str, Any]:
        return {"agents": []}


class FakeMessaging:
    id = "s-000000000001"

    def __init__(self) -> None:
        self.threads: list[Any] = []
        self.release = threading.Event()

    def note_thread(self, thread_id: Any) -> None:
        self.threads.append(thread_id)

    def hook(self, event: str, data: dict[str, Any]) -> dict[str, Any] | None:
        return {"event": event, "keys": sorted(data)}

    def wait(self, given: dict[str, Any], cancel: threading.Event | None = None) -> dict[str, Any]:
        assert cancel is not None
        while not cancel.is_set() and not self.release.is_set():
            cancel.wait(0.02)
        return {"message": None, "cancelled": cancel.is_set()}

    def mod_unread(self) -> dict[str, Any]:
        return {"count": 0, "senders": []}


class StdioServerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.out = Capture()
        self.service = FakeService()
        self.messaging = FakeMessaging()
        self.addCleanup(self.messaging.release.set)
        deps = ToolDeps(self.service, None, self.messaging, None, None)
        self.server = StdioServer(Tools(lambda: deps, False), Output(self.out), self.initialized)
        self.clients: list[str | None] = []

    def initialized(self, name: str | None) -> None:
        self.clients.append(name)

    def send(self, message: dict[str, Any]) -> None:
        self.server.receive_line(json.dumps({"jsonrpc": "2.0", **message}).encode("utf-8"))

    def reply(self, request_id: Any, timeout: float = 10.0) -> dict[str, Any] | None:
        return self.out.wait_for(lambda m: "method" not in m and m.get("id") == request_id, timeout)

    def init(self, client: str, version: str = "2025-06-18") -> dict[str, Any]:
        params = {"protocolVersion": version, "capabilities": {}, "clientInfo": {"name": client, "version": "1"}}
        self.out.lines.clear()
        self.send({"id": 0, "method": "initialize", "params": params})
        reply = self.reply(0)
        assert reply is not None
        self.send({"method": "notifications/initialized"})
        return reply["result"]

    def call(self, request_id: Any, name: str, arguments: Any = None, meta: dict[str, Any] | None = None) -> None:
        params: dict[str, Any] = {"name": name}
        if arguments is not None:
            params["arguments"] = arguments
        if meta is not None:
            params["_meta"] = meta
        self.send({"id": request_id, "method": "tools/call", "params": params})

    def test_initialize_echoes_a_supported_version_and_offers_the_latest_otherwise(self) -> None:
        self.assertEqual(self.init("claude-code", "2024-11-05")["protocolVersion"], "2024-11-05")
        result = self.init("claude-code", "1999-01-01")
        self.assertEqual(result["protocolVersion"], LATEST_PROTOCOL_VERSION)
        self.assertEqual(result["capabilities"], {"tools": {}})
        self.assertEqual(result["serverInfo"]["name"], "ide-agent-tabs")
        self.assertNotIn("jev_", result["instructions"])
        self.assertEqual(self.clients, ["claude-code", "claude-code"])

    def test_tool_list_depends_on_the_client(self) -> None:
        names = {c: [t["name"] for t in tools_for(c, False)] for c in ("claude-code", "codex-mcp-client", "other")}
        self.assertIn("agent_tabs_mod", names["claude-code"])
        self.assertNotIn("agent_tabs_hook", names["claude-code"])
        self.assertIn("agent_tabs_hook", names["codex-mcp-client"])
        self.assertNotIn("agent_tabs_mod", names["other"])
        self.assertFalse([n for n in names["other"] if n.startswith("jev_")])
        self.assertTrue([t for t in tools_for("other", True) if t["name"].startswith("jev_")])
        self.assertNotIn("jev_status", [t["name"] for t in tools_for("other", True)])

    def test_unknown_method_and_bad_lines(self) -> None:
        self.server.receive_line(b"not json")
        self.server.receive_line(b'{"id": 1, "method": "ping"}')
        self.send({"id": 2, "method": "resources/list"})
        self.assertEqual(self.reply(2), {"jsonrpc": "2.0", "id": 2, "error": {"code": -32601, "message": "Method not found"}})
        self.assertIsNone(self.reply(1, timeout=0.2))

    def test_a_long_wait_does_not_block_another_call_and_a_cancel_drops_its_reply(self) -> None:
        self.init("claude-code")
        self.call(10, "wait_for_message", {"timeout": 30})
        self.call(11, "agent_tabs_mod", {"op": "unread"})
        quick = self.reply(11)
        assert quick is not None
        self.assertEqual(json.loads(quick["result"]["content"][0]["text"]), {"count": 0, "senders": []})
        self.assertIsNone(self.reply(10, timeout=0.2))
        self.send({"method": "notifications/cancelled", "params": {"requestId": 10, "reason": "user"}})
        self.assertIsNone(self.reply(10, timeout=0.5))
        self.call("12", "wait_for_message", {})
        self.messaging.release.set()
        done = self.reply("12")
        assert done is not None
        self.assertEqual(json.loads(done["result"]["content"][0]["text"]), {"message": None, "cancelled": False})

    def test_open_tab_reports_progress_with_the_request_token(self) -> None:
        self.init("claude-code")
        self.call(20, "open_tab", {"path": "/w"}, {"progressToken": "p-1"})
        reply = self.reply(20)
        assert reply is not None
        progress = self.out.wait_for(lambda m: m.get("method") == "notifications/progress")
        assert progress is not None
        self.assertEqual(progress["params"], {"progressToken": "p-1", "progress": 5, "total": 40, "message": "Waiting for the IDE"})
        self.assertEqual(self.service.opened, [{"path": "/w", "wait": "background"}])

    def test_codex_thread_id_comes_from_meta_and_the_hook_falls_back_to_its_session_id(self) -> None:
        self.init("codex-mcp-client")
        self.call(30, "list_agents", None, {"threadId": "019a-thread"})
        self.reply(30)
        self.call(31, "agent_tabs_hook", {"event": "Stop", "session_id": "019a-other", "extra": 1})
        hooked = self.reply(31)
        assert hooked is not None
        self.assertEqual(json.loads(hooked["result"]["content"][0]["text"]), {"event": "Stop", "keys": ["event", "session_id"]})
        self.assertEqual(self.messaging.threads, ["019a-thread", "019a-other"])

    def test_invalid_arguments_and_hidden_tools_are_tool_errors(self) -> None:
        self.init("claude-code")
        self.call(40, "agent_tabs_hook", {"event": "Stop"})
        self.call(41, "send_message", None)
        hidden = self.reply(40)
        invalid = self.reply(41)
        assert hidden is not None and invalid is not None
        self.assertEqual(
            hidden["result"], {"content": [{"type": "text", "text": "MCP error -32602: Tool agent_tabs_hook not found"}], "isError": True}
        )
        self.assertEqual(
            invalid["result"]["content"][0]["text"],
            "MCP error -32602: Input validation error: Invalid arguments for tool send_message: Invalid input: expected object, received undefined",
        )


class ToolInputTest(unittest.TestCase):
    def test_lengths_count_code_points_and_issues_follow_zod(self) -> None:
        schema = compile_schema(
            {
                "type": "object",
                "required": ["a"],
                "properties": {
                    "a": {"type": "string", "maxLength": 3},
                    "n": {"type": "integer", "minimum": 0, "maximum": 600},
                    "m": {"type": "string", "pattern": "^[a-z]{1,3}$"},
                    "r": {"type": "object", "propertyNames": {"type": "string"}, "additionalProperties": {"type": "string"}},
                },
            }
        )
        out, issues = parse_args(schema, {"a": "\U0001f600" * 3, "extra": 1})
        self.assertEqual((out, issues), ({"a": "\U0001f600" * 3}, []))
        _, issues = parse_args(schema, {"a": "abcd", "n": 1e20, "m": "ab\n", "r": {"k": 1, "2": 3}})
        self.assertEqual(
            issues_text(issues).split("\n"),
            [
                "Too big: expected string to have <=3 characters at a",
                "Too big: expected int to be <=9007199254740991 at n",
                "Too big: expected number to be <=600 at n",
                "Invalid string: must match pattern /^[a-z]{1,3}$/ at m",
                "Invalid input: expected string, received number at r.2",
                "Invalid input: expected string, received number at r.k",
            ],
        )

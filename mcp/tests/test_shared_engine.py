from __future__ import annotations

import json
import os
import threading
import time
import unittest
from typing import Any

from ide_agent_tabs.clock import now_iso
from ide_agent_tabs.messaging.sessions import read_presence, update_presence
from ide_agent_tabs.shared.engine import Engine
from ide_agent_tabs.shared.hub import derived_id
from shared_support import McpHttp, pid_headers, request, reset_store_mode, serve
from support import temp_home


class EngineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.home = temp_home(self, "iat-engine-")
        env = {k: v for k, v in os.environ.items() if not k.startswith("IDE_AGENT_TABS")}
        self.engine = Engine(self.home, lambda _m: None, env=env, detect=False)
        self.server = serve(self, self.home, self.engine)

    def client(self, start: int, **extra: str) -> McpHttp:
        return McpHttp(self.server.port, self.server.token, pid_headers(os.getpid(), start, **extra), self.home)

    def test_the_tool_list_is_claude_codes_and_follows_the_jev_setting(self) -> None:
        listed = [t["name"] for t in self.client(1).rpc("tools/list").json["result"]["tools"]]
        self.assertIn("agent_tabs_mod", listed)
        self.assertNotIn("agent_tabs_hook", listed)
        self.assertFalse(any(n.startswith("jev_") for n in listed))
        self.assertIn("list_sessions", self.client(1).rpc("server/discover").json["result"]["instructions"])

    def test_a_codex_session_over_the_classic_path_runs_the_codex_only_hook_tool(self) -> None:
        headers = {
            "Authorization": f"Bearer {self.server.token}",
            "Accept": "application/json",
            "mcp-protocol-version": "2025-06-18",
            "User-Agent": "codex-mcp-client/0.162.1",
            **pid_headers(os.getpid(), 9, x_agent_tabs_agent="codex"),
        }
        hook = {"event": "UserPromptSubmit", "session_id": "thread-9", "turn_id": "turn-1"}
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "agent_tabs_hook", "arguments": hook}})
        reply = request(self.server.port, "/mcp", headers=headers, body=body)
        self.assertEqual(reply.status, 200, reply.text)
        self.assertNotIn("isError", reply.json["result"], reply.text)
        listed = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        names = [t["name"] for t in request(self.server.port, "/mcp", headers=headers, body=listed).json["result"]["tools"]]
        self.assertIn("agent_tabs_hook", names)
        self.assertNotIn("agent_tabs_mod", names)

    def test_two_sessions_message_each_other_through_the_real_tools(self) -> None:
        a, b = self.client(1), self.client(2)
        id_a, id_b = a.self_id(), b.self_id()
        self.assertEqual(id_a, derived_id(f"pid:{os.getpid()}:1"))
        presence = read_presence(self.home, id_a)
        assert presence is not None
        self.assertEqual((presence.get("pid"), presence.get("pidStart"), presence.get("agent")), (os.getpid(), 1, "claude"))
        sent = a.call("send_message", {"to": id_b, "text": "hello from a"})
        self.assertFalse(sent["isError"], sent["text"])
        read = b.call("read_messages")["json"]["messages"]
        self.assertEqual([(m["text"], m["from"]["id"]) for m in read], [("hello from a", id_a)])
        bad = a.call("send_message", {"to": id_b})
        self.assertTrue(bad["isError"])

    def test_a_wait_returns_a_message_and_a_dropped_wait_ends(self) -> None:
        a, b = self.client(3), self.client(4)
        id_a, id_b = a.self_id(), b.self_id()
        got: list[dict[str, object]] = []
        waiter = threading.Thread(target=lambda: got.append(b.call("wait_for_message", {"timeout": 30})))
        waiter.start()
        time.sleep(0.5)
        a.call("send_message", {"to": id_b, "text": "wake up"})
        waiter.join(30)
        self.assertIn("wake up", json.dumps(got))
        self.assertIn(id_a, json.dumps(got))

    def test_the_meta_of_a_tool_call_reaches_the_tools_and_a_progress_token_has_nowhere_to_go(self) -> None:
        a = self.client(5)
        a.self_id()
        thread = "019a2b3c-4d5e-7f60-8a9b-0c1d2e3f4a5b"
        meta = {"threadId": thread, "progressToken": "p1"}
        reply = a.rpc("tools/call", {"name": "list_sessions", "arguments": {}, "_meta": meta})
        self.assertNotIn("isError", reply.json["result"], reply.text)
        self.assertEqual(a.self_id(), f"codex-{thread}")

    def test_a_classic_call_passes_its_meta_on_and_ignores_a_meta_that_is_not_an_object(self) -> None:
        headers = {
            "Authorization": f"Bearer {self.server.token}",
            "Accept": "application/json",
            "mcp-protocol-version": "2025-06-18",
            **pid_headers(os.getpid(), 6, x_agent_tabs_cwd=self.home),
        }

        def classic(meta: object) -> Any:
            params = {"name": "list_sessions", "arguments": {}, "_meta": meta}
            body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params})
            return request(self.server.port, "/mcp", headers=headers, body=body).json["result"]

        thread = "019a2b3c-4d5e-7f60-8a9b-0c1d2e3f4a5c"
        self.assertNotIn("isError", classic("not an object"))
        self.assertNotIn("isError", classic({"threadId": thread, "progressToken": 7}))
        ids = json.loads(classic({})["content"][0]["text"])["sessions"]
        self.assertEqual([s["id"] for s in ids if s["self"]], [f"codex-{thread}"])

    def test_a_tab_claim_binds_only_a_free_tab(self) -> None:
        held = {"id": "tab-held", "agent": "claude", "path": "/x", "pid": os.getppid(), "startedAt": now_iso(), "state": "idle"}
        update_presence(self.home, "tab-held", lambda _c: held)
        self.assertEqual(self.client(7, x_agent_tabs_tab="tab-held").self_id(), derived_id(f"pid:{os.getpid()}:7"))
        self.assertEqual(self.client(8, x_agent_tabs_tab="tab-free").self_id(), "tab-free")

    def test_end_removes_the_presence_and_release_keeps_it(self) -> None:
        me = self.client(9).self_id()
        other = self.client(10).self_id()
        self.assertEqual(self.server.hub.end_pid(os.getpid()), 2)
        self.assertIsNone(read_presence(self.home, me))
        self.assertIsNone(read_presence(self.home, other))
        kept = self.client(11).self_id()
        self.server.stop("test")
        self.assertIsNotNone(read_presence(self.home, kept))


def tearDownModule() -> None:
    reset_store_mode()


if __name__ == "__main__":
    unittest.main()

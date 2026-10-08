from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
import unittest
from typing import Any

from ide_agent_tabs.clock import now_iso
from ide_agent_tabs.messaging.sessions import read_presence, update_presence
from ide_agent_tabs.shared.host import Binding, BoundSession, Catalog, Progress
from ide_agent_tabs.shared.hub import Identity, derived_id, root_path, session_key
from ide_agent_tabs.shared.server import IDLE_EXIT_MS, SharedServer
from ide_agent_tabs.shared.state import read_state, state_path
from shared_support import PROTOCOL, McpHttp, free_port, pid_headers, request, serve
from store_host import StoreHost
from support import temp_home


class _Blocking:
    def __init__(self, started: threading.Event, done: list[str]) -> None:
        self.started = started
        self.done = done

    @property
    def id(self) -> str:
        return "s-blocking"

    def call(self, name: str, arguments: dict[str, Any], progress: Progress | None, cancel: threading.Event) -> dict[str, Any]:
        self.started.set()
        self.done.append("cancelled" if cancel.wait(20) else "timed out")
        return {"content": []}

    def end(self) -> None:
        return None

    def release(self) -> None:
        return None


class BlockingHost(Catalog):
    def __init__(self, home: str) -> None:
        super().__init__(home)
        self.started = threading.Event()
        self.done: list[str] = []

    def bind(self, binding: Binding) -> BoundSession:
        return _Blocking(self.started, self.done)

    def close(self) -> None:
        return None


class FrontTest(unittest.TestCase):
    def setUp(self) -> None:
        self.home = temp_home(self, "iat-shared-")
        self.host = StoreHost(self.home)
        self.server = serve(self, self.home, self.host)
        self.port = self.server.port

    def test_foreign_host_or_origin_and_a_missing_token_get_403_and_health_needs_no_token(self) -> None:
        s = self.server
        self.assertEqual(request(self.port, "/health", "GET").json["service"], "ide-agent-tabs")
        self.assertEqual(request(self.port, "/health", "GET", {"Host": f"evil.example:{self.port}"}).status, 403)
        self.assertEqual(request(self.port, "/health", "GET", {"Origin": "http://evil.example"}).status, 403)
        self.assertEqual(request(self.port, "/health", "GET", {"Origin": f"http://localhost:{self.port}"}).status, 200)
        self.assertEqual(McpHttp(self.port, "wrong").rpc("server/discover").status, 403)
        self.assertEqual(McpHttp(self.port, "").rpc("server/discover").status, 403)
        self.assertEqual(request(self.port, "/shutdown", headers={"Authorization": f"Bearer {s.token}"}).status, 403)
        self.assertEqual(request(self.port, "/nowhere", headers={"Authorization": f"Bearer {s.token}"}).status, 404)
        self.assertEqual(request(self.port, "/mcp", "GET", {"Authorization": f"Bearer {s.token}"}).status, 405)
        health = request(self.port, "/health", "GET").json
        self.assertEqual((health["pid"], health["port"], health["sessions"]), (os.getpid(), self.port, 0))

    def test_discovery_and_tool_listing_answer_from_the_catalog_and_older_protocols_are_refused(self) -> None:
        client = McpHttp(self.port, self.server.token)
        discovered = client.rpc("server/discover")
        self.assertEqual(discovered.status, 200)
        result = discovered.json["result"]
        self.assertEqual(result["supportedVersions"], [PROTOCOL])
        self.assertEqual(result["resultType"], "complete")
        self.assertEqual(result["_meta"]["io.modelcontextprotocol/serverInfo"], {"name": "ide-agent-tabs", "version": "test"})
        self.assertIn("list_sessions", result["instructions"])
        listed = [t["name"] for t in client.rpc("tools/list").json["result"]["tools"]]
        self.assertIn("send_message", listed)
        self.assertIn("agent_tabs_mod", listed)
        self.assertNotIn("agent_tabs_hook", listed)
        self.assertFalse(any(n.startswith("jev_") for n in listed))
        with open(os.path.join(self.home, "config.json"), "w", encoding="utf-8") as f:
            json.dump({"jev": {"enabled": True}}, f)
        self.assertIn("jev_ask", [t["name"] for t in client.rpc("tools/list").json["result"]["tools"]])
        self.assertEqual(client.rpc("resources/list").json["result"]["resources"], [])
        self.assertEqual(client.rpc("ping").json["result"]["resultType"], "complete")
        classic = client.rpc("initialize", {}, {"mcp-protocol-version": "2025-11-25"})
        self.assertEqual(classic.status, 400)
        self.assertEqual(classic.json["error"]["code"], -32022)
        self.assertEqual(classic.json["error"]["data"], {"supported": [PROTOCOL], "requested": "2025-11-25"})
        self.assertEqual(client.rpc("tools/list", {}, {"mcp-method": "tools/call"}).json["error"]["code"], -32020)
        self.assertEqual(client.rpc("tools/call", {"name": "x"}, {"mcp-name": "y"}).json["error"]["code"], -32020)
        self.assertEqual(client.rpc("nothing/here").status, 404)
        token = {"Authorization": f"Bearer {self.server.token}", "Accept": "application/json", "mcp-protocol-version": PROTOCOL}
        self.assertEqual(request(self.port, "/mcp", headers=token, body="{").json["error"]["code"], -32700)
        self.assertEqual(request(self.port, "/mcp", headers=token, body="[]").json["error"]["code"], -32600)
        self.assertEqual(request(self.port, "/mcp", headers={**token, "Accept": "text/html"}, body="{}").status, 406)
        notification = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual(request(self.port, "/mcp", headers=token, body=notification).status, 202)
        no_meta = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        self.assertEqual(
            request(self.port, "/mcp", headers={**token, "mcp-method": "tools/list"}, body=no_meta).json["error"]["code"], -32602
        )

    def test_the_first_tool_call_asks_for_roots_and_the_session_keeps_its_folder_pid_and_start_time(self) -> None:
        cwd = temp_home(self, "iat-shared-cwd-")
        client = McpHttp(self.port, self.server.token, pid_headers(os.getpid(), 1234), cwd)
        first = client.rpc("tools/call", {"name": "list_sessions", "arguments": {}})
        result = first.json["result"]
        self.assertEqual(result["resultType"], "input_required")
        self.assertEqual(next(iter(result["inputRequests"].values()))["method"], "roots/list")
        sessions = client.call("list_sessions")["json"]["sessions"]
        me = next(s for s in sessions if s["self"])
        self.assertEqual(me["id"], derived_id(f"pid:{os.getpid()}:1234"))
        self.assertEqual(os.path.normcase(os.path.realpath(me["path"])), os.path.normcase(cwd))
        presence = read_presence(self.home, me["id"])
        assert presence is not None
        self.assertEqual((presence.get("pid"), presence.get("pidStart")), (os.getpid(), 1234))
        again = client.rpc("tools/call", {"name": "list_sessions", "arguments": {}})
        self.assertEqual(again.json["result"]["resultType"], "complete")

    def test_two_agent_processes_get_two_sessions_and_a_reconnect_keeps_its_session(self) -> None:
        a = McpHttp(self.port, self.server.token, pid_headers(os.getpid(), 1))
        b = McpHttp(self.port, self.server.token, pid_headers(os.getpid(), 2))
        id_a, id_b = a.self_id(), b.self_id()
        self.assertNotEqual(id_a, id_b)
        self.assertEqual(a.call("send_message", {"to": id_b, "text": "hello from a"})["json"]["to"], id_b)
        read = b.call("read_messages")["json"]["messages"]
        self.assertEqual([(m["text"], m["from"]["id"]) for m in read], [("hello from a", id_a)])
        reconnected = McpHttp(self.port, self.server.token, {**pid_headers(os.getpid(), 1), "x-agent-tabs-client": "another-connect"})
        self.assertEqual(reconnected.self_id(), id_a)
        self.assertEqual(self.server.hub.size, 2)

    def test_concurrent_first_calls_of_one_process_bind_one_session(self) -> None:
        clients = [
            McpHttp(self.port, self.server.token, {**pid_headers(os.getpid(), 77), "x-agent-tabs-client": f"c{i}"}) for i in range(8)
        ]
        ids: list[str] = []
        threads = [threading.Thread(target=lambda c=c: ids.append(c.self_id())) for c in clients]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(len(self.host.bound), 1)

    def test_a_tab_id_binds_only_when_no_other_live_process_holds_it(self) -> None:
        held = {"id": "tab-held", "agent": "claude", "path": "/x", "pid": os.getppid(), "startedAt": now_iso(), "state": "idle"}
        update_presence(self.home, "tab-held", lambda _c: held)
        claim = McpHttp(self.port, self.server.token, pid_headers(os.getpid(), 7, x_agent_tabs_tab="tab-held"))
        self.assertEqual(claim.self_id(), derived_id(f"pid:{os.getpid()}:7"))
        free = McpHttp(self.port, self.server.token, pid_headers(os.getpid(), 8, x_agent_tabs_tab="tab-free"))
        self.assertEqual(free.self_id(), "tab-free")

    def test_a_connection_without_identity_gets_a_tool_error(self) -> None:
        anonymous = McpHttp(self.port, self.server.token)
        result = anonymous.rpc("tools/call", {"name": "list_sessions", "arguments": {}}).json["result"]
        self.assertTrue(result["isError"])
        self.assertIn("no session identity", result["content"][0]["text"])

    def test_a_client_id_alone_keys_a_session_on_the_server_pid(self) -> None:
        client = McpHttp(self.port, self.server.token, {"x-agent-tabs-client": "only-client"})
        me = client.self_id()
        self.assertEqual(me, derived_id("client:only-client"))
        self.assertEqual(read_presence(self.home, me)["pid"], os.getpid())  # type: ignore[index]
        self.assertEqual(self.server.hub.end_pid(os.getpid()), 0)

    def test_end_posts_need_a_pid_and_the_token(self) -> None:
        token = {"Authorization": f"Bearer {self.server.token}", "Content-Type": "application/json"}
        self.assertEqual(request(self.port, "/end", headers=token, body='{"pid":"x"}').status, 400)
        self.assertEqual(request(self.port, "/end", headers=token, body='{"pid":1.5}').status, 400)
        self.assertEqual(request(self.port, "/end", headers={}, body='{"pid":5}').status, 403)
        self.assertEqual(request(self.port, "/end", headers=token, body='{"pid":5}').json, {"ended": 0})


class LifecycleTest(unittest.TestCase):
    def test_a_session_ends_when_its_agent_process_exits_or_its_session_end_hook_reports_it(self) -> None:
        home = temp_home(self, "iat-shared-")
        dead: set[int] = set()
        host = StoreHost(home, alive=lambda pid: pid not in dead)
        server = serve(self, home, host, alive=lambda pid: pid not in dead)
        a = McpHttp(server.port, server.token, pid_headers(os.getpid(), 11))
        b = McpHttp(server.port, server.token, pid_headers(os.getppid(), 12))
        id_a, id_b = a.self_id(), b.self_id()
        self.assertEqual(server.hub.live_sessions(), 2)
        dead.add(os.getpid())
        self.assertEqual(server.hub.sweep(), 1)
        self.assertIsNone(read_presence(home, id_a))
        token = {"Authorization": f"Bearer {server.token}", "Content-Type": "application/json"}
        self.assertEqual(request(server.port, "/end", headers=token, body=json.dumps({"pid": os.getppid()})).json, {"ended": 1})
        self.assertIsNone(read_presence(home, id_b))
        self.assertEqual(server.hub.size, 0)
        self.assertEqual(host.ended, [id_a, id_b])

    def test_a_dropped_request_cancels_its_tool_call(self) -> None:
        home = temp_home(self, "iat-shared-")
        host = BlockingHost(home)
        server = serve(self, home, host)
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "wait_for_message",
                    "arguments": {},
                    "inputResponses": {"agent-tabs-roots": {"roots": []}},
                    "_meta": {"io.modelcontextprotocol/protocolVersion": PROTOCOL, "io.modelcontextprotocol/clientCapabilities": {}},
                },
            }
        ).encode("utf-8")
        head = (
            f"POST /mcp HTTP/1.1\r\nHost: 127.0.0.1:{server.port}\r\nAuthorization: Bearer {server.token}\r\n"
            f"Accept: application/json\r\nContent-Type: application/json\r\nmcp-protocol-version: {PROTOCOL}\r\n"
            f"mcp-method: tools/call\r\nmcp-name: wait_for_message\r\nx-agent-tabs-client: drop\r\nx-agent-tabs-pid: {os.getpid()}\r\n"
            f"Content-Length: {len(body)}\r\n\r\n"
        )
        sock = socket.create_connection(("127.0.0.1", server.port))
        sock.sendall(head.encode("latin-1") + body)
        self.assertTrue(host.started.wait(10))
        sock.close()
        deadline = time.monotonic() + 5
        while not host.done and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(host.done, ["cancelled"])

    def test_stop_releases_sessions_keeps_their_presence_and_removes_the_state_file(self) -> None:
        home = temp_home(self, "iat-shared-")
        host = StoreHost(home)
        server = serve(self, home, host)
        me = McpHttp(server.port, server.token, pid_headers(os.getpid(), 3)).self_id()
        self.assertIsNotNone(read_state(home, server.port))
        server.stop("test")
        self.assertEqual(host.released, [me])
        self.assertTrue(host.closed)
        self.assertIsNotNone(read_presence(home, me))
        self.assertFalse(os.path.exists(state_path(home, server.port)))

    def test_shutdown_needs_the_state_file_token_and_stops_the_server(self) -> None:
        home = temp_home(self, "iat-shared-")
        server = serve(self, home, StoreHost(home))
        self.assertEqual(request(server.port, "/shutdown", "GET", {"Authorization": f"Bearer {server.shutdown_token}"}).status, 405)
        self.assertEqual(request(server.port, "/shutdown", headers={"Authorization": f"Bearer {server.shutdown_token}"}).json, {"ok": True})
        self.assertTrue(server.stopped.wait(10))

    def test_idle_exit_waits_eight_hours_without_requests_and_without_live_sessions(self) -> None:
        home = temp_home(self, "iat-shared-")
        clock = [1000.0]
        alive = {os.getpid()}
        server = serve(self, home, StoreHost(home), alive=lambda pid: pid in alive, now=lambda: clock[0])
        McpHttp(server.port, server.token, pid_headers(os.getpid(), 5)).self_id()
        clock[0] += IDLE_EXIT_MS / 1000 - 1
        self.assertFalse(server.check_idle())
        clock[0] += 2
        self.assertFalse(server.check_idle(), "a live session keeps the server")
        alive.clear()
        self.assertTrue(server.check_idle())
        self.assertTrue(server.stopped.wait(10))

    def test_a_request_resets_the_idle_clock(self) -> None:
        home = temp_home(self, "iat-shared-")
        clock = [0.0]
        server = serve(self, home, StoreHost(home), now=lambda: clock[0])
        clock[0] += IDLE_EXIT_MS / 1000
        McpHttp(server.port, server.token).rpc("tools/list")
        self.assertFalse(server.idle_expired())
        clock[0] += IDLE_EXIT_MS / 1000
        self.assertTrue(server.idle_expired())

    def test_a_server_whose_state_file_is_gone_or_names_another_pid_stops(self) -> None:
        home = temp_home(self, "iat-shared-")
        server = serve(self, home, StoreHost(home))
        self.assertFalse(server.check_state())
        os.remove(state_path(home, server.port))
        self.assertTrue(server.check_state())
        self.assertTrue(server.stopped.wait(10))
        other = serve(self, home, StoreHost(home))
        with open(state_path(home, other.port), encoding="utf-8") as f:
            state = json.load(f)
        state["pid"] = os.getpid() + 1
        with open(state_path(home, other.port), "w", encoding="utf-8") as f:
            json.dump(state, f)
        self.assertTrue(other.check_state())
        self.assertTrue(other.stopped.wait(10))

    def test_the_state_file_matches_the_node_format(self) -> None:
        home = temp_home(self, "iat-shared-")
        server = serve(self, home, StoreHost(home))
        with open(state_path(home, server.port), encoding="utf-8") as f:
            text = f.read()
        self.assertEqual(list(json.loads(text)), ["pid", "port", "version", "startedAt", "shutdownToken"])
        self.assertTrue(text.startswith('{\n  "pid": '))
        if sys.platform != "win32":
            self.assertEqual(os.stat(os.path.join(home, "server", "token")).st_mode & 0o777, 0o600)


class UnitTest(unittest.TestCase):
    def test_session_keys_come_from_the_agent_pid_and_start_time_else_the_client_id(self) -> None:
        self.assertEqual(session_key(Identity(client="x", pid=5, pid_start=9)), "pid:5:9")
        self.assertEqual(session_key(Identity(pid=5)), "pid:5:0")
        self.assertEqual(session_key(Identity(client="abc")), "client:abc")
        self.assertIsNone(session_key(Identity()))
        self.assertRegex(derived_id("pid:5:9"), r"^s-[0-9a-f]{12}$")

    def test_roots_must_be_local_file_urls(self) -> None:
        root = "file:///C:/work/repo" if sys.platform == "win32" else "file:///work/repo"
        expected = "C:\\work\\repo" if sys.platform == "win32" else "/work/repo"
        self.assertEqual(root_path({"agent-tabs-roots": {"roots": [{"uri": "https://x"}, {"uri": root}]}}), expected)
        self.assertIsNone(root_path({"agent-tabs-roots": {"roots": [{"uri": "file://server/share"}]}}))
        self.assertIsNone(root_path({"agent-tabs-roots": {"roots": "nope"}}))
        self.assertIsNone(root_path(None))

    def test_the_server_refuses_a_port_another_listener_holds(self) -> None:
        home = temp_home(self, "iat-shared-")
        port = free_port()
        holder = socket.socket()
        holder.bind(("127.0.0.1", port))
        holder.listen(1)
        self.addCleanup(holder.close)
        server = SharedServer(home, port, StoreHost(home), version="9.9.9", log=lambda _m: None)
        self.assertFalse(server.claim(wait_ms=200))


if __name__ == "__main__":
    unittest.main()

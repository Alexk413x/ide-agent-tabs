from __future__ import annotations

import http.client
import json
import os
import socket
import threading
import unittest
from pathlib import Path
from typing import Any, Callable

from ide_agent_tabs.shared.host import ToolHost
from ide_agent_tabs.shared.server import SharedServer

PROTOCOL = "2026-07-28"
RESERVED_PORTS = range(47821, 47830)


# The shared server sets the message store's process-wide shared mode on its first bind; tests that run
# servers in this process turn it back off so later store tests see the stdio defaults.
def reset_store_mode() -> None:
    from ide_agent_tabs.messaging.db import BUSY_TIMEOUT_MS, set_busy_timeout
    from ide_agent_tabs.messaging.wake import skip_wake_files_for_local_waiters

    set_busy_timeout(BUSY_TIMEOUT_MS)
    skip_wake_files_for_local_waiters(False)


def free_port() -> int:
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = int(s.getsockname()[1])
        if port not in RESERVED_PORTS:
            return port


class Reply:
    def __init__(self, status: int, text: str) -> None:
        self.status = status
        self.text = text
        self.json: dict[str, Any] = json.loads(text) if text else {}


def request(
    port: int, route: str, method: str = "POST", headers: dict[str, str] | None = None, body: str = "", timeout: float = 30.0
) -> Reply:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        sent = {"Host": f"127.0.0.1:{port}", "Content-Length": str(len(body.encode("utf-8")))}
        sent.update(headers or {})
        conn.request(method, route, body=body.encode("utf-8"), headers=sent)
        response = conn.getresponse()
        return Reply(response.status, response.read().decode("utf-8"))
    finally:
        conn.close()


def pid_headers(pid: int, start: int, **extra: str) -> dict[str, str]:
    out = {"x-agent-tabs-client": f"c{pid}x{start}", "x-agent-tabs-pid": str(pid), "x-agent-tabs-pid-start": str(start)}
    out.update({k.replace("_", "-"): v for k, v in extra.items()})
    return out


class McpHttp:
    def __init__(self, port: int, token: str, identity: dict[str, str] | None = None, cwd: str | None = None) -> None:
        self.port = port
        self.token = token
        self.identity = identity or {}
        self.cwd = cwd or os.path.realpath(os.path.expanduser("~"))
        self.next = 1
        self._lock = threading.Lock()

    def rpc(self, method: str, params: dict[str, Any] | None = None, extra: dict[str, str] | None = None) -> Reply:
        params = dict(params or {})
        with self._lock:
            request_id = self.next
            self.next += 1
        meta = {"io.modelcontextprotocol/protocolVersion": PROTOCOL, "io.modelcontextprotocol/clientCapabilities": {"roots": {}}}
        body = json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": {**params, "_meta": meta}})
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "mcp-protocol-version": PROTOCOL,
            "mcp-method": method,
        }
        if isinstance(params.get("name"), str):
            headers["mcp-name"] = params["name"]
        headers.update(self.identity)
        headers.update(extra or {})
        return request(self.port, "/mcp", headers=headers, body=body, timeout=300.0)

    def call(self, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        reply = self.rpc("tools/call", {"name": name, "arguments": args or {}})
        result = reply.json.get("result", {})
        asked = result.get("inputRequests")
        if result.get("resultType") == "input_required" and isinstance(asked, dict):
            answers = {key: {"roots": [{"uri": Path(self.cwd).as_uri()}]} for key in asked}
            reply = self.rpc("tools/call", {"name": name, "arguments": args or {}, "inputResponses": answers})
        if "error" in reply.json:
            raise RuntimeError(f"{name}: {reply.json['error']['message']}")
        result = reply.json["result"]
        text = result.get("content", [{}])[0].get("text", "")
        is_error = result.get("isError") is True
        return {"isError": is_error, "text": text, "json": None if is_error or text == "" else json.loads(text)}

    def self_id(self) -> str:
        sessions = self.call("list_sessions")["json"]["sessions"]
        return next(s["id"] for s in sessions if s["self"])


def serve(
    test: unittest.TestCase,
    home: str,
    host: ToolHost,
    alive: Callable[[int], bool] | None = None,
    version: str = "test",
    now: Callable[[], float] | None = None,
) -> SharedServer:
    port = free_port()
    kwargs: dict[str, Any] = {"version": version, "log": lambda _m: None}
    if alive is not None:
        kwargs["alive"] = alive
    if now is not None:
        kwargs["now"] = now
    server = SharedServer(home, port, host, **kwargs)
    test.assertTrue(server.claim())
    thread = threading.Thread(target=server.serve, daemon=True)
    thread.start()

    def stop() -> None:
        server.stop("test over")
        thread.join(10)

    test.addCleanup(reset_store_mode)
    test.addCleanup(stop)
    return server

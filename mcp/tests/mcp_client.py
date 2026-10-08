from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Mapping
from typing import Any

from support import ROOT

PY_SERVER = os.path.join(ROOT, "claude-plugin", "mcp", "launch", "stdio_server.py")
NODE_SERVER = os.path.join(ROOT, "claude-plugin", "dist", "mcp-server.mjs")
REPLY_TIMEOUT_S = 60.0


def server_command(impl: str) -> list[str]:
    if impl == "python":
        return [sys.executable, "-I", "-S", PY_SERVER]
    node = shutil.which("node")
    assert node is not None
    return [node, NODE_SERVER]


class McpProcess:
    def __init__(self, impl: str, env: Mapping[str, str], cwd: str, stderr_path: str) -> None:
        self.impl = impl
        self._stderr = open(stderr_path, "wb")  # noqa: SIM115
        self.child = subprocess.Popen(
            server_command(impl), cwd=cwd, env=dict(env), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._stderr
        )
        self.messages: list[dict[str, Any]] = []
        self.closed = False
        self._cond = threading.Condition()
        self._next_id = 0
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        assert self.child.stdout is not None
        for raw in self.child.stdout:
            try:
                message = json.loads(raw.decode("utf-8"))
            except ValueError:
                continue
            with self._cond:
                self.messages.append(message)
                self._cond.notify_all()
        with self._cond:
            self.closed = True
            self._cond.notify_all()

    def send(self, message: Mapping[str, Any]) -> None:
        assert self.child.stdin is not None
        self.child.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        self.child.stdin.flush()

    def send_raw(self, data: bytes) -> None:
        assert self.child.stdin is not None
        self.child.stdin.write(data)
        self.child.stdin.flush()

    def new_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def start(self, method: str, params: Mapping[str, Any] | None = None, request_id: Any = None) -> Any:
        rid = self.new_id() if request_id is None else request_id
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            message["params"] = dict(params)
        self.send(message)
        return rid

    def notify(self, method: str, params: Mapping[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = dict(params)
        self.send(message)

    def response(self, request_id: Any, timeout: float = REPLY_TIMEOUT_S) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                for i, m in enumerate(self.messages):
                    if "method" not in m and m.get("id") == request_id:
                        return self.messages.pop(i)
                left = deadline - time.monotonic()
                if self.closed or left <= 0:
                    return None
                self._cond.wait(left)

    def notifications(self, method: str) -> list[dict[str, Any]]:
        with self._cond:
            found = [m for m in self.messages if m.get("method") == method]
            self.messages = [m for m in self.messages if m.get("method") != method]
            return found

    def request(self, method: str, params: Mapping[str, Any] | None = None, timeout: float = REPLY_TIMEOUT_S) -> dict[str, Any]:
        rid = self.start(method, params)
        reply = self.response(rid, timeout)
        if reply is None:
            raise AssertionError(f"{self.impl} server sent no reply to {method} {params}")
        return reply

    def call(self, name: str, arguments: Any = None, meta: Mapping[str, Any] | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"name": name}
        if arguments is not None:
            params["arguments"] = arguments
        if meta is not None:
            params["_meta"] = dict(meta)
        return self.request("tools/call", params)

    def initialize(self, client: str, version: str) -> dict[str, Any]:
        reply = self.request(
            "initialize", {"protocolVersion": version, "capabilities": {}, "clientInfo": {"name": client, "version": "1.0.0"}}
        )
        self.notify("notifications/initialized")
        return reply

    def close(self, timeout: float = 20.0) -> int | None:
        if self.child.stdin is not None:
            try:
                self.child.stdin.close()
            except OSError:
                pass
        try:
            code = self.child.wait(timeout)
        except subprocess.TimeoutExpired:
            self.child.kill()
            code = None
            self.child.wait()
        if self.child.stdout is not None:
            self.child.stdout.close()
        self._stderr.close()
        return code


def result_json(reply: Mapping[str, Any]) -> Any:
    content = reply["result"]["content"]
    return json.loads(content[0]["text"])

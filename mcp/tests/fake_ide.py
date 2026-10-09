from __future__ import annotations

import json
import os
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

Handler = Callable[[dict[str, Any]], "tuple[int, Any] | dict[str, Any]"]


class _QuietServer(ThreadingHTTPServer):
    def handle_error(self, request: Any, client_address: Any) -> None:
        pass


class FakeIde:
    def __init__(self, product: str = "IntelliJ IDEA", projects: list[dict[str, Any]] | None = None, token: str = "secret") -> None:
        self.product = product
        self.projects = projects if projects is not None else []
        self.token = token
        self.tabs: list[dict[str, Any]] = []
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.handlers: dict[str, Handler] = {}
        self.raw: dict[str, tuple[int, bytes, dict[str, str]]] = {}
        self.delay_s = 0.0
        fake = self

        class RequestHandler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: Any) -> None:
                pass

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                route = self.path.rsplit("/", 1)[-1]
                fake.requests.append((route, body))
                if fake.delay_s:
                    time.sleep(fake.delay_s)
                if route in fake.raw:
                    status, payload, headers = fake.raw[route]
                    self.send_response(status)
                    for name, value in headers.items():
                        self.send_header(name, value)
                    if "Transfer-Encoding" not in headers:
                        self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                if self.headers.get("Authorization") != f"Bearer {fake.token}":
                    self._reply(401, {"ok": False, "error": "bad token"})
                    return
                handler = fake.handlers.get(route)
                if handler is not None:
                    answer = handler(body)
                    status, reply = answer if isinstance(answer, tuple) else (200, answer)
                    self._reply(status, reply)
                    return
                self._reply(200, fake.default(route, body))

            def _reply(self, status: int, value: Any) -> None:
                data = json.dumps(value).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = _QuietServer(("127.0.0.1", 0), RequestHandler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/ide-agent-tabs"

    def default(self, route: str, body: dict[str, Any]) -> dict[str, Any]:
        if route == "info":
            return {"ok": True, "product": self.product, "version": "2026.1", "projects": self.projects}
        if route == "list":
            return {"ok": True, "tabs": self.tabs}
        if route == "open":
            tab = {"id": f"ide-tab-{len(self.tabs) + 1}", "agent": body.get("agent", "claude"), "path": body["path"]}
            self.tabs.append(tab)
            return {"ok": True, "id": tab["id"], "agent": tab["agent"], "project": "app", "path": body["path"]}
        if route == "close":
            self.tabs = [t for t in self.tabs if t["id"] != body.get("id")]
            return {"ok": True}
        return {"ok": True}

    def register(self, home: str, endpoint_id: str, started_at: int = 1000, pid: int | None = None, **extra: Any) -> str:
        folder = os.path.join(home, "endpoints")
        os.makedirs(folder, exist_ok=True)
        file = os.path.join(folder, f"{endpoint_id}.json")
        record = {
            "protocol": 1,
            "ide": "jetbrains",
            "product": self.product,
            "version": "2026.1",
            "pid": pid if pid is not None else os.getpid(),
            "url": self.url,
            "token": self.token,
            "startedAt": started_at,
            **extra,
        }
        with open(file, "w", encoding="utf-8") as f:
            json.dump(record, f)
        return file

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def fake_ide(test: unittest.TestCase, *args: Any, **kwargs: Any) -> FakeIde:
    ide = FakeIde(*args, **kwargs)
    test.addCleanup(ide.close)
    return ide

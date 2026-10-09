from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from support import FIXTURES

SEEN_HEADERS = ("authorization", "accept", "content-type", "user-agent", "x-typesafe-sdk", "x-typesafe-retry-count")


def jev_fixtures() -> dict[str, Any]:
    with open(os.path.join(FIXTURES, "jev.json"), encoding="utf-8") as f:
        return json.load(f)


def expand(value: Any) -> Any:
    if isinstance(value, list):
        return [expand(v) for v in value]
    if not isinstance(value, dict):
        return value
    if set(value) == {"$repeat"}:
        text, count = value["$repeat"]
        return text * count
    return {k: expand(v) for k, v in value.items()}


class QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        pass


class StubTypeSafe:
    def __init__(self) -> None:
        self.canned: dict[str, Any] | None = None
        self.delay_s = 0.0
        self.seen: list[dict[str, Any]] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: Any) -> None:
                pass

            def do_POST(self) -> None:
                length = int(self.headers.get("content-length") or 0)
                body = self.rfile.read(length).decode("utf-8")
                stub.seen.append(
                    {
                        "method": self.command,
                        "path": self.path,
                        "headers": {h: self.headers.get(h) for h in SEEN_HEADERS},
                        "body": body,
                    }
                )
                if stub.delay_s:
                    threading.Event().wait(stub.delay_s)
                reply = stub.canned or {"status": 500, "body": "no canned response"}
                data = reply["body"].encode("utf-8")
                try:
                    self.send_response(reply["status"])
                    for name, value in (reply.get("headers") or {}).items():
                        self.send_header(name, value)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except OSError:
                    pass

        self.server = QuietServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def set(self, canned: dict[str, Any] | None, delay_s: float = 0.0) -> None:
        self.canned = canned
        self.delay_s = delay_s
        self.seen.clear()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

from __future__ import annotations

import contextlib
import math
import re
import select
import socket
import socketserver
import sys
import threading
from typing import Any, Callable, NamedTuple, cast

from ..jsjson import js_string, parse, stringify
from .hub import Identity
from .state import HEADER_AGENT, HEADER_CLIENT, HEADER_PID, HEADER_PID_START, HEADER_TAB

MODERN_PROTOCOL = "2026-07-28"
VERSION_KEY = "io.modelcontextprotocol/protocolVersion"
CAPABILITIES_KEY = "io.modelcontextprotocol/clientCapabilities"
SERVER_INFO_KEY = "io.modelcontextprotocol/serverInfo"
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
HEADER_MISMATCH = -32020
UNSUPPORTED_VERSION = -32022
STATUS = {
    PARSE_ERROR: 400,
    INVALID_REQUEST: 400,
    INVALID_PARAMS: 400,
    HEADER_MISMATCH: 400,
    UNSUPPORTED_VERSION: 400,
    METHOD_NOT_FOUND: 404,
}
MAX_BODY_BYTES = 8 * 1024 * 1024
KEEPALIVE_TIMEOUT_S = 60.0
LISTED = {"ttlMs": 0, "cacheScope": "private"}
EMPTY_LISTS = {"prompts/list": "prompts", "resources/list": "resources", "resources/templates/list": "resourceTemplates"}
NOT_ONE_MESSAGE = "Body must be a single JSON-RPC request or notification object"
CLAUDE_AGENT = "claude"
_CLIENT_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_TAB_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_AGENT_NAME = re.compile(r"[A-Za-z0-9._-]{1,64}")
_DIGITS = re.compile(r"[0-9]{1,16}")
_MAX_SAFE_INTEGER = 2**53 - 1
_WATCH_POLL_S = 0.25
_WATCH_LIMIT = 500
_DISCARD_LIMIT = 1024 * 1024
_MAX_LINE = 65536
_MAX_HEADERS = 100
_REASONS = {200: "OK", 202: "Accepted", 400: "Bad Request", 403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed"}
_REASONS.update({406: "Not Acceptable", 413: "Content Too Large", 414: "URI Too Long", 431: "Request Header Fields Too Large"})
_PERCENT_RUN = re.compile("(?:%[0-9A-Fa-f]{2})+")


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


class BodyError(ValueError):
    def __init__(self, status: int) -> None:
        super().__init__(status)
        self.status = status


class FrontDeps(NamedTuple):
    port: int
    token: str
    shutdown_token: str
    server_info: dict[str, str]
    health: Callable[[], dict[str, Any]]
    tools: Callable[[str], list[dict[str, Any]]]
    instructions: Callable[[str], str]
    call: Callable[[Identity, dict[str, Any], threading.Event], dict[str, Any]]
    end: Callable[[int], int]
    on_request: Callable[[], None]
    on_shutdown: Callable[[], None]


def positive_int(value: str | None, minimum: int) -> int | None:
    if value is None or not _DIGITS.fullmatch(value):
        return None
    number = int(value)
    return number if minimum <= number <= _MAX_SAFE_INTEGER else None


def percent_decoded(text: str) -> str:
    return _PERCENT_RUN.sub(lambda m: bytes.fromhex(m.group().replace("%", "")).decode("utf-8", "replace"), text)


def _bearer(value: str | None, expected: str) -> bool:
    import hmac

    parts = (value or "").split(" ")
    if len(parts) < 1 or parts[0].lower() != "bearer" or expected == "":
        return False
    token = (parts[1] if len(parts) > 1 else "").strip()
    return hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8"))


# A client that drops its connection while a tool call runs cancels the call, as Node's response 'close' does.
# One thread watches every open call's socket for the end of the stream.
class DropWatch:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._watched: dict[socket.socket, threading.Event] = {}
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def watch(self, sock: socket.socket, cancel: threading.Event) -> None:
        with self._lock:
            if len(self._watched) >= _WATCH_LIMIT:
                return
            self._watched[sock] = cancel
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="drop-watch", daemon=True)
                self._thread.start()

    def unwatch(self, sock: socket.socket) -> None:
        with self._lock:
            self._watched.pop(sock, None)

    def close(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                socks = [s for s in self._watched if s.fileno() != -1]
            if not socks:
                self._stop.wait(_WATCH_POLL_S)
                continue
            try:
                readable, _, _ = select.select(socks, [], [], _WATCH_POLL_S)
            except (OSError, ValueError):
                self._stop.wait(_WATCH_POLL_S)
                continue
            for sock in readable:
                try:
                    data = sock.recv(1, socket.MSG_PEEK)
                except BlockingIOError:
                    continue
                except OSError:
                    data = b""
                with self._lock:
                    cancel = self._watched.pop(sock, None)
                if cancel is not None and data == b"":
                    cancel.set()


class Headers:
    def __init__(self) -> None:
        self._values: dict[str, list[str]] = {}

    def add(self, name: str, value: str) -> None:
        self._values.setdefault(name.lower(), []).append(value)

    def get_all(self, name: str) -> list[str] | None:
        return self._values.get(name.lower())

    def get(self, name: str) -> str | None:
        values = self._values.get(name.lower())
        return values[0] if values else None


class Front(socketserver.ThreadingTCPServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = 128

    def __init__(self, sock: socket.socket, deps: FrontDeps) -> None:
        super().__init__(sock.getsockname()[:2], Handler, bind_and_activate=False)
        self.socket.close()
        self.socket = sock
        sock.setblocking(True)
        self.deps = deps
        self.hosts = {f"127.0.0.1:{deps.port}", f"localhost:{deps.port}"}
        self.origins = {f"http://{host}" for host in self.hosts}
        self.drops = DropWatch()

    def handle_error(self, request: Any, client_address: Any) -> None:
        if not isinstance(sys.exc_info()[1], OSError):
            super().handle_error(request, client_address)

    def server_close(self) -> None:
        self.drops.close()
        super().server_close()


# http.server would double the server's start-up with the email, mimetypes and ssl modules it imports; this
# handler reads the HTTP/1.1 subset MCP clients send: one request line, headers, a Content-Length or chunked body.
class Handler(socketserver.StreamRequestHandler):
    timeout = KEEPALIVE_TIMEOUT_S
    body_read = False
    close_connection = True
    command = ""
    path = ""
    headers = Headers()

    @property
    def front(self) -> Front:
        return cast(Front, self.server)

    def handle(self) -> None:
        while True:
            try:
                if not self.read_request():
                    return
                self.route()
            except (OSError, ValueError):
                return
            if self.close_connection:
                return

    def read_request(self) -> bool:
        line = self.rfile.readline(_MAX_LINE + 1)
        if not line:
            return False
        if len(line) > _MAX_LINE:
            self.reject(414)
            return False
        parts = line.decode("latin-1").split()
        if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
            self.reject(400)
            return False
        self.command, self.path, version = parts
        headers = Headers()
        for _ in range(_MAX_HEADERS + 1):
            raw = self.rfile.readline(_MAX_LINE + 1)
            if not raw:
                return False
            if len(raw) > _MAX_LINE:
                self.reject(431)
                return False
            if raw in (b"\r\n", b"\n"):
                break
            name, colon, value = raw.decode("latin-1").partition(":")
            if not colon or not name or name != name.strip():
                self.reject(400)
                return False
            headers.add(name, value.strip())
        else:
            self.reject(431)
            return False
        tokens = {t.strip().lower() for t in (headers.get("connection") or "").split(",")}
        self.close_connection = "close" in tokens or (version == "HTTP/1.0" and "keep-alive" not in tokens)
        self.headers = headers
        self.body_read = False
        return True

    def reject(self, status: int) -> None:
        self.wfile.write(
            f"HTTP/1.1 {status} {_REASONS.get(status, '')}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode("latin-1")
        )

    def header(self, name: str) -> str | None:
        values = self.headers.get_all(name)
        if values is None or len(values) != 1:
            return None
        return values[0].strip() or None

    def identity(self) -> Identity:
        client = self.header(HEADER_CLIENT)
        tab = self.header(HEADER_TAB)
        agent = self.header(HEADER_AGENT)
        pid = positive_int(self.header(HEADER_PID), 1)
        pid_start = positive_int(self.header(HEADER_PID_START), 0)
        return Identity(
            client if client is not None and _CLIENT_ID.fullmatch(client) else None,
            tab if tab is not None and _TAB_ID.fullmatch(tab) else None,
            agent if agent is not None and _AGENT_NAME.fullmatch(agent) else None,
            pid,
            pid_start if pid is not None else None,
        )

    def send(self, status: int, body: Any = None, close: bool = False, headers: dict[str, str] | None = None) -> None:
        text = b"" if body is None else stringify(body).encode("utf-8")
        if close:
            self.discard_body()
            self.close_connection = True
        lines = [f"HTTP/1.1 {status} {_REASONS.get(status, '')}"]
        if text:
            lines.append("Content-Type: application/json")
        lines.extend(f"{name}: {value}" for name, value in (headers or {}).items())
        lines.append(f"Content-Length: {len(text)}")
        if self.close_connection:
            lines.append("Connection: close")
        self.wfile.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + text)

    def refuse(self, status: int, error: str) -> None:
        self.send(status, {"error": error}, close=True)

    # Windows resets a connection closed with unread request bytes, and the client then loses the reply.
    def discard_body(self) -> None:
        if self.body_read:
            return
        self.body_read = True
        length = positive_int((self.headers.get("content-length") or "").strip(), 1)
        if length is not None and length <= _DISCARD_LIMIT:
            with contextlib.suppress(OSError):
                self.rfile.read(length)

    def rpc_error(self, request_id: Any, error: RpcError) -> None:
        body: dict[str, Any] = {"code": error.code, "message": error.message}
        if error.data is not None:
            body["data"] = error.data
        self.send(STATUS.get(error.code, 200), {"jsonrpc": "2.0", "id": request_id, "error": body})

    def route(self) -> None:
        front = self.front
        deps = front.deps
        if (self.header("host") or "").lower() not in front.hosts:
            self.refuse(403, "Host is not this server")
            return
        origin = self.header("origin")
        if origin is not None and origin.lower() not in front.origins:
            self.refuse(403, "Origin not allowed")
            return
        path = self.path.split("?", 1)[0]
        if path == "/health" and self.command == "GET":
            self.send(200, deps.health())
            return
        if path == "/shutdown":
            if not _bearer(self.header("authorization"), deps.shutdown_token):
                self.refuse(403, "missing or wrong bearer token")
                return
            if self.command != "POST":
                self.refuse(405, "use POST")
                return
            self.send(200, {"ok": True}, close=True)
            deps.on_shutdown()
            return
        # A 401 starts Claude Code's OAuth flow, which this server doesn't offer, so a bad token gets 403.
        if not _bearer(self.header("authorization"), deps.token):
            self.refuse(403, "missing or wrong bearer token")
            return
        if path == "/end" and self.command == "POST":
            self.ended()
            return
        if path != "/mcp":
            self.refuse(404, "not found")
            return
        if self.command != "POST":
            self.send(405, close=True, headers={"Allow": "POST"})
            return
        deps.on_request()
        accept = self.header("accept") or ""
        if "application/json" not in accept and "*/*" not in accept:
            self.refuse(406, "accept application/json")
            return
        try:
            body = self.body()
        except BodyError as e:
            self.refuse(e.status, "unreadable body")
            return
        try:
            message = parse(body.decode("utf-8", "replace"))
        except (ValueError, RecursionError):
            self.rpc_error(None, RpcError(PARSE_ERROR, "Parse error"))
            return
        self.message(message)

    def body(self) -> bytes:
        self.body_read = True
        chunked = "chunked" in (self.headers.get("transfer-encoding") or "").lower()
        length = self.headers.get("content-length")
        if chunked and length is not None:
            raise BodyError(400)
        if chunked:
            return self.chunks()
        try:
            size = int(length or 0)
        except ValueError:
            raise BodyError(400) from None
        if size < 0:
            raise BodyError(400)
        if size > MAX_BODY_BYTES:
            raise BodyError(413)
        data = self.rfile.read(size)
        if len(data) != size:
            raise BodyError(400)
        return data

    def chunks(self) -> bytes:
        out = bytearray()
        while True:
            line = self.rfile.readline(1024)
            try:
                size = int(line.split(b";", 1)[0].strip(), 16)
            except ValueError:
                raise BodyError(400) from None
            if size < 0:
                raise BodyError(400)
            if size == 0:
                while self.rfile.readline(1024).strip():
                    pass
                return bytes(out)
            if len(out) + size > MAX_BODY_BYTES:
                raise BodyError(413)
            chunk = self.rfile.read(size)
            if len(chunk) != size or self.rfile.readline(3).strip():
                raise BodyError(400)
            out += chunk

    def ended(self) -> None:
        pid = None
        with contextlib.suppress(BodyError, ValueError, RecursionError):
            data = parse(self.body().decode("utf-8", "replace"))
            value = data.get("pid") if isinstance(data, dict) else None
            pid = positive_int("" if value is None else js_string(value), 1)
        if pid is None:
            self.refuse(400, "pid must be a process id")
            return
        self.send(200, {"ended": self.front.deps.end(pid)})

    def message(self, msg: Any) -> None:
        version = self.header("mcp-protocol-version")
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
            self.rpc_error(None, RpcError(INVALID_REQUEST, NOT_ONE_MESSAGE))
            return

        def unsupported(request_id: Any) -> None:
            data = {"supported": [MODERN_PROTOCOL], "requested": version or ""}
            self.rpc_error(request_id, RpcError(UNSUPPORTED_VERSION, "Unsupported protocol version", data))

        if "id" not in msg:
            if version != MODERN_PROTOCOL:
                unsupported(None)
            else:
                self.send(202)
            return
        request_id = msg["id"]
        valid_number = isinstance(request_id, (int, float)) and not isinstance(request_id, bool) and math.isfinite(request_id)
        if not isinstance(request_id, str) and not valid_number:
            self.rpc_error(None, RpcError(INVALID_REQUEST, NOT_ONE_MESSAGE))
            return
        if version != MODERN_PROTOCOL:
            unsupported(request_id)
            return
        params = msg.get("params")
        meta = params.get("_meta") if isinstance(params, dict) else None
        if not isinstance(params, dict) or not isinstance(meta, dict) or VERSION_KEY not in meta or CAPABILITIES_KEY not in meta:
            text = f"params._meta must carry the {VERSION_KEY} and {CAPABILITIES_KEY} keys"
            self.rpc_error(request_id, RpcError(INVALID_PARAMS, text))
            return
        method = msg["method"]
        if meta[VERSION_KEY] != version:
            text = "mcp-protocol-version header does not match the request envelope's protocol version"
            self.rpc_error(request_id, RpcError(HEADER_MISMATCH, text))
            return
        if self.header("mcp-method") != method:
            self.rpc_error(request_id, RpcError(HEADER_MISMATCH, "mcp-method header does not match the request body's method"))
            return
        if (
            method == "tools/call"
            and isinstance(params.get("name"), str)
            and percent_decoded(self.header("mcp-name") or "") != params["name"]
        ):
            self.rpc_error(request_id, RpcError(HEADER_MISMATCH, "mcp-name header does not match the request body's name"))
            return
        try:
            result = self.dispatch(method, params)
        except RpcError as e:
            self.rpc_error(request_id, e)
            return
        except Exception as e:  # noqa: BLE001 - any tool failure becomes a JSON-RPC error, as the Node front answers
            self.rpc_error(request_id, RpcError(INTERNAL_ERROR, str(e)))
            return
        if result is None:
            self.rpc_error(request_id, RpcError(METHOD_NOT_FOUND, "Method not found", method))
            return
        meta_out = {SERVER_INFO_KEY: self.front.deps.server_info}
        self.send(200, {"jsonrpc": "2.0", "id": request_id, "result": {"resultType": "complete", "_meta": meta_out, **result}})

    def dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any] | None:
        deps = self.front.deps
        if method == "server/discover":
            capabilities = {"tools": {"listChanged": False}}
            instructions = deps.instructions(CLAUDE_AGENT)
            return {**LISTED, "supportedVersions": [MODERN_PROTOCOL], "capabilities": capabilities, "instructions": instructions}
        if method == "tools/list":
            return {"tools": deps.tools(CLAUDE_AGENT), **LISTED}
        if method == "ping":
            return {}
        if method in EMPTY_LISTS:
            return {EMPTY_LISTS[method]: [], **LISTED}
        if method == "tools/call":
            if not isinstance(params.get("name"), str):
                raise RpcError(INVALID_PARAMS, "tools/call needs a tool name")
            cancel = threading.Event()
            sock = cast(socket.socket, self.connection)
            self.front.drops.watch(sock, cancel)
            try:
                return deps.call(self.identity(), params, cancel)
            finally:
                self.front.drops.unwatch(sock)
        return None


def bind(port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if sys.platform == "win32":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)  # pyright: ignore[reportAttributeAccessIssue]
        else:
            # POSIX refuses a second listener either way; this only lets a new server bind past TIME_WAIT
            # connections that the server it took over from left behind.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))
        sock.listen(128)
    except BaseException:
        sock.close()
        raise
    return sock

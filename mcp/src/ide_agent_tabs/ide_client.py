from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, NamedTuple, Optional

from .clock import now_ms
from .jsjson import number, parse, stringify, utf16_slice
from .registry import Endpoint, split_url
from .version import PACKAGE_VERSION

if TYPE_CHECKING:
    import socket

USER_AGENT = f"ide-agent-tabs-mcp/{PACKAGE_VERSION}"
IDE_TIMEOUT_MS = 15_000

ROUTES = ("info", "agents", "open", "close", "list", "input", "reveal")

IdeCall = Callable[[Endpoint, str, Optional[dict]], dict]

_MAX_BODY = 64 * 1024 * 1024


class IdeError(Exception):
    def __init__(self, message: str, status: int | None = None, body: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class _Failed(Exception):
    pass


class _TimedOut(Exception):
    pass


class _Reply(NamedTuple):
    status: int
    body: bytes


def _socket() -> Any:
    import socket

    return socket


def _timeout() -> type[BaseException]:
    return _socket().timeout


def _header_safe(value: str) -> bool:
    return all(" " <= c <= "\xff" and c != "\x7f" for c in value)


class _Reader:
    def __init__(self, sock: socket.socket, deadline: float) -> None:
        self.sock = sock
        self.deadline = deadline
        self.buffer = b""
        self.closed = False

    def _fill(self) -> None:
        left = self.deadline - now_ms()
        if left <= 0:
            raise _TimedOut
        self.sock.settimeout(left / 1000)
        try:
            chunk = self.sock.recv(65536)
        except _timeout() as e:
            raise _TimedOut from e
        if not chunk:
            self.closed = True
        self.buffer += chunk
        if len(self.buffer) > _MAX_BODY:
            raise _Failed("response too large")

    def line(self) -> bytes:
        while b"\r\n" not in self.buffer:
            if self.closed:
                raise _Failed("connection closed")
            self._fill()
        head, _, self.buffer = self.buffer.partition(b"\r\n")
        return head

    def exactly(self, size: int) -> bytes:
        while len(self.buffer) < size:
            if self.closed:
                raise _Failed("connection closed")
            self._fill()
        data, self.buffer = self.buffer[:size], self.buffer[size:]
        return data

    def rest(self) -> bytes:
        while not self.closed:
            self._fill()
        data, self.buffer = self.buffer, b""
        return data


def _read_reply(reader: _Reader) -> _Reply:
    while True:
        status_line = reader.line().decode("latin-1")
        parts = status_line.split(" ", 2)
        if len(parts) < 2 or not parts[0].startswith("HTTP/") or not parts[1].isdigit():
            raise _Failed(f"bad status line {status_line!r}")
        status = int(parts[1])
        headers: dict[str, str] = {}
        while True:
            raw = reader.line()
            if raw == b"":
                break
            name, _, value = raw.decode("latin-1").partition(":")
            headers[name.strip().lower()] = value.strip()
        if not 100 <= status < 200:
            break
    if status in (204, 304):
        return _Reply(status, b"")
    if "chunked" in headers.get("transfer-encoding", "").lower():
        body = b""
        while True:
            size = int(reader.line().split(b";")[0].strip() or b"0", 16)
            if size == 0:
                break
            body += reader.exactly(size)
            reader.exactly(2)
        return _Reply(status, body)
    length = headers.get("content-length")
    if length is not None and length.isdigit():
        return _Reply(status, reader.exactly(int(length)))
    return _Reply(status, reader.rest())


def _post(url: str, token: str, payload: bytes, timeout_ms: float) -> _Reply:
    parts = split_url(url)
    if parts is None or parts.scheme not in ("http", "https") or not _header_safe(token):
        raise _Failed("bad request")
    deadline = now_ms() + timeout_ms
    host_header = f"[{parts.host}]" if ":" in parts.host else parts.host
    if parts.port != (443 if parts.scheme == "https" else 80):
        host_header += f":{parts.port}"
    head = (
        f"POST {parts.path or '/'} HTTP/1.1\r\n"
        f"Host: {host_header}\r\n"
        f"Authorization: Bearer {token}\r\n"
        "Content-Type: application/json\r\n"
        f"User-Agent: {USER_AGENT}\r\n"
        "Accept: */*\r\n"
        f"Content-Length: {len(payload)}\r\n"
        "Connection: close\r\n\r\n"
    )
    try:
        sock = _socket().create_connection((parts.host, parts.port), timeout=timeout_ms / 1000)
    except _timeout() as e:
        raise _TimedOut from e
    try:
        if parts.scheme == "https":
            import ssl

            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=parts.host)
        sock.settimeout(max(0.001, (deadline - now_ms()) / 1000))
        try:
            sock.sendall(head.encode("latin-1") + payload)
        except _timeout() as e:
            raise _TimedOut from e
        return _read_reply(_Reader(sock, deadline))
    finally:
        sock.close()


def _decode(raw: bytes) -> str:
    text = raw.decode("utf-8", "replace")
    return text.removeprefix("﻿")


def ide_caller(timeout_ms: float = IDE_TIMEOUT_MS) -> IdeCall:
    def call(endpoint: Endpoint, route: str, body: dict | None = None) -> dict:
        payload = stringify({} if body is None else body).encode("utf-8")
        try:
            reply = _post(f"{endpoint.url}/{route}", endpoint.token, payload, timeout_ms)
        except _TimedOut as e:
            reason = (
                f"no answer within {number(timeout_ms / 1000)} s. {endpoint.product} may be busy or showing a modal dialog; "
                "ask the user to check it, then retry once"
            )
            raise IdeError(f"{endpoint.id} {route} failed: {reason}") from e
        except (OSError, ValueError, _Failed) as e:
            raise IdeError(f"{endpoint.id} {route} failed: fetch failed") from e
        status = reply.status
        text = _decode(reply.body)
        try:
            value = parse(text)
        except (ValueError, RecursionError):
            raise IdeError(
                f"{endpoint.id} {route} answered HTTP {status} with non-JSON: {utf16_slice(text, 0, 500)}", status, text
            ) from None
        obj = value if isinstance(value, dict) else None
        if not (200 <= status <= 299) or obj is None or obj.get("ok") is not True:
            given = obj.get("error") if obj is not None else None
            error = given if isinstance(given, str) else text
            hint = _next_step(endpoint, route, status, error)
            raise IdeError(f"{endpoint.id} {route} answered HTTP {status}: {error}{f'. {hint}' if hint else ''}", status, value)
        return obj

    return call


def _next_step(endpoint: Endpoint, route: str, status: int, error: str) -> str | None:
    if status == 401:
        return (
            f"{endpoint.product} refused the token in {endpoint.id}, so that endpoint is stale. "
            "Run the Agent Tabs command line's list-ides for the current ids"
        )
    if status == 409 and route == "open":
        return (
            "To open the tab in a terminal instead, pass ide set to a terminal id such as windows-terminal, ghostty, kitty, wezterm or tmux"
        )
    if status == 503:
        return f"A modal dialog is likely open in {endpoint.product}. Ask the user to close it, then retry once"
    if error.startswith("unknown agent"):
        return "Call list_agents for the profile names"
    return None

from __future__ import annotations

import json
import re
import socket
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, NoReturn, Union

from ..messaging.sessions import SESSION_ID
from .policy import ALIAS

PROTOCOL_VERSION = 1
HELLO = "agent-tabs-net"
MAX_FRAME = 65536
MAX_DEPTH = 16
MAX_INT_DIGITS = 16
MAX_SESSIONS = 256
MAX_NAME_CHARS = 128
MAX_PATH_CHARS = 1024
MAX_FOLDERS = 64
MAX_TASK_CHARS = 30000
MAX_TEXT_CHARS = 32000
OPS = ("open", "close", "send", "inventory")
OPEN_AGENTS = ("claude", "codex")
SESSION_AGENTS = (
    "claude",
    "codex",
    "agy",
    "copilot",
    "grok",
    "pi",
    "hermes",
    "opencode",
    "qwen",
    "goose",
    "codex-lmstudio",
    "codex-ollama",
)
STATUSES = ("idle", "busy", "permission", "waking", "unknown")

_REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_SESSION_ID = SESSION_ID
_TAB_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_MESSAGE_ID = re.compile(r"m-[0-9a-f]{16}")
_MODEL = re.compile(r"[A-Za-z0-9._:/@+-]{1,200}")
_ABSOLUTE_PATH = re.compile(r"(?:[A-Za-z]:[\\/]|/(?![/\\])).*", re.DOTALL)
_BAD_NAME_CATEGORIES = frozenset({"Cc", "Cs", "Zl", "Zp"})
_ESCAPED_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})
_SHORT_ESCAPES = {"\\": "\\\\", "\n": "\\n", "\r": "\\r", "\t": "\\t"}


class WireError(Exception):
    pass


@dataclass(frozen=True)
class OpenBody:
    agent: str
    folder: str | None
    task: str
    model: str | None = None
    path: str | None = None


@dataclass(frozen=True)
class CloseBody:
    tab: str


@dataclass(frozen=True)
class SendBody:
    to: str
    text: str
    reply_to: str | None = None


@dataclass(frozen=True)
class InventoryBody:
    pass


Body = Union[OpenBody, CloseBody, SendBody, InventoryBody]


@dataclass(frozen=True)
class Request:
    id: str
    op: str
    body: Body


@dataclass(frozen=True)
class Reply:
    re: str
    ok: bool
    body: Mapping[str, Any] | None


@dataclass(frozen=True)
class RemoteSession:
    id: str
    name: str
    agent: str
    status: str


def _fail(reason: str) -> NoReturn:
    raise WireError(reason)


def _read_exact(sock: socket.socket, count: int) -> bytes:
    chunks: list[bytes] = []
    remaining = count
    while remaining > 0:
        chunk = sock.recv(min(remaining, 16384))
        if not chunk:
            _fail("connection closed mid-frame")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def write_frame(sock: socket.socket, payload: bytes) -> None:
    if len(payload) > MAX_FRAME:
        _fail("frame is too large")
    sock.sendall(len(payload).to_bytes(4, "big") + payload)


def read_frame(sock: socket.socket, *, max_size: int = MAX_FRAME) -> bytes:
    size = int.from_bytes(_read_exact(sock, 4), "big")
    if size == 0:
        _fail("empty frame")
    if size > max_size:
        _fail("frame is too large")
    return _read_exact(sock, size)


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail("duplicate key")
        result[key] = value
    return result


def _reject_constant(_name: str) -> NoReturn:
    _fail("NaN and Infinity are not allowed")


def _reject_float(_text: str) -> NoReturn:
    _fail("numbers must be integers")


def _parse_int(text: str) -> int:
    if len(text.lstrip("-")) > MAX_INT_DIGITS:
        _fail("integer is too large")
    return int(text)


def _check_depth(text: str) -> None:
    depth = 0
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
            if depth > MAX_DEPTH:
                _fail("nesting is too deep")
        elif char in "]}":
            depth -= 1


def _check_strings(value: object) -> None:
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            if any("\ud800" <= char <= "\udfff" for char in item):
                _fail("unpaired surrogate in a string")
        elif isinstance(item, dict):
            for key, child in item.items():
                pending.append(key)
                pending.append(child)
        elif isinstance(item, list):
            pending.extend(item)


def parse_json(data: bytes) -> object:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        _fail("not valid UTF-8")
    _check_depth(text)
    try:
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicates,
            parse_constant=_reject_constant,
            parse_float=_reject_float,
            parse_int=_parse_int,
        )
    except (ValueError, RecursionError):
        _fail("not valid JSON")
    _check_strings(value)
    return value


def encode(obj: object) -> bytes:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def _object(value: object, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(f"{what} must be an object")
    return value


def _only(obj: dict[str, Any], required: Sequence[str], optional: Sequence[str], what: str) -> None:
    for key in obj:
        if key not in required and key not in optional:
            _fail(f"unknown field in {what}")
    for key in required:
        if key not in obj:
            _fail(f"{what} is missing {key}")


def _text(value: object, field: str, pattern: re.Pattern[str] | None = None, max_chars: int | None = None) -> str:
    if not isinstance(value, str):
        _fail(f"{field} must be a string")
    if pattern is not None and pattern.fullmatch(value) is None:
        _fail(f"{field} is malformed")
    if max_chars is not None and not 1 <= len(value) <= max_chars:
        _fail(f"{field} has a bad length")
    return value


def _one_of(value: object, field: str, allowed: Sequence[str]) -> str:
    if not isinstance(value, str) or value not in allowed:
        _fail(f"{field} is not allowed")
    return value


def check_hello(obj: object) -> None:
    frame = _object(obj, "hello")
    _only(frame, ("v", "hello"), (), "hello")
    version = frame["v"]
    if isinstance(version, bool) or not isinstance(version, int) or version != PROTOCOL_VERSION:
        _fail("unsupported protocol version")
    if frame["hello"] != HELLO:
        _fail("not an agent-tabs-net hello")


def hello_frame() -> bytes:
    return encode({"v": PROTOCOL_VERSION, "hello": HELLO})


def valid_path(value: object) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= MAX_PATH_CHARS
        and _ABSOLUTE_PATH.fullmatch(value) is not None
        and not any(unicodedata.category(char) in _ESCAPED_CATEGORIES for char in value)
    )


def _path(value: object, field: str) -> str:
    if not valid_path(value):
        _fail(f"{field} is malformed")
    return str(value)


def _parse_open(body: dict[str, Any]) -> OpenBody:
    _only(body, ("agent", "task"), ("folder", "path", "model"), "open body")
    if ("folder" in body) == ("path" in body):
        _fail("open body needs exactly one of folder and path")
    return OpenBody(
        agent=_one_of(body["agent"], "agent", OPEN_AGENTS),
        folder=_text(body["folder"], "folder", ALIAS) if "folder" in body else None,
        task=_text(body["task"], "task", None, MAX_TASK_CHARS),
        model=_text(body["model"], "model", _MODEL) if "model" in body else None,
        path=_path(body["path"], "path") if "path" in body else None,
    )


def _parse_close(body: dict[str, Any]) -> CloseBody:
    _only(body, ("tab",), (), "close body")
    return CloseBody(tab=_text(body["tab"], "tab", _TAB_ID))


def _parse_send(body: dict[str, Any]) -> SendBody:
    _only(body, ("to", "text"), ("replyTo",), "send body")
    return SendBody(
        to=_text(body["to"], "to", _SESSION_ID),
        text=_text(body["text"], "text", None, MAX_TEXT_CHARS),
        reply_to=_text(body["replyTo"], "replyTo", _MESSAGE_ID) if "replyTo" in body else None,
    )


def _parse_inventory_request(body: dict[str, Any]) -> InventoryBody:
    _only(body, (), (), "inventory body")
    return InventoryBody()


def parse_request(obj: object) -> Request:
    frame = _object(obj, "request")
    _only(frame, ("id", "op", "body"), (), "request")
    request_id = _text(frame["id"], "id", _REQUEST_ID)
    op = _one_of(frame["op"], "op", OPS)
    body = _object(frame["body"], "body")
    if op == "open":
        return Request(request_id, op, _parse_open(body))
    if op == "close":
        return Request(request_id, op, _parse_close(body))
    if op == "send":
        return Request(request_id, op, _parse_send(body))
    return Request(request_id, op, _parse_inventory_request(body))


def parse_reply(obj: object) -> Reply:
    frame = _object(obj, "reply")
    if "ok" not in frame or not isinstance(frame["ok"], bool):
        _fail("ok must be a boolean")
    if frame["ok"]:
        _only(frame, ("re", "ok", "body"), (), "reply")
        return Reply(_text(frame["re"], "re", _REQUEST_ID), True, _object(frame["body"], "body"))
    _only(frame, ("re", "ok", "refused"), (), "reply")
    if frame["refused"] is not True:
        _fail("refused must be true")
    return Reply(_text(frame["re"], "re", _REQUEST_ID), False, None)


def ok_reply(request_id: str, body: Mapping[str, Any]) -> dict[str, Any]:
    return {"re": request_id, "ok": True, "body": dict(body)}


def refused_reply(request_id: str) -> dict[str, Any]:
    return {"re": request_id, "ok": False, "refused": True}


def _session_name(value: object) -> str:
    if not isinstance(value, str):
        _fail("name must be a string")
    if len(value) > MAX_NAME_CHARS:
        _fail("name is too long")
    if any(unicodedata.category(char) in _BAD_NAME_CATEGORIES for char in value):
        _fail("name has a control character")
    return value


def parse_inventory(body: object) -> list[RemoteSession]:
    frame = _object(body, "inventory")
    _only(frame, ("sessions",), ("folders",), "inventory")
    rows = frame["sessions"]
    if not isinstance(rows, list):
        _fail("sessions must be an array")
    if len(rows) > MAX_SESSIONS:
        _fail("too many sessions")
    seen: set[str] = set()
    sessions: list[RemoteSession] = []
    for row in rows:
        item = _object(row, "session")
        _only(item, ("id", "name", "agent", "status"), (), "session")
        session_id = _text(item["id"], "id", _SESSION_ID)
        if session_id in seen:
            _fail("duplicate session id")
        seen.add(session_id)
        sessions.append(
            RemoteSession(
                id=session_id,
                name=_session_name(item["name"]),
                agent=_one_of(item["agent"], "agent", SESSION_AGENTS),
                status=_one_of(item["status"], "status", STATUSES),
            )
        )
    return sessions


def parse_inventory_folders(body: object) -> list[str] | None:
    frame = _object(body, "inventory")
    if "folders" not in frame:
        return None
    rows = frame["folders"]
    if not isinstance(rows, list):
        _fail("folders must be an array")
    if len(rows) > MAX_FOLDERS:
        _fail("too many folders")
    folders = [_path(row, "folder") for row in rows]
    if len(set(folders)) != len(folders):
        _fail("duplicate folder")
    return folders


def inventory_body(sessions: Sequence[RemoteSession], folders: Sequence[str] | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"sessions": [{"id": s.id, "name": s.name, "agent": s.agent, "status": s.status} for s in sessions]}
    if folders is not None:
        body["folders"] = list(folders)
    parse_inventory(body)
    parse_inventory_folders(body)
    return body


def _escape_char(char: str) -> str:
    short = _SHORT_ESCAPES.get(char)
    if short is not None:
        return short
    if unicodedata.category(char) not in _ESCAPED_CATEGORIES:
        return char
    code = ord(char)
    if code <= 0xFF:
        return f"\\x{code:02x}"
    if code <= 0xFFFF:
        return f"\\u{code:04x}"
    return f"\\U{code:08x}"


def escape_display(name: str) -> str:
    return "".join(_escape_char(char) for char in name)

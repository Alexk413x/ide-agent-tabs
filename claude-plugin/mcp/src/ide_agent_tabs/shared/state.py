from __future__ import annotations

import contextlib
import os
import re
from typing import Any, NamedTuple
from urllib.parse import urlsplit

from ..files import ensure_private_dir, read_text_if_exists, remove_file, write_atomically, write_new_private_file
from ..jsjson import parse, stringify, trim

SERVICE = "ide-agent-tabs"
DEFAULT_PORT = 47828
SERVER_DIR = "server"
TOKEN_FILE = "token"
PORT_OPTION_ENV = "CLAUDE_PLUGIN_OPTION_SERVER_PORT"
SERVER_LAUNCHER = "shared_server.py"
HEADER_CLIENT = "x-agent-tabs-client"
HEADER_TAB = "x-agent-tabs-tab"
HEADER_AGENT = "x-agent-tabs-agent"
HEADER_PID = "x-agent-tabs-pid"
HEADER_PID_START = "x-agent-tabs-pid-start"
_PORT = re.compile("[0-9]{1,5}")
_TOKEN = re.compile("[0-9a-f]{64}")


class ServerState(NamedTuple):
    pid: float
    port: int
    version: str
    started_at: str
    shutdown_token: str


def server_dir(home: str) -> str:
    return os.path.join(home, SERVER_DIR)


def token_path(home: str) -> str:
    return os.path.join(server_dir(home), TOKEN_FILE)


def state_path(home: str, port: int) -> str:
    return os.path.join(server_dir(home), f"state-{port}.json")


def parse_port(value: str | None) -> int | None:
    if value is None or not _PORT.fullmatch(trim(value)):
        return None
    port = int(trim(value))
    return port if 0 < port <= 65535 else None


def port_from_url(value: str | None) -> int | None:
    if not value:
        return None
    try:
        url = urlsplit(value)
        if url.scheme != "http" or url.hostname != "127.0.0.1":
            return None
        return parse_port(str(url.port) if url.port is not None else None)
    except ValueError:
        return None


def new_token() -> str:
    return os.urandom(32).hex()


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def read_token(home: str) -> str | None:
    try:
        text = read_text_if_exists(token_path(home))
    except OSError:
        return None
    if text is None:
        return None
    text = trim(text)
    return text if _TOKEN.fullmatch(text) else None


def read_state(home: str, port: int) -> ServerState | None:
    try:
        data = parse(read_text_if_exists(state_path(home, port)) or "")
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    pid = data.get("pid")
    token = data.get("shutdownToken")
    version = data.get("version")
    if not isinstance(pid, (int, float)) or isinstance(pid, bool) or not _is_number(data.get("port")) or data.get("port") != port:
        return None
    if not isinstance(token, str) or not isinstance(version, str):
        return None
    started = data.get("startedAt")
    return ServerState(pid, port, version, "" if started is None else str(started), token)


def ensure_token(home: str) -> str:
    known = read_token(home)
    if known is not None:
        return known
    ensure_private_dir(server_dir(home))
    with contextlib.suppress(FileExistsError):
        write_new_private_file(token_path(home), new_token())
    token = read_token(home)
    if token is None:
        raise RuntimeError(f"{token_path(home)} holds no valid token; delete it and start again")
    return token


def write_state(home: str, state: ServerState) -> None:
    body = {
        "pid": state.pid,
        "port": state.port,
        "version": state.version,
        "startedAt": state.started_at,
        "shutdownToken": state.shutdown_token,
    }
    write_atomically(state_path(home, state.port), stringify(body, 2) + "\n")


def remove_state(home: str, port: int, pid: int) -> None:
    state = read_state(home, port)
    if state is not None and state.pid == pid:
        with contextlib.suppress(OSError):
            remove_file(state_path(home, port))

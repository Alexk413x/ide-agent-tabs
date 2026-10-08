from __future__ import annotations

import os
import re
from typing import Any, NamedTuple

from ..files import read_text_if_exists
from ..jsjson import parse, trim

SERVICE = "ide-agent-tabs"
DEFAULT_PORT = 47828
SERVER_DIR = "server"
TOKEN_FILE = "token"
PORT_OPTION_ENV = "CLAUDE_PLUGIN_OPTION_SERVER_PORT"
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

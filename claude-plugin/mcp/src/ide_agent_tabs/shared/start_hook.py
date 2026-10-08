from __future__ import annotations

import os
import threading
from collections.abc import Mapping

from ..home import agent_tabs_home
from ..jsjson import parse
from ..version import PACKAGE_VERSION
from .client import ensure_server, notify_end, server_command
from .state import DEFAULT_PORT, PORT_OPTION_ENV, parse_port

STDIN_WAIT_S = 1.0


def read_stdin(fd: int = 0, wait_s: float = STDIN_WAIT_S) -> bytes:
    chunks: list[bytes] = []

    def pump() -> None:
        try:
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    return
                chunks.append(chunk)
        except OSError:
            return

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    reader.join(wait_s)
    return b"".join(chunks)


def _reason(raw: bytes) -> object:
    try:
        data = parse(raw.decode("utf-8", "replace"))
    except ValueError:
        return None
    return data.get("reason") if isinstance(data, dict) else None


def run(event: str, env: Mapping[str, str], launcher: str, stdin: bytes | None = None, version: str = PACKAGE_VERSION) -> str | None:
    port = parse_port(env.get(PORT_OPTION_ENV)) or DEFAULT_PORT
    home = agent_tabs_home(env)
    if event == "SessionEnd":
        reason = _reason(read_stdin() if stdin is None else stdin)
        text = env.get("CLAUDE_PID") or ""
        pid = int(text) if text.isdigit() and len(text) <= 16 else 0
        if reason != "clear" and pid > 0:
            notify_end(home, port, pid)
        return None
    if event != "SessionStart":
        return None
    ensured = ensure_server(server_command(launcher, port), port, home, version, env=env)
    return None if ensured.problem is None else f"Agent Tabs: {ensured.problem}."

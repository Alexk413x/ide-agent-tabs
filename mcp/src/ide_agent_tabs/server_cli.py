from __future__ import annotations

import os
import sys
from collections.abc import Mapping, Sequence
from typing import Any, Callable

from .home import agent_tabs_home
from .jsjson import stringify
from .shared.client import probe, stop_server
from .shared.state import DEFAULT_PORT, PORT_OPTION_ENV, parse_port

USAGE = "server status|stop [--port <port>]"


def _stdout(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


def main(args: Sequence[str], env: Mapping[str, str] | None = None, out: Callable[[str], None] = _stdout) -> int:
    env = os.environ if env is None else env
    action, rest = (args[0], list(args[1:])) if args else ("", [])
    at = rest.index("--port") if "--port" in rest else -1
    given = None if at == -1 else parse_port(rest[at + 1] if at + 1 < len(rest) else None)
    if action not in ("status", "stop") or (at != -1 and given is None) or len(rest) != (0 if at == -1 else 2):
        out(stringify({"error": f"Usage: agent-tabs {USAGE}"}) + "\n")
        return 2
    port = given or parse_port(env.get(PORT_OPTION_ENV)) or DEFAULT_PORT
    home = agent_tabs_home(env)
    if action == "stop":
        result = stop_server(home, port)
        out(stringify({"port": port, **result}, 2) + "\n")
        return 0 if result["stopped"] else 1
    found = probe(port)
    status: dict[str, Any]
    if found.kind == "ours" and found.health is not None:
        status = {"running": True, **found.health}
    else:
        status = {"port": port, "running": False}
        if found.kind == "other":
            status["problem"] = f"port {port} belongs to another program"
    out(stringify(status, 2) + "\n")
    return 0 if found.kind == "ours" else 1

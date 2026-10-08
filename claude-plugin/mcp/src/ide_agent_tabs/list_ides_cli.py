from __future__ import annotations

import os
import sys
from collections.abc import Sequence
from typing import Any, BinaryIO, Callable

from .home import agent_tabs_home
from .ide_client import ide_caller
from .ide_installs import node_platform
from .jsjson import stringify
from .service import Service, ServiceDeps
from .terminals import TERMINAL_DRIVERS
from .terminals.shell import LAUNCHER_PS1

DETECTION_MAX_AGE_MS = 60 * 60 * 1000
USAGE = "list-ides"

_PACKAGE = os.path.dirname(os.path.abspath(__file__))
_PLUGIN = os.path.dirname(os.path.dirname(os.path.dirname(_PACKAGE)))


def scripts_dir() -> str:
    candidates = [
        os.path.join(_PACKAGE, "launch"),
        os.path.join(os.path.dirname(_PACKAGE), "launch"),
        os.path.join(_PLUGIN, "dist", "launch"),
    ]
    return next((d for d in candidates if os.path.exists(os.path.join(d, LAUNCHER_PS1))), candidates[0])


class SessionPresence:
    def read(self, home: str, session_id: str) -> dict[str, Any] | None:
        from .messaging.sessions import read_presence

        return read_presence(home, session_id)

    def update(self, home: str, session_id: str, change: Callable[[dict[str, Any] | None], dict[str, Any] | None]) -> Any:
        from .messaging.sessions import update_presence

        return update_presence(home, session_id, change)

    def with_state(self, p: dict[str, Any], state: str, now: int) -> dict[str, Any]:
        from .messaging.sessions import with_state

        return with_state(p, state, now)


def system_service(home: str, log: Callable[[str], None] | None = None) -> Service:
    return Service(
        ServiceDeps(
            home=home,
            scripts_dir=scripts_dir(),
            platform=node_platform(),
            env=os.environ,
            call_ide=ide_caller(),
            drivers=TERMINAL_DRIVERS,
            log=log,
            presence=SessionPresence(),
        )
    )


def _write(stream: BinaryIO, text: str) -> None:
    stream.write(text.encode("utf-8", "surrogatepass"))
    stream.flush()


def cli_list_ides(argv: Sequence[str], stdout: BinaryIO | None = None, stderr: BinaryIO | None = None) -> int:
    out = stdout if stdout is not None else sys.stdout.buffer
    err = stderr if stderr is not None else sys.stderr.buffer
    try:
        if argv:
            from .cli import usage

            raise ValueError(f"list-ides takes no arguments. {usage()}")
        listed = system_service(agent_tabs_home()).list_ides(DETECTION_MAX_AGE_MS)
        _write(out, stringify(listed, 2) + "\n")
        return 0
    except Exception as e:  # noqa: BLE001
        _write(err, stringify({"error": str(e)}) + "\n")
        return 1


def main(args: Sequence[str]) -> int:
    return cli_list_ides(args)


def agent_profiles() -> list[dict[str, Any]]:
    return system_service(agent_tabs_home()).list_agents()["agents"]

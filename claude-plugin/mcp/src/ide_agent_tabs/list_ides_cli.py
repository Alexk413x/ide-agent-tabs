from __future__ import annotations

import os
import sys
from collections.abc import Sequence
from typing import BinaryIO, Callable

from .home import agent_tabs_home
from .ide_client import ide_caller
from .ide_installs import node_platform
from .jsjson import stringify
from .service import Service, ServiceDeps
from .terminals import TERMINAL_DRIVERS
from .terminals.shell import LAUNCHER_PS1

DETECTION_MAX_AGE_MS = 60 * 60 * 1000
CLI_USAGE = "Usage: agent-tabs list-ides | jev <subcommand> | server status|stop [--port <port>]"

_PACKAGE = os.path.dirname(os.path.abspath(__file__))
_PLUGIN = os.path.dirname(os.path.dirname(os.path.dirname(_PACKAGE)))


def scripts_dir() -> str:
    candidates = [
        os.path.join(_PACKAGE, "launch"),
        os.path.join(os.path.dirname(_PACKAGE), "launch"),
        os.path.join(_PLUGIN, "dist", "launch"),
    ]
    return next((d for d in candidates if os.path.exists(os.path.join(d, LAUNCHER_PS1))), candidates[0])


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
            raise ValueError(f"list-ides takes no arguments. {CLI_USAGE}")
        listed = system_service(agent_tabs_home()).list_ides(DETECTION_MAX_AGE_MS)
        _write(out, stringify(listed, 2) + "\n")
        return 0
    except Exception as e:  # noqa: BLE001
        _write(err, stringify({"error": str(e)}) + "\n")
        return 1

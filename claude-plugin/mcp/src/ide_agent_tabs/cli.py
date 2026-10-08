from __future__ import annotations

import importlib
import sys
from collections.abc import Sequence

from .jsjson import stringify
from .processes import utf8_stdio

COMMANDS: dict[str, str] = {
    "jev": "ide_agent_tabs.jev.cli",
    "server": "ide_agent_tabs.server_cli",
}


def usage() -> str:
    parts = [importlib.import_module(module).USAGE for module in COMMANDS.values()]
    return f"Usage: agent-tabs {' | '.join(parts)}"


def main(argv: Sequence[str]) -> int:
    utf8_stdio()
    if not argv or argv[0] not in COMMANDS:
        sys.stderr.write(stringify({"error": usage()}) + "\n")
        return 2
    return importlib.import_module(COMMANDS[argv[0]]).main(list(argv[1:]))

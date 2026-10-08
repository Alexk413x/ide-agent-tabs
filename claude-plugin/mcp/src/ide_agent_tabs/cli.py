from __future__ import annotations

import importlib
import sys
from collections.abc import Sequence

from .jsjson import stringify
from .processes import utf8_stdio

COMMANDS: dict[str, str] = {
    "list-ides": "ide_agent_tabs.list_ides_cli",
    "jev": "ide_agent_tabs.jev.cli",
    "server": "ide_agent_tabs.server_cli",
    "sync-ides": "ide_agent_tabs.sync_cli",
}


def usage() -> str:
    parts = [importlib.import_module(module).USAGE for module in COMMANDS.values()]
    return f"Usage: agent-tabs {' | '.join(parts)}"


def main(argv: Sequence[str]) -> int:
    utf8_stdio()
    if not argv or argv[0] not in COMMANDS:
        sys.stderr.write(stringify({"error": usage()}) + "\n")
        return 2
    module = importlib.import_module(COMMANDS[argv[0]])
    if argv[0] == "jev":
        from .list_ides_cli import agent_profiles

        return module.main(list(argv[1:]), profiles=agent_profiles)
    return module.main(list(argv[1:]))

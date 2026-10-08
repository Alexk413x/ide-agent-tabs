from __future__ import annotations

import os
import re
import threading
from collections.abc import Mapping

from ..home import agent_tabs_home
from ..version import PACKAGE_VERSION
from .ancestry import LOOKUP_TIMEOUT_S, ProcessInfo, find_agent_process
from .client import START_WAIT_MS, ensure_server, server_command
from .state import DEFAULT_PORT, PORT_OPTION_ENV, SERVER_LAUNCHER, parse_port, port_from_url

_TAB_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_AGENT_NAME = re.compile(r"[A-Za-z0-9._-]{1,64}")


def launcher_path(plugin_root: str) -> str:
    return os.path.join(plugin_root, "mcp", "launch", SERVER_LAUNCHER)


# A project's .claude/settings.json can set environment variables, so the tab id is only a claim: the server
# binds it when no other live process holds that tab.
def helper_headers(
    env: Mapping[str, str],
    plugin_root: str,
    lookup_timeout_s: float = LOOKUP_TIMEOUT_S,
    wait_ms: float = START_WAIT_MS,
    version: str = PACKAGE_VERSION,
) -> dict[str, str]:
    port = port_from_url(env.get("CLAUDE_CODE_MCP_SERVER_URL")) or parse_port(env.get(PORT_OPTION_ENV)) or DEFAULT_PORT
    home = agent_tabs_home(env)
    headers = {"X-Agent-Tabs-Client": os.urandom(12).hex()}
    tab = env.get("IDE_AGENT_TABS_ID")
    if tab is not None and _TAB_ID.fullmatch(tab) and not tab.startswith(("s-", "codex-")):
        headers["X-Agent-Tabs-Tab"] = tab
    agent = env.get("IDE_AGENT_TABS_AGENT")
    if agent is not None and _AGENT_NAME.fullmatch(agent):
        headers["X-Agent-Tabs-Agent"] = agent
    found: list[ProcessInfo | None] = [None]

    def lookup() -> None:
        found[0] = find_agent_process(None, lookup_timeout_s)

    finder = threading.Thread(target=lookup, daemon=True)
    finder.start()
    try:
        ensured = ensure_server(server_command(launcher_path(plugin_root), port), port, home, version, wait_ms, env)
    except Exception:  # noqa: BLE001 - the helper always prints headers; a missing token is reported by the server's 403
        ensured = None
    finder.join(lookup_timeout_s)
    agent_process = found[0]
    if agent_process is not None:
        headers["X-Agent-Tabs-Pid"] = str(agent_process.pid)
        headers["X-Agent-Tabs-Pid-Start"] = str(max(0, round(agent_process.start_ms)))
    if ensured is not None and ensured.token is not None:
        headers["Authorization"] = f"Bearer {ensured.token}"
    return headers

from __future__ import annotations

import os
import threading
from collections.abc import Mapping
from urllib.parse import quote

from ..home import agent_tabs_home
from ..version import PACKAGE_VERSION
from .ancestry import LOOKUP_TIMEOUT_S, ProcessInfo, find_agent_process
from .client import ensure_server, server_command
from .state import (
    AGENT_NAME,
    CODEX_ID_PREFIX,
    DEFAULT_PORT,
    GENERATED_ID_PREFIX,
    PORT_OPTION_ENV,
    SERVER_LAUNCHER,
    TAB_ID,
    parse_port,
    port_from_url,
)

# Claude Code gives a headers helper 10 s, and a helper that prints no token gets the server marked as needing
# auth until the user clears it; so the helper waits for a slow first start up to well inside that limit.
HELPER_START_WAIT_MS = 7_000
MAX_CWD_CHARS = 1024


# Codex hands a headers helper a stripped environment, so a Codex tab passes what Claude Code passes in the
# environment as arguments: --tab, --agent, --port and --home.
_OPTION_ENV = {"--tab": "IDE_AGENT_TABS_ID", "--agent": "IDE_AGENT_TABS_AGENT", "--port": PORT_OPTION_ENV, "--home": "IDE_AGENT_TABS_HOME"}


def option_env(argv: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for flag, value in zip(argv[::2], argv[1::2]):
        name = _OPTION_ENV.get(flag)
        if name is not None:
            out[name] = value
    return out


def launcher_path(plugin_root: str) -> str:
    return os.path.join(plugin_root, "mcp", "launch", SERVER_LAUNCHER)


# A project's .claude/settings.json can set environment variables, so the tab id is only a claim: the server
# binds it when no other live process holds that tab.
def helper_headers(
    env: Mapping[str, str],
    plugin_root: str,
    lookup_timeout_s: float = LOOKUP_TIMEOUT_S,
    wait_ms: float = HELPER_START_WAIT_MS,
    version: str = PACKAGE_VERSION,
    launcher: str | None = None,
) -> dict[str, str]:
    port = port_from_url(env.get("CLAUDE_CODE_MCP_SERVER_URL")) or parse_port(env.get(PORT_OPTION_ENV)) or DEFAULT_PORT
    home = agent_tabs_home(env)
    headers = {"X-Agent-Tabs-Client": os.urandom(12).hex()}
    tab = env.get("IDE_AGENT_TABS_ID")
    if tab is not None and TAB_ID.fullmatch(tab) and not tab.startswith((GENERATED_ID_PREFIX, CODEX_ID_PREFIX)):
        headers["X-Agent-Tabs-Tab"] = tab
    agent = env.get("IDE_AGENT_TABS_AGENT")
    if agent is not None and AGENT_NAME.fullmatch(agent):
        headers["X-Agent-Tabs-Agent"] = agent
    try:
        cwd = os.getcwd()
    except OSError:
        cwd = ""
    if cwd and len(cwd) <= MAX_CWD_CHARS:
        headers["X-Agent-Tabs-Cwd"] = quote(cwd, safe="/: \\")
    found: list[ProcessInfo | None] = [None]

    def lookup() -> None:
        found[0] = find_agent_process(None, lookup_timeout_s)

    finder = threading.Thread(target=lookup, daemon=True)
    finder.start()
    try:
        ensured = ensure_server(server_command(launcher or launcher_path(plugin_root), port), port, home, version, wait_ms, env)
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

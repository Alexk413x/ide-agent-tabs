from __future__ import annotations

import os
import sys

STDIN_WAIT_S = 2.0
OWNER_LOOKUP_S = 2.0


def _parse(raw: bytes) -> dict[str, object]:
    from ide_agent_tabs.jsjson import parse

    try:
        data = parse(raw.decode("utf-8", "replace"))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def main() -> None:
    tab = os.environ.get("IDE_AGENT_TABS_ID")
    if not tab or tab == os.environ.get("IDE_AGENT_TABS_MOD"):
        return
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
    from ide_agent_tabs.home import agent_tabs_home
    from ide_agent_tabs.jsjson import stringify
    from ide_agent_tabs.messaging.hook import HOOK_EVENTS, run_hook
    from ide_agent_tabs.processes import read_stdin

    args = sys.argv[1:]
    cli = args[0] if args else ""
    event = args[1] if len(args) > 1 else ""
    process = None
    if HOOK_EVENTS.get(cli, {}).get(event, {}).get("owner"):
        from ide_agent_tabs.shared.ancestry import find_agent_process

        found = find_agent_process(None, OWNER_LOOKUP_S)
        process = None if found is None else (found.pid, max(0, round(found.start_ms)))
    output = run_hook(cli, event, _parse(read_stdin(STDIN_WAIT_S)), agent_tabs_home(), tab, agent_process=process)
    if output is not None:
        os.write(1, (stringify(output) + "\n").encode("utf-8"))


if __name__ == "__main__":
    # A hook that fails must not fail the agent's turn, and a daemon thread may still block in a read of stdin:
    # _exit drops any exception and skips the interpreter shutdown that would wait on that thread.
    try:
        main()
    finally:
        os._exit(0)

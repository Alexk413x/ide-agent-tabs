from __future__ import annotations

import os
import sys


def main() -> None:
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, os.path.join(os.path.dirname(here), "src"))
    from ide_agent_tabs.shared.start_hook import run
    from ide_agent_tabs.shared.state import SERVER_LAUNCHER

    line = run(sys.argv[1] if len(sys.argv) > 1 else "", os.environ, os.path.join(here, SERVER_LAUNCHER))
    if line is not None:
        os.write(1, (line + "\n").encode("utf-8"))


if __name__ == "__main__":
    # A hook that fails must not fail the session, and a stdin reader thread may still block: _exit drops any
    # exception and skips the interpreter shutdown that would wait on that thread.
    try:
        main()
    finally:
        os._exit(0)

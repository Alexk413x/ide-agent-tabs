from __future__ import annotations

import os
import sys

if sys.version_info < (3, 9):  # noqa: UP036
    sys.stderr.write('{"error":"Agent Tabs needs Python 3.9 or later."}\n')
    sys.exit(1)

# -I leaves the script's folder off sys.path, so the package folder is added by hand.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from ide_agent_tabs.cli import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

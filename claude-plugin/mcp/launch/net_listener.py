from __future__ import annotations

import os
import sys

# -I leaves the script's folder off sys.path, so the package folder is added by hand.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from ide_agent_tabs.net.listener import main

if __name__ == "__main__":
    # Connection threads still waiting run as daemons; _exit ends them instead of waiting at interpreter shutdown.
    os._exit(main(sys.argv[1:]))

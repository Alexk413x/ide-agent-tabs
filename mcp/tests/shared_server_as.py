from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(sys.path[0])), "claude-plugin", "mcp", "src"))

from ide_agent_tabs.shared.server import load_host, main
from store_host import StoreHost

# Runs the shared server as another build: shared_server_as.py <version> [--store] --port <port>. --store serves
# the messaging tools from store_host.StoreHost instead of the tool layer.
if __name__ == "__main__":
    args = sys.argv[2:]
    store = "--store" in args
    rest = [a for a in args if a != "--store"]
    code = main(rest, version=sys.argv[1], load=(lambda home, _log: StoreHost(home)) if store else load_host)
    os._exit(code)

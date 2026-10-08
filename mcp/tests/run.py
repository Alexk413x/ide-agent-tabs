from __future__ import annotations

import os
import sys
import unittest

TESTS = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(os.path.dirname(TESTS)), "claude-plugin", "mcp", "src")


def main(argv: list[str]) -> int:
    sys.path[:0] = [SRC, TESTS]
    verbosity = 2 if "-v" in argv else 1
    names = [a for a in argv if a != "-v"]
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromNames(names) if names else loader.discover(TESTS, pattern="test_*.py", top_level_dir=TESTS)
    print(f"Python {sys.version.split()[0]} at {sys.executable}", flush=True)
    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

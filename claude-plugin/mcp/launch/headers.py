from __future__ import annotations

import os
import sys


def main() -> None:
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(os.path.dirname(here))
    headers: dict[str, str] = {}
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(here), "src"))
        from ide_agent_tabs.jsjson import stringify
        from ide_agent_tabs.shared.headers import helper_headers, option_env

        env = {**os.environ, **option_env(sys.argv[1:])}
        headers = helper_headers(env, root, launcher=os.path.join(here, "shared_server.py"))
        text = stringify(headers)
    except Exception:  # noqa: BLE001 - Claude Code needs a JSON object even when the lookup fails
        import json

        text = json.dumps(headers)
    os.write(1, (text + "\n").encode("utf-8"))


if __name__ == "__main__":
    # A process lookup thread may still run; _exit skips the interpreter shutdown that would wait on it.
    try:
        main()
    finally:
        os._exit(0)

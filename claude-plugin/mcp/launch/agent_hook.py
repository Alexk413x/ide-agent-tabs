from __future__ import annotations

import os
import sys

STDIN_WAIT_S = 2.0


def _read_stdin() -> bytes:
    import threading

    chunks: list[bytes] = []

    def pump() -> None:
        try:
            while True:
                chunk = os.read(0, 65536)
                if not chunk:
                    return
                chunks.append(chunk)
        except OSError:
            return

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    reader.join(STDIN_WAIT_S)
    return b"".join(chunks)


def _parse(raw: bytes) -> dict[str, object]:
    from ide_agent_tabs.jsjson import parse

    try:
        data = parse(raw.decode("utf-8", "replace"))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _remember_interpreter(cache: str) -> None:
    if sys.version_info < (3, 9) or not sys.executable:
        return
    try:
        os.makedirs(os.path.dirname(cache), exist_ok=True)
        with open(cache, "w", encoding="utf-8") as f:
            f.write(sys.executable.replace("\\", "/") + "\n")
    except OSError:
        return


def main() -> None:
    cache = os.environ.get("IDE_AGENT_TABS_HOOK_PYTHON")
    if cache:
        _remember_interpreter(cache)
    tab = os.environ.get("IDE_AGENT_TABS_ID")
    if not tab or tab == os.environ.get("IDE_AGENT_TABS_MOD"):
        return
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
    from ide_agent_tabs.home import agent_tabs_home
    from ide_agent_tabs.jsjson import stringify
    from ide_agent_tabs.messaging.hook import run_hook

    args = sys.argv[1:]
    cli = args[0] if args else ""
    event = args[1] if len(args) > 1 else ""
    output = run_hook(cli, event, _parse(_read_stdin()), agent_tabs_home(), tab)
    if output is not None:
        os.write(1, (stringify(output) + "\n").encode("utf-8"))


if __name__ == "__main__":
    # A hook that fails must not fail the agent's turn, and a daemon thread may still block in a read of stdin:
    # _exit drops any exception and skips the interpreter shutdown that would wait on that thread.
    try:
        main()
    finally:
        os._exit(0)

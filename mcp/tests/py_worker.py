from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, Callable

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "claude-plugin", "mcp", "src")
)

from ide_agent_tabs.clock import now_ms
from ide_agent_tabs.files import file_lock
from ide_agent_tabs.processes import utf8_stdio

MODES: dict[str, Callable[[dict[str, Any]], None]] = {}


def mode(fn: Callable[[dict[str, Any]], None]) -> Callable[[dict[str, Any]], None]:
    head, *rest = fn.__name__.split("_")
    MODES[head + "".join(part.title() for part in rest)] = fn
    return fn


def say(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def go() -> None:
    say("ready")
    sys.stdin.readline()


def done(result: Any) -> None:
    say(json.dumps(result, ensure_ascii=False))


@mode
def lock_count(args: dict[str, Any]) -> None:
    file = args["file"]
    rounds = int(args["rounds"])
    go()
    for _ in range(rounds):
        with file_lock(file):
            with open(file, encoding="utf-8") as f:
                n = int(f.read() or "0")
            time.sleep(0.001)
            with open(file, "w", encoding="utf-8") as f:
                f.write(str(n + 1))
    done({"rounds": rounds})


@mode
def lock_once(args: dict[str, Any]) -> None:
    file = args["file"]
    go()
    started = now_ms()
    try:
        with file_lock(file, timeout_ms=float(args.get("timeoutMs", 15_000))):
            pass
        done({"ok": True, "ms": now_ms() - started})
    except TimeoutError as e:
        done({"ok": False, "error": str(e), "ms": now_ms() - started})


@mode
def lock_hold(args: dict[str, Any]) -> None:
    with file_lock(args["file"]):
        say("locked")
        sys.stdin.readline()
    done({"released": True})


def main() -> None:
    utf8_stdio()
    name = sys.argv[1]
    args = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    MODES[name](args)


if __name__ == "__main__":
    main()

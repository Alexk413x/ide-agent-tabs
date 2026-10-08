from __future__ import annotations

import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HOOK = os.path.join(ROOT, "mcp", "tests", "node080", "agent-hook.mjs")
RUNS = int(os.environ.get("RUNS", "20"))


def timed(command: list[str], env: dict[str, str], stdin: str) -> float:
    started = time.perf_counter()
    subprocess.run(command, input=stdin.encode("utf-8"), env=env, capture_output=True, check=False)
    return (time.perf_counter() - started) * 1000


def measure(label: str, command: list[str], env: dict[str, str], stdin: str = "") -> dict[str, object]:
    timed(command, env, stdin)
    samples = sorted(timed(command, env, stdin) for _ in range(RUNS))
    row = {
        "case": label,
        "runs": RUNS,
        "median_ms": round(statistics.median(samples), 1),
        "min_ms": round(samples[0], 1),
        "max_ms": round(samples[-1], 1),
    }
    print(json.dumps(row), flush=True)
    return row


def main() -> None:
    node = shutil.which("node")
    if node is None:
        sys.exit("node is not on PATH")
    home = tempfile.mkdtemp(prefix="iat-hook-bench-")
    try:
        base = {k: v for k, v in os.environ.items() if not k.startswith("IDE_AGENT_TABS")}
        base["IDE_AGENT_TABS_HOME"] = home
        tab = {**base, "IDE_AGENT_TABS_ID": "tab-bench-0001"}
        event = json.dumps({"session_id": "bench", "hook_event_name": "UserPromptSubmit", "prompt": "hi"})
        measure("node -e 0", [node, "-e", "0"], base)
        measure(f"python {sys.version.split()[0]} -I -S -c pass", [sys.executable, "-I", "-S", "-c", "pass"], base)
        measure("agent-hook, no tab (early exit)", [node, HOOK, "claude", "UserPromptSubmit"], base, event)
        measure("agent-hook, tab, UserPromptSubmit", [node, HOOK, "claude", "UserPromptSubmit"], tab, event)
        measure("agent-hook, tab, Stop", [node, HOOK, "claude", "Stop"], tab, json.dumps({"session_id": "bench", "hook_event_name": "Stop"}))
    finally:
        shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    main()

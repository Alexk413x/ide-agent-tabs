from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any

BENCH = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(BENCH))
PLUGIN = os.path.join(ROOT, "claude-plugin")
sys.path.insert(0, os.path.join(PLUGIN, "mcp", "src"))
sys.path.insert(0, os.path.join(ROOT, "mcp", "tests"))
sys.path.insert(0, BENCH)

from shared_server import working_set_mb  # noqa: E402
from test_shared_stress import StdioSession, clean_env  # noqa: E402

LAUNCHER = os.path.join(PLUGIN, "mcp", "launch", "agent_tabs.py")
SETTLE_S = 1.0


def median(values: list[float]) -> float:
    return round(statistics.median(values), 1) if values else 0.0


def p95(values: list[float]) -> float:
    return round(sorted(values)[max(0, int(len(values) * 0.95) - 1)], 1) if values else 0.0


def open_sessions(home: str, count: int) -> list[StdioSession]:
    sessions: list[StdioSession] = []
    lock = threading.Lock()

    def one(i: int) -> None:
        s = StdioSession(home, f"bench-{i:03d}")
        with lock:
            sessions.append(s)

    threads = [threading.Thread(target=one, args=(i,)) for i in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(120)
    return sorted(sessions, key=lambda s: s.tab)


def bench_startup(runs: int) -> dict[str, Any]:
    times: list[float] = []
    memory: list[float] = []
    for _ in range(runs):
        home = tempfile.mkdtemp(prefix="iat-bench-")
        began = time.perf_counter()
        s = StdioSession(home, "bench-000")
        times.append((time.perf_counter() - began) * 1000)
        s.call("list_sessions")
        time.sleep(SETTLE_S)
        memory.append(working_set_mb(s.child.pid))
        s.close()
        shutil.rmtree(home, ignore_errors=True)
    return {"bench": "stdio startup", "runs": runs, "initializeMs": [round(min(times)), median(times), round(max(times))], "sessionMb": median(memory)}


def bench_latency(agents: int, rounds: int) -> dict[str, Any]:
    home = tempfile.mkdtemp(prefix="iat-bench-")
    sessions = open_sessions(home, max(agents, 2))
    for s in sessions:
        s.call("list_sessions")
    time.sleep(SETTLE_S)
    ids = [s.tab for s in sessions]
    timings: dict[str, list[float]] = {"list_sessions": [], "send_message": [], "read_messages": []}
    sent: dict[str, str] = {}
    read: list[tuple[str, str]] = []
    failed: list[str] = []
    lock = threading.Lock()

    def timed(op: str, work: Any) -> Any:
        began = time.perf_counter()
        try:
            out = work()
        except Exception as e:  # noqa: BLE001
            with lock:
                failed.append(f"{op}: {e}")
            return None
        with lock:
            timings[op].append((time.perf_counter() - began) * 1000)
        return out

    def run(i: int, s: StdioSession) -> None:
        for r in range(rounds):
            timed("list_sessions", lambda: s.call("list_sessions"))
            to = ids[(i + 1 + (r % (len(ids) - 1))) % len(ids)]
            out = timed("send_message", lambda to=to, r=r: s.call("send_message", {"to": to, "text": f"round {r} from {s.tab}"}))
            if out and out.get("id"):
                with lock:
                    sent[out["id"]] = to
            got = timed("read_messages", lambda: s.call("read_messages"))
            with lock:
                read.extend((m["id"], s.tab) for m in (got or {}).get("messages", []))

    threads = [threading.Thread(target=run, args=(i, s)) for i, s in enumerate(sessions[:agents])]
    for t in threads:
        t.start()
    for t in threads:
        t.join(600)
    deadline = time.monotonic() + 30
    while len(read) < len(sent) and time.monotonic() < deadline:
        for s in sessions:
            read.extend((m["id"], s.tab) for m in s.call("read_messages").get("messages", []))
    seen: set[str] = set()
    twice = sum(1 for i, _ in read if i in seen or seen.add(i))
    for s in sessions:
        s.close()
    shutil.rmtree(home, ignore_errors=True)
    return {
        "bench": "stdio latency",
        "agents": agents,
        "listMs": median(timings["list_sessions"]),
        "sendMs": median(timings["send_message"]),
        "readMs": median(timings["read_messages"]),
        "sendP95Ms": p95(timings["send_message"]),
        "failed": len(failed),
        "lost": len(set(sent) - seen),
        "readTwice": twice,
        "wrongReader": sum(1 for i, by in read if sent.get(i) not in (None, by)),
    }


def bench_cli(runs: int) -> list[dict[str, Any]]:
    rows = []
    home = tempfile.mkdtemp(prefix="iat-bench-")
    with open(os.path.join(home, "config.json"), "w", encoding="utf-8") as f:
        json.dump({"jev": {"enabled": True}}, f)
    env = clean_env(home, TYPESAFE_API_KEY="bench-key")
    for args in (["jev", "status"], ["list-ides"]):
        times = []
        for _ in range(runs):
            began = time.perf_counter()
            subprocess.run([sys.executable, "-I", "-S", LAUNCHER, *args], env=env, capture_output=True, check=False)
            times.append((time.perf_counter() - began) * 1000)
        rows.append({"bench": "cli", "command": " ".join(args), "medianMs": median(times), "minMs": round(min(times)), "maxMs": round(max(times))})
    shutil.rmtree(home, ignore_errors=True)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="stdio server start-up, memory and messaging latency, and CLI timing.")
    parser.add_argument("what", choices=("startup", "latency", "cli", "all"))
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--agents", default="1,16")
    parser.add_argument("--rounds", type=int, default=20)
    args = parser.parse_args()
    rows: list[dict[str, Any]] = []
    if args.what in ("startup", "all"):
        rows.append(bench_startup(args.runs))
    if args.what in ("latency", "all"):
        rows += [bench_latency(int(n), args.rounds) for n in args.agents.split(",")]
    if args.what in ("cli", "all"):
        rows += bench_cli(args.runs)
    for row in rows:
        print(json.dumps(row), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

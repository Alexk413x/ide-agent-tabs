from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from typing import Any

BENCH = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(BENCH))
PLUGIN = os.path.join(ROOT, "claude-plugin")
sys.path.insert(0, os.path.join(PLUGIN, "mcp", "src"))
sys.path.insert(0, os.path.join(ROOT, "mcp", "tests"))

from ide_agent_tabs.shared.client import probe, stop_server
from ide_agent_tabs.shared.state import read_token

PY_SERVER = os.path.join(PLUGIN, "mcp", "launch", "shared_server.py")
PY_HELPER = os.path.join(PLUGIN, "mcp", "launch", "headers.py")
NODE_SERVER = os.path.join(PLUGIN, "dist", "shared-server.mjs")
NODE_HELPER = os.path.join(PLUGIN, "mcp", "launch", "headers.mjs")
SESSION_ENV = ("IDE_AGENT_TABS_ID", "IDE_AGENT_TABS_AGENT", "IDE_AGENT_TABS_MOD", "CLAUDE_PLUGIN_ROOT", "CLAUDE_PLUGIN_OPTION_SERVER_PORT")


def env_for(home: str, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in SESSION_ENV}
    env["IDE_AGENT_TABS_HOME"] = home
    env.update(extra)
    return env


def working_set_mb(pid: int) -> float:
    if sys.platform == "win32":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True, text=True, check=True).stdout
        return int("".join(c for c in out.split('","')[-1] if c.isdigit())) / 1024
    return int(subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True, check=True).stdout.strip()) / 1024


def start(command: list[str], home: str, port: int) -> tuple[subprocess.Popen[bytes], float]:
    began = time.perf_counter()
    child = subprocess.Popen(command, env=env_for(home), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    while True:
        found = probe(port)
        if found.health is not None and found.health.get("pid") == child.pid:
            return child, (time.perf_counter() - began) * 1000
        if child.poll() is not None:
            raise RuntimeError(f"{command[0]} exited with {child.returncode}")
        time.sleep(0.005)


def stop(child: subprocess.Popen[bytes], home: str, port: int) -> None:
    stop_server(home, port)
    try:
        child.wait(10)
    except subprocess.TimeoutExpired:
        child.kill()


def servers(node: str | None) -> dict[str, list[str]]:
    out = {"python": [sys.executable, "-I", "-S", PY_SERVER]}
    if node and os.path.exists(NODE_SERVER):
        out["node 0.8.0"] = [node, "--max-semi-space-size=1", NODE_SERVER]
    return out


def first_call(port: int, home: str) -> None:
    from shared_support import McpHttp

    token = read_token(home) or ""
    McpHttp(port, token, {"x-agent-tabs-client": "bench", "x-agent-tabs-pid": str(os.getpid())}, home).call("list_sessions")


def bench_start(port: int, runs: int, node: str | None) -> list[dict[str, Any]]:
    rows = []
    for name, command in servers(node).items():
        times, idle, after = [], [], []
        for _ in range(runs):
            home = tempfile.mkdtemp(prefix="iat-bench-")
            child, ms = start([*command, "--port", str(port)], home, port)
            times.append(ms)
            time.sleep(1.0)
            idle.append(working_set_mb(child.pid))
            first_call(port, home)
            time.sleep(1.0)
            after.append(working_set_mb(child.pid))
            stop(child, home, port)
            shutil.rmtree(home, ignore_errors=True)
        rows.append(
            {
                "server": name,
                "startToHealthMs": [round(min(times)), round(statistics.median(times)), round(max(times))],
                "idleMb": round(statistics.median(idle), 1),
                "afterFirstCallMb": round(statistics.median(after), 1),
            }
        )
    return rows


def bench_helper(port: int, runs: int, node: str | None) -> list[dict[str, Any]]:
    home = tempfile.mkdtemp(prefix="iat-bench-")
    child, _ = start([sys.executable, "-I", "-S", PY_SERVER, "--port", str(port)], home, port)
    env = env_for(home, CLAUDE_CODE_MCP_SERVER_URL=f"http://127.0.0.1:{port}/mcp")
    commands: dict[str, list[str]] = {"python -I -S -c pass": [sys.executable, "-I", "-S", "-c", "pass"], "python headers.py": [sys.executable, "-I", "-S", PY_HELPER]}
    if sys.platform == "win32" and shutil.which("py"):
        commands["py -3 headers.py"] = ["py", "-3", "-I", "-S", PY_HELPER]
    if node:
        commands["node -e 0"] = [node, "-e", "0"]
        if os.path.exists(NODE_HELPER):
            commands["node headers.mjs (0.8.0)"] = [node, NODE_HELPER]
    times: dict[str, list[float]] = {name: [] for name in commands}
    try:
        for _ in range(runs):
            for name, command in commands.items():
                began = time.perf_counter()
                out = subprocess.run(command, env=env, capture_output=True, check=False)
                times[name].append((time.perf_counter() - began) * 1000)
                if "headers" in name and "Authorization" not in json.loads(out.stdout or b"{}"):
                    raise RuntimeError(f"{name} printed no token: {out.stdout!r}")
    finally:
        stop(child, home, port)
        shutil.rmtree(home, ignore_errors=True)
    return [{"command": name, "medianMs": round(statistics.median(t)), "minMs": round(min(t)), "maxMs": round(max(t))} for name, t in times.items()]


def main() -> int:
    parser = argparse.ArgumentParser(description="Shared server start-up, idle memory and headers helper timing.")
    parser.add_argument("what", choices=("start", "helper", "all"))
    parser.add_argument("--port", type=int, default=47912)
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--no-node", action="store_true")
    args = parser.parse_args()
    if 47821 <= args.port <= 47829:
        parser.error("use a port outside 47821-47829")
    node = None if args.no_node else shutil.which("node")
    rows: list[dict[str, Any]] = []
    if args.what in ("start", "all"):
        rows += bench_start(args.port, args.runs, node)
    if args.what in ("helper", "all"):
        rows += bench_helper(args.port, args.runs, node)
    for row in rows:
        print(json.dumps(row))
    return 0


if __name__ == "__main__":
    sys.exit(main())

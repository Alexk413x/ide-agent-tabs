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
PLUGIN = os.path.join(ROOT, "claude-plugin")
NODE_HOOK = os.path.join(PLUGIN, "dist", "agent-hook.mjs")
PY_HOOK = os.path.join(PLUGIN, "mcp", "launch", "agent_hook.py")
RUNS = int(os.environ.get("RUNS", "20"))


def shell_command(event: str) -> str:
    return f'set -- claude {event}; . "{PLUGIN.replace(os.sep, "/")}/mcp/launch/agent-hook.ps1"'


def bash() -> str:
    if sys.platform == "win32":
        for candidate in (os.environ.get("CLAUDE_CODE_GIT_BASH_PATH"), r"C:\Program Files\Git\bin\bash.exe"):
            if candidate and os.path.exists(candidate):
                return candidate
        sys.exit("Git Bash not found")
    return "/bin/sh"


def timed(command: list[str], env: dict[str, str], stdin: bytes) -> tuple[float, bytes]:
    started = time.perf_counter()
    done = subprocess.run(command, input=stdin, env=env, capture_output=True, check=False)
    return (time.perf_counter() - started) * 1000, done.stdout


def measure(label: str, command: list[str], env: dict[str, str], stdin: bytes, before: object = None) -> None:
    timed(command, env, stdin)
    runs = []
    for _ in range(RUNS):
        if callable(before):
            before()
        runs.append(timed(command, env, stdin))
    samples = sorted(ms for ms, _ in runs)
    row = {
        "case": label,
        "median_ms": round(statistics.median(samples), 1),
        "min_ms": round(samples[0], 1),
        "max_ms": round(samples[-1], 1),
        "stdout": runs[-1][1].decode("utf-8", "replace").strip()[:60],
    }
    print(json.dumps(row), flush=True)


def main() -> None:
    node = shutil.which("node") or "node"
    home = tempfile.mkdtemp(prefix="iat-hook-forms-")
    cache = os.path.join(home, "mcp", "hook-python")
    try:
        base = {k: v for k, v in os.environ.items() if not k.startswith(("IDE_AGENT_TABS", "CLAUDE_PLUGIN"))}
        base.update({"IDE_AGENT_TABS_HOME": home, "CLAUDE_PLUGIN_ROOT": PLUGIN.replace(os.sep, "/")})
        tab = {**base, "IDE_AGENT_TABS_ID": "tab-forms-0001"}
        mod = {**tab, "IDE_AGENT_TABS_MOD": "tab-forms-0001"}
        prompt = json.dumps({"session_id": "s", "hook_event_name": "UserPromptSubmit", "prompt": "hi"}).encode()
        shells = [("sh", [bash(), "-c"])]
        if sys.platform == "win32":
            shells += [(name, [path, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command"]) for name in ("powershell", "pwsh") if (path := shutil.which(name))]

        def forget() -> None:
            if os.path.exists(cache):
                os.remove(cache)

        for label, env in (("no tab", base), ("mod-driven tab", mod), ("tab", tab)):
            if os.path.exists(NODE_HOOK):
                measure(f"{label}: node exec form (0.8.0)", [node, NODE_HOOK, "claude", "UserPromptSubmit"], env, prompt)
            measure(f"{label}: python.exe exec form", [sys.executable, "-I", "-S", PY_HOOK, "claude", "UserPromptSubmit"], env, prompt)
            for name, shell in shells:
                command = [*shell, shell_command("UserPromptSubmit")]
                measure(f"{label}: {name}, interpreter not yet recorded", command, env, prompt, forget)
                timed(command, env, prompt)
                measure(f"{label}: {name}, recorded interpreter", command, env, prompt)
    finally:
        shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    main()

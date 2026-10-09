from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PLUGIN = os.path.join(ROOT, "claude-plugin")
NODE_HOOK = os.path.join(ROOT, "mcp", "tests", "node080", "agent-hook.mjs")
RUNS = int(os.environ.get("RUNS", "10"))
TAB = "tab-bench-0001"
FORMS = ("node", "old", "new", "exec-only", "noop-only")
sys.path.insert(0, os.path.join(PLUGIN, "mcp", "src"))

from ide_agent_tabs.messaging import db, store  # noqa: E402
from ide_agent_tabs.register import RegisterContext, refresh_copy, tab_settings_file  # noqa: E402
from ide_agent_tabs.server_copy import Source, base_interpreter  # noqa: E402


def plugin_root(work: str, name: str, hooks: dict[str, Any]) -> str:
    root = os.path.join(work, f"plugin-{name}")
    os.makedirs(os.path.join(root, ".claude-plugin"))
    os.makedirs(os.path.join(root, "hooks"))
    with open(os.path.join(root, ".claude-plugin", "plugin.json"), "w", encoding="utf-8") as f:
        json.dump({"name": f"iat-bench-{name}", "version": "0.0.1"}, f)
    with open(os.path.join(root, "hooks", "hooks.json"), "w", encoding="utf-8") as f:
        json.dump({"hooks": hooks}, f, indent=1)
    return root


def setup(work: str) -> dict[str, Any]:
    with open(os.path.join(PLUGIN, "hooks", "hooks.json"), encoding="utf-8") as f:
        plugin_hooks = json.load(f)["hooks"]
    agent = {
        event: [g for g in groups if any("agent-hook.ps1" in h.get("command", "") for h in g["hooks"])]
        for event, groups in plugin_hooks.items()
    }
    agent = {event: groups for event, groups in agent.items() if groups}
    node = (shutil.which("node") or "node").replace("\\", "/")
    node_hooks = {
        event: [
            {
                **({"matcher": g["matcher"]} if "matcher" in g else {}),
                "hooks": [{"type": "command", "command": node, "args": [NODE_HOOK, "claude", event], "timeout": 5}],
            }
            for g in groups
        ]
        for event, groups in agent.items()
    }
    py = plugin_root(work, "py", agent)
    shutil.copytree(os.path.join(PLUGIN, "mcp"), os.path.join(py, "mcp"), ignore=shutil.ignore_patterns("__pycache__"))
    home = os.path.join(work, "home")
    project = os.path.join(work, "project")
    os.makedirs(project)
    ctx = RegisterContext(
        Source(os.path.join(PLUGIN, "mcp"), os.path.join(work, "no-scripts"), "0.0.0"), home, sys.platform, {}, work, base_interpreter()
    )
    refresh_copy(ctx)
    env_only = os.path.join(work, "env-only.json")
    with open(env_only, "w", encoding="utf-8") as f:
        json.dump({"env": {"IDE_AGENT_TABS_HOOKS": "1"}}, f)
    return {
        "home": home,
        "project": project,
        "roots": {"py": py, "node": plugin_root(work, "node", node_hooks), "empty": plugin_root(work, "empty", {})},
        "settings": {"new": tab_settings_file(ctx), "exec-only": tab_settings_file(ctx), "noop-only": env_only},
        "work": work,
    }


def run_once(form: str, s: dict[str, Any]) -> dict[str, Any]:
    with contextlib.suppress(db.MailError):
        store.send_message(
            s["home"], {"from": {"id": "s-0123456789ab", "agent": "codex", "path": "/w"}, "to": TAB, "text": f"ping {time.time()}"}
        )
    db.close_db(s["home"])
    debug = os.path.join(s["work"], f"debug-{form}-{time.time_ns()}.log")
    root = s["roots"][{"node": "node", "exec-only": "empty"}.get(form, "py")]
    # --setting-sources local, --strict-mcp-config and --no-session-persistence keep the user's settings, plugins,
    # MCP servers and transcripts out of the run.
    command = [shutil.which("claude") or "claude", "-p", "Reply with the single word ok.", "--model", "haiku", "--plugin-dir", root]
    command += ["--setting-sources", "local", "--strict-mcp-config", "--no-session-persistence", "--debug-file", debug]
    command += ["--output-format", "stream-json", "--verbose", "--include-hook-events"]
    if form in s["settings"]:
        command += ["--settings", s["settings"][form]]
    env = {k: v for k, v in os.environ.items() if not k.startswith(("IDE_AGENT_TABS", "CLAUDE", "MSYSTEM"))}
    env.update(IDE_AGENT_TABS_HOME=s["home"], IDE_AGENT_TABS_ID=TAB)
    p = subprocess.Popen(
        command,
        cwd=s["project"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
    )
    assert p.stdout is not None
    lines = [(time.perf_counter(), line) for line in p.stdout]
    p.wait()
    started: dict[str, float] = {}
    hooks: list[float] = []
    for at, line in lines:
        try:
            m = json.loads(line)
        except ValueError:
            continue
        if m.get("type") == "system" and m.get("subtype") == "hook_started":
            started[m["hook_id"]] = at
        elif m.get("type") == "system" and m.get("subtype") == "hook_response" and m.get("hook_event") == "UserPromptSubmit":
            hooks.append((at - started.get(m["hook_id"], at)) * 1000)
    with open(debug, encoding="utf-8", errors="replace") as f:
        settled = [float(v) for v in re.findall(r"prompt\.submit settled in ([\d.]+)ms", f.read())]
    return {"form": form, "rc": p.returncode, "hooks_ms": [round(h, 1) for h in hooks], "settled_ms": settled[0] if settled else None}


def main() -> None:
    work = tempfile.mkdtemp(prefix="iat-tab-hooks-")
    try:
        s = setup(work)
        results: dict[str, list[dict[str, Any]]] = {form: [] for form in FORMS}
        for i in range(RUNS + 1):
            for form in FORMS:
                row = run_once(form, s)
                if i:
                    results[form].append(row)
                print(json.dumps({"warm-up": i == 0, **row}), flush=True)
        for form, rows in results.items():
            slowest = [max(r["hooks_ms"]) for r in rows if r["hooks_ms"]]
            settled = [r["settled_ms"] for r in rows if r["settled_ms"] is not None]
            print(
                json.dumps(
                    {
                        "form": form,
                        "runs": len(rows),
                        "slowest_hook_median_ms": round(statistics.median(slowest), 1) if slowest else None,
                        "prompt_submit_median_ms": round(statistics.median(settled), 1) if settled else None,
                        "failed_runs": sum(1 for r in rows if r["rc"] != 0),
                    }
                ),
                flush=True,
            )
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()

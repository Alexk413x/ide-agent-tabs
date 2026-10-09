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
PROMPT = "Run the shell command `echo hi` with the Bash tool once, then reply with the single word ok."
FORMS = ("tab, node 0.8.0", "tab, 0.9.0", "no tab, node 0.8.0", "no tab, 0.9.0")
sys.path.insert(0, os.path.join(PLUGIN, "mcp", "src"))

from ide_agent_tabs.agent_config import HookTarget, claude_tab_settings  # noqa: E402
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
    # The server and sync hooks start the shared server and touch editors, so the bench leaves them out; every
    # other hook of the plugin stays.
    current = {
        event: kept
        for event, groups in plugin_hooks.items()
        if (
            kept := [
                g
                for g in groups
                if not any("server-hook" in h.get("command", "") or "sync-ides" in h.get("command", "") for h in g["hooks"])
            ]
        )
    }
    node = (shutil.which("node") or "node").replace("\\", "/")
    tab_hooks = claude_tab_settings(HookTarget(node, NODE_HOOK, sys.platform))["hooks"]
    for groups in tab_hooks.values():
        for g in groups:
            g["hooks"][0]["args"] = g["hooks"][0]["args"][2:]
    home = os.path.join(work, "home")
    project = os.path.join(work, "project")
    os.makedirs(project)
    ctx = RegisterContext(
        Source(os.path.join(PLUGIN, "mcp"), os.path.join(work, "no-scripts"), "0.0.0"), home, sys.platform, {}, work, base_interpreter()
    )
    refresh_copy(ctx)
    return {
        "home": home,
        "project": project,
        "roots": {"node": plugin_root(work, "node", tab_hooks), "current": plugin_root(work, "current", current)},
        "settings": tab_settings_file(ctx),
        "work": work,
    }


def run_once(form: str, s: dict[str, Any]) -> dict[str, Any]:
    with contextlib.suppress(db.MailError):
        store.send_message(
            s["home"], {"from": {"id": "s-0123456789ab", "agent": "codex", "path": "/w"}, "to": TAB, "text": f"ping {time.time()}"}
        )
    db.close_db(s["home"])
    debug = os.path.join(s["work"], f"debug-{time.time_ns()}.log")
    root = s["roots"]["node" if "node" in form else "current"]
    # --setting-sources local, --strict-mcp-config and --no-session-persistence keep the user's settings, plugins,
    # MCP servers and transcripts out of the run.
    command = [
        shutil.which("claude") or "claude",
        "-p",
        PROMPT,
        "--model",
        "haiku",
        "--plugin-dir",
        root,
        "--allowedTools",
        "Bash(echo hi)",
    ]
    command += ["--setting-sources", "local", "--strict-mcp-config", "--no-session-persistence", "--debug-file", debug]
    command += ["--output-format", "stream-json", "--verbose", "--include-hook-events"]
    env = {k: v for k, v in os.environ.items() if not k.startswith(("IDE_AGENT_TABS", "CLAUDE", "MSYSTEM"))}
    env["IDE_AGENT_TABS_HOME"] = s["home"]
    if form.startswith("tab"):
        env["IDE_AGENT_TABS_ID"] = TAB
        if "node" not in form:
            command += ["--settings", s["settings"]]
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
    hooks: dict[str, list[float]] = {"UserPromptSubmit": [], "PostToolUse": []}
    tools = 0
    for at, line in lines:
        try:
            m = json.loads(line)
        except ValueError:
            continue
        if m.get("type") == "assistant":
            tools += sum(1 for c in m.get("message", {}).get("content", []) if c.get("type") == "tool_use")
        if m.get("type") == "system" and m.get("subtype") == "hook_started":
            started[m["hook_id"]] = at
        elif m.get("type") == "system" and m.get("subtype") == "hook_response" and m.get("hook_event") in hooks:
            hooks[m["hook_event"]].append((at - started.get(m["hook_id"], at)) * 1000)
    with open(debug, encoding="utf-8", errors="replace") as f:
        settled = [float(v) for v in re.findall(r"prompt\.submit settled in ([\d.]+)ms", f.read())]
    return {
        "form": form,
        "rc": p.returncode,
        "tool_calls": tools,
        "prompt_hooks_ms": [round(h, 1) for h in hooks["UserPromptSubmit"]],
        "tool_hooks_ms": [round(h, 1) for h in hooks["PostToolUse"]],
        "settled_ms": settled[0] if settled else None,
    }


def median(values: list[float]) -> float | None:
    return round(statistics.median(values), 1) if values else None


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
            summary = {
                "form": form,
                "runs": len(rows),
                "prompt_submit_median_ms": median([r["settled_ms"] for r in rows if r["settled_ms"] is not None]),
                "prompt_hooks_per_run": sorted({len(r["prompt_hooks_ms"]) for r in rows}),
                "slowest_prompt_hook_median_ms": median([max(r["prompt_hooks_ms"]) for r in rows if r["prompt_hooks_ms"]]),
                "tool_calls": sum(r["tool_calls"] for r in rows),
                "tool_hooks": sum(len(r["tool_hooks_ms"]) for r in rows),
                "tool_hook_median_ms": median([h for r in rows for h in r["tool_hooks_ms"]]),
                "runs_without_a_tool_call": sum(1 for r in rows if r["tool_calls"] == 0),
                "failed_runs": sum(1 for r in rows if r["rc"] != 0),
            }
            print(json.dumps(summary), flush=True)
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()

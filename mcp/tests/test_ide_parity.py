from __future__ import annotations

import json
import math
import os
import unittest
from typing import Any, Callable

from ide_agent_tabs import closed, ide_catalog, ide_installs, jsjson, launch_plan, profiles, registry, resume, routing, spec, tab_store
from ide_agent_tabs import detection as detection_mod
from ide_agent_tabs.editor_clis import cli_invocation, editor_cli_locations
from ide_agent_tabs.terminals import default_terminal_name, powershell, processes, shell
from ide_agent_tabs.terminals.driver import OpenOptions
from ide_agent_tabs.version import compare_versions
from support import FIXTURES

with open(os.path.join(FIXTURES, "ide.json"), encoding="utf-8") as f:
    DATA: dict[str, list[dict[str, Any]]] = json.load(f)


def profile_json(p: profiles.AgentProfile) -> dict[str, Any]:
    out: dict[str, Any] = {"name": p.name, "label": p.label, "command": p.command, "args": list(p.args), "env": dict(p.env)}
    if p.prompt_flag is not None:
        out["promptFlag"] = p.prompt_flag
    if p.model_flag is not None:
        out["modelFlag"] = p.model_flag
    if p.icon is not None:
        out["icon"] = p.icon
    return out


def builtin(name: str) -> profiles.AgentProfile:
    return next(p for p in profiles.BUILTIN_PROFILES if p.name == name)


def launch_json(launch: profiles.AgentLaunch) -> dict[str, Any]:
    out: dict[str, Any] = {"agent": launch.agent, "command": launch.command, "args": launch.args}
    if launch.prompt is not None:
        out["prompt"] = launch.prompt
    out["env"] = launch.env
    return out


def settings(agents: str | None, config: str | None) -> dict[str, Any]:
    s = profiles.resolve_settings(agents, config, "/h/agents.json", "/h/config.json")
    t = s.terminal
    return {
        "profiles": [p.name if any(p is b for b in profiles.BUILTIN_PROFILES) else profile_json(p) for p in s.profiles],
        "defaultAgent": s.default_agent.name,
        "tabRouting": t.tab_routing,
        "terminalWindow": t.terminal_window,
        "launchVia": t.launch_via,
        "focusNewTabs": t.focus_new_tabs,
        "claudeMod": t.claude_mod,
        "allowResume": t.allow_resume,
        "ideStartTimeoutSec": t.ide_start_timeout_sec,
        "preferredTerminal": t.preferred_terminal,
        "shell": t.shell,
        "jev": {"enabled": s.jev.enabled, "sure": s.jev.sure, "tiers": s.jev.tiers, "pricePerMillionInput": s.jev.price_per_million_input},
        "warnings": s.warnings,
    }


def plan(name: str, r: dict[str, Any]) -> dict[str, Any]:
    request = launch_plan.LaunchRequest(
        args=r["args"],
        env=r["env"],
        launch_via=r["launchVia"],
        ori=r["ori"],
        platform=r["platform"],
        prompt=r.get("prompt"),
        model=r.get("model"),
        via=r.get("via"),
        cmd_shim=r.get("cmdShim"),
    )
    result = launch_plan.plan_launch(builtin(name), request)
    return {"via": result.via, "launch": launch_json(result.launch)}


def candidates(listed: list[dict[str, Any]]) -> list[routing.IdeCandidate]:
    return [
        routing.IdeCandidate(c["id"], c["startedAt"], [routing.Project(p["name"], p["path"], p["focused"]) for p in c["projects"]])
        for c in listed
    ]


def choice(value: routing.Choice | routing.ChoiceError | None, key: str = "id") -> Any:
    if value is None:
        return None
    if isinstance(value, routing.ChoiceError):
        return {"error": value.error}
    return {key: value.name, "reason": value.reason}


def endpoint(text: str, file: str, at: float) -> dict[str, Any]:
    parsed = registry.parse_endpoint(text, file, at)
    if isinstance(parsed, registry.Skip):
        return {"kind": "skip", "reason": parsed.reason, "warn": parsed.warn}
    e = parsed
    out: dict[str, Any] = {
        "id": e.id,
        "file": e.file,
        "ide": e.ide,
        "product": e.product,
        "version": e.version,
        "pid": e.pid,
        "url": e.url,
        "token": e.token,
        "startedAt": e.started_at,
    }
    if e.beat_ms is not None:
        out["beatMs"] = e.beat_ms
    return {"kind": "endpoint", "endpoint": out}


def product_info(text: str | None) -> Any:
    info = ide_installs.parse_product_info(text)
    if info is None:
        return None
    out: dict[str, Any] = {}
    for key, value in (
        ("name", info.name),
        ("version", info.version),
        ("buildNumber", info.build_number),
        ("productCode", info.product_code),
    ):
        if value is not None:
            out[key] = value
    out["launch"] = [{"os": e.os, "launcherPath": e.launcher_path, **({"arch": e.arch} if e.arch else {})} for e in info.launch]
    return out


def _info(text: str) -> ide_installs.ProductInfo:
    info = ide_installs.parse_product_info(text)
    assert info is not None
    return info


def install_json(i: ide_installs.IdeInstall) -> dict[str, Any]:
    out: dict[str, Any] = {"key": i.key, "product": i.product, "kind": i.kind}
    if i.version is not None:
        out["version"] = i.version
    out["launcher"] = i.launcher
    return out


def discover(d: dict[str, Any]) -> list[dict[str, Any]]:
    files, dirs = d["files"], d["dirs"]
    fs = ide_installs.DiscoveryFs(lambda f: f in files or f in dirs, lambda folder: dirs.get(folder, []), files.get)
    ctx = ide_installs.DiscoveryContext(d["platform"], d["env"], d["userHome"], d["arch"], fs)
    return [install_json(i) for i in ide_installs.discover_ides(ctx)]


def launch_command(install: dict[str, Any], folder: str, platform: str, comspec: str | None) -> dict[str, Any]:
    i = ide_installs.IdeInstall(install["key"], install["product"], install["kind"], install["launcher"], install.get("version"))
    c = ide_installs.ide_launch_command(i, folder, platform, comspec)
    return {"command": c.command, "args": c.args, "windowsVerbatimArguments": c.windows_verbatim_arguments, "windowsHide": c.windows_hide}


def launch_spec(s: dict[str, Any]) -> spec.LaunchSpec:
    return spec.LaunchSpec(s["id"], s["agent"], s["cwd"], s["command"], s["args"], s["env"], s.get("prompt"), s.get("pidFile"))


def login(s: dict[str, Any]) -> shell.LoginShell:
    return shell.LoginShell(s["path"], s["kind"])


def options(o: dict[str, Any] | None) -> OpenOptions | None:
    return None if o is None else OpenOptions(o.get("window", "last"), o.get("near"), o.get("focus"))


def probe(p: dict[str, Any]) -> powershell.ShellProbe:
    versions = p.get("versions", {})
    return powershell.ShellProbe(
        p["env"],
        lambda f: f in p["files"],
        lambda d: p["dirs"].get(d),
        lambda f: p["links"].get(f),
        lambda f: p.get("mtimes", {}).get(f),
        versions.get,
    )


def candidate_json(c: Any) -> dict[str, Any]:
    out = {"path": c.path, "source": c.source, "key": c.key}
    if c.version is not None:
        out["version"] = c.version
    return out


def sign(value: float) -> Any:
    if isinstance(value, float) and math.isnan(value):
        return None
    return (value > 0) - (value < 0)


ADAPTERS: dict[str, Callable[..., Any]] = {
    "builtinProfiles": lambda: [profile_json(p) for p in profiles.BUILTIN_PROFILES],
    "settings": settings,
    "launchOf": lambda name, prompt, args, env: launch_json(profiles.launch_of(builtin(name), prompt, args, env)),
    "planLaunch": plan,
    "projectDepth": routing.project_depth,
    "chooseIde": lambda c, target, windows, caller, how: choice(routing.choose_ide(candidates(c), target, windows, caller, how)),
    "chooseTerminal": lambda preferred, default, known: choice(routing.choose_terminal(preferred, default, known), "name"),
    "defaultTerminal": default_terminal_name,
    "parseEndpoint": endpoint,
    "findIdeEntry": lambda n: e.key if (e := ide_catalog.find_ide_entry(n)) else None,
    "normalizeIdeName": ide_catalog.normalize_ide_name,
    "entryForProduct": lambda p: e.key if (e := ide_catalog.entry_for_product(p)) else None,
    "productMatchesName": ide_catalog.product_matches_name,
    "entryForProductInfo": lambda n, c: e.key if (e := ide_catalog.entry_for_product_info(n, c)) else None,
    "parseProductInfo": product_info,
    "launcherPath": lambda t, platform, arch: ide_installs.launcher_path(_info(t), platform, arch),
    "discoverIdes": discover,
    "editorCliLocations": editor_cli_locations,
    "cliInvocation": lambda c, a, p, s: dict(zip(("command", "args", "windowsVerbatimArguments"), cli_invocation(c, a, p, s))),
    "ideLaunchCommand": launch_command,
    "launchEnvironment": ide_installs.launch_environment,
    "terminalEnvironment": processes.terminal_environment,
    "compareBuilds": compare_versions,
    "powerShellSpec": lambda s: spec.power_shell_spec(launch_spec(s)),
    "posixSpec": lambda s: spec.posix_spec(launch_spec(s)).hex(),
    "checkPosixEnvNames": spec.check_posix_env_names,
    "loginShell": lambda e, p: shell.login_shell(e, p)._asdict(),
    "surfaceArgv": lambda s: shell.surface_argv(login(s)),
    "surfaceCommand": lambda s: shell.surface_command(login(s)),
    "argvModeCommand": lambda s, launcher, sp: shell.argv_mode_command(login(s), launcher, sp),
    "checkArgvPaths": shell.check_argv_paths,
    "tabTitle": shell.tab_title,
    "isShellName": processes.is_shell_name,
    "comparePowerShellVersions": lambda a, b: sign(powershell.compare_versions(a, b)),
    "shellLabel": powershell.shell_label,
    "parseVersionOutput": powershell.parse_version_output,
    "ago": resume.ago,
    "sizeText": resume.size_text,
    "costCheck": lambda r, age, model: dict(zip(("cheap", "reasons", "withinCache"), resume.cost_check(r, age, model))),
    "previewOf": closed.preview_of,
    "parseClosed": lambda t: closed.parse_closed(t),
    "parseDetection": detection_mod.parse_detection,
    "parseTabs": tab_store.parse_tabs,
    "pickPowerShell": lambda shells, configured, present: powershell.pick_power_shell(shells, configured, lambda f: f in present),
}

SPECIAL = {
    "probes",
    "candidatePaths",
    "listPowerShells",
    "detectPowerShells",
    "closedListing",
    "closedListingEmpty",
}


def outcome(fn: Callable[..., Any], args: list[Any]) -> dict[str, Any]:
    try:
        value = fn(*args)
    except (ValueError, TypeError, RuntimeError, KeyError) as e:
        return {"error": str(e)}
    return {"result": json.loads(jsjson.stringify(value))}


class ParityTest(unittest.TestCase):
    maxDiff = None

    def test_service_functions_match_the_typescript_build(self) -> None:
        for name, fn in ADAPTERS.items():
            for case in DATA[name]:
                with self.subTest(name=name, args=case["args"]):
                    expected = {"error": case["error"]} if "error" in case else {"result": case["result"]}
                    got = outcome(fn, case["args"])
                    if name == "comparePowerShellVersions" and "result" in expected:
                        expected = {"result": sign(expected["result"]) if expected["result"] is not None else None}
                    self.assertEqual(got, expected)

    def test_power_shell_discovery_matches(self) -> None:
        probes = DATA["probes"]
        for case in DATA["candidatePaths"]:
            self.assertEqual([candidate_json(c) for c in powershell.candidate_paths(probe(probes[case["args"][0]]))], case["result"])
        for case in DATA["listPowerShells"]:
            self.assertEqual(powershell.list_power_shells(probe(probes[case["args"][0]])), case["result"])
        for case in DATA["detectPowerShells"]:
            i, previous = case["args"]
            self.assertEqual(powershell.detect_power_shells(probe(probes[i]), previous), case["result"])

    def test_closed_listing_matches(self) -> None:
        texts = [c["args"][0] for c in DATA["parseClosed"]]
        records = [r for r in (closed.parse_closed(t) for t in texts) if r is not None]
        for case in DATA["closedListing"]:
            self.assertEqual(resume.closed_listing(records, case["args"][0]), case["result"])
        self.assertEqual(resume.closed_listing([], 0), DATA["closedListingEmpty"][0]["result"])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from typing import Any, Callable

from .clock import iso, now_ms
from .editor_clis import path_var_of
from .files import read_text_if_exists, write_atomically
from .installed import find_on_path
from .jsjson import parse, stringify
from .parallel import run_all
from .processes import RunResult, run
from .terminals.driver import TerminalContext, TerminalDriver
from .terminals.powershell import ShellProbe, detect_power_shells, system_probe

DETECTED_FILE = "detected.json"
DRIVER_ERRORS = (OSError, TimeoutError, ValueError, RuntimeError, KeyError, TypeError)
_SHELL_SOURCES = ("path", "msi", "store", "preview", "windows")

Detection = dict[str, Any]
OriRunner = Callable[[str, list[str]], RunResult]


def _is_shell(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("path"), str)
        and isinstance(value.get("label"), str)
        and isinstance(value.get("version"), str)
        and value.get("source") in _SHELL_SOURCES
    )


def _valid_ori(ori: Any) -> bool:
    return (
        isinstance(ori, dict)
        and isinstance(ori.get("path"), str)
        and isinstance(ori.get("version"), str)
        and isinstance(ori.get("agents"), list)
        and all(isinstance(a, str) for a in ori["agents"])
    )


def parse_detection(text: str | None) -> Detection | None:
    if text is None:
        return None
    try:
        value = parse(text)
    except (ValueError, RecursionError):
        return None
    if not isinstance(value, dict):
        return None
    if (
        not (value.get("version") == 1 and not isinstance(value.get("version"), bool))
        or not isinstance(value.get("detectedAt"), str)
        or not isinstance(value.get("shells"), list)
        or not isinstance(value.get("terminals"), list)
    ):
        return None
    detection = dict(value)
    detection["shells"] = [s for s in value["shells"] if _is_shell(s)]
    detection["ori"] = value.get("ori") if _valid_ori(value.get("ori")) else None
    return detection


def read_detection(home: str) -> Detection | None:
    try:
        text = read_text_if_exists(os.path.join(home, DETECTED_FILE))
    except OSError:
        return None
    return parse_detection(text)


def available_terminals(drivers: Sequence[TerminalDriver], ctx: TerminalContext) -> list[dict[str, str]]:
    return [{"id": d.name, "name": d.label} for d in available_drivers(drivers, ctx)]


def _available(driver: TerminalDriver, ctx: TerminalContext) -> bool:
    try:
        return driver.available(ctx)
    except DRIVER_ERRORS:
        return False


def available_drivers(drivers: Sequence[TerminalDriver], ctx: TerminalContext) -> list[TerminalDriver]:
    return [d for d in drivers if _available(d, ctx)]


def find_ori(env: Mapping[str, str], platform: str) -> str | None:
    exe = "ori.exe" if platform == "win32" else "ori"
    on_path = find_on_path(path_var_of(env), exe)
    if on_path:
        return on_path
    home = env.get("USERPROFILE") if platform == "win32" else env.get("HOME")
    installed = os.path.join(home, ".local", "bin", exe) if home else None
    return installed if installed and os.path.exists(installed) else None


def _default_ori_runner(exe: str, args: list[str]) -> RunResult:
    return run(exe, args, timeout=15)


def _data_of(stdout: str) -> dict[str, Any] | None:
    start = stdout.find("{")
    value = parse(stdout[max(0, start) :])
    if isinstance(value, dict) and value.get("ok") is True and isinstance(value.get("data"), (dict, list)):
        data = value["data"]
        return data if isinstance(data, dict) else {}
    return None


def detect_ori(exe: str | None, runner: OriRunner | None = None) -> dict[str, Any] | None:
    if exe is None:
        return None
    runner = runner or _default_ori_runner
    try:
        version, harnesses = run_all([lambda: runner(exe, ["--version", "--json"]), lambda: runner(exe, ["harness", "list", "--json"])])
        launchable = (_data_of(harnesses.stdout) or {}).get("launchable")
        agents = (
            [h["kind"] for h in launchable if isinstance(h, dict) and h.get("installed") is True and isinstance(h.get("kind"), str)]
            if isinstance(launchable, list)
            else []
        )
        v = (_data_of(version.stdout) or {}).get("version")
        return {"path": exe, "version": v.split("+")[0] if isinstance(v, str) else "", "agents": agents}
    except DRIVER_ERRORS:
        return {"path": exe, "version": "", "agents": []}


def detect(
    home: str,
    platform: str,
    env: Mapping[str, str],
    terminals: list[dict[str, str]],
    probe: ShellProbe | None = None,
    ori_runner: OriRunner | None = None,
    now: int | None = None,
) -> Detection:
    previous = read_detection(home) if platform == "win32" else None
    probe_or_system = probe or system_probe(env, True)
    shells, ori = run_all(
        [
            lambda: detect_power_shells(probe_or_system, previous) if platform == "win32" else [],
            lambda: detect_ori(find_ori(env, platform), ori_runner),
        ]
    )
    return {
        "version": 1,
        "detectedAt": iso(now_ms() if now is None else now),
        "platform": platform,
        "terminals": terminals,
        "shells": shells,
        "ori": ori,
    }


def write_detection(home: str, detection: Detection) -> None:
    write_atomically(os.path.join(home, DETECTED_FILE), stringify(detection, 2) + "\n")


def refresh_detection_file(home: str, platform: str, env: Mapping[str, str], drivers: Sequence[TerminalDriver]) -> Detection:
    ctx = TerminalContext(home, "", path_var_of(env), env)
    detection = detect(home, platform, env, available_terminals(drivers, ctx))
    write_detection(home, detection)
    return detection

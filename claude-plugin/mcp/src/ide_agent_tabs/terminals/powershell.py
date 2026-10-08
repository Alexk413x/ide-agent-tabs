from __future__ import annotations

import functools
import math
import ntpath
import os
import re
from collections.abc import Mapping
from typing import Any, Callable, NamedTuple

from .. import winapi
from ..clock import parse_iso
from ..jsjson import js_trim
from ..jspath import win32_is_absolute, win32_normalize
from ..parallel import run_all
from ..processes import run

SHELL_SOURCES = ("path", "msi", "store", "preview", "windows")
_STORE_PACKAGE = re.compile(r"^Microsoft\.PowerShell(Preview)?_(\d+(?:\.\d+)*)_", re.IGNORECASE)
_VERSION = re.compile(r"\d+(?:\.\d+)*(?:-[0-9A-Za-z.-]+)?")
_PREVIEW = re.compile("preview", re.IGNORECASE)
_DIGITS = re.compile(r"\d+")
WINDOWS_POWERSHELL_VERSION = "5.1"
VERSION_COMMAND = "$PSVersionTable.PSVersion.ToString()"

DetectedShell = dict[str, str]


class ShellProbe(NamedTuple):
    env: Mapping[str, str]
    exists: Callable[[str], bool]
    readdir: Callable[[str], list[str] | None]
    readlink: Callable[[str], str | None]
    mtime_ms: Callable[[str], float | None]
    version: Callable[[str], str | None] | None = None


def _env_value(env: Mapping[str, str], name: str) -> str | None:
    key = next((k for k in env if k.upper() == name.upper()), None)
    value = env.get(key) if key is not None else None
    return value if value and js_trim(value) != "" else None


class _Locations(NamedTuple):
    program_files: str | None
    windows_apps: str | None
    aliases: str | None
    system32: str | None


def _locations(env: Mapping[str, str]) -> _Locations:
    program_files = _env_value(env, "ProgramFiles")
    local = _env_value(env, "LOCALAPPDATA")
    system_root = _env_value(env, "SystemRoot") or _env_value(env, "windir")
    return _Locations(
        program_files,
        ntpath.join(program_files, "WindowsApps") if program_files else None,
        ntpath.join(local, "Microsoft", "WindowsApps") if local else None,
        ntpath.join(system_root, "System32") if system_root else None,
    )


def _lower(p: str) -> str:
    return win32_normalize(p).lower()


def _is_inside(file: str, folder: str | None) -> bool:
    if not folder:
        return False
    base = _lower(folder).rstrip("\\")
    target = _lower(file).rstrip("\\")
    if not target.startswith(base + "\\"):
        return False
    rel = target[len(base) + 1 :]
    return rel != "" and not rel.startswith("..") and not win32_is_absolute(rel)


def _store_version(package_dir: str) -> tuple[str, bool] | None:
    m = _STORE_PACKAGE.match(ntpath.basename(package_dir))
    if not m:
        return None
    return ".".join(m.group(2).split(".")[:3]), m.group(1) is not None


class _Candidate(NamedTuple):
    path: str
    source: str
    key: str
    version: str | None = None


def _classify(file: str, loc: _Locations, probe: ShellProbe) -> _Candidate:
    name = ntpath.basename(file).lower()
    target = probe.readlink(file)
    key = _lower(target if target is not None else file)
    if loc.program_files and _is_inside(file, ntpath.join(loc.program_files, "PowerShell")):
        folder = ntpath.relpath(ntpath.dirname(file), ntpath.join(loc.program_files, "PowerShell"))
        preview = _PREVIEW.search(folder) is not None
        version = folder if not preview and _DIGITS.fullmatch(folder) else None
        return _Candidate(file, "preview" if preview else "msi", key, version)
    package_dir = (
        ntpath.dirname(file)
        if _is_inside(file, loc.windows_apps)
        else ntpath.dirname(target)
        if target and _is_inside(target, loc.windows_apps)
        else None
    )
    if package_dir or _is_inside(file, loc.aliases):
        store = _store_version(package_dir) if package_dir else None
        preview = store[1] if store is not None else name == "pwsh-preview.exe"
        version = store[0] if store is not None and not preview else None
        return _Candidate(file, "preview" if preview else "store", key, version)
    if loc.system32 and _is_inside(file, ntpath.join(loc.system32, "WindowsPowerShell")) and name == "powershell.exe":
        return _Candidate(file, "windows", key, WINDOWS_POWERSHELL_VERSION)
    return _Candidate(file, "path", key)


def _js_number(text: str) -> float:
    text = js_trim(text)
    if text == "":
        return 0
    try:
        return float(text) if re.fullmatch(r"[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?", text) else math.nan
    except ValueError:
        return math.nan


def _numeric_collate(a: str, b: str) -> int:
    def chunks(s: str) -> list[tuple[int, Any]]:
        return [(0, int(c)) if c.isdigit() else (1, c.lower()) for c in re.findall(r"\d+|\D+", s)]

    ca, cb = chunks(a), chunks(b)
    return (ca > cb) - (ca < cb) if ca != cb else (a > b) - (a < b)


def compare_versions(a: str, b: str) -> float:
    a_core, _, a_pre = a.partition("-")
    b_core, _, b_pre = b.partition("-")
    a_has, b_has = "-" in a, "-" in b
    an = [_js_number(x) for x in a_core.split(".")]
    bn = [_js_number(x) for x in b_core.split(".")]
    for i in range(max(len(an), len(bn))):
        d = (an[i] if i < len(an) else 0) - (bn[i] if i < len(bn) else 0)
        if d != 0:
            return d
    if a_has == b_has and a_pre == b_pre:
        return 0
    if not a_has:
        return 1
    if not b_has:
        return -1
    return _numeric_collate(a_pre, b_pre)


def _cmp(compare: Callable[[Any, Any], float]) -> Any:
    def sign(a: Any, b: Any) -> int:
        d = compare(a, b)
        return 0 if math.isnan(d) else (1 if d > 0 else -1 if d < 0 else 0)

    return functools.cmp_to_key(sign)


def _newest_store_package(loc: _Locations, probe: ShellProbe, preview: bool) -> str | None:
    if not loc.windows_apps:
        return None
    versions = [v[0] for v in (_store_version(n) for n in probe.readdir(loc.windows_apps) or []) if v is not None and v[1] == preview]
    ordered = sorted(versions, key=_cmp(compare_versions))
    return ordered[-1] if ordered else None


def _path_dirs(env: Mapping[str, str]) -> list[str]:
    return [d for d in (js_trim(raw).strip('"') for raw in (_env_value(env, "PATH") or "").split(";")) if d != ""]


def candidate_paths(probe: ShellProbe) -> list[_Candidate]:
    loc = _locations(probe.env)
    files: list[str] = []
    for folder in _path_dirs(probe.env):
        for name in ("pwsh.exe", "powershell.exe"):
            file = ntpath.join(folder, name)
            if probe.exists(file):
                files.append(file)
    if loc.program_files:
        root = ntpath.join(loc.program_files, "PowerShell")
        for folder in sorted(probe.readdir(root) or []):
            file = ntpath.join(root, folder, "pwsh.exe")
            if probe.exists(file):
                files.append(file)
    if loc.aliases:
        for name in ("pwsh.exe", "pwsh-preview.exe"):
            file = ntpath.join(loc.aliases, name)
            if probe.exists(file):
                files.append(file)
    if loc.system32:
        file = ntpath.join(loc.system32, "WindowsPowerShell", "v1.0", "powershell.exe")
        if probe.exists(file):
            files.append(file)
    by_key: dict[str, _Candidate] = {}
    for file in files:
        c = _classify(file, loc, probe)
        seen = by_key.get(c.key) or by_key.get(_lower(c.path))
        # A Store package folder's path changes with every update, so its stable app alias wins.
        if seen is None or (_is_inside(seen.path, loc.windows_apps) and _is_inside(c.path, loc.aliases)):
            if seen is not None:
                del by_key[seen.key]
            by_key[c.key] = c
    for key, c in by_key.items():
        if c.source == "store" and c.version is None:
            version = _newest_store_package(loc, probe, False)
            if version:
                by_key[key] = c._replace(version=version)
    return list(by_key.values())


def parse_version_output(stdout: str) -> str | None:
    lines = re.split(r"\r?\n", js_trim(stdout))
    line = js_trim(lines[0]) if lines else ""
    return line if _VERSION.fullmatch(line) else None


def shell_label(file: str, version: str, source: str) -> str:
    is_pwsh = ntpath.basename(file).lower().startswith("pwsh")
    name = "Windows PowerShell" if source == "windows" or not is_pwsh else "PowerShell"
    head = f"{name} {version}" if version else name
    suffix = {"msi": " (MSI)", "store": " (Store)", "preview": " (preview)", "path": " (PATH)"}.get(source, "")
    return head + suffix


def _cached_version(c: _Candidate, probe: ShellProbe, previous: dict[str, Any] | None) -> str | None:
    if previous is None:
        return None
    before = next(
        (s for s in previous.get("shells", []) if _lower(s["path"]) == _lower(c.path) and s.get("source") == c.source),
        None,
    )
    if before is None or not before.get("version") or not _VERSION.fullmatch(before["version"]):
        return None
    mtime = probe.mtime_ms(c.path)
    at = parse_iso(previous.get("detectedAt", "")) if isinstance(previous.get("detectedAt"), str) else None
    return before["version"] if mtime is not None and at is not None and mtime < at else None


def _by_rank(a: DetectedShell, b: DetectedShell) -> float:
    windows = (a["source"] == "windows") - (b["source"] == "windows")
    return windows or compare_versions(b["version"] or "0", a["version"] or "0")


def _to_shell(c: _Candidate, version: str) -> DetectedShell:
    return {"path": c.path, "label": shell_label(c.path, version, c.source), "version": version, "source": c.source}


# Versions come from folder names where they carry one; otherwise detection runs the shell once. A tab
# launch never runs one: it reads detected.json, or lists the shells without versions.
def detect_power_shells(probe: ShellProbe, previous: dict[str, Any] | None = None) -> list[DetectedShell]:
    candidates = candidate_paths(probe)

    def one(c: _Candidate) -> DetectedShell:
        exact = c.version is not None and "." in c.version
        version = (c.version if exact else None) or _cached_version(c, probe, previous)
        if version is None and probe.version is not None:
            try:
                version = probe.version(c.path)
            except (OSError, TimeoutError, ValueError):
                version = None
        return _to_shell(c, version if version is not None else c.version or "")

    if not candidates:
        return []
    shells = run_all([lambda c=c: one(c) for c in candidates])
    return sorted(shells, key=_cmp(_by_rank))


def list_power_shells(probe: ShellProbe) -> list[DetectedShell]:
    return sorted((_to_shell(c, c.version or "") for c in candidate_paths(probe)), key=_cmp(_by_rank))


def _is_power_shell_7(shell: DetectedShell) -> bool:
    if shell["source"] == "windows":
        return False
    if shell["version"] == "":
        return ntpath.basename(shell["path"]).lower().startswith("pwsh")
    return _js_number(shell["version"].split(".")[0]) >= 7


def _is_stable(shell: DetectedShell) -> bool:
    return shell["source"] != "preview" and "-" not in shell["version"]


def pick_power_shell(shells: list[DetectedShell], configured: str | None, exists: Callable[[str], bool]) -> str:
    if configured and exists(configured):
        return configured
    present = [s for s in shells if exists(s["path"])]
    seven = [s for s in present if _is_power_shell_7(s)]
    pool = [s for s in seven if _is_stable(s)] if any(_is_stable(s) for s in seven) else seven
    newest = sorted(pool, key=_cmp(lambda a, b: compare_versions(b["version"] or "0", a["version"] or "0")))
    if newest:
        return newest[0]["path"]
    return next(
        (s["path"] for s in present if s["source"] == "windows" or ntpath.basename(s["path"]).lower() == "powershell.exe"),
        "powershell.exe",
    )


# lstat, not stat: the Store's pwsh.exe is an app execution alias that stat cannot follow.
def file_exists(file: str) -> bool:
    try:
        os.lstat(file)
    except (OSError, ValueError):
        return False
    return True


def default_power_shell(env: Mapping[str, str]) -> str:
    return pick_power_shell(list_power_shells(system_probe(env, False)), None, file_exists)


def run_version(exe: str) -> str | None:
    result = run(exe, ["-NoLogo", "-NoProfile", "-NonInteractive", "-Command", VERSION_COMMAND], timeout=20)
    return parse_version_output(result.stdout) if result.code == 0 else None


def _readdir(folder: str) -> list[str] | None:
    try:
        return os.listdir(folder)
    except OSError:
        return None


def _readlink(file: str) -> str | None:
    try:
        return os.readlink(file)
    except (OSError, ValueError):
        return winapi.read_app_exec_link(file)


def _mtime(file: str) -> float | None:
    try:
        return os.lstat(file).st_mtime_ns / 1e6
    except OSError:
        return None


def system_probe(env: Mapping[str, str], run_shells: bool) -> ShellProbe:
    return ShellProbe(env, file_exists, _readdir, _readlink, _mtime, run_version if run_shells else None)

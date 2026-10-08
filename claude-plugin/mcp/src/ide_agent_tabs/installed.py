from __future__ import annotations

import os

from .jsjson import js_trim
from .jspath import is_absolute

WINDOWS_EXTENSIONS = (".exe", ".cmd", ".bat", ".ps1")


# lstat, not stat: the Microsoft Store pwsh.exe and wt.exe under WindowsApps are app execution aliases that
# stat cannot follow, so a stat-based lookup misses them.
def exists(file: str) -> bool:
    try:
        os.lstat(file)
    except (OSError, ValueError):
        return False
    return True


def _path_dirs(path_var: str) -> list[str]:
    dirs: list[str] = []
    for raw in path_var.split(os.pathsep):
        folder = js_trim(raw).strip('"')
        if folder:
            dirs.append(folder)
    return dirs


def find_on_path(path_var: str, executable: str) -> str | None:
    for folder in _path_dirs(path_var):
        candidate = os.path.join(folder, executable)
        if exists(candidate):
            return candidate
    return None


def is_installed(command: str, path_var: str, is_windows: bool) -> bool:
    names = [command, *(command + e for e in WINDOWS_EXTENSIONS)] if is_windows else [command]
    if is_absolute(command):
        return any(exists(n) for n in names)
    if "/" in command or os.sep in command:
        return False
    return any(find_on_path(path_var, n) is not None for n in names)


def is_cmd_shim(command: str, path_var: str) -> bool:
    for folder in _path_dirs(path_var):
        for ext in (".exe", ".cmd", ".bat"):
            if exists(os.path.join(folder, command + ext)):
                return ext != ".exe"
    return True

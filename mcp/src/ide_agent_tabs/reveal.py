from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Sequence
from typing import Callable, NamedTuple

from .jsjson import CONTROL
from .jspath import is_absolute

BUNDLE_SEGMENT = re.compile(r"\.(app|bundle|framework|pkg|plugin|prefPane)\Z", re.IGNORECASE)
_SEPARATORS = re.compile("/")
_TRAILING = re.compile(r"/+\Z")


class RevealDeps(NamedTuple):
    realpath: Callable[[str], str | None]
    is_directory: Callable[[str], bool]
    open: Callable[[str], None]
    platform: str


def file_manager_command(platform: str) -> str:
    return "explorer.exe" if platform == "win32" else "open" if platform == "darwin" else "xdg-open"


# No CREATE_NO_WINDOW and no hidden start: Explorer applies a hidden start to the folder window it opens, so
# a hidden launch leaves an invisible window that never closes.
def open_in_file_manager(platform: str, folder: str) -> None:
    subprocess.Popen(
        [file_manager_command(platform), folder],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=sys.platform != "win32",
    )


def _realpath(target: str) -> str | None:
    try:
        os.stat(target)
        return os.path.realpath(target)
    except (OSError, ValueError):
        return None


def _is_directory(target: str) -> bool:
    try:
        return os.path.isdir(target)
    except (OSError, ValueError):
        return False


def system_reveal(platform: str) -> RevealDeps:
    return RevealDeps(_realpath, _is_directory, lambda folder: open_in_file_manager(platform, folder), platform)


# Only a folder of a live session or an open IDE project is revealed, and never a macOS bundle: the OS
# opens a bundle by launching it.
def check_reveal_target(target: str, known: Sequence[str], deps: RevealDeps) -> str:
    if not is_absolute(target) or CONTROL.search(target):
        raise ValueError(f"not an absolute path: {target}")
    real = deps.realpath(target)
    if real is None or not deps.is_directory(real):
        raise ValueError(f"not a folder on this machine: {target}")
    if deps.platform == "darwin" and any(BUNDLE_SEGMENT.search(segment) for segment in _SEPARATORS.split(real)):
        raise ValueError(f"refused: {target} is inside a macOS bundle")

    def key(p: str) -> str:
        trimmed = _TRAILING.sub("", p) or p
        return trimmed.lower() if deps.platform in ("win32", "darwin") else trimmed

    allowed: set[str] = set()
    for folder in known:
        resolved = deps.realpath(folder)
        if resolved is not None:
            allowed.add(key(resolved))
    if key(real) not in allowed:
        raise ValueError(f"refused: {target} is not the folder of a live session or an open IDE project")
    return real

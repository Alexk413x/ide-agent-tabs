from __future__ import annotations

import ntpath
import posixpath
import re
from collections.abc import Sequence
from types import ModuleType

_WINDOWS_PATH = re.compile(r"[A-Za-z]:[\\/]|\\\\")
_WINDOWS_SYSTEM = frozenset({"windows", "program files", "program files (x86)", "programdata"})
_POSIX_SYSTEM = frozenset({"usr", "etc", "bin", "sbin", "var", "system", "library", "opt"})


def _flavor(path: str) -> ModuleType:
    return ntpath if _WINDOWS_PATH.match(path) else posixpath


def _split(flavor: ModuleType, path: str) -> tuple[str, list[str]]:
    drive, rest = flavor.splitdrive(flavor.normpath(path))
    return flavor.normcase(drive), [part for part in rest.replace("\\", "/").split("/") if part]


def _starts_with(parts: Sequence[str], prefix: Sequence[str]) -> bool:
    return list(parts[: len(prefix)]) == list(prefix)


def default_member_folder(session_folder: str, home: str) -> str:
    flavor = _flavor(session_folder)
    drive, parts = _split(flavor, session_folder)
    itself = flavor.normpath(session_folder)
    if not parts:
        return itself
    folded = [flavor.normcase(part) for part in parts]
    home_drive, home_parts = _split(flavor, home) if home else ("", [])
    home_folded = [flavor.normcase(part) for part in home_parts] if home_drive == drive else []
    inside_home = bool(home_folded) and len(folded) > len(home_folded) and _starts_with(folded, home_folded)
    depth = len(home_folded) + 1 if inside_home else 1
    system = _WINDOWS_SYSTEM if flavor is ntpath else _POSIX_SYSTEM
    if not inside_home and (_starts_with(home_folded, folded[:1]) or parts[0].casefold() in system):
        return itself
    return flavor.splitdrive(itself)[0] + flavor.sep + flavor.sep.join(parts[:depth])

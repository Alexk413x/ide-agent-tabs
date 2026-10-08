from __future__ import annotations

import ntpath
import os
import posixpath
import sys


def win32_is_absolute(p: str) -> bool:
    if not p:
        return False
    if p[0] in "/\\":
        return True
    return len(p) > 2 and p[0].isascii() and p[0].isalpha() and p[1] == ":" and p[2] in "/\\"


def posix_is_absolute(p: str) -> bool:
    return p.startswith("/")


def is_absolute(p: str, windows: bool | None = None) -> bool:
    windows = sys.platform == "win32" if windows is None else windows
    return win32_is_absolute(p) if windows else posix_is_absolute(p)


def _keep_trailing(original: str, normal: str, seps: str, sep: str) -> str:
    if original and original[-1] in seps and not normal.endswith(sep):
        return normal + sep
    return normal


def win32_normalize(p: str) -> str:
    if p == "":
        return "."
    return _keep_trailing(p, ntpath.normpath(p), "/\\", "\\")


def posix_normalize(p: str) -> str:
    if p == "":
        return "."
    normal = posixpath.normpath(p)
    if normal.startswith("//") and not normal.startswith("///"):
        normal = normal[1:]
    return _keep_trailing(p, normal, "/", "/")


def normalize(p: str, windows: bool | None = None) -> str:
    windows = sys.platform == "win32" if windows is None else windows
    return win32_normalize(p) if windows else posix_normalize(p)


def win32_resolve(*paths: str) -> str:
    joined = ""
    for p in reversed(paths):
        if not p:
            continue
        joined = ntpath.join(p, joined) if joined else p
        drive, rest = ntpath.splitdrive(joined)
        if drive and rest.startswith(("\\", "/")):
            break
    drive, rest = ntpath.splitdrive(joined)
    if not drive or not rest.startswith(("\\", "/")):
        cwd = os.getcwd() if sys.platform == "win32" else "C:\\"
        joined = ntpath.join(cwd, joined) if joined else cwd
    return ntpath.normpath(joined)


def posix_resolve(*paths: str) -> str:
    joined = ""
    for p in reversed(paths):
        if not p:
            continue
        joined = posixpath.join(p, joined) if joined else p
        if joined.startswith("/"):
            break
    if not joined.startswith("/"):
        joined = posixpath.join(os.getcwd() if sys.platform != "win32" else "/", joined)
    normal = posixpath.normpath(joined)
    return "/" + normal.lstrip("/") if normal.startswith("//") else normal


def resolve(*paths: str, windows: bool | None = None) -> str:
    windows = sys.platform == "win32" if windows is None else windows
    return win32_resolve(*paths) if windows else posix_resolve(*paths)

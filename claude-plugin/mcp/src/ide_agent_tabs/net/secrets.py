from __future__ import annotations

import contextlib
import functools
import os
import sys
from pathlib import Path
from typing import Any

from ..files import replace_retrying
from .private_files import check_private, ensure_private_dir, make_private_file

_DPAPI_FILE = "passphrase.dpapi"
_PLAIN_FILE = "passphrase"
_CRYPTPROTECT_UI_FORBIDDEN = 0x1


class SecretsError(Exception):
    pass


def new_passphrase() -> bytes:
    return os.urandom(32).hex().encode("ascii")


@functools.cache
def _api() -> Any:
    assert sys.platform == "win32"
    import ctypes
    from ctypes import wintypes

    class DataBlob(ctypes.Structure):
        _fields_ = (("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char)))

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32.LocalFree.restype = ctypes.c_void_p
    kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
    blob = ctypes.POINTER(DataBlob)
    crypt32.CryptProtectData.restype = wintypes.BOOL
    crypt32.CryptProtectData.argtypes = (blob, wintypes.LPCWSTR, blob, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, blob)
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    crypt32.CryptUnprotectData.argtypes = (blob, ctypes.c_void_p, blob, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, blob)
    return kernel32, crypt32, DataBlob


def _dpapi(data: bytes, protect: bool) -> bytes:
    import ctypes

    kernel32, crypt32, data_blob = _api()
    buffer = ctypes.create_string_buffer(data, len(data))
    source = data_blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    result = data_blob()
    if protect:
        ok = crypt32.CryptProtectData(
            ctypes.byref(source), "agent-tabs", None, None, None, _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(result)
        )
    else:
        ok = crypt32.CryptUnprotectData(ctypes.byref(source), None, None, None, None, _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(result))
    if not ok:
        raise SecretsError(f"DPAPI {'protect' if protect else 'unprotect'} failed: {ctypes.WinError(ctypes.get_last_error())}")
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(result.pbData, ctypes.c_void_p))


def _file_name() -> str:
    return _DPAPI_FILE if sys.platform == "win32" else _PLAIN_FILE


def store_passphrase(dir: Path, passphrase: bytes) -> None:
    if not passphrase:
        raise SecretsError("the passphrase is empty")
    ensure_private_dir(dir)
    content = _dpapi(passphrase, True) if sys.platform == "win32" else passphrase
    target = dir / _file_name()
    temp = dir / f"{_file_name()}.{os.getpid()}.tmp"
    with contextlib.suppress(OSError):
        os.remove(temp)
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
        make_private_file(temp)
        replace_retrying(str(temp), str(target))
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(temp)
        raise
    problems = check_private(target)
    if problems:
        raise SecretsError("; ".join(problems))


def load_passphrase(dir: Path) -> bytes:
    target = dir / _file_name()
    if not os.path.lexists(target):
        raise FileNotFoundError(f"no stored passphrase at {target}")
    problems = check_private(dir) + check_private(target)
    if problems:
        raise SecretsError("; ".join(problems))
    content = target.read_bytes()
    passphrase = _dpapi(content, False) if sys.platform == "win32" else content
    if not passphrase:
        raise SecretsError("the stored passphrase is empty")
    return passphrase

from __future__ import annotations

import functools
import os
import sys
from typing import Any

GENERIC_READ = 0x80000000
FILE_SHARE_ALL = 0x1 | 0x2 | 0x4
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x80
INVALID_HANDLE_VALUE = -1


@functools.cache
def kernel32() -> Any:
    import ctypes
    from ctypes import wintypes

    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.restype = wintypes.HANDLE
    k.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    k.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    k.CloseHandle.argtypes = (wintypes.HANDLE,)
    k.CreateFileW.restype = wintypes.HANDLE
    k.CreateFileW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    return k


# Python's open() on Windows withholds FILE_SHARE_DELETE, so while it reads a file no other process can delete
# or rename over it; Node's readers share delete, and its lock release gives up on the refusal.
def open_shared_read(path: str) -> int:
    if sys.platform != "win32":
        return os.open(path, os.O_RDONLY)
    import _winapi
    import msvcrt

    handle = _winapi.CreateFile(path, GENERIC_READ, FILE_SHARE_ALL, 0, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, 0)
    try:
        return msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        _winapi.CloseHandle(handle)
        raise

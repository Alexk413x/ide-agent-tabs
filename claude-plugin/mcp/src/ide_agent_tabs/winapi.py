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
    import ctypes
    import msvcrt

    handle = kernel32().CreateFileW(path, GENERIC_READ, FILE_SHARE_ALL, None, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None)
    if handle is None or handle == ctypes.c_void_p(INVALID_HANDLE_VALUE).value:
        code = ctypes.get_last_error()
        raise ctypes.WinError(code)
    try:
        return msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        kernel32().CloseHandle(handle)
        raise

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
    assert sys.platform == "win32"
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


_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FSCTL_GET_REPARSE_POINT = 0x000900A8
_IO_REPARSE_TAG_APPEXECLINK = 0x8000001B
_REPARSE_BUFFER_SIZE = 16 * 1024


# os.readlink reads symlinks and junctions only; the Store's pwsh.exe and wt.exe are app execution aliases,
# whose target Node's readlink returns.
def read_app_exec_link(path: str) -> str | None:
    if sys.platform != "win32":
        return None
    import ctypes
    import struct
    from ctypes import wintypes

    k = kernel32()
    handle = k.CreateFileW(path, 0, FILE_SHARE_ALL, None, OPEN_EXISTING, _FILE_FLAG_OPEN_REPARSE_POINT | _FILE_FLAG_BACKUP_SEMANTICS, None)
    if handle is None or handle == ctypes.c_void_p(INVALID_HANDLE_VALUE).value:
        return None
    try:
        buffer = ctypes.create_string_buffer(_REPARSE_BUFFER_SIZE)
        returned = wintypes.DWORD()
        k.DeviceIoControl.argtypes = (
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPVOID,
        )
        ok = k.DeviceIoControl(handle, _FSCTL_GET_REPARSE_POINT, None, 0, buffer, _REPARSE_BUFFER_SIZE, ctypes.byref(returned), None)
        if not ok:
            return None
        data = buffer.raw[: returned.value]
    finally:
        k.CloseHandle(handle)
    if len(data) < 12 or struct.unpack_from("<I", data)[0] != _IO_REPARSE_TAG_APPEXECLINK:
        return None
    strings = data[12:].decode("utf-16-le", "replace").split("\0")
    return strings[2] if len(strings) > 2 and strings[2] else None

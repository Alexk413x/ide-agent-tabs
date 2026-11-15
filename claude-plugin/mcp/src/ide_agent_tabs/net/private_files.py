from __future__ import annotations

import functools
import os
import stat
import struct
import subprocess
import sys
from pathlib import Path
from typing import Any

_SYSTEM_SID = "S-1-5-18"
_ADMINISTRATORS_SID = "S-1-5-32-544"
_ICACLS_TIMEOUT_S = 30

_TOKEN_QUERY = 0x8
_TOKEN_USER = 1
_SE_FILE_OBJECT = 1
_OWNER_AND_DACL = 0x1 | 0x4
_ACL_SIZE_INFORMATION = 2
_ACCESS_ALLOWED_ACE_TYPE = 0
_INHERITED_ACE = 0x10
_SE_DACL_PROTECTED = 0x1000
_ERROR_INSUFFICIENT_BUFFER = 122


class PrivateFileError(OSError):
    pass


@functools.cache
def _api() -> Any:
    assert sys.platform == "win32"
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    pointer = ctypes.c_void_p
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetCurrentProcess.argtypes = ()
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.LocalFree.restype = pointer
    kernel32.LocalFree.argtypes = (pointer,)
    kernel32.GetSystemWindowsDirectoryW.restype = wintypes.UINT
    kernel32.GetSystemWindowsDirectoryW.argtypes = (wintypes.LPWSTR, wintypes.UINT)
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.OpenProcessToken.argtypes = (wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE))
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = (wintypes.HANDLE, wintypes.DWORD, pointer, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = (pointer, ctypes.POINTER(pointer))
    advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi32.GetNamedSecurityInfoW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(pointer),
        ctypes.POINTER(pointer),
        ctypes.POINTER(pointer),
        ctypes.POINTER(pointer),
        ctypes.POINTER(pointer),
    )
    advapi32.GetSecurityDescriptorControl.restype = wintypes.BOOL
    advapi32.GetSecurityDescriptorControl.argtypes = (pointer, ctypes.POINTER(wintypes.WORD), ctypes.POINTER(wintypes.DWORD))
    advapi32.GetAclInformation.restype = wintypes.BOOL
    advapi32.GetAclInformation.argtypes = (pointer, pointer, wintypes.DWORD, wintypes.DWORD)
    advapi32.GetAce.restype = wintypes.BOOL
    advapi32.GetAce.argtypes = (pointer, wintypes.DWORD, ctypes.POINTER(pointer))
    return kernel32, advapi32


def _sid_string(sid: int) -> str:
    import ctypes

    kernel32, advapi32 = _api()
    text = ctypes.c_void_p()
    if not advapi32.ConvertSidToStringSidW(ctypes.c_void_p(sid), ctypes.byref(text)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        assert text.value is not None
        return ctypes.wstring_at(text.value)
    finally:
        kernel32.LocalFree(text)


@functools.cache
def _user_sid() -> str:
    import ctypes
    from ctypes import wintypes

    kernel32, advapi32 = _api()
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        size = wintypes.DWORD()
        advapi32.GetTokenInformation(token, _TOKEN_USER, None, 0, ctypes.byref(size))
        if ctypes.get_last_error() != _ERROR_INSUFFICIENT_BUFFER:
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(token, _TOKEN_USER, buffer, size, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        sid = ctypes.c_void_p.from_buffer(buffer).value
        assert sid is not None
        return _sid_string(sid)
    finally:
        kernel32.CloseHandle(token)


def _windows_security(path: str) -> tuple[str, int, list[tuple[int, int, str]] | None]:
    import ctypes
    from ctypes import wintypes

    kernel32, advapi32 = _api()
    owner = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    error = advapi32.GetNamedSecurityInfoW(
        path, _SE_FILE_OBJECT, _OWNER_AND_DACL, ctypes.byref(owner), None, ctypes.byref(dacl), None, ctypes.byref(descriptor)
    )
    if error:
        raise ctypes.WinError(error)
    try:
        assert owner.value is not None
        owner_sid = _sid_string(owner.value)
        control = wintypes.WORD()
        revision = wintypes.DWORD()
        if not advapi32.GetSecurityDescriptorControl(descriptor, ctypes.byref(control), ctypes.byref(revision)):
            raise ctypes.WinError(ctypes.get_last_error())
        if dacl.value is None:
            return owner_sid, control.value, None
        counts = (wintypes.DWORD * 3)()
        if not advapi32.GetAclInformation(dacl, counts, ctypes.sizeof(counts), _ACL_SIZE_INFORMATION):
            raise ctypes.WinError(ctypes.get_last_error())
        entries: list[tuple[int, int, str]] = []
        for index in range(counts[0]):
            ace = ctypes.c_void_p()
            if not advapi32.GetAce(dacl, index, ctypes.byref(ace)):
                raise ctypes.WinError(ctypes.get_last_error())
            assert ace.value is not None
            ace_type, flags = struct.unpack("<BB", ctypes.string_at(ace.value, 2))
            # An allowed ACE is a 4-byte header, a 4-byte access mask, then the SID.
            entries.append((ace_type, flags, _sid_string(ace.value + 8) if ace_type == _ACCESS_ALLOWED_ACE_TYPE else ""))
        return owner_sid, control.value, entries
    finally:
        kernel32.LocalFree(descriptor)


def _windows_problems(path: str, directory: bool) -> list[str]:
    try:
        owner, control, entries = _windows_security(path)
    except OSError as error:
        return [f"cannot read the access list of {path}: {error}"]
    allowed = {_user_sid(), _SYSTEM_SID, _ADMINISTRATORS_SID}
    problems: list[str] = []
    if owner not in allowed:
        problems.append(f"{path} is owned by {owner}, not the current user")
    if entries is None:
        problems.append(f"{path} has no access list, so everyone has full access")
        return problems
    if directory and not control & _SE_DACL_PROTECTED:
        problems.append(f"{path} inherits access from its parent folder")
    for ace_type, flags, sid in entries:
        if ace_type != _ACCESS_ALLOWED_ACE_TYPE:
            problems.append(f"{path} has an access list entry of type {ace_type}, which is not an allow entry")
        elif sid not in allowed:
            inherited = " (inherited)" if flags & _INHERITED_ACE else ""
            problems.append(f"{path} grants access to {sid}{inherited}")
    return problems


def _posix_problems(path: str, status: os.stat_result) -> list[str]:
    assert sys.platform != "win32"
    problems: list[str] = []
    if status.st_uid != os.getuid():
        problems.append(f"{path} is owned by uid {status.st_uid}, not the current user")
    if status.st_mode & 0o077:
        problems.append(f"{path} has mode {stat.S_IMODE(status.st_mode):04o}, which grants access to group or others")
    return problems


def _is_link(status: os.stat_result) -> bool:
    return stat.S_ISLNK(status.st_mode) or bool(getattr(status, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _components(path: Path, root: Path | None) -> list[Path]:
    if root is None:
        return [path]
    relative = path.relative_to(root)
    return [root.joinpath(*relative.parts[: count + 1]) for count in range(len(relative.parts))] or [path]


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(path))


def check_private(path: Path, *, root: Path | None = None) -> list[str]:
    path = _absolute(path)
    try:
        components = _components(path, _absolute(root) if root is not None else None)
    except ValueError:
        return [f"{path} is not inside {root}"]
    for component in components:
        try:
            status = os.lstat(component)
        except OSError as error:
            return [f"cannot inspect {component}: {error}"]
        if _is_link(status):
            return [f"{component} is a symbolic link or reparse point"]
    status = os.lstat(path)
    directory = stat.S_ISDIR(status.st_mode)
    if not directory and not stat.S_ISREG(status.st_mode):
        return [f"{path} is neither a file nor a folder"]
    if sys.platform == "win32":
        return _windows_problems(str(path), directory)
    return _posix_problems(str(path), status)


def _icacls(path: Path, arguments: list[str]) -> None:
    import ctypes

    kernel32, _ = _api()
    buffer = ctypes.create_unicode_buffer(520)
    if not kernel32.GetSystemWindowsDirectoryW(buffer, 520):
        raise PrivateFileError("cannot locate the Windows folder")
    environment = {name: os.environ[name] for name in ("SYSTEMROOT", "SYSTEMDRIVE") if name in os.environ}
    try:
        result = subprocess.run(
            [os.path.join(buffer.value, "System32", "icacls.exe"), str(path), *arguments],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            env=environment,
            timeout=_ICACLS_TIMEOUT_S,
            shell=False,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise PrivateFileError(f"icacls failed for {path}: {error}") from error
    if result.returncode:
        output = (result.stdout + result.stderr).decode("utf-8", "replace").strip()
        raise PrivateFileError(f"icacls exited {result.returncode} for {path}: {output}")


def _restrict(path: Path, directory: bool) -> None:
    if sys.platform != "win32":
        os.chmod(path, 0o700 if directory else 0o600)
        return
    suffix = ":(OI)(CI)F" if directory else ":F"
    _icacls(path, ["/reset"])
    _icacls(path, ["/inheritance:r", "/grant:r", *(f"*{sid}{suffix}" for sid in (_user_sid(), _SYSTEM_SID, _ADMINISTRATORS_SID))])


def _refuse_links(path: Path) -> None:
    try:
        status = os.lstat(path)
    except FileNotFoundError:
        return
    if _is_link(status):
        raise PrivateFileError(f"{path} is a symbolic link or reparse point")


def _verify(path: Path) -> None:
    problems = check_private(path)
    if problems:
        raise PrivateFileError("; ".join(problems))


def ensure_private_dir(path: Path) -> None:
    path = _absolute(path)
    _refuse_links(path)
    os.makedirs(path, mode=0o700, exist_ok=True)
    if check_private(path):
        _restrict(path, True)
    _verify(path)


def make_private_file(path: Path) -> None:
    path = _absolute(path)
    _refuse_links(path)
    if not path.is_file():
        raise PrivateFileError(f"{path} is not a file")
    _restrict(path, False)
    _verify(path)

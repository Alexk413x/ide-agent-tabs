from __future__ import annotations

import os
import sys
from collections.abc import Callable

from . import private_files

_TOKEN_QUERY = 0x8
_TOKEN_ELEVATION = 20
_POLICY_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System"
_BUILTIN_ADMINISTRATOR_RID = "500"


class ElevationError(Exception):
    pass


def _token_is_elevated() -> bool:
    assert sys.platform == "win32"
    import ctypes
    from ctypes import wintypes

    kernel32, advapi32 = private_files._api()
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        elevation = wintypes.DWORD()
        size = wintypes.DWORD()
        if not advapi32.GetTokenInformation(token, _TOKEN_ELEVATION, ctypes.byref(elevation), ctypes.sizeof(elevation), ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        return elevation.value != 0
    finally:
        kernel32.CloseHandle(token)


def _enable_lua() -> int | None:
    assert sys.platform == "win32"
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _POLICY_KEY) as key:
            value, _kind = winreg.QueryValueEx(key, "EnableLUA")
    except FileNotFoundError:
        return None
    return value if isinstance(value, int) else None


def _user_sid() -> str:
    return private_files._user_sid()


def _euid() -> int:
    assert sys.platform != "win32"
    return os.geteuid()


def is_elevated(
    platform: str | None = None,
    token_is_elevated: Callable[[], bool] = _token_is_elevated,
    euid: Callable[[], int] | None = None,
) -> bool:
    if (platform or sys.platform) == "win32":
        return token_is_elevated()
    return (euid or _euid)() == 0


def unelevated_token_available(
    platform: str | None = None,
    enable_lua: Callable[[], int | None] = _enable_lua,
    user_sid: Callable[[], str] = _user_sid,
) -> bool:
    if (platform or sys.platform) != "win32":
        return True
    # A missing value counts as UAC on: Windows ships with EnableLUA set, and only an explicit 0 turns it off.
    if enable_lua() == 0:
        return False
    return user_sid().rsplit("-", 1)[-1] != _BUILTIN_ADMINISTRATOR_RID


def unsupported_reason(
    platform: str | None = None,
    enable_lua: Callable[[], int | None] = _enable_lua,
    user_sid: Callable[[], str] = _user_sid,
) -> str | None:
    if (platform or sys.platform) != "win32":
        return None
    if enable_lua() == 0:
        return (
            "User Account Control is off on this computer, so Windows can't give this account an unelevated token. "
            "Version 1 doesn't support the network feature here. Turn User Account Control on and restart to use it."
        )
    if user_sid().rsplit("-", 1)[-1] == _BUILTIN_ADMINISTRATOR_RID:
        return (
            "This is the built-in Administrator account, which Windows doesn't give an unelevated token. "
            "Version 1 doesn't support the network feature for it. Sign in with another account to use it."
        )
    return None


def refuse_if_elevated(
    platform: str | None = None,
    token_is_elevated: Callable[[], bool] = _token_is_elevated,
    enable_lua: Callable[[], int | None] = _enable_lua,
    user_sid: Callable[[], str] = _user_sid,
    euid: Callable[[], int] | None = None,
) -> None:
    if not is_elevated(platform, token_is_elevated, euid):
        return
    reason = unsupported_reason(platform, enable_lua, user_sid)
    if reason is not None:
        raise ElevationError(reason)
    raise ElevationError(
        "Stop: this command runs with administrator rights, and pairing never continues under the administrator token. "
        "Close this window and run the same command from a normal terminal that is not elevated."
    )

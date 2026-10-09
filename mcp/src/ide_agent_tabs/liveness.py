from __future__ import annotations

import os
import sys

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_ERROR_ACCESS_DENIED = 5


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        if pid > 0xFFFFFFFF:
            return False
        # os.kill(pid, 0) on Windows calls TerminateProcess with exit code 0, so it ends the process it asks about.
        import _winapi

        try:
            handle = _winapi.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        except OSError as e:
            return getattr(e, "winerror", None) == _ERROR_ACCESS_DENIED
        try:
            return _winapi.GetExitCodeProcess(handle) == _STILL_ACTIVE
        except OSError:
            return False
        finally:
            _winapi.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True

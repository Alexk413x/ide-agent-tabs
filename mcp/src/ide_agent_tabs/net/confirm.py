from __future__ import annotations

import os
import sys
from collections.abc import Callable, Sequence
from typing import Any

ANSWER = "yes"


class ConfirmError(Exception):
    pass


def _console(stream: Any) -> bool:
    try:
        descriptor = stream.fileno()
        if not os.isatty(descriptor):
            return False
    except (OSError, ValueError, AttributeError):
        return False
    if sys.platform != "win32":
        return True
    import ctypes
    import msvcrt

    # CPython's isatty is true for NUL and other character devices; only a console handle answers GetConsoleMode.
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetConsoleMode.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32))
    kernel32.GetConsoleMode.restype = ctypes.c_int
    mode = ctypes.c_uint32()
    return bool(kernel32.GetConsoleMode(msvcrt.get_osfhandle(descriptor), ctypes.byref(mode)))


def interactive() -> bool:
    return _console(sys.stdin) and _console(sys.stdout)


def _print(text: str) -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def confirm(lines: Sequence[str], *, read: Callable[[str], str] | None = None, write: Callable[[str], None] = _print) -> bool:
    if read is None:
        if not interactive():
            raise ConfirmError("this command widens what peers can do, so it needs an interactive terminal; run it yourself in a terminal")
        read = input
    for line in lines:
        write(line)
    try:
        answer = read(f"Type {ANSWER} to apply this change: ")
    except (EOFError, KeyboardInterrupt):
        return False
    return answer.strip() == ANSWER

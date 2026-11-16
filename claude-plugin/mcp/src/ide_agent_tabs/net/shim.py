from __future__ import annotations

import os
import re
import shlex
import sys
from collections.abc import Callable
from pathlib import Path

from ..files import write_atomically
from .private_files import PrivateFileError, ensure_private_dir, make_private_file

SHIM_FOLDER = "bin"
CMD_FILE = "agent-tabs.cmd"
PS1_FILE = "agent-tabs.ps1"
SH_FILE = "agent-tabs"
SHIM_FILES = (CMD_FILE, PS1_FILE, SH_FILE)
WM_SETTINGCHANGE = 0x1A
HWND_BROADCAST = 0xFFFF
SMTO_ABORTIFHUNG = 0x2
BROADCAST_TIMEOUT_MS = 5000

_UNSAFE_PATH = re.compile(r"[\x00-\x1f\"]")
_CMD_UNSAFE = re.compile(r"[%]")
_USER_ENVIRONMENT = "Environment"

_PS1_TAIL = r"""# Windows PowerShell 5.1 drops the quotes inside an argument that it passes to a program, so the command line is
# built by hand, with the escaping that CommandLineToArgvW undoes.
function ConvertTo-Argument([string]$Text) {
  if ($Text -ne '' -and $Text -notmatch '[\s"]') { return $Text }
  $quoted = [regex]::Replace($Text, '(\\*)"', '$1$1\"')
  '"' + [regex]::Replace($quoted, '(\\+)$', '$1$1') + '"'
}
$info = New-Object System.Diagnostics.ProcessStartInfo
$info.FileName = $exe
$info.Arguments = (@($lead) + @($args) | ForEach-Object { ConvertTo-Argument ([string]$_) }) -join ' '
$info.UseShellExecute = $false
$piped = $MyInvocation.ExpectingInput
if ($piped) {
  $info.RedirectStandardInput = $true
}
# A UTF-8 console (code page 65001) makes Windows PowerShell 5.1 send a byte order mark ahead of piped input, so a
# UTF-8 encoding without one is set for the start.
$inputEncoding = $null
if ($piped) {
  try {
    $inputEncoding = [Console]::InputEncoding
    [Console]::InputEncoding = New-Object System.Text.UTF8Encoding($false)
  } catch {
    $inputEncoding = $null
  }
}
try {
  $process = [System.Diagnostics.Process]::Start($info)
} finally {
  if ($inputEncoding) {
    try { [Console]::InputEncoding = $inputEncoding } catch {}
  }
}
if ($piped) {
  $bytes = (New-Object System.Text.UTF8Encoding($false)).GetBytes((@($input) -join "`n"))
  $process.StandardInput.BaseStream.Write($bytes, 0, $bytes.Length)
  $process.StandardInput.Close()
}
$process.WaitForExit()
exit $process.ExitCode
"""


class ShimError(Exception):
    pass


def shim_folder(home: str) -> Path:
    return Path(home) / SHIM_FOLDER


def _checked(path: str, what: str) -> str:
    if not os.path.isabs(path) or _UNSAFE_PATH.search(path):
        raise ShimError(f"{what} must be an absolute path without quotes or control characters: {path!r}")
    return path


def cmd_source(interpreter: str, entry: str) -> str:
    if _CMD_UNSAFE.search(interpreter) or _CMD_UNSAFE.search(entry):
        raise ShimError("a path with a percent sign can't go into a .cmd file")
    return f'@"{interpreter}" -I -S "{entry}" %*\r\n'


def _ps_literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def ps1_source(interpreter: str, entry: str) -> str:
    head = f"$exe = {_ps_literal(interpreter)}\n$lead = @('-I', '-S', {_ps_literal(entry)})\n"
    return head + _PS1_TAIL


def sh_source(interpreter: str, entry: str) -> str:
    return f'#!/bin/sh\nexec {shlex.quote(interpreter)} -I -S {shlex.quote(entry)} "$@"\n'


def _contents(interpreter: str, entry: str) -> dict[str, str]:
    return {CMD_FILE: cmd_source(interpreter, entry), PS1_FILE: ps1_source(interpreter, entry), SH_FILE: sh_source(interpreter, entry)}


def install_shim(home: str, interpreter: str, entry: str) -> Path:
    interpreter = _checked(interpreter, "the interpreter")
    entry = _checked(entry, "the launcher script")
    contents = _contents(interpreter, entry)
    folder = shim_folder(home)
    try:
        ensure_private_dir(folder)
        for name in os.listdir(folder):
            if name.endswith(".tmp") and name.startswith(SHIM_FILES):
                os.remove(folder / name)
        others = sorted(name for name in os.listdir(folder) if name not in SHIM_FILES)
        if others:
            raise ShimError(f"{folder} holds files that are not shims ({', '.join(others)}); it must hold nothing else")
        for name, text in contents.items():
            path = folder / name
            write_atomically(str(path), text.encode("utf-8"))
            make_private_file(path)
            if sys.platform != "win32":
                os.chmod(path, 0o700 if name == SH_FILE else 0o600)
    except (PrivateFileError, OSError) as error:
        raise ShimError(f"the command shims could not be written to {folder}: {error}") from error
    return folder


def _normal(entry: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.expandvars(entry.strip().strip('"'))))


def _contains(path_value: str, folder: str, separator: str = os.pathsep) -> bool:
    wanted = _normal(folder)
    return any(entry.strip() and _normal(entry) == wanted for entry in path_value.split(separator))


def _read_user_path() -> tuple[str, int]:
    assert sys.platform == "win32"
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _USER_ENVIRONMENT) as key:
            value, kind = winreg.QueryValueEx(key, "Path")
    except FileNotFoundError:
        return "", winreg.REG_EXPAND_SZ
    return (value if isinstance(value, str) else ""), (kind if kind in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) else winreg.REG_EXPAND_SZ)


def _write_user_path(value: str, kind: int) -> None:
    assert sys.platform == "win32"
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _USER_ENVIRONMENT, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, "Path", 0, kind, value)


def _broadcast_change() -> None:
    assert sys.platform == "win32"
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SendMessageTimeoutW.restype = ctypes.c_void_p
    user32.SendMessageTimeoutW.argtypes = (
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPCWSTR,
        wintypes.UINT,
        wintypes.UINT,
        ctypes.POINTER(ctypes.c_void_p),
    )
    result = ctypes.c_void_p()
    user32.SendMessageTimeoutW(
        HWND_BROADCAST, WM_SETTINGCHANGE, 0, _USER_ENVIRONMENT, SMTO_ABORTIFHUNG, BROADCAST_TIMEOUT_MS, ctypes.byref(result)
    )


def path_contains(
    folder: str | Path,
    path_value: str | None = None,
    platform: str | None = None,
    read_user_path: Callable[[], tuple[str, int]] = _read_user_path,
) -> bool:
    text = str(folder)
    if _contains(os.environ.get("PATH", "") if path_value is None else path_value, text):
        return True
    if (platform or sys.platform) != "win32" or path_value is not None:
        return False
    try:
        return _contains(read_user_path()[0], text, ";")
    except OSError:
        return False


def _profile_name(shell: str) -> str:
    name = os.path.basename(shell)
    if name == "zsh":
        return "~/.zshrc"
    return "~/.bashrc" if name == "bash" else "~/.profile"


def profile_line(folder: str | Path) -> str:
    return f'export PATH="$PATH":{shlex.quote(str(folder))}'


def append_to_user_path(
    folder: str | Path,
    platform: str | None = None,
    read_user_path: Callable[[], tuple[str, int]] = _read_user_path,
    write_user_path: Callable[[str, int], None] = _write_user_path,
    broadcast: Callable[[], None] = _broadcast_change,
    shell: str | None = None,
) -> str:
    text = str(folder)
    if (platform or sys.platform) != "win32":
        return f"Add this line to {_profile_name(shell or os.environ.get('SHELL', ''))}, then open a new terminal:\n{profile_line(text)}"
    if ";" in text or _UNSAFE_PATH.search(text):
        raise ShimError(f"{text!r} can't go into the PATH: it holds a semicolon, a quote or a control character")
    current, kind = read_user_path()
    if _contains(current, text, ";"):
        return f"{text} is already on your user PATH."
    updated = f"{current.rstrip(';')};{text}" if current.strip(";") else text
    write_user_path(updated, kind)
    broadcast()
    return f"Appended {text} to your user PATH. Open a new terminal to use agent-tabs."

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping, Sequence

TITLE = "Agent Tabs"
MAX_CHARS = 200
NOTICE_ENV = "AGENT_TABS_NOTICE"
_MACOS_TOOL = "/usr/bin/osascript"
_LINUX_TOOLS = ("/usr/bin/notify-send", "/usr/local/bin/notify-send", "/bin/notify-send")
_WINDOWS_SCRIPT = (
    "Add-Type -AssemblyName System.Windows.Forms;"
    "Add-Type -AssemblyName System.Drawing;"
    "$n = New-Object System.Windows.Forms.NotifyIcon;"
    "$n.Icon = [System.Drawing.SystemIcons]::Information;"
    "$n.Visible = $true;"
    f"$n.ShowBalloonTip(5000, '{TITLE}', $env:{NOTICE_ENV}, [System.Windows.Forms.ToolTipIcon]::Info);"
    "Start-Sleep -Seconds 5;"
    "$n.Dispose()"
)

Spawn = Callable[[Sequence[str], Mapping[str, str]], object]


def powershell_path() -> str | None:
    root = os.environ.get("SYSTEMROOT") or os.environ.get("WINDIR")
    if not root:
        return None
    path = os.path.join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    return path if os.path.isfile(path) else None


def _single_line(text: str) -> str:
    cleaned = "".join(char if char.isprintable() else " " for char in text).strip()
    return cleaned[:MAX_CHARS]


def _command(text: str) -> list[str] | None:
    if sys.platform == "win32":
        shell = powershell_path()
        return None if shell is None else [shell, "-NoProfile", "-NonInteractive", "-Command", _WINDOWS_SCRIPT]
    if sys.platform == "darwin":
        if not os.path.isfile(_MACOS_TOOL):
            return None
        script = ["-e", "on run argv", "-e", f'display notification (item 1 of argv) with title "{TITLE}"', "-e", "end run"]
        return [_MACOS_TOOL, *script, text]
    tool = next((path for path in _LINUX_TOOLS if os.path.isfile(path)), None)
    return None if tool is None else [tool, TITLE, text]


def _spawn(command: Sequence[str], env: Mapping[str, str]) -> object:
    from ..processes import spawn_detached

    return spawn_detached(command, env)


def notify(what: str, spawn: Spawn | None = None) -> bool:
    text = _single_line(f"{TITLE}: {what}")
    command = _command(text)
    if command is None:
        return False
    env = {**os.environ, NOTICE_ENV: text}
    try:
        (spawn or _spawn)(command, env)
    except (OSError, ValueError):
        return False
    return True

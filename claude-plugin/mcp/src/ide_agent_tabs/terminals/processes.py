from __future__ import annotations

import contextlib
import os
import posixpath
import signal
import subprocess
import sys
from collections.abc import Mapping, Sequence

from ..clock import now_ms
from ..files import read_bytes
from ..installed import find_on_path
from ..jsjson import trim
from ..processes import CREATE_BREAKAWAY_FROM_JOB, detached_flags, pid_alive, run
from .driver import TerminalTab

STARTUP_GRACE_MS = 60_000
GUI_SETTLE_MS = 2_000
_SHELL_NAMES = {"bash", "zsh", "fish"}

_SESSION_ENV = {
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_SSE_PORT",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_BRIDGE_SESSION_ID",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_CODE_EXECPATH",
    "CLAUDE_PID",
    "CLAUDE_EFFORT",
    "CLAUDE_PLUGIN_ROOT",
    "CLAUDE_PLUGIN_DATA",
    "IDE_AGENT_TABS_ID",
    "IDE_AGENT_TABS_AGENT",
    "CODEX_SANDBOX",
    "CODEX_SANDBOX_NETWORK_DISABLED",
    "GEMINI_CLI",
    "OPENCODE_SESSION_ID",
    "ANTIGRAVITY_CLI_ALIAS",
}


# A terminal the server starts, or a tmux server it starts, keeps this environment for every later tab, so
# variables that identify the calling agent session are dropped.
def terminal_environment(env: Mapping[str, str]) -> dict[str, str]:
    return {name: value for name, value in env.items() if name.upper() not in _SESSION_ENV}


def find_executable(path_var: str, name: str, fallbacks: Sequence[str]) -> str | None:
    return find_on_path(path_var, name) or next((p for p in fallbacks if os.path.exists(p)), None)


def _popen(command: str | Sequence[str], env: Mapping[str, str] | None, flags: int) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        command if isinstance(command, str) else list(command),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=dict(env) if env is not None else None,
        creationflags=flags,
        start_new_session=sys.platform != "win32",
    )


def spawn_detached(command: str | Sequence[str], env: Mapping[str, str] | None) -> subprocess.Popen[bytes]:
    try:
        return _popen(command, env, detached_flags())
    except PermissionError:
        if sys.platform != "win32":
            raise
    # A job object that forbids breakaway refuses CREATE_BREAKAWAY_FROM_JOB with access denied.
    return _popen(command, env, detached_flags() & ~CREATE_BREAKAWAY_FROM_JOB)


def start_detached(command: str, args: Sequence[str], env: Mapping[str, str], settle_ms: float) -> None:
    child = spawn_detached([command, *args], env)
    try:
        code = child.wait(timeout=settle_ms / 1000)
    except subprocess.TimeoutExpired:
        return
    if code != 0:
        raise RuntimeError(f"{os.path.basename(command)} exited with code {code}")


def read_pid(file: str | None) -> int | None:
    if not file:
        return None
    try:
        text = trim(read_bytes(file).decode("utf-8", "replace"))
    except OSError:
        return None
    if not text.isdigit() or not text.isascii():
        return None
    pid = int(text)
    return pid if 0 < pid <= 2**53 - 1 else None


def is_shell_name(comm: str) -> bool:
    name = posixpath.basename(trim(comm))
    return (name.removeprefix("-")) in _SHELL_NAMES


def _process_name(pid: int) -> str | None:
    if sys.platform.startswith("linux"):
        try:
            with open(f"/proc/{pid}/comm", encoding="utf-8", errors="replace") as f:
                return f.read()
        except OSError:
            return None
    try:
        result = run("ps", ["-p", str(pid), "-o", "comm="], timeout=10)
    except (OSError, TimeoutError):
        return None
    return result.stdout if result.code == 0 else None


def shell_running(pid: int) -> bool:
    if not pid_alive(pid):
        return False
    name = _process_name(pid)
    return name is not None and is_shell_name(name)


def pid_tabs_alive(tabs: list[TerminalTab], now: float | None = None) -> set[str]:
    now = now_ms() if now is None else now
    alive: set[str] = set()
    for tab in tabs:
        pid = read_pid(tab.get("pidFile"))
        if shell_running(pid) if pid is not None else now - tab["createdAt"] < STARTUP_GRACE_MS:
            alive.add(tab["id"])
    return alive


# The shell leads its own process group in a new terminal window. When it gets SIGHUP it passes the signal
# to its jobs, and the window closes once it exits.
def hang_up(tab: TerminalTab) -> None:
    pid = read_pid(tab.get("pidFile"))
    if pid is None:
        raise RuntimeError(f"tab {tab['id']} has no running shell yet, or its shell has ended")
    if shell_running(pid) and sys.platform != "win32":
        try:
            os.killpg(pid, signal.SIGHUP)
        except OSError:
            os.kill(pid, signal.SIGHUP)
    if tab.get("pidFile"):
        with contextlib.suppress(FileNotFoundError):
            os.remove(tab["pidFile"])

from __future__ import annotations

import ipaddress
import ntpath
import os
import posixpath
import re
import shlex
import shutil
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from ..processes import terminal_environment
from .login_task import Environment
from .notify import notify

CREATE_NEW_CONSOLE = 0x00000010
CONTINUE_MESSAGE = "Continue in the window that opened."
INVITATION_PREFIX = "atp1-invite:"
MAX_INVITATION_CHARS = 4096
MAX_CALLER_CHARS = 40
FALLBACK_CALLER = "an unknown program"

_INVITATION = re.compile(r"atp1-invite:[A-Za-z0-9_-]+")
_ADDRESS = re.compile(r"(?:0|[1-9][0-9]{0,2})(?:\.(?:0|[1-9][0-9]{0,2})){3}")
_MANUAL_TOKEN = re.compile(r"[A-Z2-7]{6}")
_CALLER_UNSAFE = re.compile(r"[^A-Za-z0-9 ._-]+")
_JOIN = ("net", "pair", "--join")
_PEER = ("net", "pair", "--peer")
_ALLOWED = (
    ("net", "pair"),
    ("net", "pair", "--invite"),
    _JOIN,
    ("net", "trust"),
    ("net", "grant"),
    ("net", "lan", "on"),
)
_LINUX_TERMINALS = (
    ("x-terminal-emulator", ("-e",)),
    ("gnome-terminal", ("--",)),
    ("konsole", ("-e",)),
    ("xfce4-terminal", ("-x",)),
    ("xterm", ("-e",)),
)
_MACOS_TOOL = "/usr/bin/osascript"
_SHELL = "/bin/sh"
_POSIX_HOLD = '"$@"; printf "\\nPress Enter to close this window. "; read _'
_WINDOWS_HOLD = (
    "import runpy, sys\n"
    "sys.argv = sys.argv[1:]\n"
    "try:\n"
    "    runpy.run_path(sys.argv[0], run_name='__main__')\n"
    "finally:\n"
    "    try:\n"
    "        input('\\nPress Enter to close this window.')\n"
    "    except (EOFError, KeyboardInterrupt):\n"
    "        pass\n"
)

Popen = Callable[..., Any]


class WindowError(Exception):
    pass


def validate_invitation(invitation: str) -> str:
    if (
        not isinstance(invitation, str)  # pyright: ignore[reportUnnecessaryIsInstance]
        or len(invitation) > MAX_INVITATION_CHARS
        or not _INVITATION.fullmatch(invitation)
    ):
        raise WindowError(f"the invitation must be {INVITATION_PREFIX} followed by letters, digits, - and _ only")
    return invitation


def validate_peer(address: str, token: str) -> tuple[str, str]:
    try:
        if not isinstance(address, str) or _ADDRESS.fullmatch(address) is None:  # pyright: ignore[reportUnnecessaryIsInstance]
            raise ValueError
        ipaddress.IPv4Address(address)
    except ValueError as error:
        raise WindowError("--peer must be a numeric IPv4 address") from error
    if not isinstance(token, str) or _MANUAL_TOKEN.fullmatch(token) is None:  # pyright: ignore[reportUnnecessaryIsInstance]
        raise WindowError("--token must be 6 letters and digits 2 to 7")
    return address, token


def _arguments(subcommand: Sequence[str], invitation: str | None) -> list[str]:
    key = tuple(subcommand)
    if len(key) == 6 and key[:3] == _PEER and key[4] == "--token":
        if invitation is not None:
            raise WindowError("agent-tabs net pair --peer takes no invitation")
        address, token = validate_peer(key[3], key[5])
        return [*_PEER, address, "--token", token]
    if key not in _ALLOWED:
        raise WindowError(f"agent-tabs {' '.join(subcommand)} is not a command that opens in its own window")
    if key == _JOIN:
        if invitation is None:
            raise WindowError("net pair --join needs an invitation")
        return [*key, validate_invitation(invitation)]
    if invitation is not None:
        raise WindowError(f"agent-tabs {' '.join(key)} takes no invitation")
    return list(key)


def _caller(text: str) -> str:
    cleaned = " ".join(_CALLER_UNSAFE.sub(" ", text if isinstance(text, str) else "").split())  # pyright: ignore[reportUnnecessaryIsInstance]
    return cleaned[:MAX_CALLER_CHARS].strip() or FALLBACK_CALLER


def console_interpreter(interpreter: str) -> str:
    if ntpath.basename(interpreter).lower() == "pythonw.exe":
        sibling = ntpath.join(ntpath.dirname(interpreter), "python.exe")
        if os.path.isfile(sibling):
            return sibling
    return interpreter


def _checked(path: str, what: str, platform: str) -> str:
    absolute = ntpath.isabs(path) if platform == "win32" else posixpath.isabs(path)
    if not absolute or re.search(r"[\x00-\x1f\"]", path):
        raise WindowError(f"{what} must be an absolute path without quotes or control characters: {path!r}")
    return path


def _popen(command: Sequence[str], **options: Any) -> Any:
    import subprocess

    return subprocess.Popen(list(command), **options)


def _spawn_windows(command: Sequence[str], environ: Mapping[str, str], cwd: str, popen: Popen) -> None:
    # No std handle is passed: the new console supplies its own, so nothing the caller holds reaches the window.
    popen(list(command), env=dict(environ), cwd=cwd, creationflags=CREATE_NEW_CONSOLE, close_fds=True, shell=False)


def _spawn_posix(command: Sequence[str], environ: Mapping[str, str], cwd: str, popen: Popen) -> None:
    import subprocess

    popen(
        list(command),
        env=dict(environ),
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
        shell=False,
    )


def _posix_command(env: Environment, arguments: Sequence[str]) -> list[str]:
    return [_SHELL, "-c", _POSIX_HOLD, "agent-tabs", env.interpreter, "-I", "-S", env.launch_script, *arguments]


def _terminal_command(
    env: Environment, arguments: Sequence[str], environ: Mapping[str, str], which: Callable[[str], str | None]
) -> list[str] | None:
    inner = _posix_command(env, arguments)
    if env.platform == "darwin":
        if not os.path.isfile(_MACOS_TOOL):
            return None
        script = ["-e", "on run argv", "-e", 'tell application "Terminal"', "-e", "activate", "-e", "do script (item 1 of argv)"]
        return [_MACOS_TOOL, *script, "-e", "end tell", "-e", "end run", shlex.join(inner)]
    if not (environ.get("DISPLAY") or environ.get("WAYLAND_DISPLAY")):
        return None
    for name, lead in _LINUX_TERMINALS:
        found = which(name)
        if found:
            return [found, *lead, *inner]
    return None


def _manual(arguments: Sequence[str]) -> str:
    return f"No terminal window could be opened from here. Run this in a terminal: agent-tabs {' '.join(arguments)}"


def open_command_window(
    subcommand: list[str],
    invitation: str | None,
    caller: str,
    env: Environment | None = None,
    *,
    popen: Popen | None = None,
    notifier: Callable[[str], object] | None = None,
    environ: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> str:
    env = env or Environment()
    arguments = _arguments(subcommand, invitation)
    interpreter = _checked(env.interpreter, "the interpreter", env.platform)
    launch_script = _checked(env.launch_script, "the launch script", env.platform)
    start = popen or _popen
    variables = terminal_environment(os.environ if environ is None else environ)
    cwd = env.user_home
    try:
        if env.platform == "win32":
            command = [console_interpreter(interpreter), "-I", "-S", "-c", _WINDOWS_HOLD, launch_script, *arguments]
            _spawn_windows(command, variables, cwd, start)
        else:
            command = _terminal_command(env, arguments, variables, which)
            if command is None:
                return _manual(arguments)
            _spawn_posix(command, variables, cwd, start)
    except (OSError, ValueError) as error:
        raise WindowError(f"the window could not be opened: {error}") from error
    shown = " ".join(subcommand)
    (notifier or notify)(f"{_caller(caller)} asked to run agent-tabs {shown}. Continue in the window that opened.")
    return CONTINUE_MESSAGE

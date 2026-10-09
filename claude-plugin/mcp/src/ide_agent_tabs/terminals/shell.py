from __future__ import annotations

import posixpath
import re
import time
import unicodedata
from typing import NamedTuple

from ..jsjson import CONTROL, stringify, trim, utf16_len

LAUNCHER_SH = "agent-launch.sh"
LAUNCHER_FISH = "agent-launch.fish"
LAUNCHER_PS1 = "agent-launch.ps1"
_SAFE_PATH = re.compile(r"/[A-Za-z0-9._/+-]+")
_TITLE_MAX = 40
MAX_INPUT_CHARS = 500
ENTER_DELAY_MS = 200


class LoginShell(NamedTuple):
    path: str
    kind: str


def login_shell(shell_env: str | None, platform: str) -> LoginShell:
    if shell_env and _SAFE_PATH.fullmatch(shell_env):
        name = posixpath.basename(shell_env)
        if name in ("bash", "zsh"):
            return LoginShell(shell_env, "posix")
        if name == "fish":
            return LoginShell(shell_env, "fish")
    return LoginShell("/bin/bash" if platform == "linux" else "/bin/zsh", "posix")


def launcher_name(shell: LoginShell) -> str:
    return LAUNCHER_FISH if shell.kind == "fish" else LAUNCHER_SH


def _check_shell(shell: LoginShell) -> None:
    if not _SAFE_PATH.fullmatch(shell.path):
        raise ValueError(f"unsafe shell path: {shell.path}")


def _source_launcher(shell: LoginShell) -> str:
    _check_shell(shell)
    if shell.kind == "fish":
        return f'source "$IDE_AGENT_TABS_LAUNCHER"; exec {shell.path} -l -i'
    return f'. "$IDE_AGENT_TABS_LAUNCHER"; exec {shell.path} -l -i'


def surface_argv(shell: LoginShell) -> list[str]:
    return [shell.path, "-l", "-i", "-c", _source_launcher(shell)]


# Ghostty on macOS runs a surface's command through /bin/sh -c. The shell path is checked against
# _SAFE_PATH, and everything else the launch needs reaches it through the surface's environment variables.
def surface_command(shell: LoginShell) -> str:
    return f"{shell.path} -l -i -c '{_source_launcher(shell)}'"


# Argv mode, for a terminal whose new tab can't take environment variables: the launcher and spec paths are
# positional arguments of the shell, never part of the script it interprets. fish 3.2 or later puts them in
# $argv; bash and zsh take the first one as $0.
def argv_mode_command(shell: LoginShell, launcher: str, spec: str) -> list[str]:
    _check_shell(shell)
    check_argv_paths("the login shell", [launcher, spec])
    if shell.kind == "fish":
        script = f'set -gx IDE_AGENT_TABS_SPEC $argv[2]; source "$argv[1]"; exec {shell.path} -l -i'
        return [shell.path, "-l", "-i", "-c", script, launcher, spec]
    script = f'IDE_AGENT_TABS_SPEC=$2; export IDE_AGENT_TABS_SPEC; . "$1"; exec {shell.path} -l -i'
    return [shell.path, "-l", "-i", "-c", script, "agent-tabs", launcher, spec]


def check_argv_paths(terminal: str, paths: list[str], refuse: str = "") -> None:
    for p in paths:
        bad = next((c for c in p if CONTROL.match(c) or c in refuse), None)
        if bad is not None:
            what = "a control character" if CONTROL.match(bad) else f"'{bad}'"
            raise ValueError(f"{terminal} can't start a path that holds {what}: {stringify(p)}")


_SPACES = re.compile(r"\s+")


def _without_other(label: str) -> str:
    out: list[str] = []
    for c in label:
        if unicodedata.category(c).startswith("C"):
            if not out or out[-1] != "\0":
                out.append("\0")
        else:
            out.append(c)
    return "".join(" " if c == "\0" else c for c in out)


def tab_title(label: str) -> str:
    collapsed = trim(_SPACES.sub(" ", _without_other(label)))
    clean = trim(collapsed[:_TITLE_MAX])
    return clean or "Agent"


def check_input_line(text: str) -> None:
    if utf16_len(text) > MAX_INPUT_CHARS or CONTROL.search(text):
        raise ValueError(f"input must be one line of at most {MAX_INPUT_CHARS} characters with no control characters")


def sleep_ms(ms: float) -> None:
    time.sleep(ms / 1000)

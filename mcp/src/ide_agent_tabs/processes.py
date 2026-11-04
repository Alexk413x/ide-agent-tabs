from __future__ import annotations

import io
import os
import sys
import threading
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    import subprocess

CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
CREATE_NO_WINDOW = 0x08000000
DETACHED_FLAGS = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB
RUN_TIMEOUT_S = 30.0

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
    "IDE_AGENT_TABS_CWD",
    "CODEX_SANDBOX",
    "CODEX_SANDBOX_NETWORK_DISABLED",
    "OPENCODE_SESSION_ID",
    "ANTIGRAVITY_CLI_ALIAS",
}


# A terminal the server starts, or a tmux server it starts, keeps this environment for every later tab, so
# variables that identify the calling agent session are dropped.
def terminal_environment(env: Mapping[str, str]) -> dict[str, str]:
    return {name: value for name, value in env.items() if name.upper() not in _SESSION_ENV}


class RunResult(NamedTuple):
    code: int | None
    stdout: str
    stderr: str


def platform_name() -> str:
    return "linux" if sys.platform.startswith("linux") else sys.platform


def child_flags() -> int:
    return CREATE_NO_WINDOW if sys.platform == "win32" else 0


def detached_flags() -> int:
    return DETACHED_FLAGS if sys.platform == "win32" else 0


def _popen(command: str | Sequence[str], env: Mapping[str, str] | None, flags: int, cwd: str | None) -> subprocess.Popen[bytes]:
    import subprocess

    return subprocess.Popen(
        command if isinstance(command, str) else list(command),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=dict(env) if env is not None else None,
        cwd=cwd,
        creationflags=flags,
        start_new_session=sys.platform != "win32",
    )


def spawn_detached(command: str | Sequence[str], env: Mapping[str, str] | None, cwd: str | None = None) -> subprocess.Popen[bytes]:
    try:
        return _popen(command, env, detached_flags(), cwd)
    except PermissionError:
        if sys.platform != "win32":
            raise
    # A job object that forbids breakaway refuses CREATE_BREAKAWAY_FROM_JOB with access denied.
    return _popen(command, env, detached_flags() & ~CREATE_BREAKAWAY_FROM_JOB, cwd)


def run(
    command: str,
    args: Sequence[str] = (),
    *,
    input: str = "",
    timeout: float = RUN_TIMEOUT_S,
    env: Mapping[str, str] | None = None,
    cwd: str | None = None,
) -> RunResult:
    import subprocess

    try:
        done = subprocess.run(
            [command, *args],
            input=input,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=dict(env) if env is not None else None,
            cwd=cwd,
            creationflags=child_flags(),
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise TimeoutError(f"{command} did not finish within {timeout:g} s") from e
    return RunResult(done.returncode, done.stdout or "", done.stderr or "")


def read_stdin(wait_s: float) -> bytes:
    chunks: list[bytes] = []

    def pump() -> None:
        try:
            while True:
                chunk = os.read(0, 65536)
                if not chunk:
                    return
                chunks.append(chunk)
        except OSError:
            return

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    reader.join(wait_s)
    return b"".join(chunks)


def utf8_stdio() -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8", errors="replace")

from __future__ import annotations

import io
import sys
from collections.abc import Mapping, Sequence
from typing import NamedTuple

CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
CREATE_NO_WINDOW = 0x08000000
DETACHED_FLAGS = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB
RUN_TIMEOUT_S = 30.0


class RunResult(NamedTuple):
    code: int | None
    stdout: str
    stderr: str


def child_flags() -> int:
    return CREATE_NO_WINDOW if sys.platform == "win32" else 0


def detached_flags() -> int:
    return DETACHED_FLAGS if sys.platform == "win32" else 0


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


def utf8_stdio() -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8", errors="replace")

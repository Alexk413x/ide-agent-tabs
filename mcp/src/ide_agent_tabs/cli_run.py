from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Mapping

from .editor_clis import cli_invocation, env_value
from .processes import RunResult, child_flags


def run_cli_result(platform: str, env: Mapping[str, str], cli: str, args: list[str], timeout_s: float, cwd: str | None = None) -> RunResult:
    inv = cli_invocation(cli, args, platform, env_value(env, "ComSpec", "COMSPEC"))
    command: str | list[str] = [inv.command, *inv.args]
    if inv.windows_verbatim_arguments:
        head = f'"{inv.command}"' if " " in inv.command else inv.command
        command = " ".join([head, *inv.args])
    try:
        done = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            env=dict(env),
            cwd=cwd,
            creationflags=child_flags(),
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise TimeoutError(f"{os.path.basename(cli)} {args[0] if args else ''} did not finish within {timeout_s:g} s") from e
    return RunResult(done.returncode, done.stdout or "", done.stderr or "")


def cli_failure(cli: str, args: list[str], result: RunResult) -> RuntimeError:
    output = result.stderr.strip() or result.stdout.strip()
    detail = " ".join(re.split(r"\r?\n", output)[-3:]) if output else ""
    return RuntimeError(f"{os.path.basename(cli)} {args[0]} exited with {result.code}{': ' + detail if detail else ''}")


def run_cli(platform: str, env: Mapping[str, str], cli: str, args: list[str], timeout_s: float, cwd: str | None = None) -> str:
    result = run_cli_result(platform, env, cli, args, timeout_s, cwd)
    if result.code != 0:
        raise cli_failure(cli, args, result)
    return result.stdout

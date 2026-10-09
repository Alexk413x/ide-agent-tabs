from __future__ import annotations

import re
from typing import NamedTuple

from .jsjson import entries, stringify, well_formed
from .profiles import AgentLaunch

SPEC_VERSION = 1
POSIX_SPEC_MAGIC = "ide-agent-tabs-spec-1"


class LaunchSpec(NamedTuple):
    id: str
    agent: str
    cwd: str
    command: str
    args: list[str]
    env: dict[str, str]
    prompt: str | None = None
    pid_file: str | None = None

    def with_pid_file(self, pid_file: str) -> LaunchSpec:
        return self._replace(pid_file=pid_file)


def launch_spec(spec_id: str, cwd: str, launch: AgentLaunch, pid_file: str | None = None) -> LaunchSpec:
    return LaunchSpec(spec_id, launch.agent, cwd, launch.command, list(launch.args), dict(launch.env), launch.prompt, pid_file)


# env is a list of pairs, not an object: Windows PowerShell's ConvertFrom-Json fails on keys that differ
# only in case, which a caller's env may hold.
def power_shell_spec(spec: LaunchSpec) -> str:
    return stringify(
        {
            "version": SPEC_VERSION,
            "id": spec.id,
            "agent": spec.agent,
            "cwd": spec.cwd,
            "command": spec.command,
            "args": spec.args,
            "prompt": spec.prompt,
            "env": [{"name": name, "value": value} for name, value in entries(spec.env)],
            "pidFile": spec.pid_file,
        }
    )


# The pid file is an optional last field; the launchers treat any field after the prompt as the pid file.
def posix_spec(spec: LaunchSpec) -> bytes:
    env = entries(spec.env)
    fields = [
        POSIX_SPEC_MAGIC,
        spec.id,
        spec.agent,
        spec.cwd,
        spec.command,
        str(len(env)),
        *(part for pair in env for part in pair),
        str(len(spec.args)),
        *spec.args,
        *(["1", spec.prompt] if spec.prompt is not None else ["0"]),
        *([spec.pid_file] if spec.pid_file is not None else []),
    ]
    if any("\0" in f for f in fields):
        raise ValueError("launch spec fields must not hold a NUL")
    return well_formed("".join(f"{f}\0" for f in fields)).encode("utf-8")


def parse_posix_spec(data: bytes) -> LaunchSpec:
    fields = data.decode("utf-8", "replace").split("\0")
    fields.pop()
    i = 0

    def take() -> str:
        nonlocal i
        if i >= len(fields):
            raise ValueError("truncated launch spec")
        i += 1
        return fields[i - 1]

    if take() != POSIX_SPEC_MAGIC:
        raise ValueError("not a launch spec")
    spec_id, agent, cwd, command = take(), take(), take(), take()
    env: dict[str, str] = {}
    for _ in range(int(take() or 0)):
        name = take()
        env[name] = take()
    args = [take() for _ in range(int(take() or 0))]
    prompt = take() if take() == "1" else None
    pid_file = take() if i < len(fields) else None
    return LaunchSpec(spec_id, agent, cwd, command, args, env, prompt, pid_file)


_POSIX_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def check_posix_env_names(env: dict[str, str]) -> None:
    bad = [n for n, _ in entries(env) if not _POSIX_ENV_NAME.fullmatch(n)]
    if bad:
        raise ValueError(f"env names must be shell identifiers for a terminal tab: {', '.join(bad)}")

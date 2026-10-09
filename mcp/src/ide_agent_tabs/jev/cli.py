from __future__ import annotations

import os
import sys
from collections.abc import Mapping, Sequence
from typing import Any, Callable, NamedTuple

from ..home import agent_tabs_home
from ..jsjson import parse, stringify, trim
from .client import JevError
from .inputs import parse_input
from .service import Jev, Profiles, start_jev
from .tools import RUNNERS, TOOL_NAMES, takes_input

SUBCOMMANDS = tuple(name[len("jev_") :] for name in TOOL_NAMES)
USAGE = f"jev <{'|'.join(SUBCOMMANDS)}>"


class CliIo(NamedTuple):
    read_stdin: Callable[[], str]
    stdout: Callable[[str], None]
    stderr: Callable[[str], None]


def _read_stdin() -> str:
    return sys.stdin.buffer.read().decode("utf-8", "replace")


def _write(stream: Any) -> Callable[[str], None]:
    def write(text: str) -> None:
        stream.write(text)
        stream.flush()

    return write


def process_io() -> CliIo:
    return CliIo(_read_stdin, _write(sys.stdout), _write(sys.stderr))


def _parse_request(text: str) -> Any:
    if trim(text) == "":
        return {}
    try:
        return parse(text)
    except ValueError as e:
        raise ValueError(f"stdin is not JSON: {e}") from None


def run_jev_cli(args: Sequence[str], jev: Jev | None, off_message: str, io: CliIo) -> int:
    try:
        tool = f"jev_{args[0]}" if len(args) == 1 else ""
        if tool not in RUNNERS:
            raise ValueError(f"Usage: agent-tabs {USAGE}, with one JSON request on stdin.")
        if jev is None:
            raise ValueError(off_message)
        data = parse_input(tool, _parse_request(io.read_stdin()) if takes_input(tool) else {})
        io.stdout(stringify(RUNNERS[tool](jev, data), 2) + "\n")
        return 0
    except Exception as e:  # noqa: BLE001
        io.stderr(stringify({"error": e.message if isinstance(e, JevError) else str(e)}) + "\n")
        return 1


def main(args: Sequence[str], env: Mapping[str, str] | None = None, io: CliIo | None = None, profiles: Profiles | None = None) -> int:
    env = os.environ if env is None else env
    home = agent_tabs_home(env)
    started = start_jev(home, env, sys.platform, profiles)
    return run_jev_cli(args, started.jev, started.off, io or process_io())

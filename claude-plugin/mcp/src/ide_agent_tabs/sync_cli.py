from __future__ import annotations

import os
import sys
from collections.abc import Mapping, Sequence
from typing import Callable

from .home import agent_tabs_home
from .ide_installs import node_platform
from .jsjson import stringify
from .parallel import Task
from .register import RegisterContext, agents_report, register_agents, unregister_agents
from .server_copy import Source, base_interpreter, plugin_source
from .sync import append_log, sync_hook, sync_install, sync_status

USAGE = (
    "sync-ides --hook | --status | --install [--jetbrains] [<editor cli>...] | --agents | --register <agent>... | --unregister <agent>..."
)


def _stdout(text: str) -> None:
    sys.stdout.buffer.write(text.encode("utf-8", "surrogatepass"))
    sys.stdout.buffer.flush()


def _stderr(text: str) -> None:
    sys.stderr.buffer.write(text.encode("utf-8", "surrogatepass"))
    sys.stderr.buffer.flush()


def bundle_dir(source: Source) -> str:
    return os.path.join(os.path.dirname(source.mcp_dir), "dist", "ide")


def system_context(env: Mapping[str, str] | None = None) -> RegisterContext:
    env = os.environ if env is None else env
    return RegisterContext(plugin_source(), agent_tabs_home(env), node_platform(), env, os.path.expanduser("~"), base_interpreter())


def _refresh_detection(ctx: RegisterContext) -> None:
    from .detection import refresh_detection_file
    from .terminals import TERMINAL_DRIVERS

    try:
        refresh_detection_file(ctx.home, ctx.platform, ctx.env, TERMINAL_DRIVERS)
    except Exception as e:  # noqa: BLE001
        try:
            append_log(ctx.home, f"detection: {e}")
        except OSError:
            pass


def run(
    args: Sequence[str], ctx: RegisterContext, bundle: str, out: Callable[[str], None] = _stdout, err: Callable[[str], None] = _stderr
) -> int:
    mode = args[0] if args else ""
    rest = list(args[1:])
    if mode == "--hook":
        detection = Task(lambda: _refresh_detection(ctx), "detection")
        try:
            message = sync_hook(ctx, bundle)
            if message:
                out(
                    stringify(
                        {"systemMessage": message, "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": message}}
                    )
                    + "\n"
                )
        except Exception as e:  # noqa: BLE001
            try:
                append_log(ctx.home, f"hook: {e}")
            except OSError:
                pass
        detection.thread.join()
        return 0
    if mode == "--status":
        out(stringify(sync_status(ctx, bundle), 2) + "\n")
        return 0
    if mode == "--install":
        report = sync_install(ctx, bundle, [a for a in rest if a != "--jetbrains"], "--jetbrains" in rest)
        out(stringify(report, 2) + "\n")
        return 1 if report["errors"] else 0
    if mode == "--agents":
        out(stringify(agents_report(ctx), 2) + "\n")
        return 0
    if mode in ("--register", "--unregister") and rest:
        report = register_agents(ctx, rest) if mode == "--register" else unregister_agents(ctx, rest)
        out(stringify(report, 2) + "\n")
        return 1 if report["errors"] else 0
    err(f"Usage: agent-tabs {USAGE}\n")
    return 2


def main(args: Sequence[str]) -> int:
    ctx = system_context()
    try:
        return run(args, ctx, bundle_dir(ctx.source))
    except Exception as e:  # noqa: BLE001
        _stderr(f"{e}\n")
        return 0 if args[:1] == ["--hook"] else 1

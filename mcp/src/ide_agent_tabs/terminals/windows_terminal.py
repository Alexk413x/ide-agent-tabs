from __future__ import annotations

import contextlib
import os
import re
import sys

from ..clock import now_ms
from ..files import write_new_private_file
from ..installed import exists, find_on_path
from ..jsjson import is_safe_integer
from ..processes import run
from ..spec import LaunchSpec, power_shell_spec
from .driver import OpenOptions, TerminalContext, TerminalDriver, TerminalTab, caps
from .powershell import default_power_shell
from .processes import STARTUP_GRACE_MS, read_pid, start_detached, terminal_environment
from .shell import LAUNCHER_PS1, check_argv_paths, tab_title
from .window_memory import DEDICATED_NAME

WINDOWS_TERMINAL = "windows-terminal"
WT_SETTLE_MS = 10_000
WT_LAST_WINDOW = "0"
_SHELL_IMAGES = {"pwsh.exe", "powershell.exe"}
_CELL = re.compile(r'"([^"]*)"')
_LINES = re.compile(r"\r?\n")


def find_windows_terminal(path_var: str, local_app_data: str | None) -> str | None:
    on_path = find_on_path(path_var, "wt.exe")
    if on_path:
        return on_path
    if not local_app_data:
        return None
    alias = os.path.join(local_app_data, "Microsoft", "WindowsApps", "wt.exe")
    return alias if exists(alias) else None


# wt reads ';' in its command line as a subcommand separator, so no argument may hold one.
def wt_title(label: str) -> str:
    return tab_title(label.replace(";", " "))


def power_shell_argv(shell: str, launcher: str, spec: str) -> list[str]:
    return [shell, "-NoLogo", "-NoExit", "-ExecutionPolicy", "Bypass", "-File", launcher, spec]


def wt_window(options: OpenOptions | None) -> str:
    if options is not None and options.near is not None:
        return options.near.get("window") or WT_LAST_WINDOW
    return DEDICATED_NAME if options is not None and options.window == "dedicated" else WT_LAST_WINDOW


def wt_args(title: str, shell: str, launcher: str, spec: str, window: str | None = None) -> list[str]:
    check_argv_paths("Windows Terminal", [shell, launcher, spec], ";")
    return [
        "-w",
        window if window is not None else WT_LAST_WINDOW,
        "new-tab",
        "--title",
        wt_title(title),
        *power_shell_argv(shell, launcher, spec),
    ]


def parse_tasklist(csv: str) -> dict[int, str]:
    images: dict[int, str] = {}
    for line in _LINES.split(csv):
        cells = _CELL.findall(line)
        if len(cells) < 2:
            continue
        text = cells[1].strip()
        if text.isdigit() and text.isascii() and is_safe_integer(int(text)):
            images[int(text)] = cells[0].lower()
    return images


def _shell_images() -> dict[int, str]:
    return parse_tasklist(run("tasklist.exe", ["/FO", "CSV", "/NH"], timeout=15).stdout)


class WindowsTerminal(TerminalDriver):
    name = WINDOWS_TERMINAL
    label = "Windows Terminal"
    capabilities = caps("tab", "tracked", "best-effort")

    def available(self, ctx: TerminalContext) -> bool:
        return sys.platform == "win32" and find_windows_terminal(ctx.path_var, ctx.env.get("LOCALAPPDATA")) is not None

    def open(self, ctx: TerminalContext, spec: LaunchSpec, title: str, options: OpenOptions | None = None) -> TerminalTab:
        wt = find_windows_terminal(ctx.path_var, ctx.env.get("LOCALAPPDATA"))
        if not wt:
            raise RuntimeError("Windows Terminal (wt.exe) was not found")
        folder = os.path.join(ctx.home, "launch")
        spec_file = os.path.join(folder, f"{spec.id}.json")
        pid_file = os.path.join(folder, f"{spec.id}.pid")
        window = wt_window(options)
        shell = ctx.power_shell or default_power_shell(ctx.env)
        args = wt_args(title, shell, os.path.join(ctx.scripts_dir, LAUNCHER_PS1), spec_file, window)
        write_new_private_file(spec_file, power_shell_spec(spec.with_pid_file(pid_file)))
        try:
            start_detached(wt, args, terminal_environment(ctx.env), WT_SETTLE_MS)
        except BaseException:
            with contextlib.suppress(OSError):
                os.remove(spec_file)
            raise
        tab: TerminalTab = {
            "id": spec.id,
            "terminal": WINDOWS_TERMINAL,
            "agent": spec.agent,
            "path": spec.cwd,
            "createdAt": now_ms(),
            "pidFile": pid_file,
        }
        if window != WT_LAST_WINDOW:
            tab["window"] = window
        return tab

    def alive(self, ctx: TerminalContext, tabs: list[TerminalTab]) -> set[str]:
        images = _shell_images()
        alive: set[str] = set()
        for tab in tabs:
            pid = read_pid(tab.get("pidFile"))
            if images.get(pid, "") in _SHELL_IMAGES if pid is not None else now_ms() - tab["createdAt"] < STARTUP_GRACE_MS:
                alive.add(tab["id"])
        return alive

    def close(self, ctx: TerminalContext, tab: TerminalTab) -> None:
        pid = read_pid(tab.get("pidFile"))
        if pid is None:
            raise RuntimeError(f"tab {tab['id']} has no running shell yet, or its shell has ended")
        if _shell_images().get(pid, "") in _SHELL_IMAGES:
            result = run("taskkill.exe", ["/PID", str(pid), "/T", "/F"], timeout=15)
            if result.code != 0:
                raise RuntimeError(f"taskkill failed: {result.stderr.strip() or result.stdout.strip()}")
        if tab.get("pidFile"):
            with contextlib.suppress(FileNotFoundError):
                os.remove(tab["pidFile"])


windows_terminal = WindowsTerminal()

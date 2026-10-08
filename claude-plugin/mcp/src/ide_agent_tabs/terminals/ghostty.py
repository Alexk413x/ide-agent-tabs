from __future__ import annotations

import contextlib
import os
import posixpath
import re
import sys
from typing import NamedTuple, Union

from ..clock import now_ms
from ..files import write_new_private_file
from ..jsjson import CONTROL
from ..processes import run
from ..spec import LaunchSpec, check_posix_env_names, posix_spec
from .driver import Capabilities, OpenOptions, TerminalContext, TerminalDriver, TerminalTab, caps
from .processes import GUI_SETTLE_MS, find_executable, hang_up, pid_tabs_alive, start_detached, terminal_environment
from .shell import LoginShell, check_argv_paths, check_input_line, launcher_name, login_shell, surface_argv, surface_command
from .window_memory import RememberedWindow, read_window, remember_window

GHOSTTY = "ghostty"
_LINES = re.compile(r"\r?\n")


def ghostty_linux_locations(home: str) -> list[str]:
    return ["/usr/bin/ghostty", "/usr/local/bin/ghostty", posixpath.join(home, ".local", "bin", "ghostty"), "/snap/bin/ghostty"]


def _find_on_linux(ctx: TerminalContext) -> str | None:
    return find_executable(ctx.path_var, "ghostty", ghostty_linux_locations(os.path.expanduser("~")))


def ghostty_capabilities(platform: str) -> Capabilities:
    return caps("window", "tracked", "best-effort") if platform == "linux" else caps("tab", "yes", "yes")


# Ghostty on Linux can't open a tab in a running instance from outside (ghostty#12136), so each agent gets a
# new Ghostty process with one window, tracked through its shell's pid.
def ghostty_linux_args(cwd: str, shell: LoginShell) -> list[str]:
    check_argv_paths("Ghostty", [cwd])
    return [
        "--gtk-single-instance=false",
        f"--working-directory={cwd}",
        "--confirm-close-surface=false",
        "--wait-after-command=false",
        "-e",
        *surface_argv(shell),
    ]


def apple_script_string(value: str) -> str:
    if CONTROL.search(value):
        raise ValueError("AppleScript strings here must not hold control characters")
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


class NearTab(NamedTuple):
    near_tab: str


class Dedicated(NamedTuple):
    dedicated: str | None


GhosttyPlace = Union[NearTab, Dedicated]


def _find_window_lines(place: GhosttyPlace) -> list[str]:
    if isinstance(place, NearTab):
        return [
            "\trepeat with cw in windows",
            "\t\trepeat with ct in tabs of cw",
            f"\t\t\tif (id of ct as text) is {apple_script_string(place.near_tab)} then set w to (contents of cw)",
            "\t\tend repeat",
            "\tend repeat",
            "\tif w is missing value and (count of windows) > 0 then set w to front window",
        ]
    if place.dedicated is None:
        return []
    return [
        "\trepeat with cw in windows",
        f"\t\tif (id of cw as text) is {apple_script_string(place.dedicated)} then set w to (contents of cw)",
        "\tend repeat",
    ]


def open_script(command: str, env: dict[str, str], place: GhosttyPlace | None = None, keep_focus: bool = False) -> str:
    variables = [apple_script_string(f"{k}={v}") for k, v in env.items()]
    head = [
        'tell application "Ghostty"',
        "\tset cfg to new surface configuration",
        f"\tset command of cfg to {apple_script_string(command)}",
        f"\tset environment variables of cfg to {{{', '.join(variables)}}}",
    ]

    def new_tab_in(window: str) -> list[str]:
        if keep_focus:
            return [
                f"\t\tset previousTab to selected tab of {window}",
                f"\t\tset t to new tab in {window} with configuration cfg",
                "\t\tselect tab previousTab",
            ]
        return [f"\t\tset t to new tab in {window} with configuration cfg"]

    if place is None:
        return "\n".join(
            [
                *head,
                "\tif (count of windows) > 0 then",
                *new_tab_in("front window"),
                "\telse",
                "\t\tset w to new window with configuration cfg",
                "\t\tset t to selected tab of w",
                "\tend if",
                "\treturn (id of t as text) & linefeed & (id of (focused terminal of t) as text)",
                "end tell",
                "",
            ]
        )
    return "\n".join(
        [
            *head,
            "\tset w to missing value",
            *_find_window_lines(place),
            "\tif w is not missing value then",
            *new_tab_in("w"),
            "\telse",
            "\t\tset w to new window with configuration cfg",
            "\t\tset t to selected tab of w",
            "\tend if",
            "\treturn (id of t as text) & linefeed & (id of (focused terminal of t) as text) & linefeed & (id of w as text)",
            "end tell",
            "",
        ]
    )


def ghostty_place(options: OpenOptions | None, remembered: RememberedWindow | None) -> GhosttyPlace | None:
    near = options.near if options is not None else None
    if near is not None and near.get("terminal") == GHOSTTY and near.get("terminalTabId"):
        return NearTab(near["terminalTabId"])
    if options is not None and options.window == "dedicated":
        return Dedicated(remembered.get("id") if remembered is not None else None)
    return None


_LIST_SCRIPT = (
    'if application "Ghostty" is not running then return ""\n'
    'set out to ""\n'
    'tell application "Ghostty"\n'
    "\trepeat with term in terminals\n"
    "\t\tset out to out & (id of term as text) & linefeed\n"
    "\tend repeat\n"
    "end tell\n"
    "return out\n"
)


def list_script() -> str:
    return _LIST_SCRIPT


def close_script(terminal_id: str) -> str:
    return "\n".join(
        [
            'if application "Ghostty" is not running then return "missing"',
            'tell application "Ghostty"',
            "\trepeat with term in terminals",
            f"\t\tif (id of term as text) is {apple_script_string(terminal_id)} then",
            "\t\t\tclose (contents of term)",
            '\t\t\treturn "closed"',
            "\t\tend if",
            "\tend repeat",
            "end tell",
            'return "missing"',
            "",
        ]
    )


# input text arrives as a paste when the program turned on bracketed paste, so Enter is a separate key event.
def input_script(terminal_id: str, text: str) -> str:
    check_input_line(text)
    return "\n".join(
        [
            'tell application "Ghostty"',
            f"\tset t to terminal id {apple_script_string(terminal_id)}",
            f"\tinput text {apple_script_string(text)} to t",
            "\tdelay 0.2",
            '\tsend key "enter" to t',
            "end tell",
            "",
        ]
    )


class OpenResult(NamedTuple):
    tab_id: str
    terminal_id: str
    window_id: str | None


def parse_open_result(stdout: str) -> OpenResult:
    parts = _LINES.split(stdout.strip())
    tab_id = parts[0] if parts else ""
    terminal_id = parts[1] if len(parts) > 1 else ""
    window_id = parts[2] if len(parts) > 2 and parts[2] else None
    if not tab_id or not terminal_id:
        raise RuntimeError(f"unexpected answer from Ghostty: {stdout.strip()}")
    return OpenResult(tab_id, terminal_id, window_id)


def osascript(script: str) -> str:
    result = run("/usr/bin/osascript", ["-"], input=script, timeout=30)
    if result.code != 0:
        raise RuntimeError(f"osascript failed: {result.stderr.strip()}")
    return result.stdout


def _ghostty_app(home: str) -> str | None:
    return next((p for p in ("/Applications/Ghostty.app", os.path.join(home, "Applications", "Ghostty.app")) if os.path.exists(p)), None)


def _remove(file: str) -> None:
    with contextlib.suppress(OSError):
        os.remove(file)


class Ghostty(TerminalDriver):
    name = GHOSTTY
    label = "Ghostty"
    capabilities = ghostty_capabilities("linux" if sys.platform.startswith("linux") else sys.platform)
    can_input = True

    def available(self, ctx: TerminalContext) -> bool:
        if sys.platform.startswith("linux"):
            return _find_on_linux(ctx) is not None
        return sys.platform == "darwin" and _ghostty_app(os.path.expanduser("~")) is not None

    def _open_on_linux(self, ctx: TerminalContext, spec: LaunchSpec, shell: LoginShell, spec_file: str, launcher: str) -> TerminalTab:
        exe = _find_on_linux(ctx)
        if not exe:
            raise RuntimeError("ghostty was not found")
        pid_file = os.path.join(os.path.dirname(spec_file), f"{spec.id}.pid")
        args = ghostty_linux_args(spec.cwd, shell)
        env = {**terminal_environment(ctx.env), "IDE_AGENT_TABS_LAUNCHER": launcher, "IDE_AGENT_TABS_SPEC": spec_file}
        write_new_private_file(spec_file, posix_spec(spec.with_pid_file(pid_file)))
        try:
            start_detached(exe, args, env, GUI_SETTLE_MS)
        except BaseException:
            _remove(spec_file)
            raise
        return {"id": spec.id, "terminal": GHOSTTY, "agent": spec.agent, "path": spec.cwd, "createdAt": now_ms(), "pidFile": pid_file}

    def open(self, ctx: TerminalContext, spec: LaunchSpec, title: str, options: OpenOptions | None = None) -> TerminalTab:
        check_posix_env_names(spec.env)
        platform = "linux" if sys.platform.startswith("linux") else sys.platform
        shell = login_shell(ctx.env.get("SHELL"), platform)
        spec_file = os.path.join(ctx.home, "launch", f"{spec.id}.spec")
        launcher = os.path.join(ctx.scripts_dir, launcher_name(shell))
        if platform == "linux":
            return self._open_on_linux(ctx, spec, shell, spec_file, launcher)
        dedicated = options is not None and options.window == "dedicated" and options.near is None
        remembered: RememberedWindow | None = None
        if dedicated:
            try:
                remembered = read_window(ctx.home, GHOSTTY)
            except OSError:
                remembered = None
        script = open_script(
            surface_command(shell),
            {"IDE_AGENT_TABS_LAUNCHER": launcher, "IDE_AGENT_TABS_SPEC": spec_file},
            ghostty_place(options, remembered),
            options is not None and options.focus is False,
        )
        write_new_private_file(spec_file, posix_spec(spec))
        try:
            ids = parse_open_result(osascript(script))
        except BaseException:
            _remove(spec_file)
            raise
        if dedicated and ids.window_id is not None and ids.window_id != (remembered or {}).get("id"):
            with contextlib.suppress(OSError, TimeoutError):
                remember_window(ctx.home, GHOSTTY, {"id": ids.window_id})
        tab: TerminalTab = {
            "id": spec.id,
            "terminal": GHOSTTY,
            "agent": spec.agent,
            "path": spec.cwd,
            "createdAt": now_ms(),
            "terminalTabId": ids.tab_id,
            "terminalId": ids.terminal_id,
        }
        if ids.window_id is not None:
            tab["window"] = ids.window_id
        return tab

    def alive(self, ctx: TerminalContext, tabs: list[TerminalTab]) -> set[str]:
        alive = pid_tabs_alive([t for t in tabs if t.get("pidFile")])
        scripted = [t for t in tabs if t.get("terminalId")]
        if not scripted:
            return alive
        ids = {line for line in _LINES.split(osascript(list_script())) if line != ""}
        alive.update(t["id"] for t in scripted if t["terminalId"] in ids)
        return alive

    def close(self, ctx: TerminalContext, tab: TerminalTab) -> None:
        if tab.get("pidFile"):
            hang_up(tab)
            return
        if not tab.get("terminalId"):
            raise RuntimeError(f"tab {tab['id']} has no Ghostty terminal id")
        if osascript(close_script(tab["terminalId"])).strip() != "closed":
            raise RuntimeError(f"Ghostty has no terminal {tab['terminalId']}; the tab is already closed")

    def input(self, ctx: TerminalContext, tab: TerminalTab, text: str) -> None:
        if tab.get("pidFile") or not tab.get("terminalId"):
            raise RuntimeError("Ghostty on Linux can't take input from outside")
        osascript(input_script(tab["terminalId"], text))


ghostty = Ghostty()

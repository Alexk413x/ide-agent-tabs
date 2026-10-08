from __future__ import annotations

import contextlib
import ntpath
import os
import posixpath
import re
import sys
import time
from collections.abc import Mapping
from typing import Callable, NamedTuple, Union

from ..clock import now_ms
from ..files import write_new_private_file
from ..jsjson import is_number, number, parse
from ..processes import RunResult, pid_alive, run
from ..spec import LaunchSpec, check_posix_env_names, posix_spec, power_shell_spec
from .driver import OpenOptions, TerminalContext, TerminalDriver, TerminalTab, caps
from .powershell import default_power_shell
from .processes import GUI_SETTLE_MS, STARTUP_GRACE_MS, find_executable, start_detached, terminal_environment
from .shell import ENTER_DELAY_MS, argv_mode_command, check_argv_paths, check_input_line, launcher_name, login_shell, sleep_ms
from .window_memory import RememberedWindow, read_window, remember_window
from .windows_terminal import LAUNCHER_PS1, power_shell_argv

WEZTERM = "wezterm"
START_WAIT_MS = 10_000
_GUI_SOCKET = re.compile(r"gui-sock-(\d+)")
_DIGITS = re.compile(r"\d+")
_NO_GUI = re.compile(r"connect|socket|no running|not running", re.IGNORECASE)


def _platform() -> str:
    return "linux" if sys.platform.startswith("linux") else sys.platform


def wezterm_locations(platform: str, home: str, program_files: str | None) -> list[str]:
    if platform == "win32":
        return [ntpath.join(program_files, "WezTerm", "wezterm.exe")] if program_files else []
    if platform == "darwin":
        return [
            "/Applications/WezTerm.app/Contents/MacOS/wezterm",
            posixpath.join(home, "Applications", "WezTerm.app", "Contents", "MacOS", "wezterm"),
        ]
    return [
        "/usr/bin/wezterm",
        "/usr/local/bin/wezterm",
        posixpath.join(home, ".local", "bin", "wezterm"),
        "/home/linuxbrew/.linuxbrew/bin/wezterm",
    ]


def _find_wezterm(ctx: TerminalContext) -> str | None:
    name = "wezterm.exe" if sys.platform == "win32" else "wezterm"
    program_files = ctx.env.get("ProgramFiles")
    if program_files is None:
        program_files = ctx.env.get("PROGRAMFILES")
    return find_executable(ctx.path_var, name, wezterm_locations(_platform(), os.path.expanduser("~"), program_files))


def wezterm_runtime_dir(platform: str, env: Mapping[str, str], home: str) -> str:
    runtime = env.get("XDG_RUNTIME_DIR")
    if platform == "linux" and runtime:
        return posixpath.join(runtime, "wezterm")
    return os.path.join(home, ".local", "share", "wezterm")


# Each WezTerm GUI listens on gui-sock-<pid>, and the files outlive a GUI that was killed, so only sockets
# whose GUI still runs count. Newest first where the file can be stat'ed: on Windows a socket file is a
# reparse point that stat refuses with EACCES.
def find_gui_sockets(folder: str, running: Callable[[int], bool] = pid_alive) -> list[str]:
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    found: list[tuple[str, float]] = []
    for name in names:
        match = _GUI_SOCKET.match(name)
        if match is None or match.end() != len(name):
            continue
        pid = int(match.group(1))
        if pid > 2**53 - 1 or not running(pid):
            continue
        file = os.path.join(folder, name)
        try:
            mtime = os.stat(file).st_mtime_ns / 1e6
        except OSError:
            mtime = 0.0
        found.append((file, mtime))
    return [file for file, _ in sorted(found, key=lambda s: -s[1])]


def _gui_sockets(env: Mapping[str, str]) -> list[str]:
    found = find_gui_sockets(wezterm_runtime_dir(_platform(), env, os.path.expanduser("~")))
    own = env.get("WEZTERM_UNIX_SOCKET")
    return [own, *found] if own and own not in found else found


# `wezterm cli` names the socket to use explicitly: on Windows it drops the folder from the GUI's socket
# and fails to connect (wezterm#4456), and without --no-auto-start it starts a hidden mux server.
def wezterm_cli_args(args: list[str]) -> list[str]:
    return ["cli", "--no-auto-start", *args]


def _cli(exe: str, socket: str, args: list[str], env: Mapping[str, str], timeout_ms: float = 15_000) -> RunResult:
    return run(exe, wezterm_cli_args(args), env={**env, "WEZTERM_UNIX_SOCKET": socket}, timeout=timeout_ms / 1000)


class PanePlace(NamedTuple):
    pane_id: str


class WindowPlace(NamedTuple):
    window_id: str


class NewWindow(NamedTuple):
    new_window: bool = True


WeztermPlace = Union[PanePlace, WindowPlace, NewWindow]


def _place_args(place: WeztermPlace | None) -> list[str]:
    if place is None:
        return []
    if isinstance(place, NewWindow):
        return ["--new-window"]
    flag, ident = ("--pane-id", place.pane_id) if isinstance(place, PanePlace) else ("--window-id", place.window_id)
    if not _DIGITS.fullmatch(ident):
        raise ValueError(f"not a WezTerm id: {ident}")
    return [flag, ident]


def wezterm_spawn_args(cwd: str, argv: list[str], place: WeztermPlace | None = None) -> list[str]:
    check_argv_paths("WezTerm", [cwd])
    return ["spawn", *_place_args(place), "--cwd", cwd, "--", *argv]


def wezterm_start_args(cwd: str, argv: list[str]) -> list[str]:
    check_argv_paths("WezTerm", [cwd])
    return ["start", "--cwd", cwd, "--", *argv]


# --no-paste sends the bytes as typed input. As a bracketed paste, the CR would not submit the line.
def wezterm_input_args(pane_id: str, text: str) -> tuple[list[str], list[str]]:
    check_input_line(text)
    return (
        ["send-text", "--pane-id", pane_id, "--no-paste", "--", text],
        ["send-text", "--pane-id", pane_id, "--no-paste", "--", "\r"],
    )


def parse_pane_id(stdout: str) -> str:
    found = stdout.strip()
    if not _DIGITS.fullmatch(found):
        raise RuntimeError(f"unexpected answer from wezterm cli spawn: {found}")
    return found


def parse_wezterm_pane_windows(stdout: str) -> dict[str, str]:
    panes = parse(stdout)
    if not isinstance(panes, list):
        raise TypeError("unexpected answer from wezterm cli list")
    windows: dict[str, str] = {}
    for p in panes:
        record = p if isinstance(p, dict) else {}
        if is_number(record.get("pane_id")):
            window = record.get("window_id")
            windows[number(record["pane_id"])] = number(window) if isinstance(window, (int, float)) and is_number(window) else ""
    return windows


def parse_wezterm_panes(stdout: str) -> list[str]:
    return list(parse_wezterm_pane_windows(stdout))


def is_no_gui_error(stderr: str) -> bool:
    return _NO_GUI.search(stderr) is not None


def _list_pane_windows(exe: str, socket: str, env: Mapping[str, str]) -> dict[str, str] | None:
    result = _cli(exe, socket, ["list", "--format", "json"], env)
    if result.code == 0:
        return parse_wezterm_pane_windows(result.stdout)
    if is_no_gui_error(result.stderr):
        return None
    raise RuntimeError(f"wezterm cli list failed: {result.stderr.strip()}")


def _list_panes(exe: str, socket: str, env: Mapping[str, str]) -> set[str] | None:
    panes = _list_pane_windows(exe, socket, env)
    return set(panes) if panes is not None else None


def _quiet_list(exe: str, socket: str, env: Mapping[str, str]) -> dict[str, str] | None:
    try:
        return _list_pane_windows(exe, socket, env)
    except (OSError, TimeoutError, ValueError, TypeError, RuntimeError):
        return None


class Pane(NamedTuple):
    socket: str
    pane_id: str | None = None


# A GUI the server starts holds only the agent's pane, so its pane id is known once its socket answers.
def _start_gui(exe: str, args: list[str], env: Mapping[str, str]) -> Pane | None:
    before = set(_gui_sockets(env))
    start_detached(exe, args, env, GUI_SETTLE_MS)
    deadline = now_ms() + START_WAIT_MS
    while now_ms() < deadline:
        socket = next((s for s in _gui_sockets(env) if s not in before), None)
        panes = _quiet_list(exe, socket, env) if socket else None
        if socket and panes:
            return Pane(socket, next(iter(panes))) if len(panes) == 1 else Pane(socket)
        time.sleep(0.25)
    return None


def _pane_argv(ctx: TerminalContext, spec: LaunchSpec, spec_file: str) -> list[str]:
    if sys.platform == "win32":
        argv = power_shell_argv(ctx.power_shell or default_power_shell(ctx.env), os.path.join(ctx.scripts_dir, LAUNCHER_PS1), spec_file)
        check_argv_paths("WezTerm", argv)
        return argv
    check_posix_env_names(spec.env)
    shell = login_shell(ctx.env.get("SHELL"), _platform())
    return argv_mode_command(shell, os.path.join(ctx.scripts_dir, launcher_name(shell)), spec_file)


def _spawn_on(exe: str, socket: str, args: list[str], env: Mapping[str, str]) -> Pane | None:
    result = _cli(exe, socket, args, env, 30_000)
    if result.code == 0:
        return Pane(socket, parse_pane_id(result.stdout))
    if not is_no_gui_error(result.stderr):
        raise RuntimeError(f"wezterm cli spawn failed: {result.stderr.strip()}")
    return None


def _spawn_in_gui(exe: str, args: list[str], env: Mapping[str, str]) -> Pane | None:
    for socket in _gui_sockets(env):
        pane = _spawn_on(exe, socket, args, env)
        if pane is not None:
            return pane
    return None


class WeztermTarget(NamedTuple):
    socket: str | None
    place: WeztermPlace | None
    remember: bool


def plan_wezterm_targets(
    options: OpenOptions | None,
    near_panes: dict[str, str] | None,
    remembered: RememberedWindow | None,
    remembered_panes: dict[str, str] | None,
) -> list[WeztermTarget]:
    targets: list[WeztermTarget] = []
    near = options.near if options is not None else None
    if near is not None and near.get("socket") and near.get("terminalId") and near_panes is not None and near["terminalId"] in near_panes:
        targets.append(WeztermTarget(near["socket"], PanePlace(near["terminalId"]), False))
    if options is None or options.window != "dedicated":
        return [*targets, WeztermTarget(None, None, False)]
    if remembered is not None and remembered.get("socket") and remembered["id"] in (remembered_panes or {}).values():
        targets.append(WeztermTarget(remembered["socket"], WindowPlace(remembered["id"]), False))
    return [*targets, WeztermTarget(None, NewWindow(), True)]


def _wezterm_targets(exe: str, home: str, env: Mapping[str, str], options: OpenOptions | None) -> list[WeztermTarget]:
    near_socket = options.near.get("socket") if options is not None and options.near is not None else None
    near = _quiet_list(exe, near_socket, env) if near_socket else None
    remembered = read_window(home, WEZTERM) if options is not None and options.window == "dedicated" else None
    panes = _quiet_list(exe, remembered["socket"], env) if remembered is not None and remembered.get("socket") else None
    return plan_wezterm_targets(options, near, remembered, panes)


def _remember_pane_window(exe: str, home: str, env: Mapping[str, str], pane: Pane) -> None:
    panes = _list_pane_windows(exe, pane.socket, env)
    if pane.pane_id is not None:
        window_id = panes.get(pane.pane_id) if panes is not None else None
    else:
        window_id = next(iter(panes.values())) if panes else None
    if window_id:
        remember_window(home, WEZTERM, {"id": window_id, "socket": pane.socket})


def _wezterm_path(ctx: TerminalContext) -> str:
    exe = _find_wezterm(ctx)
    if not exe:
        raise RuntimeError("wezterm was not found")
    return exe


def _remove(file: str) -> None:
    with contextlib.suppress(OSError):
        os.remove(file)


class Wezterm(TerminalDriver):
    name = WEZTERM
    label = "WezTerm"
    capabilities = caps("tab", "yes", "yes")
    can_input = True

    def available(self, ctx: TerminalContext) -> bool:
        return _find_wezterm(ctx) is not None

    def open(self, ctx: TerminalContext, spec: LaunchSpec, title: str, options: OpenOptions | None = None) -> TerminalTab:
        exe = _wezterm_path(ctx)
        windows = sys.platform == "win32"
        spec_file = os.path.join(ctx.home, "launch", f"{spec.id}{'.json' if windows else '.spec'}")
        argv = _pane_argv(ctx, spec, spec_file)
        env = terminal_environment(ctx.env)
        targets = [(t, wezterm_spawn_args(spec.cwd, argv, t.place)) for t in _wezterm_targets(exe, ctx.home, env, options)]
        start_args = wezterm_start_args(spec.cwd, argv)
        write_new_private_file(spec_file, power_shell_spec(spec) if windows else posix_spec(spec))
        pane: Pane | None = None
        used: WeztermTarget | None = None
        try:
            for target, args in targets:
                used = target
                if target.socket:
                    pane = _spawn_on(exe, target.socket, args, env)
                else:
                    pane = _spawn_in_gui(exe, args, env) or _start_gui(exe, start_args, env)
                if pane is not None:
                    break
        except BaseException:
            _remove(spec_file)
            raise
        if used is not None and used.remember and pane is not None:
            with contextlib.suppress(OSError, TimeoutError, ValueError, TypeError, RuntimeError):
                _remember_pane_window(exe, ctx.home, env, pane)
        tab: TerminalTab = {"id": spec.id, "terminal": WEZTERM, "agent": spec.agent, "path": spec.cwd, "createdAt": now_ms()}
        if pane is not None and pane.pane_id is not None:
            tab["terminalId"] = pane.pane_id
            tab["socket"] = pane.socket
        return tab

    def alive(self, ctx: TerminalContext, tabs: list[TerminalTab]) -> set[str]:
        now = now_ms()
        alive = {t["id"] for t in tabs if t.get("terminalId") is None and now - t["createdAt"] < STARTUP_GRACE_MS}
        tracked = [t for t in tabs if t.get("terminalId") is not None and t.get("socket")]
        if not tracked:
            return alive
        exe = _wezterm_path(ctx)
        env = terminal_environment(ctx.env)
        for socket in dict.fromkeys(t["socket"] for t in tracked):
            panes = _list_panes(exe, socket, env)
            alive.update(t["id"] for t in tracked if t["socket"] == socket and panes is not None and t["terminalId"] in panes)
        return alive

    def close(self, ctx: TerminalContext, tab: TerminalTab) -> None:
        pane_id = tab.get("terminalId")
        if pane_id is None or not _DIGITS.fullmatch(pane_id) or not tab.get("socket"):
            raise RuntimeError(f"tab {tab['id']} has no WezTerm pane id; close it in WezTerm")
        exe = _wezterm_path(ctx)
        env = terminal_environment(ctx.env)
        panes = _list_panes(exe, tab["socket"], env)
        if panes is None or pane_id not in panes:
            raise RuntimeError(f"WezTerm has no pane {pane_id}; the tab is already closed")
        result = _cli(exe, tab["socket"], ["kill-pane", "--pane-id", pane_id], env)
        if result.code != 0:
            raise RuntimeError(f"wezterm cli kill-pane failed: {result.stderr.strip()}")

    def input(self, ctx: TerminalContext, tab: TerminalTab, text: str) -> None:
        pane_id = tab.get("terminalId")
        if pane_id is None or not _DIGITS.fullmatch(pane_id) or not tab.get("socket"):
            raise RuntimeError(f"tab {tab['id']} has no WezTerm pane id")
        typed, enter = wezterm_input_args(pane_id, text)
        exe = _wezterm_path(ctx)
        env = terminal_environment(ctx.env)
        panes = _list_panes(exe, tab["socket"], env)
        if panes is None or pane_id not in panes:
            raise RuntimeError(f"WezTerm has no pane {pane_id}")
        for args in (typed, enter):
            if args is enter:
                sleep_ms(ENTER_DELAY_MS)
            result = _cli(exe, tab["socket"], args, env)
            if result.code != 0:
                raise RuntimeError(f"wezterm cli send-text failed: {result.stderr.strip()}")


wezterm = Wezterm()

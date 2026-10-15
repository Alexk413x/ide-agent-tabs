from __future__ import annotations

import contextlib
import os
import posixpath
import re
import sys
from collections.abc import Mapping
from typing import NamedTuple, Union

from ..clock import now_ms
from ..files import write_new_private_file
from ..installed import find_on_path
from ..jsjson import is_number, number, parse
from ..processes import run
from ..spec import LaunchSpec, check_posix_env_names, posix_spec
from .driver import Capabilities, OpenOptions, TerminalContext, TerminalDriver, TerminalTab, caps
from .processes import GUI_SETTLE_MS, find_executable, hang_up, pid_tabs_alive, start_detached, terminal_environment
from .shell import ENTER_DELAY_MS, check_argv_paths, check_input_line, launcher_name, login_shell, sleep_ms, surface_argv, tab_title
from .window_memory import RememberedWindow, read_window, remember_window

KITTY = "kitty"
KITTY_SOCKET_NAME = "kitty-agent-tabs"
_REMOTE = caps("tab", "yes", "yes")
_SPAWNED = caps("window", "tracked", "best-effort")
_DIGITS = re.compile(r"\d+")
_SOCKET = re.compile(rf"{re.escape(KITTY_SOCKET_NAME)}-\d+")


def _platform() -> str:
    return "linux" if sys.platform.startswith("linux") else sys.platform


def kitty_locations(home: str) -> list[str]:
    return [
        "/Applications/kitty.app/Contents/MacOS/kitty",
        posixpath.join(home, "Applications", "kitty.app", "Contents", "MacOS", "kitty"),
        posixpath.join(home, ".local", "kitty.app", "bin", "kitty"),
    ]


def _find_kitty(ctx: TerminalContext) -> str | None:
    return find_executable(ctx.path_var, "kitty", kitty_locations(os.path.expanduser("~")))


def _find_kitten(kitty: str, path_var: str) -> str:
    try:
        sibling = os.path.join(os.path.dirname(os.path.realpath(kitty)), "kitten")
        os.stat(sibling)
        return sibling
    except OSError:
        return find_on_path(path_var, "kitten") or os.path.join(os.path.dirname(kitty), "kitten")


def kitty_socket_dir(platform: str, env: Mapping[str, str]) -> str | None:
    if platform == "linux":
        return env.get("XDG_RUNTIME_DIR") or None
    if platform == "darwin":
        if env.get("TMPDIR"):
            return env["TMPDIR"]
        import tempfile

        return tempfile.gettempdir()
    return None


# kitty appends -<pid> to the listen_on path, so each running kitty has its own socket.
def find_kitty_sockets(folder: str) -> list[str]:
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    found: list[tuple[str, float]] = []
    for n in names:
        if not _SOCKET.fullmatch(n):
            continue
        file = os.path.join(folder, n)
        try:
            found.append((file, os.stat(file).st_mtime_ns / 1e6))
        except OSError:
            continue
    return [f"unix:{file}" for file, _ in sorted(found, key=lambda s: -s[1])]


def kitty_addresses(platform: str, env: Mapping[str, str]) -> list[str]:
    own = env.get("KITTY_LISTEN_ON")
    if own and own.startswith("unix:"):
        return [own]
    folder = kitty_socket_dir(platform, env)
    return find_kitty_sockets(folder) if folder else []


class InWindow(NamedTuple):
    window_id: str


class OsWindow(NamedTuple):
    os_window: bool = True


KittyPlace = Union[InWindow, OsWindow]


def _place_args(place: KittyPlace | None) -> list[str]:
    if place is None:
        return ["--type=tab"]
    if isinstance(place, OsWindow):
        return ["--type=os-window"]
    return ["--type=tab", "--match", f"window_id:{place.window_id}"]


def kitty_launch_args(
    address: str,
    cwd: str,
    title: str,
    launcher: str,
    spec: str,
    argv: list[str],
    place: KittyPlace | None = None,
    focus: bool | None = None,
) -> list[str]:
    check_argv_paths("kitty", [address, cwd, launcher, spec])
    if isinstance(place, InWindow) and not _DIGITS.fullmatch(place.window_id):
        raise ValueError(f"not a kitty window id: {place.window_id}")
    return [
        "@",
        "--to",
        address,
        "launch",
        *_place_args(place),
        *(["--keep-focus"] if focus is False else []),
        "--cwd",
        cwd,
        "--env",
        f"IDE_AGENT_TABS_LAUNCHER={launcher}",
        "--env",
        f"IDE_AGENT_TABS_SPEC={spec}",
        "--tab-title",
        tab_title(title),
        "--",
        *argv,
    ]


def kitty_spawn_args(cwd: str, argv: list[str]) -> list[str]:
    check_argv_paths("kitty", [cwd])
    return ["--directory", cwd, *argv]


class InputCall(NamedTuple):
    args: list[str]
    input: str


# kitty reads escapes such as \r in a send-text argument, so the line and the Enter both go through stdin,
# which it sends unchanged.
def kitty_input_calls(address: str, window_id: str, text: str) -> list[InputCall]:
    check_input_line(text)
    args = ["@", "--to", address, "send-text", "--match", f"id:{window_id}", "--stdin"]
    return [InputCall(args, text), InputCall(args, "\r")]


def parse_kitty_window_id(stdout: str) -> str:
    found = stdout.strip()
    if not _DIGITS.fullmatch(found):
        raise RuntimeError(f"unexpected answer from kitten @ launch: {found}")
    return found


def parse_kitty_os_windows(stdout: str) -> dict[str, list[str]]:
    by_os_window: dict[str, list[str]] = {}
    os_windows = parse(stdout)
    if not isinstance(os_windows, list):
        raise TypeError("unexpected answer from kitten @ ls")
    for w in os_windows:
        ids: list[str] = []
        record = w if isinstance(w, dict) else {}
        for tab in record.get("tabs") or []:
            for win in (tab.get("windows") if isinstance(tab, dict) else None) or []:
                if isinstance(win, dict) and is_number(win.get("id")):
                    ids.append(number(win["id"]))
        if is_number(record.get("id")):
            by_os_window[number(record["id"])] = ids
        else:
            by_os_window[f"unknown-{len(by_os_window)}"] = ids
    return by_os_window


def parse_kitty_windows(stdout: str) -> set[str]:
    return {i for ids in parse_kitty_os_windows(stdout).values() for i in ids}


def _ls_by_os_window(kitten: str, address: str) -> dict[str, list[str]] | None:
    try:
        result = run(kitten, ["@", "--to", address, "ls"], timeout=10)
    except (OSError, TimeoutError):
        return None
    return parse_kitty_os_windows(result.stdout) if result.code == 0 else None


def _ls(kitten: str, address: str) -> set[str] | None:
    by_os_window = _ls_by_os_window(kitten, address)
    return {i for ids in by_os_window.values() for i in ids} if by_os_window is not None else None


class KittyTarget(NamedTuple):
    address: str
    place: KittyPlace | None = None


def plan_kitty_place(
    options: OpenOptions | None,
    address: str,
    near_windows: set[str] | None,
    remembered: RememberedWindow | None,
    remembered_os_windows: dict[str, list[str]] | None,
) -> KittyTarget:
    near = options.near if options is not None else None
    if (
        near is not None
        and near.get("socket")
        and near.get("terminalId")
        and near_windows is not None
        and near["terminalId"] in near_windows
    ):
        return KittyTarget(near["socket"], InWindow(near["terminalId"]))
    if options is None or options.window != "dedicated":
        return KittyTarget(address)
    in_window = None
    if remembered is not None and remembered.get("socket") and remembered_os_windows is not None:
        listed = remembered_os_windows.get(remembered["id"])
        in_window = listed[0] if listed else None
    if in_window and remembered is not None and remembered.get("socket"):
        return KittyTarget(remembered["socket"], InWindow(in_window))
    return KittyTarget(address, OsWindow())


def _kitty_place(kitten: str, address: str, home: str, options: OpenOptions | None) -> KittyTarget:
    near_socket = options.near.get("socket") if options is not None and options.near is not None else None
    near = _ls(kitten, near_socket) if near_socket else None
    remembered = read_window(home, KITTY) if options is not None and options.window == "dedicated" and options.near is None else None
    os_windows = _ls_by_os_window(kitten, remembered["socket"]) if remembered is not None and remembered.get("socket") else None
    return plan_kitty_place(options, address, near, remembered, os_windows)


def _remember_os_window(kitten: str, home: str, address: str, window_id: str) -> None:
    by_os_window = _ls_by_os_window(kitten, address) or {}
    os_window = next((key for key, ids in by_os_window.items() if window_id in ids), None)
    if os_window is not None and _DIGITS.fullmatch(os_window):
        remember_window(home, KITTY, {"id": os_window, "socket": address})


def _reachable_socket(kitten: str, env: Mapping[str, str]) -> str | None:
    for address in kitty_addresses(_platform(), env):
        try:
            if _ls(kitten, address) is not None:
                return address
        except (ValueError, TypeError):
            continue
    return None


def _tools(ctx: TerminalContext) -> tuple[str, str]:
    exe = _find_kitty(ctx)
    if not exe:
        raise RuntimeError("kitty was not found")
    return exe, _find_kitten(exe, ctx.path_var)


def _remove(file: str) -> None:
    with contextlib.suppress(OSError):
        os.remove(file)


class Kitty(TerminalDriver):
    name = KITTY
    label = "kitty"
    capabilities = _SPAWNED
    can_input = True

    def current_capabilities(self, ctx: TerminalContext) -> Capabilities:
        found = _find_kitty(ctx)
        if not found:
            return _SPAWNED
        return _REMOTE if _reachable_socket(_find_kitten(found, ctx.path_var), ctx.env) else _SPAWNED

    def available(self, ctx: TerminalContext) -> bool:
        return _platform() in ("darwin", "linux") and _find_kitty(ctx) is not None

    def open(self, ctx: TerminalContext, spec: LaunchSpec, title: str, options: OpenOptions | None = None) -> TerminalTab:
        exe, kitten = _tools(ctx)
        check_posix_env_names(spec.env)
        shell = login_shell(ctx.env.get("SHELL"), _platform())
        folder = os.path.join(ctx.home, "launch")
        spec_file = os.path.join(folder, f"{spec.id}.spec")
        launcher = os.path.join(ctx.scripts_dir, launcher_name(shell))
        argv = surface_argv(shell)
        env = terminal_environment(ctx.env)
        address = _reachable_socket(kitten, ctx.env)
        base: TerminalTab = {"id": spec.id, "terminal": KITTY, "agent": spec.agent, "path": spec.cwd}

        if address:
            target = _kitty_place(kitten, address, ctx.home, options)
            args = kitty_launch_args(
                target.address, spec.cwd, title, launcher, spec_file, argv, target.place, options.focus if options is not None else None
            )
            write_new_private_file(spec_file, posix_spec(spec))
            try:
                result = run(kitten, args, env=env, timeout=30)
                if result.code != 0:
                    raise RuntimeError(f"kitten @ launch failed: {result.stderr.strip()}")
                window_id = parse_kitty_window_id(result.stdout)
            except BaseException:
                _remove(spec_file)
                raise
            if isinstance(target.place, OsWindow):
                with contextlib.suppress(OSError, TimeoutError, ValueError, TypeError):
                    _remember_os_window(kitten, ctx.home, target.address, window_id)
            return {**base, "createdAt": now_ms(), "terminalId": window_id, "socket": target.address}

        pid_file = os.path.join(folder, f"{spec.id}.pid")
        args = kitty_spawn_args(spec.cwd, argv)
        write_new_private_file(spec_file, posix_spec(spec.with_pid_file(pid_file)))
        try:
            start_detached(exe, args, {**env, "IDE_AGENT_TABS_LAUNCHER": launcher, "IDE_AGENT_TABS_SPEC": spec_file}, GUI_SETTLE_MS)
        except BaseException:
            _remove(spec_file)
            raise
        return {**base, "createdAt": now_ms(), "pidFile": pid_file}

    def alive(self, ctx: TerminalContext, tabs: list[TerminalTab]) -> set[str]:
        alive = pid_tabs_alive([t for t in tabs if t.get("pidFile")])
        remote = [t for t in tabs if t.get("socket") and t.get("terminalId")]
        if not remote:
            return alive
        _, kitten = _tools(ctx)
        for address in dict.fromkeys(t["socket"] for t in remote):
            windows = _ls(kitten, address)
            alive.update(t["id"] for t in remote if t["socket"] == address and windows is not None and t["terminalId"] in windows)
        return alive

    def close(self, ctx: TerminalContext, tab: TerminalTab) -> None:
        if tab.get("pidFile"):
            hang_up(tab)
            return
        if not tab.get("socket") or not tab.get("terminalId") or not _DIGITS.fullmatch(tab["terminalId"]):
            raise RuntimeError(f"tab {tab['id']} has no kitty window id")
        _, kitten = _tools(ctx)
        windows = _ls(kitten, tab["socket"])
        if windows is None or tab["terminalId"] not in windows:
            raise RuntimeError(f"kitty has no window {tab['terminalId']}; the tab is already closed")
        result = run(kitten, ["@", "--to", tab["socket"], "close-window", "--match", f"id:{tab['terminalId']}"], timeout=15)
        if result.code != 0:
            raise RuntimeError(f"kitten @ close-window failed: {result.stderr.strip()}")

    def input(self, ctx: TerminalContext, tab: TerminalTab, text: str) -> None:
        if tab.get("pidFile"):
            raise RuntimeError("a kitty window started without remote control can't take input")
        if not tab.get("socket") or not tab.get("terminalId") or not _DIGITS.fullmatch(tab["terminalId"]):
            raise RuntimeError(f"tab {tab['id']} has no kitty window id")
        calls = kitty_input_calls(tab["socket"], tab["terminalId"], text)
        _, kitten = _tools(ctx)
        windows = _ls(kitten, tab["socket"])
        if windows is None or tab["terminalId"] not in windows:
            raise RuntimeError(f"kitty has no window {tab['terminalId']}")
        for i, call in enumerate(calls):
            if i == 1:
                sleep_ms(ENTER_DELAY_MS)
            result = run(kitten, call.args, input=call.input, timeout=15)
            if result.code != 0:
                raise RuntimeError(f"kitten @ send-text failed: {result.stderr.strip()}")


kitty = Kitty()

from __future__ import annotations

import contextlib
import math
import os
import re
import sys
from collections.abc import Mapping
from typing import NamedTuple, Union

from ..clock import now_ms
from ..files import write_new_private_file
from ..jsjson import is_safe_integer
from ..processes import run
from ..spec import LaunchSpec, check_posix_env_names, posix_spec
from .driver import OpenOptions, TerminalContext, TerminalDriver, TerminalTab, caps
from .processes import find_executable, terminal_environment
from .shell import ENTER_DELAY_MS, check_argv_paths, check_input_line, launcher_name, login_shell, sleep_ms, surface_argv, tab_title
from .window_memory import DEDICATED_NAME

TMUX = "tmux"
TMUX_SESSION = "agents"
TMUX_DEDICATED_SESSION = DEDICATED_NAME
_TMUX_LOCATIONS = ("/opt/homebrew/bin/tmux", "/usr/local/bin/tmux", "/usr/bin/tmux", "/home/linuxbrew/.linuxbrew/bin/tmux")
_ENV = "/usr/bin/env"
_SESSION_FORMAT = "#{session_attached} #{session_last_attached} #{session_id} #{session_name}"
_WINDOW_FORMAT = "#{window_id} #{session_id} #{pid} #{socket_path}"
_LIST_FORMAT = "#{pid} #{window_id}"
_LINES = re.compile(r"\r?\n")
_SESSION_ID = re.compile(r"\$\d+")
_WINDOW_ID = re.compile(r"@\d+")
_NO_SERVER = re.compile(r"no server running|error connecting|no sessions", re.IGNORECASE)
_NO_SOCKET = re.compile(r"no such file|connection refused", re.IGNORECASE)
_TITLE_DROPS = re.compile(r"[#;]")


class TmuxSession(NamedTuple):
    attached: float
    last_attached: float
    id: str
    name: str


class InSession(NamedTuple):
    session: str
    detached: bool


class NewSession(NamedTuple):
    new_session: str


class After(NamedTuple):
    after: str
    socket: str


TmuxTarget = Union[InSession, NewSession, After]


class TmuxWindow(NamedTuple):
    window_id: str
    session_id: str
    server_pid: int
    socket: str


class LiveWindows(NamedTuple):
    server_pid: float | None
    windows: set[str]


def _fields(line: str, count: int) -> list[str] | None:
    parts = line.split(" ")
    if len(parts) < count:
        return None
    return [*parts[: count - 1], " ".join(parts[count - 1 :])]


def _js_number(text: str) -> float:
    text = text.strip()
    if text == "":
        return 0
    try:
        return float(text)
    except ValueError:
        return float("nan")


def _or_zero(value: float) -> float:
    return value if not math.isnan(value) and value != 0 else 0


def parse_tmux_sessions(stdout: str) -> list[TmuxSession]:
    sessions: list[TmuxSession] = []
    for line in _LINES.split(stdout):
        f = _fields(line, 4)
        if not f or not _SESSION_ID.fullmatch(f[2]):
            continue
        sessions.append(TmuxSession(_or_zero(_js_number(f[0])), _or_zero(_js_number(f[1])), f[2], f[3]))
    return sessions


def plan_tmux_target(sessions: list[TmuxSession]) -> TmuxTarget:
    attached = sorted((s for s in sessions if s.attached > 0), key=lambda s: -s.last_attached)
    if attached:
        return InSession(attached[0].id, False)
    agents = next((s for s in sessions if s.name == TMUX_SESSION), None)
    return InSession(agents.id, True) if agents is not None else NewSession(TMUX_SESSION)


def plan_dedicated_tmux_target(sessions: list[TmuxSession]) -> TmuxTarget:
    own = next((s for s in sessions if s.name == TMUX_DEDICATED_SESSION), None)
    return InSession(own.id, own.attached == 0) if own is not None else NewSession(TMUX_DEDICATED_SESSION)


# tmux expands formats in -n and reads an argument that ends in ';' as a command separator, so the title
# drops '#' and ';'. The folder isn't passed with -c, which tmux also expands; the launcher changes to it.
def tmux_title(label: str) -> str:
    return tab_title(_TITLE_DROPS.sub(" ", label))


def tmux_open_args(target: TmuxTarget, title: str, launcher: str, spec: str, argv: list[str], focus: bool | None = None) -> list[str]:
    check_argv_paths("tmux", [launcher, spec], ";")
    behind = ["-d"] if focus is False else []
    if isinstance(target, After):
        head = ["-S", target.socket, "new-window", *behind, "-a", "-t", target.after, "-P", "-F", _WINDOW_FORMAT]
    elif isinstance(target, InSession):
        head = ["new-window", *behind, "-P", "-F", _WINDOW_FORMAT, "-t", f"{target.session}:"]
    else:
        head = ["new-session", "-d", "-s", target.new_session, "-P", "-F", _WINDOW_FORMAT]
    # /usr/bin/env sets the paths instead of -e: new-session -e needs tmux 3.2, and it would also leave them
    # in the session environment for later windows.
    return [*head, "-n", tmux_title(title), "--", _ENV, f"IDE_AGENT_TABS_LAUNCHER={launcher}", f"IDE_AGENT_TABS_SPEC={spec}", *argv]


# tmux ends a command at an argument that ends in ';', so a line may not end in one.
def tmux_input_args(socket: str, window_id: str, text: str) -> tuple[list[str], list[str]]:
    check_input_line(text)
    if text.endswith(";"):
        raise ValueError("tmux input can't end in ';'")
    return (
        ["-S", socket, "send-keys", "-t", window_id, "-l", "--", text],
        ["-S", socket, "send-keys", "-t", window_id, "Enter"],
    )


def parse_tmux_window(stdout: str) -> TmuxWindow:
    f = _fields(stdout.strip(), 4)
    pid = _js_number(f[2]) if f else float("nan")
    if not f or not _WINDOW_ID.fullmatch(f[0]) or not _SESSION_ID.fullmatch(f[1]) or not is_safe_integer(pid) or f[3] == "":
        raise RuntimeError(f"unexpected answer from tmux: {stdout.strip()}")
    return TmuxWindow(f[0], f[1], int(pid), f[3])


def parse_tmux_window_list(stdout: str) -> LiveWindows:
    server_pid: float | None = None
    windows: set[str] = set()
    for line in _LINES.split(stdout):
        parts = line.split(" ")
        window = parts[1] if len(parts) > 1 else ""
        if not window or not _WINDOW_ID.fullmatch(window):
            continue
        server_pid = _js_number(parts[0])
        windows.add(window)
    return LiveWindows(server_pid, windows)


def is_no_server_error(stderr: str) -> bool:
    return _NO_SERVER.search(stderr) is not None


def _find_tmux(ctx: TerminalContext) -> str | None:
    return find_executable(ctx.path_var, "tmux", _TMUX_LOCATIONS)


def _tmux_path(ctx: TerminalContext) -> str:
    exe = _find_tmux(ctx)
    if not exe:
        raise RuntimeError("tmux was not found")
    return exe


def _live_windows(tmux: str, socket: str, env: Mapping[str, str]) -> LiveWindows:
    result = run(tmux, ["-S", socket, "list-windows", "-a", "-F", _LIST_FORMAT], env=env, timeout=15)
    if result.code == 0:
        return parse_tmux_window_list(result.stdout)
    if is_no_server_error(result.stderr) or _NO_SOCKET.search(result.stderr):
        return LiveWindows(None, set())
    raise RuntimeError(f"tmux list-windows failed: {result.stderr.strip()}")


def _is_open(tab: TerminalTab, live: LiveWindows) -> bool:
    return live.server_pid is not None and tab.get("serverPid") == live.server_pid and (tab.get("terminalId") or "") in live.windows


def _choose_target(exe: str, env: Mapping[str, str], options: OpenOptions | None) -> TmuxTarget:
    near = options.near if options is not None else None
    if near is not None and near.get("socket") and near.get("terminalId") and _WINDOW_ID.fullmatch(near["terminalId"]):
        try:
            live: LiveWindows | None = _live_windows(exe, near["socket"], env)
        except (OSError, TimeoutError, RuntimeError):
            live = None
        if live is not None and _is_open(near, live):
            return After(near["terminalId"], near["socket"])
    listed = run(exe, ["list-sessions", "-F", _SESSION_FORMAT], env=env, timeout=15)
    if listed.code != 0 and not is_no_server_error(listed.stderr):
        raise RuntimeError(f"tmux list-sessions failed: {listed.stderr.strip()}")
    sessions = parse_tmux_sessions(listed.stdout) if listed.code == 0 else []
    return plan_dedicated_tmux_target(sessions) if options is not None and options.window == "dedicated" else plan_tmux_target(sessions)


class Tmux(TerminalDriver):
    name = TMUX
    label = "tmux"
    capabilities = caps("tab", "yes", "yes")
    can_input = True

    def available(self, ctx: TerminalContext) -> bool:
        return (sys.platform == "darwin" or sys.platform.startswith("linux")) and _find_tmux(ctx) is not None

    def open(self, ctx: TerminalContext, spec: LaunchSpec, title: str, options: OpenOptions | None = None) -> TerminalTab:
        exe = _tmux_path(ctx)
        check_posix_env_names(spec.env)
        shell = login_shell(ctx.env.get("SHELL"), "linux" if sys.platform.startswith("linux") else sys.platform)
        spec_file = os.path.join(ctx.home, "launch", f"{spec.id}.spec")
        launcher = os.path.join(ctx.scripts_dir, launcher_name(shell))
        env = terminal_environment(ctx.env)
        target = _choose_target(exe, env, options)
        args = tmux_open_args(target, title, launcher, spec_file, surface_argv(shell), options.focus if options is not None else None)
        write_new_private_file(spec_file, posix_spec(spec))
        try:
            result = run(exe, args, env=env, timeout=30)
            if result.code != 0:
                raise RuntimeError(f"tmux {args[0]} failed: {result.stderr.strip()}")
            window = parse_tmux_window(result.stdout)
        except BaseException:
            with contextlib.suppress(OSError):
                os.remove(spec_file)
            raise
        detached = isinstance(target, NewSession) or (isinstance(target, InSession) and target.detached)
        dedicated = options is not None and options.window == "dedicated" and options.near is None
        session = TMUX_DEDICATED_SESSION if dedicated else TMUX_SESSION
        tab: TerminalTab = {
            "id": spec.id,
            "terminal": TMUX,
            "agent": spec.agent,
            "path": spec.cwd,
            "createdAt": now_ms(),
            "terminalId": window.window_id,
            "socket": window.socket,
            "serverPid": window.server_pid,
        }
        if detached:
            tab["note"] = (
                f'No tmux client is attached, so the tab opened in the detached session "{session}". Run: tmux attach -t {session}'
            )
        return tab

    def alive(self, ctx: TerminalContext, tabs: list[TerminalTab]) -> set[str]:
        alive: set[str] = set()
        tracked = [t for t in tabs if t.get("socket") and t.get("terminalId")]
        if not tracked:
            return alive
        exe = _tmux_path(ctx)
        env = terminal_environment(ctx.env)
        for socket in dict.fromkeys(t["socket"] for t in tracked):
            live = _live_windows(exe, socket, env)
            alive.update(t["id"] for t in tracked if t["socket"] == socket and _is_open(t, live))
        return alive

    def close(self, ctx: TerminalContext, tab: TerminalTab) -> None:
        if not tab.get("socket") or not tab.get("terminalId") or not _WINDOW_ID.fullmatch(tab["terminalId"]):
            raise RuntimeError(f"tab {tab['id']} has no tmux window id")
        exe = _tmux_path(ctx)
        env = terminal_environment(ctx.env)
        if not _is_open(tab, _live_windows(exe, tab["socket"], env)):
            raise RuntimeError(f"tmux has no window {tab['terminalId']}; the tab is already closed")
        result = run(exe, ["-S", tab["socket"], "kill-window", "-t", tab["terminalId"]], env=env, timeout=15)
        if result.code != 0:
            raise RuntimeError(f"tmux kill-window failed: {result.stderr.strip()}")

    def input(self, ctx: TerminalContext, tab: TerminalTab, text: str) -> None:
        if not tab.get("socket") or not tab.get("terminalId") or not _WINDOW_ID.fullmatch(tab["terminalId"]):
            raise RuntimeError(f"tab {tab['id']} has no tmux window id")
        typed, enter = tmux_input_args(tab["socket"], tab["terminalId"], text)
        exe = _tmux_path(ctx)
        env = terminal_environment(ctx.env)
        if not _is_open(tab, _live_windows(exe, tab["socket"], env)):
            raise RuntimeError(f"tmux has no window {tab['terminalId']}")
        for args in (typed, enter):
            if args is enter:
                sleep_ms(ENTER_DELAY_MS)
            result = run(exe, args, env=env, timeout=15)
            if result.code != 0:
                raise RuntimeError(f"tmux send-keys failed: {result.stderr.strip()}")


tmux = Tmux()

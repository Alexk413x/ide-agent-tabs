from __future__ import annotations

import contextlib
import os
import posixpath
import re
import sys
from typing import Any, Callable, NamedTuple

from ..clock import now_ms
from ..files import write_new_private_file
from ..jsjson import CONTROL, stringify
from ..processes import RunResult, run
from ..spec import LaunchSpec, check_posix_env_names, posix_spec
from .driver import OpenOptions, TerminalContext, TerminalDriver, TerminalTab, caps
from .shell import argv_mode_command, check_argv_paths, check_input_line, launcher_name, login_shell, tab_title
from .window_memory import RememberedWindow, read_window, remember_window

ITERM2 = "iterm2"
ITERM2_BUNDLE_ID = "com.googlecode.iterm2"
_CAPABILITIES = caps("tab", "yes", "yes")
_APP = f'application id "{ITERM2_BUNDLE_ID}"'

# iTerm2 evaluates a tab's command as an interpolated string, where \( starts an expression, then splits it
# shell-style. Our paths must not hold ', \ or $, and each word is single-quoted.
_COMMAND_REFUSED = "'\\$"
_LINES = re.compile(r"\r?\n")
_SESSION_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_DIGITS = re.compile(r"\d+")


def iterm2_locations(home: str) -> list[str]:
    return ["/Applications/iTerm.app", posixpath.join(home, "Applications", "iTerm.app")]


def iterm2_command(argv: list[str]) -> str:
    words: list[str] = []
    for word in argv:
        if any(c in ("'", "\\") or CONTROL.match(c) for c in word):
            raise ValueError(f"iTerm2 can't run a command word that holds ', \\ or a control character: {stringify(word)}")
        words.append(f"'{word}'")
    return " ".join(words)


# Every value reaches AppleScript through osascript's argv, read by `on run argv`, never through the script
# source, so these scripts are constants.
OPEN_SCRIPT = "\n".join(
    [
        "on run argv",
        "\tset agentCommand to item 1 of argv",
        "\tset agentTitle to item 2 of argv",
        '\tset placement to "last"',
        '\tset placeRef to ""',
        "\tset keepFocus to false",
        "\tif (count of argv) > 3 then",
        "\t\tset placement to item 3 of argv",
        "\t\tset placeRef to item 4 of argv",
        "\tend if",
        '\tif (count of argv) > 4 then set keepFocus to (item 5 of argv) is "background"',
        f"\ttell {_APP}",
        "\t\tset w to missing value",
        '\t\tif placement is "session" then',
        "\t\t\trepeat with cw in windows",
        "\t\t\t\trepeat with ct in tabs of cw",
        "\t\t\t\t\trepeat with cs in sessions of ct",
        "\t\t\t\t\t\tif (unique ID of cs) is placeRef then set w to (contents of cw)",
        "\t\t\t\t\tend repeat",
        "\t\t\t\tend repeat",
        "\t\t\tend repeat",
        '\t\telse if placement is "dedicated" then',
        "\t\t\trepeat with cw in windows",
        "\t\t\t\tif ((id of cw) as text) is placeRef then set w to (contents of cw)",
        "\t\t\tend repeat",
        "\t\tend if",
        '\t\tif w is missing value and placement is not "dedicated" and (count of windows) > 0 then set w to current window',
        "\t\tif w is missing value then",
        "\t\t\tset w to (create window with default profile command agentCommand)",
        "\t\t\tset s to current session of current tab of w",
        "\t\telse",
        "\t\t\tset previousTab to current tab of w",
        "\t\t\ttell w to set t to (create tab with default profile command agentCommand)",
        "\t\t\tset s to current session of t",
        "\t\t\tif keepFocus then tell previousTab to select",
        "\t\tend if",
        "\t\tset name of s to agentTitle",
        "\t\treturn (unique ID of s) & linefeed & ((id of w) as text)",
        "\tend tell",
        "end run",
        "",
    ]
)

LIST_SCRIPT = "\n".join(
    [
        "on run argv",
        f'\tif {_APP} is not running then return ""',
        '\tset out to ""',
        f"\ttell {_APP}",
        "\t\trepeat with w in windows",
        "\t\t\trepeat with t in tabs of w",
        "\t\t\t\trepeat with s in sessions of t",
        "\t\t\t\t\tset out to out & (unique ID of s) & linefeed",
        "\t\t\t\tend repeat",
        "\t\t\tend repeat",
        "\t\tend repeat",
        "\tend tell",
        "\treturn out",
        "end run",
        "",
    ]
)


def _find_session_script(action: list[str], done: str) -> str:
    return "\n".join(
        [
            "on run argv",
            "\tset sessionId to item 1 of argv",
            f'\tif {_APP} is not running then return "missing"',
            f"\ttell {_APP}",
            "\t\trepeat with w in windows",
            "\t\t\trepeat with t in tabs of w",
            "\t\t\t\trepeat with s in sessions of t",
            "\t\t\t\t\tif (unique ID of s) is sessionId then",
            *(f"\t\t\t\t\t\t{line}" for line in action),
            f'\t\t\t\t\t\treturn "{done}"',
            "\t\t\t\t\tend if",
            "\t\t\t\tend repeat",
            "\t\t\tend repeat",
            "\t\tend repeat",
            "\tend tell",
            '\treturn "missing"',
            "end run",
            "",
        ]
    )


CLOSE_SCRIPT = _find_session_script(["close (contents of s)"], "closed")

# write text sends raw bytes to the pty, and `newline false` holds back the CR, so Enter goes separately
# after the same 200 ms the other terminals wait.
INPUT_SCRIPT = _find_session_script(
    [
        "set wakeLine to item 2 of argv",
        "tell (contents of s)",
        "\twrite text wakeLine newline false",
        "\tdelay 0.2",
        '\twrite text ""',
        "end tell",
    ],
    "sent",
)


def parse_session_id(stdout: str) -> str:
    found = stdout.strip()
    if not _SESSION_ID.fullmatch(found):
        raise RuntimeError(f"unexpected answer from iTerm2: {found}")
    return found


class OpenAnswer(NamedTuple):
    session_id: str
    window_id: str | None


def parse_open_answer(stdout: str) -> OpenAnswer:
    parts = _LINES.split(stdout.strip())
    first = parts[0] if parts else ""
    second = parts[1].strip() if len(parts) > 1 else ""
    return OpenAnswer(parse_session_id(first), second if _DIGITS.fullmatch(second) else None)


def _iterm2_place(options: OpenOptions | None, remembered: RememberedWindow | None) -> list[str]:
    near = options.near if options is not None else None
    if near is not None and near.get("terminal") == ITERM2 and near.get("terminalId"):
        return ["session", near["terminalId"]]
    if options is not None and options.window == "dedicated":
        return ["dedicated", (remembered or {}).get("id", "")]
    return []


def iterm2_placement(options: OpenOptions | None, remembered: RememberedWindow | None) -> list[str]:
    place = _iterm2_place(options, remembered)
    if options is not None and options.focus is False:
        return [*(place or ["last", ""]), "background"]
    return place


def parse_session_list(stdout: str) -> set[str]:
    return {line for line in _LINES.split(stdout) if line != ""}


_DENIED = re.compile(r"\(-1743\)|not authori[sz]ed to send apple events", re.IGNORECASE)
_NOT_INSTALLED = re.compile(r"\(-10814\)|can[’'`]?t find application|unable to find application", re.IGNORECASE)
_TIMED_OUT = re.compile(r"\(-1712\)")


def classify_osascript_error(stderr: str) -> str:
    if _DENIED.search(stderr):
        return "denied"
    if _NOT_INSTALLED.search(stderr):
        return "not-installed"
    if _TIMED_OUT.search(stderr):
        return "timeout"
    return "other"


def osascript_error_message(failure: str, stderr: str) -> str:
    if failure == "denied":
        return (
            "macOS denied permission to control iTerm2. Allow the app that runs this agent to control iTerm in "
            "System Settings > Privacy & Security > Automation, then start a new session. Until then, open_tab skips iTerm2."
        )
    if failure == "not-installed":
        return f"iTerm2 is not installed, or macOS can't find it ({stderr}). Until the server restarts, open_tab skips iTerm2."
    if failure == "timeout":
        return f"iTerm2 did not answer in time; if macOS shows a prompt to allow control of iTerm, answer it and try again ({stderr})"
    return f"osascript failed: {stderr}"


def _default_osascript(script: str, args: list[str]) -> RunResult:
    return run("/usr/bin/osascript", ["-", *args], input=script, timeout=30)


def _default_remove(file: str) -> None:
    with contextlib.suppress(FileNotFoundError):
        os.remove(file)


class Iterm2Deps(NamedTuple):
    platform: str
    find_app: Callable[[], str | None]
    osascript: Callable[[str, list[str]], RunResult]
    write_spec: Callable[[str, bytes], None]
    remove_spec: Callable[[str], None]
    read_window: Callable[[str], RememberedWindow | None]
    remember_window: Callable[[str, RememberedWindow], None]


DEFAULT_DEPS = Iterm2Deps(
    platform="linux" if sys.platform.startswith("linux") else sys.platform,
    find_app=lambda: next((p for p in iterm2_locations(os.path.expanduser("~")) if os.path.exists(p)), None),
    osascript=_default_osascript,
    write_spec=write_new_private_file,
    remove_spec=_default_remove,
    read_window=lambda home: read_window(home, ITERM2),
    remember_window=lambda home, window: remember_window(home, ITERM2, window),
)


class Iterm2(TerminalDriver):
    name = ITERM2
    label = "iTerm2"
    capabilities = _CAPABILITIES
    can_input = True

    def __init__(self, deps: Iterm2Deps = DEFAULT_DEPS) -> None:
        self.deps = deps
        self.blocked: str | None = None

    def _osascript(self, script: str, args: list[str]) -> str:
        result = self.deps.osascript(script, args)
        if result.code == 0:
            return result.stdout
        stderr = result.stderr.strip()
        failure = classify_osascript_error(stderr)
        message = osascript_error_message(failure, stderr)
        if failure in ("denied", "not-installed"):
            self.blocked = message
        raise RuntimeError(message)

    @staticmethod
    def _session_id(tab: TerminalTab) -> str:
        if not tab.get("terminalId"):
            raise RuntimeError(f"tab {tab['id']} has no iTerm2 session id")
        return tab["terminalId"]

    def available(self, ctx: TerminalContext) -> bool:
        return self.deps.platform == "darwin" and self.blocked is None and self.deps.find_app() is not None

    def open(self, ctx: TerminalContext, spec: LaunchSpec, title: str, options: OpenOptions | None = None) -> TerminalTab:
        if self.deps.platform != "darwin":
            raise RuntimeError("iTerm2 runs on macOS only")
        if self.blocked:
            raise RuntimeError(self.blocked)
        check_posix_env_names(spec.env)
        shell = login_shell(ctx.env.get("SHELL"), self.deps.platform)
        spec_file = posixpath.join(ctx.home, "launch", f"{spec.id}.spec")
        launcher = posixpath.join(ctx.scripts_dir, launcher_name(shell))
        check_argv_paths("iTerm2", [launcher, spec_file], _COMMAND_REFUSED)
        command = iterm2_command(argv_mode_command(shell, launcher, spec_file))
        dedicated = options is not None and options.window == "dedicated" and options.near is None
        remembered: RememberedWindow | None = None
        if dedicated:
            with contextlib.suppress(OSError):
                remembered = self.deps.read_window(ctx.home)
        placement = iterm2_placement(options, remembered)
        self.deps.write_spec(spec_file, posix_spec(spec))
        try:
            answer = parse_open_answer(self._osascript(OPEN_SCRIPT, [command, tab_title(title), *placement]))
        except BaseException:
            self.deps.remove_spec(spec_file)
            raise
        if dedicated and answer.window_id is not None and answer.window_id != (remembered or {}).get("id"):
            with contextlib.suppress(OSError, TimeoutError):
                self.deps.remember_window(ctx.home, {"id": answer.window_id})
        tab: TerminalTab = {
            "id": spec.id,
            "terminal": ITERM2,
            "agent": spec.agent,
            "path": spec.cwd,
            "createdAt": now_ms(),
            "terminalId": answer.session_id,
        }
        if answer.window_id is not None:
            tab["window"] = answer.window_id
        return tab

    def alive(self, ctx: TerminalContext, tabs: list[TerminalTab]) -> set[str]:
        tracked = [t for t in tabs if t.get("terminalId")]
        if not tracked:
            return set()
        ids = parse_session_list(self._osascript(LIST_SCRIPT, []))
        return {t["id"] for t in tracked if t["terminalId"] in ids}

    def close(self, ctx: TerminalContext, tab: TerminalTab) -> None:
        session = self._session_id(tab)
        if self._osascript(CLOSE_SCRIPT, [session]).strip() != "closed":
            raise RuntimeError(f"iTerm2 has no session {session}; the tab is already closed")

    def input(self, ctx: TerminalContext, tab: TerminalTab, text: str) -> None:
        check_input_line(text)
        session = self._session_id(tab)
        if self._osascript(INPUT_SCRIPT, [session, text]).strip() != "sent":
            raise RuntimeError(f"iTerm2 has no session {session}")


def create_iterm2(**overrides: Any) -> Iterm2:
    return Iterm2(DEFAULT_DEPS._replace(**overrides))


iterm2 = Iterm2()

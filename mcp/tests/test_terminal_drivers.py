from __future__ import annotations

import json
import os
import sys
import types
import unittest
from typing import Any, Callable
from unittest import mock

from ide_agent_tabs.processes import RunResult
from ide_agent_tabs.spec import LaunchSpec, posix_spec, power_shell_spec
from ide_agent_tabs.terminals import TERMINAL_DRIVERS, ghostty, kitty, tmux, wezterm
from ide_agent_tabs.terminals import windows_terminal as wt
from ide_agent_tabs.terminals.driver import OpenOptions, TerminalContext
from ide_agent_tabs.terminals.shell import argv_mode_command, login_shell, surface_argv
from support import temp_home

SPEC = LaunchSpec("tab-1", "claude", "/work/app", "claude", ["--model", "opus"], {"A": "1"}, "hi")


class Recorder:
    def __init__(self, answer: Callable[[list[str]], RunResult]) -> None:
        self.answer = answer
        self.calls: list[dict[str, Any]] = []

    def __call__(self, command: str, args: Any = (), **options: Any) -> RunResult:
        argv = [command, *args]
        self.calls.append({"argv": argv, **options})
        return self.answer(argv)


def ok(stdout: str = "") -> RunResult:
    return RunResult(0, stdout, "")


class Written:
    def __init__(self) -> None:
        self.files: dict[str, bytes | str] = {}

    def __call__(self, path: str, content: bytes | str) -> None:
        self.files[path] = content


class DriverOrderTest(unittest.TestCase):
    def test_drivers_keep_the_node_order_and_labels(self) -> None:
        self.assertEqual(
            [(d.name, d.label, d.can_input) for d in TERMINAL_DRIVERS],
            [
                ("windows-terminal", "Windows Terminal", False),
                ("ghostty", "Ghostty", True),
                ("iterm2", "iTerm2", True),
                ("kitty", "kitty", True),
                ("wezterm", "WezTerm", True),
                ("tmux", "tmux", True),
            ],
        )


class WindowsTerminalTest(unittest.TestCase):
    def test_open_starts_wt_with_the_spec_file_and_records_the_pid_file(self) -> None:
        home = temp_home(self)
        started: list[tuple[str, list[str], dict[str, str], float]] = []
        written = Written()
        ctx = TerminalContext(home, "C:\\plugin\\launch", "", {"LOCALAPPDATA": "C:\\L", "IDE_AGENT_TABS_ID": "me", "KEEP": "1"}, "pwsh.exe")
        with (
            mock.patch.object(wt, "find_windows_terminal", lambda *_: "C:\\L\\wt.exe"),
            mock.patch.object(wt, "start_detached", lambda c, a, e, s: started.append((c, a, dict(e), s))),
            mock.patch.object(wt, "write_new_private_file", written),
        ):
            tab = wt.windows_terminal.open(ctx, SPEC, "Claude; Code", OpenOptions("dedicated"))
        spec_file = os.path.join(home, "launch", "tab-1.json")
        pid_file = os.path.join(home, "launch", "tab-1.pid")
        command, args, env, settle = started[0]
        self.assertEqual(command, "C:\\L\\wt.exe")
        self.assertEqual(
            args,
            ["-w", "agent-tabs", "new-tab", "--title", "Claude Code", "pwsh.exe", "-NoLogo", "-NoExit", "-ExecutionPolicy", "Bypass", "-File",
             "C:\\plugin\\launch\\agent-launch.ps1", spec_file],
        )  # fmt: skip
        self.assertEqual(env, {"LOCALAPPDATA": "C:\\L", "KEEP": "1"})
        self.assertEqual(settle, wt.WT_SETTLE_MS)
        self.assertEqual(written.files[spec_file], power_shell_spec(SPEC.with_pid_file(pid_file)))
        self.assertEqual(
            {k: v for k, v in tab.items() if k != "createdAt"},
            {
                "id": "tab-1",
                "terminal": "windows-terminal",
                "agent": "claude",
                "path": "/work/app",
                "pidFile": pid_file,
                "window": "agent-tabs",
            },
        )

    def test_close_kills_the_shell_tree_only_when_the_pid_is_a_shell(self) -> None:
        home = temp_home(self)
        pid_file = os.path.join(home, "t.pid")
        with open(pid_file, "w", encoding="utf-8") as f:
            f.write("4321\n")
        runner = Recorder(lambda argv: ok('"pwsh.exe","4321","Console","1","1 K"\r\n') if argv[0] == "tasklist.exe" else ok())
        with mock.patch.object(wt, "run", runner):
            wt.windows_terminal.close(TerminalContext(home, "", "", {}), {"id": "t", "terminal": "windows-terminal", "pidFile": pid_file})
        self.assertEqual(
            [c["argv"] for c in runner.calls], [["tasklist.exe", "/FO", "CSV", "/NH"], ["taskkill.exe", "/PID", "4321", "/T", "/F"]]
        )
        self.assertFalse(os.path.exists(pid_file))


class TmuxTest(unittest.TestCase):
    def test_open_adds_a_window_to_the_attached_session(self) -> None:
        home = temp_home(self)

        def answer(argv: list[str]) -> RunResult:
            if "list-sessions" in argv:
                return ok("0 0 $2 agents\n1 1700 $1 main\n")
            return ok("@4 $1 999 /tmp/tmux-1/default\n")

        runner = Recorder(answer)
        written = Written()
        ctx = TerminalContext(home, "/plugin/launch", "", {"SHELL": "/bin/bash", "CLAUDECODE": "1"})
        with (
            mock.patch.object(tmux, "_find_tmux", lambda ctx: "/usr/bin/tmux"),
            mock.patch.object(tmux, "run", runner),
            mock.patch.object(tmux, "write_new_private_file", written),
        ):
            tab = tmux.tmux.open(ctx, SPEC, "Claude Code", OpenOptions(focus=False))
        spec_file = os.path.join(home, "launch", "tab-1.spec")
        launcher = os.path.join("/plugin/launch", "agent-launch.sh")
        shell = login_shell("/bin/bash", "linux")
        self.assertEqual(
            runner.calls[0]["argv"],
            ["/usr/bin/tmux", "list-sessions", "-F", "#{session_attached} #{session_last_attached} #{session_id} #{session_name}"],
        )
        self.assertEqual(
            runner.calls[1]["argv"],
            ["/usr/bin/tmux", "new-window", "-d", "-P", "-F", "#{window_id} #{session_id} #{pid} #{socket_path}", "-t", "$1:", "-n", "Claude Code", "--",
             "/usr/bin/env", f"IDE_AGENT_TABS_LAUNCHER={launcher}", f"IDE_AGENT_TABS_SPEC={spec_file}", *surface_argv(shell)],
        )  # fmt: skip
        self.assertNotIn("CLAUDECODE", runner.calls[1]["env"])
        self.assertEqual(written.files[spec_file], posix_spec(SPEC))
        self.assertEqual(tab["terminalId"], "@4")
        self.assertEqual(tab["serverPid"], 999)
        self.assertNotIn("note", tab)

    def test_a_detached_session_comes_with_a_note_and_close_checks_the_server(self) -> None:
        home = temp_home(self)
        runner = Recorder(lambda argv: RunResult(1, "", "no server running on /tmp/x") if "list-sessions" in argv else ok("@1 $0 77 /s\n"))
        with (
            mock.patch.object(tmux, "_find_tmux", lambda ctx: "/usr/bin/tmux"),
            mock.patch.object(tmux, "run", runner),
            mock.patch.object(tmux, "write_new_private_file", Written()),
        ):
            tab = tmux.tmux.open(TerminalContext(home, "/p", "", {}), SPEC, "C", OpenOptions("dedicated"))
        self.assertEqual(runner.calls[1]["argv"][1:5], ["new-session", "-d", "-s", "agent-tabs"])
        self.assertEqual(
            tab["note"],
            'No tmux client is attached, so the tab opened in the detached session "agent-tabs". Run: tmux attach -t agent-tabs',
        )
        closer = Recorder(lambda argv: ok("77 @1\n") if "list-windows" in argv else ok())
        with mock.patch.object(tmux, "_find_tmux", lambda ctx: "/usr/bin/tmux"), mock.patch.object(tmux, "run", closer):
            self.assertEqual(tmux.tmux.alive(TerminalContext(home, "/p", "", {}), [tab]), {"tab-1"})
            tmux.tmux.close(TerminalContext(home, "/p", "", {}), tab)
        self.assertEqual(closer.calls[-1]["argv"], ["/usr/bin/tmux", "-S", "/s", "kill-window", "-t", "@1"])


class KittyTest(unittest.TestCase):
    def test_open_launches_through_remote_control(self) -> None:
        home = temp_home(self)

        def answer(argv: list[str]) -> RunResult:
            if argv[-1] == "ls":
                return ok('[{"id": 1, "tabs": [{"windows": [{"id": 3}]}]}]')
            return ok("17\n")

        runner = Recorder(answer)
        ctx = TerminalContext(home, "/p", "", {"SHELL": "/usr/bin/fish"})
        with (
            mock.patch.object(kitty, "_find_kitty", lambda ctx: "/usr/bin/kitty"),
            mock.patch.object(kitty, "_find_kitten", lambda exe, path: "/usr/bin/kitten"),
            mock.patch.object(kitty, "kitty_addresses", lambda platform, env: ["unix:/run/kitty-agent-tabs-5"]),
            mock.patch.object(kitty, "run", runner),
            mock.patch.object(kitty, "write_new_private_file", Written()),
        ):
            tab = kitty.kitty.open(ctx, SPEC, "Claude Code", OpenOptions())
        spec_file = os.path.join(home, "launch", "tab-1.spec")
        shell = login_shell("/usr/bin/fish", "linux")
        self.assertEqual(
            runner.calls[-1]["argv"],
            ["/usr/bin/kitten", "@", "--to", "unix:/run/kitty-agent-tabs-5", "launch", "--type=tab", "--cwd", "/work/app",
             "--env", f"IDE_AGENT_TABS_LAUNCHER={os.path.join('/p', 'agent-launch.fish')}", "--env", f"IDE_AGENT_TABS_SPEC={spec_file}",
             "--tab-title", "Claude Code", "--", *surface_argv(shell)],
        )  # fmt: skip
        self.assertEqual((tab["terminalId"], tab["socket"]), ("17", "unix:/run/kitty-agent-tabs-5"))


class WeztermTest(unittest.TestCase):
    def test_open_spawns_a_pane_in_a_running_gui(self) -> None:
        home = temp_home(self)
        runner = Recorder(lambda argv: ok("12\n"))
        ctx = TerminalContext(home, "/p", "", {"SHELL": "/bin/zsh", "IDE_AGENT_TABS_AGENT": "x"}, "pwsh.exe")
        with (
            mock.patch.object(wezterm, "_find_wezterm", lambda ctx: "/usr/bin/wezterm"),
            mock.patch.object(wezterm, "_gui_sockets", lambda env: ["/run/gui-sock-5"]),
            mock.patch.object(wezterm, "run", runner),
            mock.patch.object(wezterm, "write_new_private_file", Written()),
        ):
            tab = wezterm.wezterm.open(ctx, SPEC, "Claude Code", OpenOptions())
        windows = sys.platform == "win32"
        spec_file = os.path.join(home, "launch", f"tab-1{'.json' if windows else '.spec'}")
        if windows:
            argv = [
                "pwsh.exe",
                "-NoLogo",
                "-NoExit",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                os.path.join("/p", "agent-launch.ps1"),
                spec_file,
            ]
        else:
            shell = login_shell("/bin/zsh", "linux" if sys.platform.startswith("linux") else sys.platform)
            argv = argv_mode_command(shell, os.path.join("/p", "agent-launch.sh"), spec_file)
        call = runner.calls[0]
        self.assertEqual(call["argv"], ["/usr/bin/wezterm", "cli", "--no-auto-start", "spawn", "--cwd", "/work/app", "--", *argv])
        self.assertEqual(call["env"]["WEZTERM_UNIX_SOCKET"], "/run/gui-sock-5")
        self.assertNotIn("IDE_AGENT_TABS_AGENT", call["env"])
        self.assertEqual((tab["terminalId"], tab["socket"]), ("12", "/run/gui-sock-5"))


class GhosttyLinuxTest(unittest.TestCase):
    def test_open_on_linux_starts_one_ghostty_window_per_agent(self) -> None:
        home = temp_home(self)
        started: list[tuple[str, list[str], dict[str, str]]] = []
        written = Written()
        with (
            mock.patch.object(ghostty, "sys", types.SimpleNamespace(platform="linux")),
            mock.patch.object(ghostty, "_find_on_linux", lambda ctx: "/usr/bin/ghostty"),
            mock.patch.object(ghostty, "start_detached", lambda c, a, e, s: started.append((c, a, dict(e)))),
            mock.patch.object(ghostty, "write_new_private_file", written),
        ):
            tab = ghostty.ghostty.open(TerminalContext(home, "/p", "", {"SHELL": "/bin/bash"}), SPEC, "Claude Code", OpenOptions())
        spec_file = os.path.join(home, "launch", "tab-1.spec")
        pid_file = os.path.join(home, "launch", "tab-1.pid")
        command, args, env = started[0]
        self.assertEqual(command, "/usr/bin/ghostty")
        self.assertEqual(args, ghostty.ghostty_linux_args("/work/app", login_shell("/bin/bash", "linux")))
        self.assertEqual(env["IDE_AGENT_TABS_SPEC"], spec_file)
        self.assertEqual(env["IDE_AGENT_TABS_LAUNCHER"], os.path.join("/p", "agent-launch.sh"))
        self.assertEqual(written.files[spec_file], posix_spec(SPEC.with_pid_file(pid_file)))
        self.assertEqual(tab["pidFile"], pid_file)


class SpecTest(unittest.TestCase):
    def test_power_shell_spec_lists_env_pairs(self) -> None:
        self.assertEqual(
            json.loads(power_shell_spec(SPEC))["env"],
            [{"name": "A", "value": "1"}],
        )


if __name__ == "__main__":
    unittest.main()

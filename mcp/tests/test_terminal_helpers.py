from __future__ import annotations

import json
import os
import unittest
from typing import Any

from ide_agent_tabs.terminals import default_terminal_name, ghostty, kitty, processes, tmux, wezterm
from ide_agent_tabs.terminals import windows_terminal as wt
from ide_agent_tabs.terminals.driver import OpenOptions, TerminalTab
from ide_agent_tabs.terminals.shell import LoginShell, surface_argv, surface_command
from ide_agent_tabs.terminals.window_memory import WINDOWS_FILE, read_window, remember_window
from support import temp_home

ARGV = ["/bin/zsh", "-l", "-i", "-c", "script", "agent-tabs", "/l.sh", "/s.spec"]
LINE = "Agent Tabs: new message from codex 01234567. Call read_messages."
BAD_LINES = ["two\nlines", "x" * 501, "esc\x1b[2J"]
ENV = {"IDE_AGENT_TABS_LAUNCHER": "/l.sh", "IDE_AGENT_TABS_SPEC": "/s.spec"}
WINDOW_FORMAT = "#{window_id} #{session_id} #{pid} #{socket_path}"


def near_tab(**over: Any) -> TerminalTab:
    return {"id": "caller", "terminal": "x", "agent": "claude", "path": "/w", "createdAt": 0, **over}


def make_sockets(test: unittest.TestCase, names_and_ages: list[tuple[str, int]]) -> str:
    folder = temp_home(test)
    now = os.stat(folder).st_mtime
    for name, age in names_and_ages:
        file = os.path.join(folder, name)
        with open(file, "w", encoding="utf-8"):
            pass
        os.utime(file, (now - age, now - age))
    return folder


class TerminalEnvironmentTest(unittest.TestCase):
    def test_a_terminal_started_by_the_server_does_not_inherit_the_calling_session(self) -> None:
        env = {
            "PATH": "p",
            "ClaudeCode": "1",
            "CLAUDE_CODE_SSE_PORT": "1",
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "IDE_AGENT_TABS_ID": "t",
            "IDE_AGENT_TABS_HOME": "h",
            "CODEX_SANDBOX": "seatbelt",
            "CODEX_SANDBOX_NETWORK_DISABLED": "1",
            "CODEX_HOME": "c",
            "GEMINI_CLI": "1",
            "OPENCODE_SESSION_ID": "s",
            "ANTIGRAVITY_CLI_ALIAS": "agy",
            "COPILOT_HOME": "g",
        }
        self.assertEqual(
            processes.terminal_environment(env),
            {"PATH": "p", "CLAUDE_CODE_USE_BEDROCK": "1", "IDE_AGENT_TABS_HOME": "h", "CODEX_HOME": "c", "COPILOT_HOME": "g"},
        )


class DefaultTerminalTest(unittest.TestCase):
    def test_the_default_terminal_is_the_first_available_one_in_the_platform_order(self) -> None:
        self.assertEqual(default_terminal_name("win32", ["wezterm", "windows-terminal"]), "windows-terminal")
        self.assertEqual(default_terminal_name("win32", ["wezterm", "tmux"]), "wezterm")
        self.assertEqual(default_terminal_name("darwin", ["tmux", "wezterm", "kitty"]), "kitty")
        self.assertEqual(default_terminal_name("linux", ["tmux"]), "tmux")
        self.assertIsNone(default_terminal_name("linux", ["windows-terminal"]))
        self.assertIsNone(default_terminal_name("freebsd", ["tmux"]))


class PidTabsAliveTest(unittest.TestCase):
    def test_a_starting_tab_counts_as_open_until_its_pid_file_exists_and_stale_or_ended_tabs_do_not(self) -> None:
        folder = temp_home(self)
        ended = os.path.join(folder, "ended.pid")
        with open(ended, "w", encoding="utf-8") as f:
            f.write("999999999\n")
        missing = os.path.join(folder, "missing.pid")
        now = 1_700_000_000_000
        base = {"terminal": "ghostty", "agent": "a", "path": "/"}
        tabs: list[TerminalTab] = [
            {**base, "id": "starting", "createdAt": now, "pidFile": missing},
            {**base, "id": "stale", "createdAt": now - 120_000, "pidFile": missing},
            {**base, "id": "ended", "createdAt": now, "pidFile": ended},
        ]
        self.assertEqual(processes.pid_tabs_alive(tabs, now), {"starting"})

    def test_a_live_process_that_is_not_a_shell_does_not_keep_its_tab_open(self) -> None:
        folder = temp_home(self)
        pid_file = os.path.join(folder, "t.pid")
        with open(pid_file, "w", encoding="utf-8") as f:
            f.write(f"{os.getpid()}\n")
        tab: TerminalTab = {"id": "t", "terminal": "ghostty", "agent": "a", "path": "/", "createdAt": 0, "pidFile": pid_file}
        self.assertEqual(processes.pid_tabs_alive([tab], 1), set())


class WindowMemoryTest(unittest.TestCase):
    def test_remembered_windows_live_in_one_file_under_the_home_with_one_entry_per_terminal(self) -> None:
        home = temp_home(self)
        self.assertIsNone(read_window(home, "wezterm"))
        remember_window(home, "wezterm", {"id": "3", "socket": "/s/gui-sock-1"})
        remember_window(home, "iterm2", {"id": "41"})
        remember_window(home, "wezterm", {"id": "4", "socket": "/s/gui-sock-2"})
        self.assertEqual(read_window(home, "wezterm"), {"id": "4", "socket": "/s/gui-sock-2"})
        self.assertEqual(read_window(home, "iterm2"), {"id": "41"})
        with open(os.path.join(home, WINDOWS_FILE), encoding="utf-8") as f:
            self.assertEqual(sorted(json.load(f)), ["iterm2", "wezterm"])

    def test_a_bad_entry_or_bad_json_reads_as_nothing(self) -> None:
        home = temp_home(self)
        file = os.path.join(home, WINDOWS_FILE)
        with open(file, "w", encoding="utf-8") as f:
            f.write('{"wezterm": {"id": 3}, "kitty": {"id": "1"}}')
        self.assertIsNone(read_window(home, "wezterm"))
        self.assertEqual(read_window(home, "kitty"), {"id": "1"})
        with open(file, "w", encoding="utf-8") as f:
            f.write("not json")
        self.assertIsNone(read_window(home, "kitty"))


class ShellRefusalTest(unittest.TestCase):
    def test_the_surface_command_and_argv_refuse_a_shell_path_with_a_space_or_no_folder(self) -> None:
        with self.assertRaises(ValueError):
            surface_command(LoginShell("/bin/my shell", "posix"))
        with self.assertRaisesRegex(ValueError, "unsafe shell path"):
            surface_argv(LoginShell("bash", "posix"))


class WindowsTerminalHelpersTest(unittest.TestCase):
    def test_title_with_only_a_semicolon_falls_back_to_agent(self) -> None:
        self.assertEqual(wt.wt_title("A; B\nC"), "A B C")
        self.assertEqual(wt.wt_title(";"), "Agent")

    def test_window_is_the_last_one_unless_dedicated_or_the_caller_tab_names_one(self) -> None:
        self.assertEqual(wt.wt_window(None), "0")
        self.assertEqual(wt.wt_window(OpenOptions("last")), "0")
        self.assertEqual(wt.wt_window(OpenOptions("dedicated")), "agent-tabs")
        self.assertEqual(wt.wt_window(OpenOptions("dedicated", near_tab(terminal="windows-terminal"))), "0")
        self.assertEqual(wt.wt_window(OpenOptions("last", near_tab(terminal="windows-terminal", window="agent-tabs"))), "agent-tabs")
        args = wt.wt_args("t", "C:\\pwsh.exe", "C:\\l.ps1", "C:\\s.json")
        self.assertEqual(args[:3], ["-w", "0", "new-tab"])


class GhosttyHelpersTest(unittest.TestCase):
    def test_linux_install_folders_are_searched_after_path(self) -> None:
        self.assertEqual(
            ghostty.ghostty_linux_locations("/home/u"),
            ["/usr/bin/ghostty", "/usr/local/bin/ghostty", "/home/u/.local/bin/ghostty", "/snap/bin/ghostty"],
        )

    def test_applescript_strings_keep_non_ascii_text_and_refuse_a_carriage_return(self) -> None:
        self.assertEqual(ghostty.apple_script_string("/Users/J Doe/é"), '"/Users/J Doe/é"')
        with self.assertRaises(ValueError):
            ghostty.apple_script_string("a\rb")

    def test_place_is_empty_for_the_last_window_and_for_a_caller_tab_without_a_tab_id(self) -> None:
        self.assertIsNone(ghostty.ghostty_place(None, None))
        self.assertIsNone(ghostty.ghostty_place(OpenOptions("last"), {"id": "5"}))
        self.assertIsNone(ghostty.ghostty_place(OpenOptions("last", near_tab(terminal="ghostty", pidFile="/p.pid")), None))
        self.assertEqual(
            ghostty.ghostty_place(OpenOptions("dedicated", near_tab(terminal="ghostty", terminalTabId="tab-9")), {"id": "5"}),
            ghostty.NearTab("tab-9"),
        )

    def test_open_script_for_a_dedicated_window_never_falls_back_to_the_front_window(self) -> None:
        self.assertEqual(ghostty.open_script("/bin/zsh", ENV), ghostty.open_script("/bin/zsh", ENV, None))
        self.assertNotIn("id of w", ghostty.open_script("/bin/zsh", ENV))
        dedicated = ghostty.open_script("/bin/zsh", ENV, ghostty.Dedicated("5"))
        self.assertIn('if (id of cw as text) is "5" then set w to (contents of cw)', dedicated)
        self.assertNotIn("front window", dedicated)
        self.assertIn("& linefeed & (id of w as text)", dedicated)
        fresh = ghostty.open_script("/bin/zsh", ENV, ghostty.Dedicated(None))
        self.assertNotIn("repeat with cw", fresh)
        self.assertIn("set w to new window with configuration cfg", fresh)
        near = ghostty.open_script("/bin/zsh", ENV, ghostty.NearTab('a"b'))
        self.assertIn('if (id of ct as text) is "a\\"b" then set w to (contents of cw)', near)
        self.assertIn("if w is missing value and (count of windows) > 0 then set w to front window", near)

    def test_open_script_reselects_the_previous_tab_only_when_focus_is_kept(self) -> None:
        front = ghostty.open_script("/bin/zsh", ENV, None, True)
        self.assertIn(
            "\tif (count of windows) > 0 then\n"
            "\t\tset previousTab to selected tab of front window\n"
            "\t\tset t to new tab in front window with configuration cfg\n"
            "\t\tselect tab previousTab\n"
            "\telse",
            front,
        )
        placed = ghostty.open_script("/bin/zsh", ENV, ghostty.Dedicated("5"), True)
        self.assertIn(
            "\tif w is not missing value then\n"
            "\t\tset previousTab to selected tab of w\n"
            "\t\tset t to new tab in w with configuration cfg\n"
            "\t\tselect tab previousTab\n"
            "\telse",
            placed,
        )
        for script in (front, placed):
            self.assertEqual(script.count("select tab"), 1, "a new window keeps its own focus")
        self.assertEqual(ghostty.open_script("/bin/zsh", ENV, None, False), ghostty.open_script("/bin/zsh", ENV))
        self.assertNotIn("previousTab", ghostty.open_script("/bin/zsh", ENV, ghostty.NearTab("x")))

    def test_list_close_and_input_scripts_quote_the_ids_and_never_launch_ghostty(self) -> None:
        self.assertTrue(ghostty.list_script().startswith('if application "Ghostty" is not running then return ""'))
        close = ghostty.close_script('ABC-"1"')
        self.assertTrue(close.startswith('if application "Ghostty" is not running then return "missing"'))
        self.assertIn('is "ABC-\\"1\\"" then', close)
        self.assertEqual(ghostty.input_script('x"y', "hi").split("\n")[1], '\tset t to terminal id "x\\"y"')
        with self.assertRaisesRegex(ValueError, "one line"):
            ghostty.input_script("x", "a\rb")


class WeztermHelpersTest(unittest.TestCase):
    def test_spawn_and_start_args_refuse_a_folder_with_a_control_character_and_allow_a_semicolon(self) -> None:
        with self.assertRaisesRegex(ValueError, "control character"):
            wezterm.wezterm_spawn_args("/w/a\rb", ARGV)
        self.assertEqual(wezterm.wezterm_start_args("C:\\w\\a;b", ARGV), ["start", "--cwd", "C:\\w\\a;b", "--", *ARGV])

    def test_input_refuses_text_that_is_not_one_short_line(self) -> None:
        for bad in BAD_LINES:
            with self.subTest(text=bad), self.assertRaisesRegex(ValueError, "one line"):
                wezterm.wezterm_input_args("1", bad)

    def test_pane_ids_come_from_cli_list(self) -> None:
        listing = json.dumps(
            [
                {"window_id": 0, "tab_id": 0, "pane_id": 3, "cwd": "file:///home/u/", "title": "zsh"},
                {"window_id": 0, "tab_id": 1, "pane_id": 7, "cwd": "file:///w/", "title": "claude"},
                {"window_id": 1},
            ]
        )
        self.assertEqual(wezterm.parse_wezterm_panes(listing), ["3", "7"])
        self.assertEqual(wezterm.parse_wezterm_panes("[]"), [])
        with self.assertRaises(TypeError):
            wezterm.parse_wezterm_panes("{}")

    def test_cli_that_cannot_connect_reports_no_gui(self) -> None:
        self.assertTrue(wezterm.is_no_gui_error('Error: failed to connect to Socket("/run/user/1000/wezterm/gui-sock-1")'))
        self.assertTrue(wezterm.is_no_gui_error("while connecting to the mux: No such file or directory"))
        self.assertFalse(wezterm.is_no_gui_error("error: unexpected argument --foo"))

    def test_install_folders_per_platform(self) -> None:
        self.assertEqual(
            wezterm.wezterm_locations("win32", "C:\\Users\\u", "C:\\Program Files"), ["C:\\Program Files\\WezTerm\\wezterm.exe"]
        )
        self.assertEqual(wezterm.wezterm_locations("win32", "C:\\Users\\u", None), [])
        self.assertEqual(
            wezterm.wezterm_locations("darwin", "/Users/u", None),
            ["/Applications/WezTerm.app/Contents/MacOS/wezterm", "/Users/u/Applications/WezTerm.app/Contents/MacOS/wezterm"],
        )
        self.assertEqual(
            wezterm.wezterm_locations("linux", "/home/u", None),
            ["/usr/bin/wezterm", "/usr/local/bin/wezterm", "/home/u/.local/bin/wezterm", "/home/linuxbrew/.linuxbrew/bin/wezterm"],
        )

    def test_gui_sockets_come_from_the_runtime_folder_for_running_guis_newest_first(self) -> None:
        self.assertEqual(wezterm.wezterm_runtime_dir("linux", {"XDG_RUNTIME_DIR": "/run/user/1000"}, "/home/u"), "/run/user/1000/wezterm")
        self.assertEqual(wezterm.wezterm_runtime_dir("darwin", {}, "/Users/u"), os.path.join("/Users/u", ".local", "share", "wezterm"))
        folder = make_sockets(self, [("gui-sock-100", 300), ("gui-sock-200", 100), ("gui-sock-300", 0), ("sock", 0), ("gui-sock-x", 0)])

        def running(pid: int) -> bool:
            return pid != 300

        self.assertEqual(
            wezterm.find_gui_sockets(folder, running),
            [os.path.join(folder, "gui-sock-200"), os.path.join(folder, "gui-sock-100")],
        )
        self.assertEqual(wezterm.find_gui_sockets(os.path.join(folder, "missing"), running), [])

    def test_targets_fall_back_when_the_remembered_or_caller_pane_is_gone(self) -> None:
        def shape(targets: list[wezterm.WeztermTarget]) -> list[Any]:
            return [(t.socket, None if t.place is None else (type(t.place).__name__, t.place[0]), t.remember) for t in targets]

        panes = {"7": "1", "8": "2"}
        remembered = {"id": "2", "socket": "/g"}
        new_window = (None, ("NewWindow", True), True)
        self.assertEqual(shape(wezterm.plan_wezterm_targets(OpenOptions("last"), None, remembered, panes)), [(None, None, False)])
        self.assertEqual(
            shape(wezterm.plan_wezterm_targets(OpenOptions("dedicated"), None, remembered, panes)),
            [("/g", ("WindowPlace", "2"), False), new_window],
        )
        self.assertEqual(
            shape(wezterm.plan_wezterm_targets(OpenOptions("dedicated"), None, {"id": "9", "socket": "/g"}, panes)), [new_window]
        )
        self.assertEqual(shape(wezterm.plan_wezterm_targets(OpenOptions("dedicated"), None, remembered, None)), [new_window])
        near = near_tab(terminal="wezterm", terminalId="7", socket="/n")
        self.assertEqual(
            shape(wezterm.plan_wezterm_targets(OpenOptions("last", near), panes, None, None)),
            [("/n", ("PanePlace", "7"), False), (None, None, False)],
        )
        self.assertEqual(shape(wezterm.plan_wezterm_targets(OpenOptions("last", near), {}, None, None)), [(None, None, False)])


class KittyHelpersTest(unittest.TestCase):
    def test_sockets_are_found_by_name_and_newest_first(self) -> None:
        folder = make_sockets(
            self, [("kitty-agent-tabs-100", 300), ("kitty-agent-tabs-200", 100), ("kitty-agent-tabs-x", 0), ("other-1", 0)]
        )
        newest_first = [f"unix:{os.path.join(folder, 'kitty-agent-tabs-200')}", f"unix:{os.path.join(folder, 'kitty-agent-tabs-100')}"]
        self.assertEqual(kitty.find_kitty_sockets(folder), newest_first)
        self.assertEqual(kitty.find_kitty_sockets(os.path.join(folder, "missing")), [])
        self.assertEqual(kitty.kitty_addresses("linux", {"XDG_RUNTIME_DIR": folder, "KITTY_LISTEN_ON": "unix:/tmp/k-1"}), ["unix:/tmp/k-1"])
        self.assertEqual(kitty.kitty_addresses("linux", {"XDG_RUNTIME_DIR": folder, "KITTY_LISTEN_ON": "tcp:localhost:5000"}), newest_first)
        self.assertEqual(kitty.kitty_addresses("win32", {}), [])

    def test_socket_folder_per_platform(self) -> None:
        self.assertIsNone(kitty.kitty_socket_dir("linux", {"TMPDIR": "/tmp"}))
        self.assertEqual(kitty.kitty_socket_dir("linux", {"XDG_RUNTIME_DIR": "/run/user/1"}), "/run/user/1")
        self.assertEqual(kitty.kitty_socket_dir("darwin", {"TMPDIR": "/var/folders/x/T/"}), "/var/folders/x/T/")
        self.assertIsNone(kitty.kitty_socket_dir("win32", {}))

    def test_install_folders(self) -> None:
        self.assertEqual(
            kitty.kitty_locations("/Users/u"),
            [
                "/Applications/kitty.app/Contents/MacOS/kitty",
                "/Users/u/Applications/kitty.app/Contents/MacOS/kitty",
                "/Users/u/.local/kitty.app/bin/kitty",
            ],
        )

    def test_launch_args_clean_the_title_and_refuse_a_path_with_a_control_character(self) -> None:
        args = kitty.kitty_launch_args("unix:/k", "/w/app", "Claude\nCode", "/d/agent-launch.sh", "/h/t.spec", ARGV)
        self.assertEqual(args[args.index("--tab-title") + 1], "Claude Code")
        with self.assertRaisesRegex(ValueError, "control character"):
            kitty.kitty_launch_args("unix:/k", "/w", "t", "/l\n", "/s", ARGV)

    def test_keep_focus_is_added_only_when_focus_is_false(self) -> None:
        base = ("unix:/k", "/w", "t", "/l.sh", "/s.spec", ARGV)
        self.assertEqual(kitty.kitty_launch_args(*base, None, False)[3:6], ["launch", "--type=tab", "--keep-focus"])
        self.assertEqual(kitty.kitty_launch_args(*base, kitty.OsWindow(), False)[3:6], ["launch", "--type=os-window", "--keep-focus"])
        self.assertNotIn("--keep-focus", kitty.kitty_launch_args(*base, None, True))
        self.assertNotIn("--keep-focus", kitty.kitty_launch_args(*base))

    def test_input_refuses_text_that_is_not_one_short_line(self) -> None:
        for bad in BAD_LINES:
            with self.subTest(text=bad), self.assertRaisesRegex(ValueError, "one line"):
                kitty.kitty_input_calls("unix:/k", "1", bad)

    def test_window_ids_come_from_ls(self) -> None:
        listing = json.dumps(
            [
                {
                    "id": 1,
                    "tabs": [
                        {"id": 1, "title": "a", "windows": [{"id": 1, "pid": 10, "cwd": "/"}, {"id": 4}]},
                        {"id": 2, "windows": [{"id": 9}]},
                    ],
                },
                {"id": 2, "tabs": []},
            ]
        )
        self.assertEqual(kitty.parse_kitty_windows(listing), {"1", "4", "9"})
        with self.assertRaises(TypeError):
            kitty.parse_kitty_windows("{}")

    def test_a_dedicated_mode_still_opens_next_to_a_caller_window_that_exists(self) -> None:
        near = near_tab(terminal="kitty", terminalId="11", socket="unix:/n")
        target = kitty.plan_kitty_place(OpenOptions("dedicated", near), "unix:/k", {"11"}, None, None)
        self.assertEqual(target.address, "unix:/n")
        self.assertIsInstance(target.place, kitty.InWindow)
        self.assertEqual(target.place and target.place[0], "11")
        gone = kitty.plan_kitty_place(OpenOptions("dedicated", near), "unix:/k", set(), None, None)
        self.assertEqual((gone.address, type(gone.place).__name__), ("unix:/k", "OsWindow"))


class TmuxHelpersTest(unittest.TestCase):
    def test_a_session_that_is_not_the_agents_session_is_not_reused(self) -> None:
        target = tmux.plan_tmux_target(tmux.parse_tmux_sessions("0 1700000000 $0 work\n"))
        self.assertEqual(target, tmux.NewSession("agents"))

    def test_open_args_pass_paths_through_env_without_e_and_clean_the_title(self) -> None:
        title, launcher, spec = "Cl#{pane_pid}aude;", "/d/agent-launch.sh", "/h/t.spec"
        tail = [
            "-n",
            "Cl {pane_pid}aude",
            "--",
            "/usr/bin/env",
            "IDE_AGENT_TABS_LAUNCHER=/d/agent-launch.sh",
            "IDE_AGENT_TABS_SPEC=/h/t.spec",
            *ARGV,
        ]
        self.assertEqual(
            tmux.tmux_open_args(tmux.InSession("$2", False), title, launcher, spec, ARGV),
            ["new-window", "-P", "-F", WINDOW_FORMAT, "-t", "$2:", *tail],
        )
        new_session = tmux.tmux_open_args(tmux.NewSession("agents"), title, launcher, spec, ARGV)
        self.assertEqual(new_session, ["new-session", "-d", "-s", "agents", "-P", "-F", WINDOW_FORMAT, *tail])
        self.assertNotIn("-e", new_session)
        with self.assertRaisesRegex(ValueError, "';'"):
            tmux.tmux_open_args(tmux.NewSession("agents"), title, launcher, "/h/a;b.spec", ARGV)
        self.assertEqual(tmux.tmux_title("#;"), "Agent")

    def test_after_target_runs_on_the_callers_server(self) -> None:
        args = tmux.tmux_open_args(tmux.After("@7", "/tmp/tmux-1/default"), "t", "/d/agent-launch.sh", "/h/t.spec", ARGV)
        self.assertEqual(args[:9], ["-S", "/tmp/tmux-1/default", "new-window", "-a", "-t", "@7", "-P", "-F", WINDOW_FORMAT])

    def test_a_background_window_gets_d_only_when_focus_is_false(self) -> None:
        def open_args(target: tmux.TmuxTarget, focus: bool | None) -> list[str]:
            return tmux.tmux_open_args(target, "t", "/d/agent-launch.sh", "/h/t.spec", ARGV, focus)

        in_session, after = tmux.InSession("$2", False), tmux.After("@7", "/s")
        self.assertEqual(open_args(in_session, False)[:2], ["new-window", "-d"])
        self.assertEqual(open_args(after, False)[2:5], ["new-window", "-d", "-a"])
        for focus in (True, None):
            self.assertNotIn("-d", open_args(in_session, focus))
            self.assertNotIn("-d", open_args(after, focus))
        self.assertEqual(open_args(tmux.NewSession("agents"), True)[:2], ["new-session", "-d"])

    def test_window_records_hold_the_window_id_server_pid_and_socket(self) -> None:
        self.assertEqual(
            tmux.parse_tmux_window("@12 $2 4242 /tmp/tmux-501/my default\n"),
            tmux.TmuxWindow("@12", "$2", 4242, "/tmp/tmux-501/my default"),
        )
        with self.assertRaises(RuntimeError):
            tmux.parse_tmux_window("12 $2 4242 /tmp/s")
        listing = tmux.parse_tmux_window_list("4242 @1\n4242 @12\n\n")
        self.assertEqual((listing.server_pid, listing.windows), (4242, {"@1", "@12"}))

    def test_no_server_errors_are_told_apart_from_other_failures(self) -> None:
        self.assertTrue(tmux.is_no_server_error("no server running on /tmp/tmux-501/default"))
        self.assertTrue(tmux.is_no_server_error("error connecting to /tmp/tmux-501/default (No such file or directory)"))
        self.assertFalse(tmux.is_no_server_error("unknown flag -e"))

    def test_input_refuses_text_that_is_not_one_short_line_or_ends_in_a_semicolon(self) -> None:
        for bad in BAD_LINES:
            with self.subTest(text=bad), self.assertRaisesRegex(ValueError, "one line"):
                tmux.tmux_input_args("/s", "@1", bad)
        self.assertEqual(
            tmux.tmux_input_args("/tmp/tmux-1000/default", "@12", LINE),
            (
                ["-S", "/tmp/tmux-1000/default", "send-keys", "-t", "@12", "-l", "--", LINE],
                ["-S", "/tmp/tmux-1000/default", "send-keys", "-t", "@12", "Enter"],
            ),
        )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import posixpath
import re
import unittest
from typing import Any

from ide_agent_tabs.processes import RunResult
from ide_agent_tabs.spec import LaunchSpec, posix_spec
from ide_agent_tabs.terminals import TERMINAL_DRIVERS, default_terminal_name, iterm2
from ide_agent_tabs.terminals.driver import OpenOptions, TerminalContext, TerminalTab

GUID = "4B5E6F70-1A2B-4C3D-8E9F-0123456789AB"
HOME = "/Users/u/.ide-agent-tabs"
CTX = TerminalContext(HOME, "/p/dist/launch", "", {"SHELL": "/bin/zsh"})
SPEC = LaunchSpec("tab-1", "claude", "/Users/u/my app", "claude", ["--x"], {"FOO": "bar"}, "hi")
SPEC_FILE = posixpath.join(HOME, "launch", "tab-1.spec")
WAKE_LINE = "Agent Tabs: new message from codex 01234567. Call read_messages."
TAB: TerminalTab = {"id": "tab-1", "terminal": "iterm2", "agent": "claude", "path": "/w", "createdAt": 0, "terminalId": GUID}
CALLER: TerminalTab = {**TAB, "id": "caller", "terminalId": "CALLER-GUID"}


def ok(stdout: str) -> RunResult:
    return RunResult(0, stdout, "")


def fail(stderr: str) -> RunResult:
    return RunResult(1, "", stderr)


class Fake:
    def __init__(self, answers: list[RunResult], **overrides: Any) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[str, list[str]]] = []
        self.written: dict[str, bytes] = {}
        self.removed: list[str] = []
        deps: dict[str, Any] = {
            "platform": "darwin",
            "find_app": lambda: "/Applications/iTerm.app",
            "osascript": self.osascript,
            "write_spec": self.written.__setitem__,
            "remove_spec": self.removed.append,
        }
        self.driver = iterm2.create_iterm2(**{**deps, **overrides})

    def osascript(self, script: str, args: list[str]) -> RunResult:
        self.calls.append((script, args))
        if not self.answers:
            raise AssertionError("unexpected osascript call")
        return self.answers.pop(0)


class WindowStore:
    def __init__(self, remembered: dict[str, str] | None = None) -> None:
        self.remembered = remembered
        self.saved: list[dict[str, str]] = []

    def read_window(self, home: str) -> dict[str, str] | None:
        return self.remembered

    def remember_window(self, home: str, window: dict[str, str]) -> None:
        self.saved.append(window)
        self.remembered = window


class Iterm2RegistrationTest(unittest.TestCase):
    def test_driver_is_registered_with_tab_capabilities_and_comes_after_ghostty_on_macos(self) -> None:
        self.assertIn(iterm2.iterm2, TERMINAL_DRIVERS)
        self.assertEqual(iterm2.iterm2.capabilities, {"open": "tab", "list": "yes", "close": "yes"})
        self.assertEqual(default_terminal_name("darwin", ["tmux", "iterm2", "kitty"]), "iterm2")
        self.assertEqual(default_terminal_name("darwin", ["tmux", "iterm2", "ghostty"]), "ghostty")
        self.assertIsNone(default_terminal_name("linux", ["iterm2"]))
        self.assertEqual(iterm2.iterm2_locations("/Users/u"), ["/Applications/iTerm.app", "/Users/u/Applications/iTerm.app"])

    def test_available_only_on_macos_with_the_app_installed(self) -> None:
        self.assertTrue(Fake([]).driver.available(CTX))
        self.assertFalse(Fake([], platform="linux").driver.available(CTX))
        self.assertFalse(Fake([], find_app=lambda: None).driver.available(CTX))

    def test_open_refuses_to_run_off_macos(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "macOS only"):
            Fake([], platform="win32").driver.open(CTX, SPEC, "t")


class Iterm2OpenTest(unittest.TestCase):
    def test_open_writes_the_spec_and_sends_a_single_quoted_argv_mode_command(self) -> None:
        fake = Fake([ok(f"{GUID}\n")])
        tab = fake.driver.open(CTX, SPEC, "Claude\nCode")
        self.assertEqual(
            {k: v for k, v in tab.items() if k != "createdAt"},
            {"id": "tab-1", "terminal": "iterm2", "agent": "claude", "path": "/Users/u/my app", "terminalId": GUID},
        )
        command = (
            "'/bin/zsh' '-l' '-i' '-c' 'IDE_AGENT_TABS_SPEC=$2; export IDE_AGENT_TABS_SPEC; . \"$1\"; exec /bin/zsh -l -i' "
            f"'agent-tabs' '/p/dist/launch/agent-launch.sh' '{SPEC_FILE}'"
        )
        self.assertEqual(fake.calls, [(iterm2.OPEN_SCRIPT, [command, "Claude Code"])])
        self.assertEqual(fake.written, {SPEC_FILE: posix_spec(SPEC)})
        self.assertEqual(fake.removed, [])

    def test_open_uses_the_fish_launcher_for_a_fish_login_shell(self) -> None:
        fake = Fake([ok(GUID)])
        fake.driver.open(CTX._replace(env={"SHELL": "/opt/homebrew/bin/fish"}), SPEC, "t")
        self.assertEqual(
            fake.calls[0][1][0],
            "'/opt/homebrew/bin/fish' '-l' '-i' '-c' 'set -gx IDE_AGENT_TABS_SPEC $argv[2]; source \"$argv[1]\"; "
            "exec /opt/homebrew/bin/fish -l -i' '/p/dist/launch/agent-launch.fish' "
            f"'{SPEC_FILE}'",
        )

    def test_every_value_reaches_osascript_as_argv_and_never_in_the_script_source(self) -> None:
        hostile = 'x" & (do shell script "id") & " '
        fake = Fake([ok(GUID), ok("sent\n"), ok("closed\n")])
        fake.driver.open(CTX, SPEC._replace(cwd=f"/w/{hostile}", prompt=hostile, args=[hostile]), hostile)
        fake.driver.input(CTX, TAB, WAKE_LINE)
        fake.driver.close(CTX, TAB)
        self.assertEqual([script for script, _ in fake.calls], [iterm2.OPEN_SCRIPT, iterm2.INPUT_SCRIPT, iterm2.CLOSE_SCRIPT])
        for script in (iterm2.OPEN_SCRIPT, iterm2.LIST_SCRIPT, iterm2.INPUT_SCRIPT, iterm2.CLOSE_SCRIPT):
            self.assertTrue(script.startswith("on run argv\n"))
            self.assertNotIn("do shell script", script)
            self.assertNotIn(GUID, script)
        self.assertNotIn("do shell script", fake.calls[0][1][0])
        self.assertEqual(fake.calls[1][1], [GUID, WAKE_LINE])
        self.assertEqual(fake.calls[2][1], [GUID])

    def test_open_refuses_launcher_and_spec_paths_with_a_quote_backslash_dollar_or_control_character(self) -> None:
        for bad in ["/Users/o'brien", "/Users/a\\(id)", "/Users/$$USER$$", "/Users/a\nb"]:
            for ctx in (CTX._replace(home=bad), CTX._replace(scripts_dir=bad)):
                with self.subTest(path=bad):
                    fake = Fake([])
                    with self.assertRaisesRegex(ValueError, "iTerm2 can't start a path"):
                        fake.driver.open(ctx, SPEC, "t")
                    self.assertEqual(fake.calls, [])
                    self.assertEqual(fake.written, {})

    def test_command_words_refuse_a_quote_or_backslash_and_keep_dollar_and_double_quote(self) -> None:
        with self.assertRaisesRegex(ValueError, "iTerm2 can't run a command word"):
            iterm2.iterm2_command(["it's"])
        with self.assertRaisesRegex(ValueError, "iTerm2 can't run a command word"):
            iterm2.iterm2_command(["a\\b"])
        self.assertEqual(iterm2.iterm2_command(["/bin/zsh", "a b", '$1"']), "'/bin/zsh' 'a b' '$1\"'")

    def test_a_folder_with_quotes_never_reaches_the_argv_because_the_launcher_changes_to_it(self) -> None:
        fake = Fake([ok(GUID)])
        fake.driver.open(CTX, SPEC._replace(cwd="/Users/u/o'brien\\x"), "t")
        self.assertNotIn("brien", " ".join(fake.calls[0][1]))


class Iterm2PlacementTest(unittest.TestCase):
    def test_dedicated_mode_opens_a_window_remembers_its_id_and_targets_it_next_time(self) -> None:
        store = WindowStore()
        fake = Fake(
            [ok(f"{GUID}\n41\n"), ok(f"{GUID}\n41\n"), ok(f"{GUID}\n52\n")],
            read_window=store.read_window,
            remember_window=store.remember_window,
        )
        options = OpenOptions("dedicated")
        first = fake.driver.open(CTX, SPEC, "t", options)
        self.assertEqual(fake.calls[0][1][2:], ["dedicated", ""])
        self.assertEqual(first["window"], "41")
        self.assertEqual(store.saved, [{"id": "41"}])
        fake.driver.open(CTX, SPEC._replace(id="tab-2"), "t", options)
        self.assertEqual(fake.calls[1][1][2:], ["dedicated", "41"])
        self.assertEqual(len(store.saved), 1, "the same window is not written again")
        fake.driver.open(CTX, SPEC._replace(id="tab-3"), "t", options)
        self.assertEqual(store.saved[-1], {"id": "52"}, "a closed window is made again and remembered")

    def test_a_session_placement_and_the_last_mode_read_no_remembered_window(self) -> None:
        def no_read(home: str) -> dict[str, str] | None:
            raise AssertionError("this mode reads no window")

        fake = Fake([ok(GUID), ok(GUID)], read_window=no_read)
        fake.driver.open(CTX, SPEC, "t", OpenOptions("dedicated", CALLER))
        self.assertEqual(fake.calls[0][1][2:], ["session", "CALLER-GUID"])
        fake.driver.open(CTX, SPEC._replace(id="tab-2"), "t", OpenOptions("last"))
        self.assertEqual(len(fake.calls[1][1]), 2)

    def test_placement_ignores_a_caller_in_another_terminal_and_the_answer_may_lack_a_window(self) -> None:
        self.assertEqual(iterm2.iterm2_placement(OpenOptions("last", {**CALLER, "terminal": "kitty"}), None), [])
        self.assertEqual(iterm2.parse_open_answer(f"{GUID}\nmissing value\n"), iterm2.OpenAnswer(GUID, None))
        self.assertEqual(iterm2.parse_open_answer(f"{GUID}\n7\n"), iterm2.OpenAnswer(GUID, "7"))

    def test_focus_false_adds_the_background_marker_to_every_placement_and_focus_true_adds_nothing(self) -> None:
        fake = Fake([ok(GUID), ok(GUID), ok(GUID)])
        fake.driver.open(CTX, SPEC, "t", OpenOptions("last", None, False))
        self.assertEqual(fake.calls[0][1][2:], ["last", "", "background"])
        fake.driver.open(CTX, SPEC._replace(id="tab-2"), "t", OpenOptions("dedicated", CALLER, False))
        self.assertEqual(fake.calls[1][1][2:], ["session", "CALLER-GUID", "background"])
        fake.driver.open(CTX, SPEC._replace(id="tab-3"), "t", OpenOptions("last", None, True))
        self.assertEqual(len(fake.calls[2][1]), 2)


class Iterm2SessionTest(unittest.TestCase):
    def test_alive_lists_sessions_by_guid_and_makes_no_call_without_a_tracked_tab(self) -> None:
        other = {**TAB, "id": "tab-2", "terminalId": "DEAD"}
        untracked = {k: v for k, v in TAB.items() if k != "terminalId"} | {"id": "tab-3"}
        fake = Fake([ok(f"AAA\n{GUID}\nBBB\n")])
        self.assertEqual(fake.driver.alive(CTX, [TAB, other, untracked]), {"tab-1"})
        self.assertEqual(fake.calls, [(iterm2.LIST_SCRIPT, [])])
        silent = Fake([])
        self.assertEqual(silent.driver.alive(CTX, []), set())
        self.assertEqual(silent.driver.alive(CTX, [untracked]), set())

    def test_close_sends_the_guid_and_reports_a_session_that_is_gone(self) -> None:
        fake = Fake([ok("closed\n"), ok("missing\n")])
        fake.driver.close(CTX, TAB)
        with self.assertRaisesRegex(RuntimeError, "no session .*already closed"):
            fake.driver.close(CTX, TAB)
        self.assertEqual([args for _, args in fake.calls], [[GUID], [GUID]])
        with self.assertRaisesRegex(RuntimeError, "no iTerm2 session id"):
            Fake([]).driver.close(CTX, {k: v for k, v in TAB.items() if k != "terminalId"})

    def test_input_reports_a_missing_session_and_refuses_text_that_is_not_one_short_line(self) -> None:
        fake = Fake([ok("missing")])
        with self.assertRaisesRegex(RuntimeError, "no session"):
            fake.driver.input(CTX, TAB, "hi")
        self.assertEqual(len(fake.calls), 1)
        for bad in ["two\nlines", "x" * 501, "esc\x1b[2J"]:
            with self.subTest(text=bad):
                refused = Fake([])
                with self.assertRaisesRegex(ValueError, "one line"):
                    refused.driver.input(CTX, TAB, bad)
                self.assertEqual(refused.calls, [])
        with self.assertRaisesRegex(RuntimeError, "no iTerm2 session id"):
            Fake([]).driver.input(CTX, {k: v for k, v in TAB.items() if k != "terminalId"}, "hi")


class Iterm2FailureTest(unittest.TestCase):
    def test_denied_automation_names_the_setting_removes_the_spec_and_blocks_the_driver(self) -> None:
        fake = Fake([fail("execution error: Not authorized to send Apple events to iTerm. (-1743)")])
        with self.assertRaisesRegex(RuntimeError, re.escape("System Settings > Privacy & Security > Automation")):
            fake.driver.open(CTX, SPEC, "t")
        self.assertEqual(fake.removed, [SPEC_FILE])
        self.assertFalse(fake.driver.available(CTX))
        written = dict(fake.written)
        with self.assertRaisesRegex(RuntimeError, "denied permission"):
            fake.driver.open(CTX, SPEC, "t")
        self.assertEqual(fake.written, written)
        self.assertEqual(len(fake.calls), 1)
        available = [*(["iterm2"] if fake.driver.available(CTX) else []), "kitty", "tmux"]
        self.assertEqual(default_terminal_name("darwin", available), "kitty")

    def test_an_app_macos_cannot_find_is_reported_as_not_installed_and_blocks_the_driver(self) -> None:
        fake = Fake([fail('execution error: Can\'t find application id "com.googlecode.iterm2". (-10814)')])
        with self.assertRaisesRegex(RuntimeError, "iTerm2 is not installed"):
            fake.driver.open(CTX, SPEC, "t")
        self.assertEqual(fake.removed, [SPEC_FILE])
        self.assertFalse(fake.driver.available(CTX))

    def test_any_other_osascript_failure_is_reported_and_keeps_the_driver_available(self) -> None:
        fake = Fake([fail("execution error: iTerm got an error: Can't get current window. (-1728)"), ok("garbage answer with spaces")])
        with self.assertRaisesRegex(RuntimeError, "^osascript failed: execution error: iTerm got an error"):
            fake.driver.open(CTX, SPEC, "t")
        self.assertTrue(fake.driver.available(CTX))
        with self.assertRaisesRegex(RuntimeError, "unexpected answer from iTerm2"):
            fake.driver.open(CTX, SPEC, "t")
        self.assertEqual(fake.removed, [SPEC_FILE, SPEC_FILE])
        self.assertEqual(iterm2.parse_session_id(f" {GUID}\n"), GUID)


if __name__ == "__main__":
    unittest.main()

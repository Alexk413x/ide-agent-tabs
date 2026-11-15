from __future__ import annotations

import os
import unittest
from typing import Any
from unittest import mock

from ide_agent_tabs.net import window
from ide_agent_tabs.net.login_task import Environment
from ide_agent_tabs.net.window import WindowError

INTERPRETER = r"C:\Users\Me\AppData\Local\Programs\Python\Python312\python.exe"
LAUNCH = r"C:\Users\Me\plugin\launch\agent_tabs.py"
INVITATION = "atp1-invite:eyJ2IjoxLCJuYW1lIjoiRGVzayJ9-_x"
HOME = r"C:\Users\Me"


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.notices: list[str] = []

    def popen(self, command: Any, **options: Any) -> object:
        self.calls.append((command, options))
        return object()

    def notify(self, text: str) -> bool:
        self.notices.append(text)
        return True


def environment(platform: str = "win32", interpreter: str = INTERPRETER, launch: str = LAUNCH) -> Environment:
    return Environment(platform=platform, interpreter=interpreter, launch_script=launch, user_home=HOME)


def open_window(
    recorder: Recorder,
    subcommand: list[str] | None = None,
    invitation: str | None = None,
    caller: str = "Claude Code",
    env: Environment | None = None,
    **options: Any,
) -> str:
    return window.open_command_window(
        ["net", "pair"] if subcommand is None else subcommand,
        invitation,
        caller,
        env or environment(),
        popen=recorder.popen,
        notifier=recorder.notify,
        environ={"PATH": "x", "DISPLAY": ":0", "CLAUDECODE": "1", "IDE_AGENT_TABS_ID": "s1", "USERPROFILE": HOME},
        **options,
    )


class WindowsWindowTest(unittest.TestCase):
    def test_a_new_console_runs_the_bare_command_from_an_argument_list(self) -> None:
        recorder = Recorder()
        message = open_window(recorder)
        self.assertEqual(message, "Continue in the window that opened.")
        command, options = recorder.calls[0]
        self.assertIsInstance(command, list)
        self.assertEqual(command[:4], [INTERPRETER, "-I", "-S", "-c"])
        self.assertEqual(command[4], window._WINDOWS_HOLD)  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(command[5:], [LAUNCH, "net", "pair"])
        self.assertEqual(options["creationflags"], window.CREATE_NEW_CONSOLE)
        self.assertEqual(window.CREATE_NEW_CONSOLE, 0x10)
        self.assertIs(options["close_fds"], True)
        self.assertIs(options["shell"], False)

    def test_no_handle_or_caller_state_is_passed(self) -> None:
        recorder = Recorder()
        open_window(recorder)
        _, options = recorder.calls[0]
        for name in ("stdin", "stdout", "stderr"):
            self.assertNotIn(name, options)
        self.assertEqual(options["cwd"], HOME)
        self.assertEqual(options["env"], {"PATH": "x", "DISPLAY": ":0", "USERPROFILE": HOME})

    def test_every_allowed_command_opens(self) -> None:
        for subcommand in (["net", "pair"], ["net", "pair", "--invite"], ["net", "trust"], ["net", "grant"], ["net", "lan", "on"]):
            recorder = Recorder()
            open_window(recorder, subcommand)
            self.assertEqual(recorder.calls[0][0][5:], [LAUNCH, *subcommand])

    def test_join_carries_the_validated_invitation_as_its_own_argument(self) -> None:
        recorder = Recorder()
        open_window(recorder, ["net", "pair", "--join"], INVITATION)
        self.assertEqual(recorder.calls[0][0][5:], [LAUNCH, "net", "pair", "--join", INVITATION])

    def test_commands_outside_the_allowlist_are_refused(self) -> None:
        for subcommand in (
            [],
            ["net"],
            ["net", "off"],
            ["net", "lan", "start"],
            ["net", "lan", "on", "--bind", "10.0.0.5"],
            ["net", "pair", "--peer", "10.0.0.5"],
            ["net", "pair", "--join", INVITATION],
            ["net", "pair", "--invite", "--join"],
            ["net", "revoke", "x"],
            ["net", "pair;calc"],
            ["Net", "pair"],
            ["jev", "choose"],
            ["-c", "import os"],
        ):
            recorder = Recorder()
            with self.assertRaises(WindowError, msg=repr(subcommand)):
                open_window(recorder, subcommand)
            self.assertEqual(recorder.calls, [])
            self.assertEqual(recorder.notices, [])

    def test_an_invitation_goes_only_with_join(self) -> None:
        recorder = Recorder()
        with self.assertRaises(WindowError):
            open_window(recorder, ["net", "pair"], INVITATION)
        with self.assertRaises(WindowError):
            open_window(recorder, ["net", "pair", "--join"], None)
        self.assertEqual(recorder.calls, [])

    def test_bad_invitations_are_refused_before_any_window_opens(self) -> None:
        for bad in (
            "",
            "atp1-invite:",
            "atp1-invite: abc",
            "atp1-invite:abc def",
            "atp1-invite:abc;calc",
            "atp1-invite:abc&calc",
            'atp1-invite:abc"',
            "atp1-invite:abc\n",
            "atp1-invite:abc=",
            "atp1-invite:abc+/",
            "atp2-invite:abc",
            "ATP1-INVITE:abc",
            "abc",
            "--evil",
            "atp1-invite:" + "a" * window.MAX_INVITATION_CHARS,
        ):
            recorder = Recorder()
            with self.assertRaises(WindowError, msg=repr(bad)):
                open_window(recorder, ["net", "pair", "--join"], bad)
            self.assertEqual(recorder.calls, [])

    def test_a_good_invitation_is_accepted(self) -> None:
        self.assertEqual(window.validate_invitation(INVITATION), INVITATION)
        self.assertEqual(window.validate_invitation("atp1-invite:A-Z_a-z0-9"), "atp1-invite:A-Z_a-z0-9")

    def test_the_notification_names_the_caller_and_the_command_but_not_the_invitation(self) -> None:
        recorder = Recorder()
        open_window(recorder, ["net", "pair", "--join"], INVITATION, caller="Codex")
        self.assertEqual(len(recorder.notices), 1)
        self.assertIn("Codex", recorder.notices[0])
        self.assertIn("net pair --join", recorder.notices[0])
        self.assertNotIn(INVITATION, recorder.notices[0])
        self.assertNotIn("eyJ", recorder.notices[0])

    def test_the_caller_name_is_cleaned(self) -> None:
        recorder = Recorder()
        open_window(recorder, caller="evil\nname; $(calc) " + "x" * 100)
        notice = recorder.notices[0]
        self.assertNotIn("\n", notice)
        self.assertNotIn("$", notice)
        self.assertNotIn(";", notice)
        self.assertLess(len(notice), 200)
        open_window(recorder, caller="\n\t")
        self.assertIn(window.FALLBACK_CALLER, recorder.notices[1])

    def test_a_failed_launch_is_reported_and_does_not_notify(self) -> None:
        recorder = Recorder()

        def broken(*_args: Any, **_options: Any) -> object:
            raise OSError("no console")

        with self.assertRaises(WindowError):
            window.open_command_window(["net", "pair"], None, "x", environment(), popen=broken, notifier=recorder.notify, environ={})
        self.assertEqual(recorder.notices, [])

    def test_relative_or_quoted_paths_are_refused(self) -> None:
        for env in (
            environment(interpreter="python.exe"),
            environment(interpreter='C:\\a"b\\python.exe'),
            environment(launch="agent_tabs.py"),
        ):
            recorder = Recorder()
            with self.assertRaises(WindowError):
                open_window(recorder, env=env)
            self.assertEqual(recorder.calls, [])

    def test_pythonw_is_swapped_for_python_so_the_console_shows_output(self) -> None:
        recorder = Recorder()
        pythonw = INTERPRETER.replace("python.exe", "pythonw.exe")
        with mock.patch.object(window.os.path, "isfile", return_value=True):
            open_window(recorder, env=environment(interpreter=pythonw))
        self.assertEqual(recorder.calls[0][0][0], INTERPRETER)

    def test_the_holding_script_runs_the_launcher_and_waits(self) -> None:
        script = window._WINDOWS_HOLD  # pyright: ignore[reportPrivateUsage]
        compile(script, "<hold>", "exec")
        self.assertIn("runpy.run_path(sys.argv[0]", script)
        self.assertIn("input(", script)
        self.assertIn("finally", script)


class PosixWindowTest(unittest.TestCase):
    def test_macos_opens_terminal_through_osascript_with_the_command_as_data(self) -> None:
        recorder = Recorder()
        env = environment("darwin", "/usr/bin/python3", "/opt/plugin/launch/agent_tabs.py")
        with mock.patch.object(window.os.path, "isfile", return_value=True):
            message = open_window(recorder, ["net", "pair", "--join"], INVITATION, env=env)
        self.assertEqual(message, window.CONTINUE_MESSAGE)
        command, options = recorder.calls[0]
        self.assertEqual(command[0], "/usr/bin/osascript")
        self.assertIn("do script (item 1 of argv)", command)
        line = command[-1]
        self.assertIn("/usr/bin/python3 -I -S /opt/plugin/launch/agent_tabs.py net pair --join " + INVITATION, line)
        self.assertIs(options["shell"], False)
        self.assertIs(options["close_fds"], True)
        self.assertIs(options["start_new_session"], True)

    def test_linux_uses_the_first_terminal_it_finds(self) -> None:
        recorder = Recorder()
        env = environment("linux", "/usr/bin/python3", "/opt/plugin/launch/agent_tabs.py")
        found = {"xterm": "/usr/bin/xterm", "konsole": "/usr/bin/konsole"}
        message = open_window(recorder, ["net", "trust"], env=env, which=found.get)
        self.assertEqual(message, window.CONTINUE_MESSAGE)
        command, options = recorder.calls[0]
        self.assertEqual(command[:2], ["/usr/bin/konsole", "-e"])
        self.assertEqual(command[-6:], ["/usr/bin/python3", "-I", "-S", "/opt/plugin/launch/agent_tabs.py", "net", "trust"])
        self.assertEqual(options["stdin"], __import__("subprocess").DEVNULL)

    def test_without_a_terminal_it_says_what_to_run(self) -> None:
        recorder = Recorder()
        env = environment("linux", "/usr/bin/python3", "/opt/plugin/launch/agent_tabs.py")
        message = open_window(recorder, ["net", "pair", "--invite"], env=env, which=lambda _name: None)
        self.assertEqual(recorder.calls, [])
        self.assertEqual(recorder.notices, [])
        self.assertIn("Run this in a terminal: agent-tabs net pair --invite", message)

    def test_without_a_display_it_says_what_to_run(self) -> None:
        recorder = Recorder()
        env = environment("linux", "/usr/bin/python3", "/opt/plugin/launch/agent_tabs.py")
        message = window.open_command_window(
            ["net", "pair"], None, "x", env, popen=recorder.popen, notifier=recorder.notify, environ={}, which=lambda _n: "/usr/bin/xterm"
        )
        self.assertEqual(recorder.calls, [])
        self.assertIn("Run this in a terminal", message)


class SessionEnvironmentTest(unittest.TestCase):
    def test_session_variables_are_dropped_from_the_window_environment(self) -> None:
        recorder = Recorder()
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "abc", "PATH": "p"}):
            window.open_command_window(["net", "pair"], None, "x", environment(), popen=recorder.popen, notifier=recorder.notify)
        variables = recorder.calls[0][1]["env"]
        self.assertNotIn("CLAUDE_CODE_SESSION_ID", variables)
        self.assertEqual(variables["PATH"], "p")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from ide_agent_tabs.net import listener, selfupdate
from ide_agent_tabs.net.listener import Listener, Options
from ide_agent_tabs.net.pairing import PairResult, PeerInfo
from ide_agent_tabs.net.selfupdate import Install, JoinUpdater, UpdateCommand
from support import temp_home

DEVICE = "AAAAAAA-BBBBBBB-CCCCCCC-DDDDDDD-EEEEEEE-FFFFFFF-GGGGGGG-HHHHHHH"
OTHER_DEVICE = "HHHHHHH-GGGGGGG-FFFFFFF-EEEEEEE-DDDDDDD-CCCCCCC-BBBBBBB-AAAAAAA"
HOUR_MS = 3_600_000
NOW = 1_700_000_000_000
COMMAND = UpdateCommand(
    (sys.executable, "plugin", "update", "ide-agent-tabs@market"),
    "tool plugin marketplace update market && tool plugin update ide-agent-tabs@market",
    (sys.executable, "plugin", "marketplace", "update", "market"),
)


class Completed:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


class Runner:
    def __init__(self, *results: Completed | BaseException) -> None:
        self.results = list(results)
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> Completed:
        self.calls.append((argv, kwargs))
        result = self.results.pop(0) if self.results else Completed()
        if isinstance(result, BaseException):
            raise result
        return result


def claude_install(test: unittest.TestCase, marketplace: str = "alexk413x", name: str = "ide-agent-tabs", *, record: bool = True) -> str:
    plugins = os.path.join(temp_home(test), ".claude", "plugins")
    root = os.path.join(plugins, "cache", marketplace, name, "0.14.3")
    os.makedirs(root)
    if record:
        Path(plugins, "installed_plugins.json").write_text(
            json.dumps({"version": 2, "plugins": {f"{name}@{marketplace}": [{"scope": "user", "installPath": root}]}}), encoding="utf-8"
        )
    return root


class UpdateCommandTest(unittest.TestCase):
    def test_a_claude_code_install_names_its_own_marketplace(self) -> None:
        root = claude_install(self)
        for spelling in (root, root + os.sep):
            command = selfupdate.update_command(spelling)
            assert command is not None
            self.assertEqual(command.argv, ("claude", "plugin", "update", "ide-agent-tabs@alexk413x"), spelling)
            self.assertEqual(command.refresh, ("claude", "plugin", "marketplace", "update", "alexk413x"))
            self.assertEqual(command.display, "claude plugin marketplace update alexk413x && claude plugin update ide-agent-tabs@alexk413x")

    def test_a_cache_folder_that_claude_code_does_not_record_has_no_command(self) -> None:
        self.assertIsNone(selfupdate.update_command(claude_install(self, record=False)))
        root = claude_install(self)
        Path(root, "..", "..", "..", "..", "installed_plugins.json").resolve().write_text(
            '{"plugins": {"ide-agent-tabs@other": []}}', encoding="utf-8"
        )
        self.assertIsNone(selfupdate.update_command(root))
        Path(root, "..", "..", "..", "..", "installed_plugins.json").resolve().write_text("not json", encoding="utf-8")
        self.assertIsNone(selfupdate.update_command(root))

    def test_a_copilot_install_uses_the_copilot_update_command(self) -> None:
        command = selfupdate.update_command("/home/me/.copilot/installed-plugins/alexk413x/ide-agent-tabs")
        assert command is not None
        self.assertEqual(command.argv, ("copilot", "plugin", "update", "ide-agent-tabs@alexk413x"))
        self.assertEqual(command.refresh, ("copilot", "plugin", "marketplace", "update", "alexk413x"))

    def test_other_places_have_no_command(self) -> None:
        for root in (
            "/work/ide-agent-tabs/claude-plugin",
            "/home/me/.ide-agent-tabs/mcp/py/0.14.3-0123456789ab",
            "/home/me/.claude/plugins/cache/alexk413x/other-plugin/0.14.3",
            "/home/me/.claude/cache/alexk413x/ide-agent-tabs/0.14.3",
            "/home/me/.claude/plugins/cache/alexk413x/ide-agent-tabs/0.14.3",
            "ide-agent-tabs/0.14.3",
            "",
        ):
            self.assertIsNone(selfupdate.update_command(root), root)

    def test_a_marketplace_name_outside_the_allowed_characters_has_no_command(self) -> None:
        for name in ("bad name", "a;b", "a&&b", "a$b", "x" * 65, "a`b", "a'b"):
            self.assertIsNone(selfupdate.update_command(claude_install(self, name)), name)

    def test_the_manual_text_gives_the_command_or_names_the_client(self) -> None:
        self.assertEqual(selfupdate.manual_text(COMMAND), f"To update ide-agent-tabs, run: {COMMAND.display}")
        self.assertEqual(selfupdate.manual_text(None), "Update ide-agent-tabs with the client that installed it.")


class RunUpdateTest(unittest.TestCase):
    def test_it_runs_the_refresh_then_the_update_without_a_shell(self) -> None:
        runner = Runner(Completed(0, "catalog updated\n"), Completed(0, "updated\nrestart required\n"))
        outcome = selfupdate.run_update(COMMAND, runner)
        self.assertEqual((outcome.ok, outcome.summary), (True, "updated | restart required"))
        self.assertEqual([call[0][1:] for call in runner.calls], [list(COMMAND.refresh or ())[1:], list(COMMAND.argv)[1:]])
        for argv, kwargs in runner.calls:
            self.assertTrue(os.path.isabs(argv[0]))
            self.assertIs(kwargs["shell"], False)
            self.assertIs(kwargs["capture_output"], True)
            self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
            self.assertEqual(kwargs["timeout"], selfupdate.UPDATE_TIMEOUT_S)

    def test_a_failing_refresh_skips_the_update(self) -> None:
        runner = Runner(Completed(1, "", "no network"))
        outcome = selfupdate.run_update(COMMAND, runner)
        self.assertEqual((outcome.ok, outcome.summary), (False, "exit 1: no network"))
        self.assertEqual(len(runner.calls), 1)

    def test_a_failing_update_reports_the_tail_of_the_output(self) -> None:
        lines = "\n".join(f"line {number}" for number in range(10))
        outcome = selfupdate.run_update(COMMAND, Runner(Completed(0), Completed(3, lines, "")))
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.summary, "exit 3: line 7 | line 8 | line 9")

    def test_the_summary_is_short_and_printable(self) -> None:
        noisy = "x" * 1000 + "\x1b[31m\x00end"
        outcome = selfupdate.run_update(COMMAND, Runner(Completed(0), Completed(1, noisy, "")))
        self.assertLessEqual(len(outcome.summary), selfupdate.SUMMARY_CHARS + len("exit 1: "))
        self.assertTrue(outcome.summary.isprintable())

    def test_a_program_that_is_not_installed_is_not_run(self) -> None:
        missing = UpdateCommand(("definitely-not-installed-xyz", "plugin", "update", "x"), "x")
        runner = Runner()
        outcome = selfupdate.run_update(missing, runner)
        self.assertEqual((outcome.ok, runner.calls), (False, []))
        self.assertIn("definitely-not-installed-xyz was not found on PATH", outcome.summary)

    def test_it_never_raises_for_a_failed_run(self) -> None:
        for error in (
            OSError("denied"),
            subprocess.TimeoutExpired("tool", 300),
            subprocess.SubprocessError("odd"),
            ValueError("embedded null"),
        ):
            outcome = selfupdate.run_update(COMMAND, Runner(error))
            self.assertFalse(outcome.ok, repr(error))
            self.assertTrue(outcome.summary)

    def test_no_command_is_a_failure_that_runs_nothing(self) -> None:
        runner = Runner()
        outcome = selfupdate.run_update(None, runner)
        self.assertEqual((outcome.ok, runner.calls), (False, []))


class AttemptTest(unittest.TestCase):
    def setUp(self) -> None:
        self.home = temp_home(self)

    def path(self) -> Path:
        return Path(self.home, "net", selfupdate.ATTEMPTS_FILE)

    def test_one_attempt_per_peer_per_hour(self) -> None:
        self.assertTrue(selfupdate.attempt_allowed(self.home, DEVICE, NOW))
        self.assertFalse(selfupdate.attempt_allowed(self.home, DEVICE, NOW + 1))
        self.assertFalse(selfupdate.attempt_allowed(self.home, DEVICE, NOW + HOUR_MS - 1))
        self.assertTrue(selfupdate.attempt_allowed(self.home, DEVICE, NOW + HOUR_MS))

    def test_another_peer_has_its_own_hour(self) -> None:
        self.assertTrue(selfupdate.attempt_allowed(self.home, DEVICE, NOW))
        self.assertTrue(selfupdate.attempt_allowed(self.home, OTHER_DEVICE, NOW + 1))
        self.assertFalse(selfupdate.attempt_allowed(self.home, OTHER_DEVICE, NOW + 2))

    def test_the_total_per_hour_is_capped_for_peers_with_new_device_ids(self) -> None:
        for number in range(selfupdate.MAX_ATTEMPTS_PER_HOUR):
            self.assertTrue(selfupdate.attempt_allowed(self.home, f"device-{number}", NOW + number))
        self.assertFalse(selfupdate.attempt_allowed(self.home, "device-new", NOW + 100))
        self.assertTrue(selfupdate.attempt_allowed(self.home, "device-new", NOW + HOUR_MS + 100))

    def test_the_record_survives_a_new_call_and_holds_no_secret(self) -> None:
        selfupdate.attempt_allowed(self.home, DEVICE, NOW)
        self.assertEqual(json.loads(self.path().read_text(encoding="utf-8")), {"attempts": [{"deviceId": DEVICE, "atMs": NOW}]})

    def test_a_damaged_file_starts_a_new_record(self) -> None:
        self.assertTrue(selfupdate.attempt_allowed(self.home, DEVICE, NOW))
        for text in ("not json", "[]", '{"attempts": 5}', '{"attempts": [{"deviceId": 1}]}'):
            self.path().write_text(text, encoding="utf-8")
            self.assertTrue(selfupdate.attempt_allowed(self.home, DEVICE, NOW), text)

    def test_a_record_dated_in_the_future_does_not_block(self) -> None:
        self.assertTrue(selfupdate.attempt_allowed(self.home, DEVICE, NOW + 10 * HOUR_MS))
        self.assertTrue(selfupdate.attempt_allowed(self.home, DEVICE, NOW))

    def test_a_file_that_cannot_be_written_allows_nothing(self) -> None:
        with mock.patch.object(selfupdate, "write_atomically", side_effect=OSError("disk full")):
            self.assertFalse(selfupdate.attempt_allowed(self.home, DEVICE, NOW))


class AutoUpdateEnabledTest(unittest.TestCase):
    def setUp(self) -> None:
        self.home = temp_home(self)

    def write(self, text: str) -> None:
        Path(self.home, "config.json").write_text(text, encoding="utf-8")

    def test_it_defaults_to_on_without_a_file_a_section_or_the_key(self) -> None:
        self.assertTrue(selfupdate.auto_update_enabled(self.home))
        for text in ("{}", '{"network": {}}', '{"network": {"network": "lan"}}', '{"theme": "dark"}'):
            self.write(text)
            self.assertTrue(selfupdate.auto_update_enabled(self.home), text)

    def test_an_explicit_value_decides(self) -> None:
        self.write('{"network": {"autoUpdate": true}}')
        self.assertTrue(selfupdate.auto_update_enabled(self.home))
        self.write('{"network": {"autoUpdate": false}}')
        self.assertFalse(selfupdate.auto_update_enabled(self.home))

    def test_a_value_that_is_present_but_not_true_turns_it_off(self) -> None:
        for value in ('"false"', '"on"', "0", "1", "null", "[]"):
            self.write(f'{{"network": {{"autoUpdate": {value}}}}}')
            self.assertFalse(selfupdate.auto_update_enabled(self.home), value)

    def test_an_unreadable_config_turns_it_off(self) -> None:
        for text in ("not json", "[]", '{"network": 5}'):
            self.write(text)
            self.assertFalse(selfupdate.auto_update_enabled(self.home), text)


class InstallTest(unittest.TestCase):
    def make(self, cache: str, version: str, *, scripts: bool = True, manifest: str | None = None) -> str:
        root = os.path.join(cache, version)
        os.makedirs(os.path.join(root, ".claude-plugin"), exist_ok=True)
        os.makedirs(os.path.join(root, "mcp", "launch"), exist_ok=True)
        Path(root, ".claude-plugin", "plugin.json").write_text(manifest or json.dumps({"version": version}), encoding="utf-8")
        if scripts:
            for name in ("agent_tabs.py", "net_listener.py"):
                Path(root, "mcp", "launch", name).write_text("", encoding="utf-8")
        return root

    def test_the_newest_complete_install_beside_the_running_one_wins(self) -> None:
        running = claude_install(self, "market")
        cache = os.path.dirname(running)
        self.make(cache, "0.14.3")
        self.make(cache, "0.15.0")
        self.make(cache, "0.16.0", scripts=False)
        self.make(cache, "0.17.0", manifest="not json")
        self.make(cache, "0.9.0")
        newest = selfupdate.newest_install(running)
        assert newest is not None
        self.assertEqual((newest.version, newest.root), ("0.15.0", os.path.join(cache, "0.15.0")))
        self.assertEqual(newest.listener_script, os.path.join(cache, "0.15.0", "mcp", "launch", "net_listener.py"))
        self.assertEqual(newest.cli_script, os.path.join(cache, "0.15.0", "mcp", "launch", "agent_tabs.py"))

    def test_an_install_that_other_users_can_change_is_never_used(self) -> None:
        running = claude_install(self, "market")
        self.make(os.path.dirname(running), "0.15.0")
        checked: list[str] = []

        def problems(folder: str) -> list[str]:
            checked.append(folder)
            return ["every user can change it"]

        self.assertIsNone(selfupdate.newest_install(running, problems))
        self.assertEqual(checked, [os.path.join(os.path.dirname(running), "0.15.0", "mcp", "launch")])

    def test_a_copilot_install_is_read_in_place(self) -> None:
        root = self.make(os.path.join(temp_home(self), "installed-plugins", "market"), "ide-agent-tabs")
        Path(root, ".claude-plugin", "plugin.json").write_text('{"version": "0.15.0"}', encoding="utf-8")
        newest = selfupdate.newest_install(root)
        assert newest is not None
        self.assertEqual((newest.root, newest.version), (root, "0.15.0"))

    def test_a_checkout_has_no_install(self) -> None:
        self.assertIsNone(selfupdate.newest_install(os.path.join(temp_home(self), "claude-plugin")))

    def test_the_restart_runs_the_new_install_detached_with_this_home_and_port(self) -> None:
        install = Install(os.path.join(temp_home(self), "new"), "0.15.0")
        os.makedirs(os.path.dirname(install.cli_script))
        Path(install.cli_script).write_text("", encoding="utf-8")
        spawned: list[tuple[list[str], dict[str, str]]] = []
        with mock.patch.dict(os.environ, {"IDE_AGENT_TABS_SESSION": "x"}):
            ok = selfupdate.spawn_lan_start("/some/home", install, 47900, lambda argv, env, cwd: spawned.append((argv, env)))
        self.assertTrue(ok)
        ((argv, env),) = spawned
        self.assertEqual(argv, [sys.executable, "-I", "-S", install.cli_script, "net", "lan", "start"])
        self.assertEqual((env["IDE_AGENT_TABS_HOME"], env["CLAUDE_PLUGIN_OPTION_SERVER_PORT"]), ("/some/home", "47900"))

    def test_the_restart_reports_a_script_that_is_missing_or_cannot_start(self) -> None:
        install = Install(os.path.join(temp_home(self), "new"), "0.15.0")
        self.assertFalse(selfupdate.spawn_lan_start("/h", install, None, lambda *_args: None))
        os.makedirs(os.path.dirname(install.cli_script))
        Path(install.cli_script).write_text("", encoding="utf-8")

        def refuse(*_args: Any) -> None:
            raise OSError("no")

        self.assertFalse(selfupdate.spawn_lan_start("/h", install, None, refuse))


class Joined:
    def __init__(self, test: unittest.TestCase, **changes: Any) -> None:
        self.home = temp_home(test)
        self.log: list[str] = []
        self.events: list[str] = []
        self.posted: list[str] = []
        self.now = NOW
        self.runner = Runner()
        self.command: UpdateCommand | None = COMMAND
        self.install: Install | None = Install("/plugins/0.15.0", "0.15.0")
        self.spawn_ok = True
        values: dict[str, Any] = {
            "home": self.home,
            "own_version": "0.14.3",
            "log": self._log,
            "post": self._post,
            "server_port": 47828,
            "command": lambda: self.command,
            "fresh": lambda: self.install,
            "runner": self._run,
            "spawn": self._spawn,
            "clock": lambda: self.now,
            "background": lambda job: job(),
        }
        values.update(changes)
        self.updater = JoinUpdater(**values)

    def _log(self, text: str) -> None:
        self.events.append("log")
        self.log.append(text)

    def _post(self, home: str, text: str) -> int:
        self.events.append("post")
        self.posted.append(text)
        return 1

    def _run(self, argv: list[str], **kwargs: Any) -> Completed:
        self.events.append("run")
        return self.runner(argv, **kwargs)

    def _spawn(self, home: str, install: Install, port: int | None) -> bool:
        self.events.append(f"spawn {install.root} {port}")
        return self.spawn_ok


class JoinUpdaterTest(unittest.TestCase):
    def test_a_newer_dialer_runs_the_update_logs_it_notifies_and_restarts(self) -> None:
        joined = Joined(self)
        self.assertTrue(joined.updater.on_join(DEVICE, "0.15.0"))
        self.assertEqual(
            joined.log,
            [f"self-update: attempt peer={DEVICE} command={COMMAND.display}", "self-update: ok"],
        )
        self.assertEqual(len(joined.runner.calls), 2)
        (text,) = joined.posted
        for part in (
            "updated ide-agent-tabs to 0.15.0",
            "a machine on version 0.15.0",
            "The listener restarts on the new build",
            "keep the old skill text and tool list",
        ):
            self.assertIn(part, text)
        self.assertEqual(joined.events[-2:], ["post", "spawn /plugins/0.15.0 47828"])

    def test_an_older_equal_or_unversioned_dialer_does_nothing_and_uses_no_attempt(self) -> None:
        joined = Joined(self)
        for version in ("0.14.3", "0.14.2", "0.1.0", None):
            self.assertFalse(joined.updater.on_join(DEVICE, version), str(version))
        self.assertEqual((joined.events, joined.posted), ([], []))
        self.assertFalse(Path(joined.home, "net", selfupdate.ATTEMPTS_FILE).exists())
        self.assertTrue(joined.updater.on_join(DEVICE, "0.15.0"))

    def test_a_failed_update_logs_the_failure_and_sends_the_manual_command(self) -> None:
        joined = Joined(self)
        joined.runner = Runner(Completed(1, "", "network unreachable"))
        joined.updater.on_join(DEVICE, "0.15.0")
        self.assertEqual(joined.log[-1], "self-update: failed exit 1: network unreachable")
        (text,) = joined.posted
        self.assertIn("The unattended update failed (exit 1: network unreachable).", text)
        self.assertIn(f"To update ide-agent-tabs, run: {COMMAND.display}", text)
        self.assertFalse([event for event in joined.events if event.startswith("spawn")])

    def test_the_switch_off_sends_the_manual_command_and_runs_nothing(self) -> None:
        joined = Joined(self)
        Path(joined.home, "config.json").write_text('{"network": {"autoUpdate": false}}', encoding="utf-8")
        self.assertTrue(joined.updater.on_join(DEVICE, "0.15.0"))
        self.assertEqual((joined.runner.calls, joined.log), ([], []))
        (text,) = joined.posted
        self.assertIn("The unattended update is off", text)
        self.assertIn(f"To update ide-agent-tabs, run: {COMMAND.display}", text)

    def test_an_install_with_no_command_sends_the_generic_text_and_runs_nothing(self) -> None:
        joined = Joined(self)
        joined.command = None
        joined.updater.on_join(DEVICE, "0.15.0")
        self.assertEqual((joined.runner.calls, joined.log), ([], []))
        (text,) = joined.posted
        self.assertIn("no known update command", text)
        self.assertIn("Update ide-agent-tabs with the client that installed it.", text)

    def test_a_second_join_from_the_same_peer_in_an_hour_does_nothing(self) -> None:
        joined = Joined(self)
        self.assertTrue(joined.updater.on_join(DEVICE, "0.15.0"))
        joined.now += HOUR_MS - 1
        self.assertFalse(joined.updater.on_join(DEVICE, "0.15.0"))
        self.assertEqual((len(joined.runner.calls), len(joined.posted)), (2, 1))
        joined.now += 1
        self.assertTrue(joined.updater.on_join(DEVICE, "0.15.0"))
        self.assertEqual((len(joined.runner.calls), len(joined.posted)), (4, 2))

    def test_the_throttle_counts_an_attempt_that_only_sent_a_notice(self) -> None:
        joined = Joined(self)
        joined.command = None
        self.assertTrue(joined.updater.on_join(DEVICE, "0.15.0"))
        self.assertFalse(joined.updater.on_join(DEVICE, "0.15.0"))
        self.assertEqual(len(joined.posted), 1)

    def test_an_update_that_installs_nothing_newer_sends_a_notice_and_does_not_restart(self) -> None:
        joined = Joined(self)
        joined.install = Install("/plugins/0.14.3", "0.14.3")
        joined.updater.on_join(DEVICE, "0.15.0")
        self.assertEqual(joined.log[-1], "self-update: ok")
        (text,) = joined.posted
        self.assertIn("found no newer install", text)
        self.assertFalse([event for event in joined.events if event.startswith("spawn")])

    def test_a_restart_that_cannot_start_is_logged(self) -> None:
        joined = Joined(self)
        joined.spawn_ok = False
        joined.updater.on_join(DEVICE, "0.15.0")
        self.assertIn("self-update: the listener restart did not start", joined.log[-1])

    def test_an_error_inside_the_update_is_logged_not_raised(self) -> None:
        def broken() -> UpdateCommand | None:
            raise RuntimeError("boom")

        joined = Joined(self, command=broken)
        self.assertTrue(joined.updater.on_join(DEVICE, "0.15.0"))
        self.assertEqual(joined.log, ["self-update: failed RuntimeError"])

    def test_the_default_background_does_not_block_the_caller(self) -> None:
        started, release, done = threading.Event(), threading.Event(), threading.Event()
        joined = Joined(self)
        joined.updater.background = selfupdate._in_thread  # pyright: ignore[reportPrivateUsage]

        def slow(argv: list[str], **kwargs: Any) -> Completed:
            started.set()
            release.wait(10)
            return Completed()

        joined.updater.runner = slow
        joined.updater.spawn = lambda *_args: done.set() or True
        self.assertTrue(joined.updater.on_join(DEVICE, "0.15.0"))
        self.assertTrue(started.wait(5))
        self.assertFalse(done.is_set())
        release.set()
        self.assertTrue(done.wait(5))


class ListenerJoinedTest(unittest.TestCase):
    def make(self) -> tuple[Listener, Joined, list[tuple[str, str | None]]]:
        joined = Joined(self)
        calls: list[tuple[str, str | None]] = []
        joined.updater.on_join = lambda device, version: calls.append((device, version)) or True  # pyright: ignore[reportAttributeAccessIssue]
        return Listener(joined.home, Options(updater=joined.updater)), joined, calls

    def peer(self, version: str | None) -> PeerInfo:
        return PeerInfo("bravo", "192.168.1.9", DEVICE, b"x", version)

    def test_a_completed_join_hands_the_dialer_version_to_the_updater(self) -> None:
        for outcome in ("paired", "incomplete", "failed"):
            instance, _joined, calls = self.make()
            instance._joined(PairResult(outcome, "x", self.peer("0.15.0")))  # pyright: ignore[reportArgumentType, reportPrivateUsage]
            self.assertEqual(calls, [(DEVICE, "0.15.0")], outcome)

    def test_a_probe_or_a_result_without_a_peer_is_not_considered(self) -> None:
        for result in (
            PairResult("probed", "x", self.peer("0.15.0")),
            PairResult("failed", "x", None),
            PairResult("declined", "x", self.peer("0.15.0")),
        ):
            instance, _joined, calls = self.make()
            instance._joined(result)  # pyright: ignore[reportPrivateUsage]
            self.assertEqual(calls, [], result.outcome)

    def test_a_refused_join_is_still_logged(self) -> None:
        instance, joined, _calls = self.make()
        with mock.patch.object(listener, "append_log") as write:
            instance._joined(PairResult("failed", "the join failed", self.peer(None)))  # pyright: ignore[reportPrivateUsage]
        self.assertIn("join refused: the join failed", write.call_args.args[1])
        del joined


if __name__ == "__main__":
    unittest.main()

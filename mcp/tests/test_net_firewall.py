from __future__ import annotations

import base64
import json
import os
import re
import unittest
from collections.abc import Sequence
from typing import Any

from ide_agent_tabs.net import firewall
from ide_agent_tabs.net.firewall import FirewallError
from ide_agent_tabs.net.login_task import Environment
from ide_agent_tabs.processes import RunResult

WINDOWS = r"C:\Windows"
NETSH = os.path.join(WINDOWS, "System32", "netsh.exe")
POWERSHELL = os.path.join(WINDOWS, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
PYTHON = r"C:\Users\Me\AppData\Local\Programs\Python\Python312\python.exe"


class FakeRun:
    def __init__(self, *results: RunResult) -> None:
        self.calls: list[tuple[str, list[str], dict[str, str]]] = []
        self.results = list(results)

    def __call__(self, command: str, args: Sequence[str] = (), **options: Any) -> RunResult:
        self.calls.append((command, list(args), dict(options.get("env") or {})))
        return self.results.pop(0) if self.results else RunResult(0, "", "")


def environment(run: FakeRun, platform: str = "win32") -> Environment:
    return Environment(platform=platform, interpreter=PYTHON, run=run, windows_directory=lambda: WINDOWS)


def plan_of(call: tuple[str, list[str], dict[str, str]]) -> dict[str, Any]:
    script = base64.b64decode(call[2][firewall._ELEVATED_ENV]).decode("utf-16-le")  # pyright: ignore[reportPrivateUsage]
    blob = re.search(r"FromBase64String\('([A-Za-z0-9+/=]+)'\)", script)
    assert blob is not None
    return json.loads(base64.b64decode(blob.group(1)))


def exists(_path: str) -> bool:
    return True


class RuleArgumentsTest(unittest.TestCase):
    def test_the_arguments_are_exact(self) -> None:
        self.assertEqual(
            firewall.rule_arguments("192.168.1.10", 47829, ["192.168.1.20", "10.0.0.7"], PYTHON),
            [
                "advfirewall",
                "firewall",
                "add",
                "rule",
                "name=Agent Tabs LAN",
                "dir=in",
                "action=allow",
                "protocol=TCP",
                "localport=47829",
                "localip=192.168.1.10",
                "remoteip=10.0.0.7,192.168.1.20",
                "profile=private",
                f"program={PYTHON}",
            ],
        )

    def test_addresses_are_normalised_deduplicated_and_ipv4_first(self) -> None:
        arguments = firewall.rule_arguments("fd00::5", 5000, ["FD00::0001", "fd00::1", "10.0.0.9"], PYTHON)
        self.assertIn("remoteip=10.0.0.9,fd00::1", arguments)
        self.assertIn("localip=fd00::5", arguments)

    def test_bad_peer_addresses_are_refused(self) -> None:
        for peer in ("10.0.0.1%3", "host.local", "0.0.0.0", "224.0.0.1", "", "10.0.0.1'; calc", "10.0.0.1,10.0.0.2", "10.0.0.0/8", "any"):
            with self.assertRaises(FirewallError, msg=peer):
                firewall.rule_arguments("192.168.1.10", 47829, ["192.168.1.20", peer], PYTHON)

    def test_no_peer_is_refused_rather_than_allowing_anyone(self) -> None:
        with self.assertRaises(FirewallError):
            firewall.rule_arguments("192.168.1.10", 47829, [], PYTHON)

    def test_bad_ports_are_refused(self) -> None:
        for port in (0, -1, 65536, "80", "80; calc", True, None):
            with self.assertRaises(FirewallError, msg=repr(port)):
                firewall.rule_arguments("192.168.1.10", port, ["192.168.1.20"], PYTHON)  # type: ignore[arg-type]

    def test_a_bind_that_is_not_private_is_refused(self) -> None:
        for bind in ("8.8.8.8", "0.0.0.0", "::", "127.0.0.1", "192.168.1.10%2", "any"):
            with self.assertRaises(FirewallError, msg=bind):
                firewall.rule_arguments(bind, 47829, ["192.168.1.20"], PYTHON)

    def test_bad_programs_are_refused(self) -> None:
        for program in (
            "python.exe",
            r"\\server\share\python.exe",
            '"C:\\a b\\python.exe"',
            'C:\\a"b\\python.exe',
            "C:\\a\nb\\python.exe",
            "/usr/bin/python3",
        ):
            with self.assertRaises(FirewallError, msg=program):
                firewall.rule_arguments("192.168.1.10", 47829, ["192.168.1.20"], program)


class ElevatedCommandTest(unittest.TestCase):
    def test_the_plan_travels_as_data_inside_a_fixed_script(self) -> None:
        delete, add = firewall.delete_arguments(), firewall.rule_arguments("192.168.1.10", 47829, ["192.168.1.20"], PYTHON)
        encoded = firewall.elevated_command(NETSH, delete, add)
        script = base64.b64decode(encoded).decode("utf-16-le")
        self.assertEqual(re.sub(r"'[A-Za-z0-9+/=]+'", "'PLAN'", script), firewall._INNER_SCRIPT.replace("{plan}", "PLAN"))  # pyright: ignore[reportPrivateUsage]
        call = ("", [], {firewall._ELEVATED_ENV: encoded})  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(plan_of(call), {"netsh": NETSH, "delete": delete, "add": add})

    def test_the_outer_script_holds_no_value_from_the_inputs(self) -> None:
        for text in (PYTHON, "192.168.1.20", "47829", NETSH):
            self.assertNotIn(text, firewall._OUTER_SCRIPT)  # pyright: ignore[reportPrivateUsage]


class ApplyTest(unittest.TestCase):
    def test_apply_asks_windows_to_elevate_one_powershell_with_a_fixed_command_line(self) -> None:
        run = FakeRun()
        message = firewall.apply_rule("192.168.1.10", 47829, ["192.168.1.20"], environment(run), exists)
        self.assertEqual(len(run.calls), 1)
        command, args, env = run.calls[0]
        self.assertEqual(command, POWERSHELL)
        self.assertEqual(args, ["-NoProfile", "-NonInteractive", "-Command", firewall._OUTER_SCRIPT])  # pyright: ignore[reportPrivateUsage]
        self.assertIn("-Verb RunAs", args[3])
        self.assertIn("-Wait", args[3])
        self.assertEqual(env[firewall._POWERSHELL_ENV], POWERSHELL)  # pyright: ignore[reportPrivateUsage]
        plan = plan_of(run.calls[0])
        self.assertEqual(plan["netsh"], NETSH)
        self.assertEqual(plan["delete"], ["advfirewall", "firewall", "delete", "rule", "name=Agent Tabs LAN"])
        self.assertEqual(plan["add"], firewall.rule_arguments("192.168.1.10", 47829, ["192.168.1.20"], PYTHON))
        self.assertIn("Agent Tabs LAN", message)

    def test_invalid_input_is_refused_before_anything_runs(self) -> None:
        run = FakeRun()
        env = environment(run)
        for bind, port, peers in (
            ("8.8.8.8", 47829, ["192.168.1.20"]),
            ("192.168.1.10", 0, ["192.168.1.20"]),
            ("192.168.1.10", 47829, ["192.168.1.20", "x; calc"]),
        ):
            with self.assertRaises(FirewallError):
                firewall.apply_rule(bind, port, peers, env, exists)
        self.assertEqual(run.calls, [])

    def test_a_declined_prompt_is_reported_as_declined(self) -> None:
        run = FakeRun(RunResult(firewall.DECLINED, "", ""))
        with self.assertRaises(FirewallError) as caught:
            firewall.apply_rule("192.168.1.10", 47829, ["192.168.1.20"], environment(run), exists)
        self.assertIn("declined", str(caught.exception))

    def test_a_netsh_failure_is_reported(self) -> None:
        run = FakeRun(RunResult(1, "", ""))
        with self.assertRaises(FirewallError) as caught:
            firewall.apply_rule("192.168.1.10", 47829, ["192.168.1.20"], environment(run), exists)
        self.assertIn("code 1", str(caught.exception))

    def test_a_missing_interpreter_is_refused(self) -> None:
        run = FakeRun()
        with self.assertRaises(FirewallError):
            firewall.apply_rule("192.168.1.10", 47829, ["192.168.1.20"], environment(run), lambda _path: False)
        self.assertEqual(run.calls, [])

    def test_a_run_that_cannot_start_is_reported(self) -> None:
        def broken(*_args: Any, **_options: Any) -> RunResult:
            raise OSError("no powershell")

        env = Environment(platform="win32", interpreter=PYTHON, run=broken, windows_directory=lambda: WINDOWS)
        with self.assertRaises(FirewallError):
            firewall.apply_rule("192.168.1.10", 47829, ["192.168.1.20"], env, exists)

    def test_without_peers_the_rule_is_removed_and_none_is_created(self) -> None:
        run = FakeRun(RunResult(0, "", ""), RunResult(0, "", ""))
        firewall.apply_rule("192.168.1.10", 47829, [], environment(run), exists)
        self.assertEqual(run.calls[0][:2], (NETSH, firewall.show_arguments()))
        self.assertEqual(plan_of(run.calls[1])["add"], [])

    def test_other_systems_get_text_and_no_change(self) -> None:
        for platform in ("darwin", "linux"):
            run = FakeRun()
            text = firewall.apply_rule("192.168.1.10", 47829, ["192.168.1.20", "10.0.0.7"], environment(run, platform), exists)
            self.assertEqual(run.calls, [])
            for part in ("47829", "192.168.1.10", "10.0.0.7, 192.168.1.20", "changes no firewall rule"):
                self.assertIn(part, text)


class RemoveTest(unittest.TestCase):
    def test_an_existing_rule_is_deleted_with_one_elevation(self) -> None:
        run = FakeRun(RunResult(0, "", ""), RunResult(0, "", ""))
        message = firewall.remove_rule(environment(run))
        self.assertEqual(run.calls[0][:2], (NETSH, ["advfirewall", "firewall", "show", "rule", "name=Agent Tabs LAN"]))
        self.assertEqual(run.calls[1][0], POWERSHELL)
        plan = plan_of(run.calls[1])
        self.assertEqual((plan["delete"], plan["add"]), (firewall.delete_arguments(), []))
        self.assertIn("Removed", message)

    def test_no_rule_means_no_elevation(self) -> None:
        run = FakeRun(RunResult(1, "No rules match the specified criteria.", ""))
        message = firewall.remove_rule(environment(run))
        self.assertEqual(len(run.calls), 1)
        self.assertIn("No firewall rule", message)

    def test_a_declined_prompt_is_reported(self) -> None:
        run = FakeRun(RunResult(0, "", ""), RunResult(firewall.DECLINED, "", ""))
        with self.assertRaises(FirewallError) as caught:
            firewall.remove_rule(environment(run))
        self.assertIn("declined", str(caught.exception))

    def test_other_systems_change_nothing(self) -> None:
        run = FakeRun()
        self.assertEqual(firewall.remove_rule(environment(run, "linux")), "")
        self.assertEqual(run.calls, [])


if __name__ == "__main__":
    unittest.main()

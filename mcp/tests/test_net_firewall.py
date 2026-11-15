from __future__ import annotations

import base64
import json
import os
import unittest
from collections.abc import Sequence
from typing import Any
from unittest import mock

from ide_agent_tabs.net import firewall
from ide_agent_tabs.net.firewall import FirewallError
from ide_agent_tabs.net.login_task import Environment
from ide_agent_tabs.processes import RunResult

WINDOWS = r"C:\Windows"
POWERSHELL = os.path.join(WINDOWS, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
PYTHON = r"C:\Users\Me\AppData\Local\Programs\Python\Python312\python.exe"
BIND = "192.168.1.10"
SUBNET = "192.168.1.0/24"
ALIAS = "Ethernet"
TAILSCALE_BIND = "100.101.102.103"
TAILSCALE_SUBNET = "100.64.0.0/10"
Call = tuple[str, list[str], dict[str, str]]
INTERFACE_JSON = json.dumps(
    {"alias": ALIAS, "address": BIND, "prefix": 24, "profile": "Private", "hardware": True, "description": "Intel(R) Ethernet Connection"}
)


class FakeRun:
    def __init__(self, *results: RunResult) -> None:
        self.calls: list[Call] = []
        self.results = list(results)

    def __call__(self, command: str, args: Sequence[str] = (), **options: Any) -> RunResult:
        self.calls.append((command, list(args), dict(options.get("env") or {})))
        return self.results.pop(0) if self.results else RunResult(0, "", "")


def environment(run: FakeRun, platform: str = "win32", interpreter: str = PYTHON) -> Environment:
    return Environment(platform=platform, interpreter=interpreter, run=run, windows_directory=lambda: WINDOWS)


def parameters_of(call: Call) -> dict[str, Any]:
    return json.loads(base64.b64decode(call[2][firewall._PARAMS_ENV]))  # pyright: ignore[reportPrivateUsage]


def interpreter_exists(exists: bool = True) -> Any:
    return mock.patch.object(firewall.os.path, "isfile", return_value=exists)


class RuleSpecTest(unittest.TestCase):
    def spec(self, **changes: str) -> firewall.RuleSpec:
        values = {
            "rule": firewall.LISTENER_RULE,
            "bind": BIND,
            "subnet": SUBNET,
            "interface_alias": ALIAS,
            "program": PYTHON,
            "profile": firewall.PROFILE_PRIVATE,
            **changes,
        }
        return firewall.rule_spec(**values)

    def test_the_listener_rule_uses_the_listener_port(self) -> None:
        self.assertEqual(
            self.spec(),
            firewall.RuleSpec("Agent Tabs LAN", 47829, PYTHON, "Private", ALIAS, SUBNET, BIND),
        )

    def test_the_pairing_rule_uses_the_pairing_port(self) -> None:
        spec = self.spec(rule=firewall.PAIRING_RULE)
        self.assertEqual((spec.rule, spec.port), ("Agent Tabs pairing", 47830))

    def test_tailscale_uses_any_profile_with_the_tailscale_range(self) -> None:
        spec = self.spec(bind=TAILSCALE_BIND, subnet=TAILSCALE_SUBNET, interface_alias="Tailscale", profile=firewall.PROFILE_ANY)
        self.assertEqual((spec.profile, spec.remote, spec.local, spec.alias), ("Any", TAILSCALE_SUBNET, TAILSCALE_BIND, "Tailscale"))

    def test_other_rule_names_are_refused(self) -> None:
        for rule in ("", "Agent Tabs", "agent tabs lan", "Agent Tabs LAN ", "Any", "Agent Tabs LAN*"):
            with self.assertRaises(FirewallError, msg=rule):
                self.spec(rule=rule)

    def test_profiles_other_than_private_and_any_are_refused(self) -> None:
        for profile in ("Public", "Domain", "private", "Private,Public", ""):
            with self.assertRaises(FirewallError, msg=profile):
                self.spec(profile=profile)

    def test_subnets_outside_the_allowed_ranges_are_refused(self) -> None:
        for subnet in (
            "8.8.8.0/24",
            "0.0.0.0/0",
            "192.168.0.0/15",
            "172.32.0.0/16",
            "172.16.0.0/11",
            "10.0.0.0/7",
            "169.254.0.0/16",
            "127.0.0.0/8",
            "192.168.1.5/24",
            "192.168.1.0/255.255.255.0",
            "192.168.1.0/24,10.0.0.0/8",
            "192.168.1.0",
            "fd00::/8",
            "any",
            "",
        ):
            with self.assertRaises(FirewallError, msg=subnet):
                self.spec(subnet=subnet)

    def test_the_profile_decides_which_ranges_are_allowed(self) -> None:
        with self.assertRaises(FirewallError):
            self.spec(profile=firewall.PROFILE_ANY)
        with self.assertRaises(FirewallError):
            self.spec(bind=TAILSCALE_BIND, subnet=TAILSCALE_SUBNET, interface_alias="Tailscale")
        with self.assertRaises(FirewallError):
            self.spec(profile=firewall.PROFILE_ANY, bind="100.63.0.1", subnet="100.63.0.0/16")

    def test_a_bind_that_is_not_one_address_inside_the_subnet_is_refused(self) -> None:
        for bind in (
            "192.168.2.10",
            "8.8.8.8",
            "192.168.1.10; calc",
            "192.168.01.10",
            "fd00::5",
            "host.local",
            "192.168.1.0/24",
            "",
            "192.168.1.10%2",
        ):
            with self.assertRaises(FirewallError, msg=bind):
                self.spec(bind=bind)

    def test_unsafe_interface_names_are_refused(self) -> None:
        for alias in ("", " Ethernet", "Ethernet ", "Eth*", "Eth?", 'Eth"ernet', "Eth; calc", "Eth\nernet", "$(calc)", "a" * 65, "Eth[1]"):
            with self.assertRaises(FirewallError, msg=repr(alias)):
                self.spec(interface_alias=alias)

    def test_ordinary_adapter_names_are_accepted(self) -> None:
        for alias in ("Ethernet", "Wi-Fi", "Ethernet 2", "vEthernet (WSL)", "Tailscale", "Local Area Connection 10"):
            self.assertEqual(self.spec(interface_alias=alias).alias, alias)

    def test_bad_programs_are_refused(self) -> None:
        for program in (
            "python.exe",
            r"\\server\share\python.exe",
            '"C:\\a b\\python.exe"',
            'C:\\a"b\\python.exe',
            "C:\\a\nb\\python.exe",
            "/usr/bin/python3",
            r"C:\Windows\System32\cmd.exe",
            r"C:\Python\python.exe.bat",
            r"C:\Python\python3.exe",
            r"C:\Python\..\Windows\python.exe",
            r"C:\Python\.\python.exe",
            r"C:\Python\a|b\python.exe",
            "",
        ):
            with self.assertRaises(FirewallError, msg=program):
                self.spec(program=program)

    def test_python_and_pythonw_are_accepted(self) -> None:
        for program in (PYTHON, PYTHON.replace("python.exe", "pythonw.exe"), r"C:\Program Files\Python312\PYTHON.EXE"):
            self.assertEqual(self.spec(program=program).program, program)


class ParametersTest(unittest.TestCase):
    def test_a_rule_travels_as_base64_json(self) -> None:
        spec = firewall.rule_spec(firewall.PAIRING_RULE, BIND, SUBNET, ALIAS, PYTHON)
        blob = firewall.encode_parameters("add", spec.rule, spec)
        self.assertRegex(blob, r"^[A-Za-z0-9+/=]+$")
        self.assertEqual(
            json.loads(base64.b64decode(blob)),
            {
                "action": "add",
                "rule": "Agent Tabs pairing",
                "port": 47830,
                "program": PYTHON,
                "profile": "Private",
                "alias": ALIAS,
                "remote": SUBNET,
                "local": BIND,
            },
        )

    def test_a_removal_carries_only_the_rule_name(self) -> None:
        blob = firewall.encode_parameters("remove", firewall.LISTENER_RULE)
        self.assertEqual(json.loads(base64.b64decode(blob)), {"action": "remove", "rule": "Agent Tabs LAN"})


class HelperScriptTest(unittest.TestCase):
    def test_the_helper_is_one_line_without_double_quotes_or_values(self) -> None:
        script = firewall.HELPER_SCRIPT
        self.assertNotIn("\n", script)
        self.assertNotIn('"', script)
        self.assertNotIn("%", script)
        for value in (PYTHON, BIND, SUBNET, ALIAS, "192.168"):
            self.assertNotIn(value, script)

    def test_the_helper_validates_creates_reads_back_and_rolls_back(self) -> None:
        script = firewall.HELPER_SCRIPT
        for part in (
            "'Agent Tabs LAN'",
            "'Agent Tabs pairing'",
            "47829",
            "47830",
            "1681915904",
            "New-NetFirewallRule",
            "Remove-NetFirewallRule",
            "Get-NetFirewallRule",
            "Get-NetFirewallPortFilter",
            "Get-NetFirewallAddressFilter",
            "Get-NetFirewallApplicationFilter",
            "Get-NetFirewallInterfaceFilter",
            "-ErrorAction Stop",
        ):
            self.assertIn(part, script)
        self.assertNotIn("Invoke-Expression", script)
        self.assertNotIn("iex ", script)

    def test_the_outer_script_holds_no_value_from_the_inputs(self) -> None:
        for text in (PYTHON, BIND, "47829", "Agent Tabs LAN"):
            self.assertNotIn(text, firewall._OUTER_SCRIPT)  # pyright: ignore[reportPrivateUsage]
        self.assertIn("-Verb RunAs", firewall._OUTER_SCRIPT)  # pyright: ignore[reportPrivateUsage]
        self.assertIn("-Wait", firewall._OUTER_SCRIPT)  # pyright: ignore[reportPrivateUsage]


class InstallTest(unittest.TestCase):
    def test_one_elevation_runs_the_fixed_helper_with_the_parameters_as_data(self) -> None:
        run = FakeRun()
        with interpreter_exists():
            message = firewall.apply_listener_rule(BIND, SUBNET, ALIAS, environment(run))
        self.assertEqual(len(run.calls), 1)
        command, args, env = run.calls[0]
        self.assertEqual(command, POWERSHELL)
        self.assertEqual(args, ["-NoProfile", "-NonInteractive", "-Command", firewall._OUTER_SCRIPT])  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(env[firewall._POWERSHELL_ENV], POWERSHELL)  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(env[firewall._HELPER_ENV], firewall.HELPER_SCRIPT)  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(
            parameters_of(run.calls[0]),
            {
                "action": "add",
                "rule": "Agent Tabs LAN",
                "port": 47829,
                "program": PYTHON,
                "profile": "Private",
                "alias": ALIAS,
                "remote": SUBNET,
                "local": BIND,
            },
        )
        for part in ("Agent Tabs LAN", "47829", BIND, SUBNET, "Private", ALIAS):
            self.assertIn(part, message)

    def test_the_pairing_rule_uses_its_own_name_and_port(self) -> None:
        run = FakeRun()
        with interpreter_exists():
            message = firewall.open_pairing_rule(BIND, SUBNET, ALIAS, environment(run))
        params = parameters_of(run.calls[0])
        self.assertEqual((params["rule"], params["port"]), ("Agent Tabs pairing", 47830))
        self.assertIn("47830", message)

    def test_tailscale_asks_for_the_any_profile_on_the_tailscale_interface(self) -> None:
        run = FakeRun()
        with interpreter_exists():
            message = firewall.apply_listener_rule(TAILSCALE_BIND, TAILSCALE_SUBNET, "Tailscale", environment(run), firewall.PROFILE_ANY)
        params = parameters_of(run.calls[0])
        self.assertEqual(
            (params["profile"], params["remote"], params["alias"], params["local"]), ("Any", TAILSCALE_SUBNET, "Tailscale", TAILSCALE_BIND)
        )
        self.assertIn("Tailscale", message)

    def test_invalid_input_is_refused_before_anything_runs(self) -> None:
        run = FakeRun()
        env = environment(run)
        with interpreter_exists():
            for bind, subnet, alias in (
                ("8.8.8.8", SUBNET, ALIAS),
                (BIND, "8.8.8.0/24", ALIAS),
                (BIND, "192.168.0.0/15", ALIAS),
                (BIND, SUBNET, "Eth; calc"),
                ("192.168.2.1", SUBNET, ALIAS),
            ):
                with self.assertRaises(FirewallError):
                    firewall.apply_listener_rule(bind, subnet, alias, env)
                with self.assertRaises(FirewallError):
                    firewall.open_pairing_rule(bind, subnet, alias, env)
        self.assertEqual(run.calls, [])

    def test_an_interpreter_that_is_not_a_python_executable_is_refused(self) -> None:
        run = FakeRun()
        with interpreter_exists():
            for interpreter in (r"C:\Windows\System32\cmd.exe", "/usr/bin/python3", 'C:\\a"b\\python.exe'):
                with self.assertRaises(FirewallError, msg=interpreter):
                    firewall.apply_listener_rule(BIND, SUBNET, ALIAS, environment(run, interpreter=interpreter))
        self.assertEqual(run.calls, [])

    def test_a_missing_interpreter_is_refused(self) -> None:
        run = FakeRun()
        with interpreter_exists(False), self.assertRaises(FirewallError):
            firewall.apply_listener_rule(BIND, SUBNET, ALIAS, environment(run))
        self.assertEqual(run.calls, [])

    def test_a_declined_prompt_is_reported_as_declined(self) -> None:
        run = FakeRun(RunResult(firewall.DECLINED, "", ""))
        with interpreter_exists(), self.assertRaises(FirewallError) as caught:
            firewall.apply_listener_rule(BIND, SUBNET, ALIAS, environment(run))
        self.assertIn("declined", str(caught.exception))
        self.assertIn("did not change", str(caught.exception))

    def test_a_read_back_mismatch_is_reported_and_says_the_helper_removed_the_rule(self) -> None:
        run = FakeRun(RunResult(firewall.EXIT_MISMATCH, "", ""))
        with interpreter_exists(), self.assertRaises(FirewallError) as caught:
            firewall.open_pairing_rule(BIND, SUBNET, ALIAS, environment(run))
        self.assertIn(f"code {firewall.EXIT_MISMATCH}", str(caught.exception))
        self.assertIn("removed it", str(caught.exception))

    def test_other_helper_failures_are_reported_with_their_reason(self) -> None:
        for code, reason in (
            (firewall.EXIT_REFUSED, "refused a value"),
            (firewall.EXIT_NOT_CREATED, "could not create"),
            (firewall.UNKNOWN, ""),
            (77, ""),
        ):
            run = FakeRun(RunResult(code, "", ""))
            with interpreter_exists(), self.assertRaises(FirewallError) as caught:
                firewall.apply_listener_rule(BIND, SUBNET, ALIAS, environment(run))
            self.assertIn(f"code {code}", str(caught.exception))
            self.assertIn(reason, str(caught.exception))

    def test_a_run_that_cannot_start_or_times_out_is_reported(self) -> None:
        for error in (OSError("no powershell"), TimeoutError("too slow")):

            def broken(*_args: Any, _error: Exception = error, **_options: Any) -> RunResult:
                raise _error

            env = Environment(platform="win32", interpreter=PYTHON, run=broken, windows_directory=lambda: WINDOWS)
            with interpreter_exists(), self.assertRaises(FirewallError):
                firewall.apply_listener_rule(BIND, SUBNET, ALIAS, env)

    def test_other_systems_get_text_and_no_change(self) -> None:
        for platform in ("darwin", "linux"):
            run = FakeRun()
            env = environment(run, platform)
            for text in (
                firewall.apply_listener_rule(BIND, SUBNET, ALIAS, env),
                firewall.open_pairing_rule(BIND, SUBNET, ALIAS, env),
            ):
                for part in (BIND, SUBNET, "changes no firewall rule"):
                    self.assertIn(part, text)
            self.assertIn("47829", firewall.apply_listener_rule(BIND, SUBNET, ALIAS, env))
            self.assertIn("47830", firewall.open_pairing_rule(BIND, SUBNET, ALIAS, env))
            self.assertEqual(run.calls, [])


class RemoveTest(unittest.TestCase):
    def test_closing_the_pairing_rule_elevates_once_without_a_probe(self) -> None:
        run = FakeRun()
        message = firewall.close_pairing_rule(environment(run))
        self.assertEqual(len(run.calls), 1)
        self.assertEqual(run.calls[0][0], POWERSHELL)
        self.assertEqual(parameters_of(run.calls[0]), {"action": "remove", "rule": "Agent Tabs pairing"})
        self.assertIn("Removed", message)

    def test_an_existing_listener_rule_is_probed_then_removed_with_one_elevation(self) -> None:
        run = FakeRun(RunResult(0, "", ""), RunResult(0, "", ""))
        message = firewall.remove_listener_rule(environment(run))
        self.assertEqual(len(run.calls), 2)
        probe_command, probe_args, probe_env = run.calls[0]
        self.assertEqual(probe_command, POWERSHELL)
        self.assertEqual(probe_args, ["-NoProfile", "-NonInteractive", "-Command", firewall._PROBE_SCRIPT])  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(probe_env[firewall._RULE_ENV], "Agent Tabs LAN")  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(parameters_of(run.calls[1]), {"action": "remove", "rule": "Agent Tabs LAN"})
        self.assertIn("Removed", message)

    def test_no_rule_means_no_elevation(self) -> None:
        for remove in (firewall.remove_listener_rule, firewall.remove_stale_pairing_rule):
            run = FakeRun(RunResult(1, "", ""))
            message = remove(environment(run))
            self.assertEqual(len(run.calls), 1)
            self.assertIn("No firewall rule", message)

    def test_a_stale_pairing_rule_is_removed(self) -> None:
        run = FakeRun(RunResult(0, "", ""), RunResult(0, "", ""))
        firewall.remove_stale_pairing_rule(environment(run))
        self.assertEqual(run.calls[0][2][firewall._RULE_ENV], "Agent Tabs pairing")  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(parameters_of(run.calls[1])["rule"], "Agent Tabs pairing")

    def test_a_probe_that_cannot_tell_still_removes(self) -> None:
        run = FakeRun(RunResult(3, "", ""), RunResult(0, "", ""))
        firewall.remove_listener_rule(environment(run))
        self.assertEqual(len(run.calls), 2)

    def test_a_declined_prompt_is_reported(self) -> None:
        run = FakeRun(RunResult(0, "", ""), RunResult(firewall.DECLINED, "", ""))
        with self.assertRaises(FirewallError) as caught:
            firewall.remove_listener_rule(environment(run))
        self.assertIn("declined", str(caught.exception))

    def test_a_removal_the_helper_could_not_finish_is_reported(self) -> None:
        run = FakeRun(RunResult(firewall.EXIT_NOT_REMOVED, "", ""))
        with self.assertRaises(FirewallError) as caught:
            firewall.close_pairing_rule(environment(run))
        self.assertIn("could not remove", str(caught.exception))

    def test_a_probe_that_cannot_run_is_reported(self) -> None:
        def broken(*_args: Any, **_options: Any) -> RunResult:
            raise OSError("no powershell")

        env = Environment(platform="win32", interpreter=PYTHON, run=broken, windows_directory=lambda: WINDOWS)
        with self.assertRaises(FirewallError):
            firewall.remove_listener_rule(env)

    def test_the_probe_refuses_other_rule_names(self) -> None:
        with self.assertRaises(FirewallError):
            firewall.rule_exists("Core Networking", environment(FakeRun()))

    def test_other_systems_change_nothing(self) -> None:
        for remove in (firewall.remove_listener_rule, firewall.remove_stale_pairing_rule, firewall.close_pairing_rule):
            run = FakeRun()
            self.assertEqual(remove(environment(run, "linux")), "")
            self.assertEqual(run.calls, [])


class ListenerServicesTest(unittest.TestCase):
    def test_apply_rule_scopes_the_rule_to_the_interface_subnet(self) -> None:
        run = FakeRun(RunResult(0, INTERFACE_JSON, ""), RunResult(0, "", ""))
        with interpreter_exists():
            message = firewall.apply_rule(BIND, 47829, ["192.168.1.20"], environment(run))
        self.assertEqual(len(run.calls), 2)
        params = parameters_of(run.calls[1])
        self.assertEqual((params["rule"], params["remote"], params["alias"], params["local"]), ("Agent Tabs LAN", SUBNET, ALIAS, BIND))
        self.assertIn("Agent Tabs LAN", message)

    def test_apply_rule_refuses_a_port_that_is_not_the_listener_port(self) -> None:
        run = FakeRun()
        with self.assertRaises(FirewallError):
            firewall.apply_rule(BIND, 47830, [], environment(run))
        self.assertEqual(run.calls, [])

    def test_apply_rule_reports_an_interface_that_windows_cannot_describe(self) -> None:
        run = FakeRun(RunResult(1, "", ""))
        with self.assertRaises(FirewallError):
            firewall.apply_rule(BIND, 47829, [], environment(run))

    def test_apply_rule_on_other_systems_returns_text(self) -> None:
        run = FakeRun()
        text = firewall.apply_rule(BIND, 47829, [], environment(run, "linux"))
        self.assertIn("changes no firewall rule", text)
        self.assertEqual(run.calls, [])

    def test_remove_rule_removes_the_listener_rule(self) -> None:
        run = FakeRun(RunResult(0, "", ""), RunResult(0, "", ""))
        firewall.remove_rule(environment(run))
        self.assertEqual(parameters_of(run.calls[1])["rule"], firewall.LISTENER_RULE)


if __name__ == "__main__":
    unittest.main()

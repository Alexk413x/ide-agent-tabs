from __future__ import annotations

import json
import os
import unittest
from collections.abc import Sequence
from typing import Any
from unittest import mock

from ide_agent_tabs.net import interfaces
from ide_agent_tabs.net.interfaces import InterfaceError, InterfaceInfo
from ide_agent_tabs.net.login_task import Environment
from ide_agent_tabs.processes import RunResult

WINDOWS = r"C:\Windows"
POWERSHELL = os.path.join(WINDOWS, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
ADDRESS = "192.168.1.10"


class FakeRun:
    def __init__(self, *results: RunResult) -> None:
        self.calls: list[tuple[str, list[str], dict[str, str], float | None]] = []
        self.results = list(results)

    def __call__(self, command: str, args: Sequence[str] = (), **options: Any) -> RunResult:
        self.calls.append((command, list(args), dict(options.get("env") or {}), options.get("timeout")))
        return self.results.pop(0) if self.results else RunResult(0, "", "")


def windows_row(**changes: Any) -> str:
    row: dict[str, Any] = {
        "alias": "Ethernet",
        "address": ADDRESS,
        "prefix": 24,
        "profile": "Private",
        "hardware": True,
        "description": "Intel(R) Ethernet Connection I219-V",
    }
    row.update(changes)
    return json.dumps(row)


def windows_env(*results: RunResult, platform: str = "win32") -> tuple[Environment, FakeRun]:
    run = FakeRun(*results)
    return Environment(platform=platform, run=run, windows_directory=lambda: WINDOWS), run


def lookup(text: str, address: str = ADDRESS) -> InterfaceInfo:
    env, _ = windows_env(RunResult(0, text, ""))
    return interfaces.interface_for(address, env)


class WindowsParsingTest(unittest.TestCase):
    def test_a_physical_private_interface_is_read(self) -> None:
        info = lookup(windows_row())
        self.assertEqual(info, InterfaceInfo("Ethernet", ADDRESS, 24, "192.168.1.0/24", "Private", True))

    def test_the_subnet_is_the_network_of_the_address_and_prefix(self) -> None:
        self.assertEqual(lookup(windows_row(prefix=20)).subnet, "192.168.0.0/20")
        self.assertEqual(lookup(windows_row(address="10.1.2.3", prefix=8), "10.1.2.3").subnet, "10.0.0.0/8")

    def test_the_query_runs_the_fixed_system32_powershell_with_a_timeout_and_the_address_as_data(self) -> None:
        env, run = windows_env(RunResult(0, windows_row(), ""))
        interfaces.interface_for(ADDRESS, env)
        command, args, variables, timeout = run.calls[0]
        self.assertEqual(command, POWERSHELL)
        self.assertEqual(args, ["-NoProfile", "-NonInteractive", "-Command", interfaces._QUERY_SCRIPT])  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(variables[interfaces._ADDRESS_ENV], ADDRESS)  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(timeout, interfaces.QUERY_TIMEOUT_S)
        self.assertNotIn(ADDRESS, interfaces._QUERY_SCRIPT)  # pyright: ignore[reportPrivateUsage]

    def test_profile_names_and_numbers_are_understood(self) -> None:
        for value, expected in (
            ("Private", "Private"),
            ("private", "Private"),
            (1, "Private"),
            ("Public", "Public"),
            (0, "Public"),
            ("DomainAuthenticated", "Domain"),
            (2, "Domain"),
            ("", "unknown"),
            (None, "unknown"),
            ("Guest", "unknown"),
            (True, "unknown"),
        ):
            self.assertEqual(lookup(windows_row(profile=value)).profile, expected, msg=repr(value))

    def test_virtual_adapters_are_not_physical(self) -> None:
        for changes in (
            {"hardware": False},
            {"hardware": None},
            {"hardware": "yes"},
            {"alias": "vEthernet (WSL)", "description": "Hyper-V Virtual Ethernet Adapter"},
            {"description": "Hyper-V Virtual Ethernet Adapter #2"},
            {"description": "TAP-Windows Adapter V9"},
            {"description": "Cisco AnyConnect Secure Mobility Client Virtual Miniport Adapter"},
            {"description": "VMware Virtual Ethernet Adapter for VMnet8"},
            {"description": "Wintun Userspace Tunnel"},
            {"alias": "Docker NAT"},
            {"alias": "Tailscale", "description": "Tailscale Tunnel"},
        ):
            self.assertFalse(lookup(windows_row(**changes)).physical, msg=repr(changes))

    def test_unreadable_answers_are_refused(self) -> None:
        for text in (
            "",
            "not json",
            "[]",
            "{}",
            windows_row(alias="Eth; calc"),
            windows_row(alias=7),
            windows_row(address="192.168.1.99"),
            windows_row(prefix=0),
            windows_row(prefix=33),
            windows_row(prefix="24"),
            windows_row(prefix=True),
        ):
            with self.assertRaises(InterfaceError, msg=text):
                lookup(text)

    def test_noise_before_the_json_line_is_ignored(self) -> None:
        self.assertEqual(lookup("WARNING: something\n" + windows_row()).alias, "Ethernet")

    def test_a_failed_query_is_refused(self) -> None:
        env, _ = windows_env(RunResult(1, "", "no such address"))
        with self.assertRaises(InterfaceError):
            interfaces.interface_for(ADDRESS, env)

    def test_a_run_that_cannot_start_or_times_out_is_refused(self) -> None:
        for error in (OSError("no powershell"), TimeoutError("too slow")):

            def broken(*_args: Any, _error: Exception = error, **_options: Any) -> RunResult:
                raise _error

            env = Environment(platform="win32", run=broken, windows_directory=lambda: WINDOWS)
            with self.assertRaises(InterfaceError):
                interfaces.interface_for(ADDRESS, env)

    def test_an_address_that_is_not_ipv4_is_refused_before_anything_runs(self) -> None:
        env, run = windows_env()
        for address in ("fd00::1", "host.local", "192.168.1.10%3", "", "192.168.1.10; calc", "1.2.3"):
            with self.assertRaises(InterfaceError, msg=address):
                interfaces.interface_for(address, env)
        self.assertEqual(run.calls, [])


class RequireLanReadyTest(unittest.TestCase):
    def require(self, text: str, **options: Any) -> InterfaceInfo:
        env, _ = windows_env(RunResult(0, text, ""))
        return interfaces.require_lan_ready(ADDRESS, env, **options)

    def test_a_physical_private_interface_passes(self) -> None:
        self.assertEqual(self.require(windows_row()).alias, "Ethernet")

    def test_a_public_network_is_refused_with_the_exact_fix(self) -> None:
        with self.assertRaises(InterfaceError) as caught:
            self.require(windows_row(profile="Public", alias="Wi-Fi"))
        self.assertIn("Settings > Network & internet > Wi-Fi > Private network", str(caught.exception))

    def test_a_domain_network_is_refused(self) -> None:
        for value in ("DomainAuthenticated", 2):
            with self.assertRaises(InterfaceError) as caught:
                self.require(windows_row(profile=value))
            self.assertIn("Domain", str(caught.exception))
            self.assertIn("Tailscale", str(caught.exception))

    def test_a_network_without_a_profile_is_refused_on_windows(self) -> None:
        with self.assertRaises(InterfaceError) as caught:
            self.require(windows_row(profile=""))
        self.assertIn("Private network", str(caught.exception))

    def test_a_virtual_adapter_is_refused_unless_it_is_named(self) -> None:
        text = windows_row(alias="vEthernet (WSL)", hardware=False)
        with self.assertRaises(InterfaceError) as caught:
            self.require(text)
        self.assertIn("virtual", str(caught.exception))
        self.assertIn("--bind", str(caught.exception))
        self.assertEqual(self.require(text, allow_virtual=True).alias, "vEthernet (WSL)")

    def test_a_virtual_adapter_on_a_public_network_is_still_refused_when_named(self) -> None:
        with self.assertRaises(InterfaceError):
            self.require(windows_row(hardware=False, profile="Public"), allow_virtual=True)

    def test_addresses_outside_rfc_1918_are_refused_before_any_query(self) -> None:
        env, run = windows_env()
        for address in ("8.8.8.8", "100.64.0.5", "169.254.1.1", "127.0.0.1", "172.32.0.1", "fd00::1", "192.169.0.1"):
            with self.assertRaises(InterfaceError, msg=address):
                interfaces.require_lan_ready(address, env)
        self.assertEqual(run.calls, [])

    def test_a_prefix_with_no_usable_subnet_is_refused(self) -> None:
        for prefix in (7, 31, 32):
            with self.assertRaises(InterfaceError, msg=str(prefix)):
                self.require(windows_row(prefix=prefix))


IP_JSON = json.dumps(
    [
        {"ifname": "lo", "addr_info": [{"family": "inet", "local": "127.0.0.1", "prefixlen": 8}]},
        {"ifname": "eth0", "addr_info": [{"family": "inet", "local": "192.168.1.10", "prefixlen": 24}]},
        {"ifname": "docker0", "addr_info": [{"family": "inet", "local": "172.17.0.1", "prefixlen": 16}]},
    ]
)


class PosixTest(unittest.TestCase):
    def test_ip_json_gives_the_interface(self) -> None:
        info = interfaces.parse_ip_json(IP_JSON, ADDRESS)
        self.assertEqual(info, InterfaceInfo("eth0", ADDRESS, 24, "192.168.1.0/24", "unknown", True))

    def test_virtual_interface_names_are_not_physical(self) -> None:
        info = interfaces.parse_ip_json(IP_JSON, "172.17.0.1")
        assert info is not None
        self.assertFalse(info.physical)

    def test_an_address_that_is_not_listed_or_bad_json_gives_none(self) -> None:
        self.assertIsNone(interfaces.parse_ip_json(IP_JSON, "10.9.9.9"))
        for text in ("", "nope", "{}", '[{"ifname": "eth0"}]', '[1, "x"]'):
            self.assertIsNone(interfaces.parse_ip_json(text, ADDRESS), msg=text)

    def test_interface_for_uses_ip_when_it_exists(self) -> None:
        run = FakeRun(RunResult(0, IP_JSON, ""))
        env = Environment(platform="linux", run=run)
        with mock.patch.object(interfaces.os.path, "isfile", side_effect=lambda path: path == "/usr/sbin/ip"):  # pyright: ignore[reportUnknownLambdaType]
            info = interfaces.interface_for(ADDRESS, env)
        self.assertEqual(run.calls[0][0], "/usr/sbin/ip")
        self.assertEqual(run.calls[0][1], ["-j", "-4", "addr"])
        self.assertEqual((info.alias, info.subnet, info.profile), ("eth0", "192.168.1.0/24", "unknown"))

    def test_without_ip_the_answer_is_unknown_and_nothing_is_refused(self) -> None:
        run = FakeRun()
        env = Environment(platform="darwin", run=run)
        with mock.patch.object(interfaces.os.path, "isfile", return_value=False):
            info = interfaces.require_lan_ready(ADDRESS, env)
        self.assertEqual((info.profile, info.physical), ("unknown", True))
        self.assertEqual(run.calls, [])
        self.assertEqual(info.prefix_len, 32)

    def test_a_posix_system_never_asks_for_a_profile(self) -> None:
        run = FakeRun(RunResult(0, IP_JSON, ""))
        env = Environment(platform="linux", run=run)
        with mock.patch.object(interfaces.os.path, "isfile", side_effect=lambda path: path == "/usr/sbin/ip"):  # pyright: ignore[reportUnknownLambdaType]
            self.assertEqual(interfaces.require_lan_ready(ADDRESS, env).alias, "eth0")


class AliasTest(unittest.TestCase):
    def test_safe_aliases(self) -> None:
        for alias in ("Ethernet", "Wi-Fi", "Ethernet 2", "vEthernet (Default Switch)", "a"):
            self.assertTrue(interfaces.is_safe_alias(alias), msg=alias)
        for alias in ("", " x", "x ", "a*", "a;b", 'a"b', "a\nb", "a" * 65):
            self.assertFalse(interfaces.is_safe_alias(alias), msg=repr(alias))


if __name__ == "__main__":
    unittest.main()

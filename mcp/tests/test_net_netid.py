from __future__ import annotations

import json
import unittest
from collections.abc import Sequence
from typing import Any
from unittest import mock

from ide_agent_tabs.net import netid
from ide_agent_tabs.net.interfaces import InterfaceError, InterfaceInfo
from ide_agent_tabs.net.policy import NETWORK_ID
from ide_agent_tabs.processes import RunResult

BIND = "192.168.1.35"
SHELL = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"


class FakeRun:
    def __init__(self, *results: RunResult | Exception) -> None:
        self.results = list(results)
        self.calls: list[tuple[str, list[str], dict[str, str]]] = []

    def __call__(self, command: str, args: Sequence[str] = (), **options: Any) -> RunResult:
        self.calls.append((command, list(args), dict(options.get("env") or {})))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def windows_reply(name: str = "Mauler's House 5", address: str = BIND, prefix: Any = 24, **extra: Any) -> RunResult:
    return RunResult(0, json.dumps({"name": name, "address": address, "prefix": prefix, **extra}) + "\r\n", "")


ARP = r"C:\Windows\System32\arp.exe"
ARP_TEXT_WINDOWS = """
Interface: 192.168.1.35 --- 0x5
  Internet Address      Physical Address      Type
  192.168.1.1           aa-bb-cc-dd-ee-ff     dynamic
"""


def windows_id(run: FakeRun, shell: str | None = SHELL, arp: str | None = ARP) -> str | None:
    return netid.current_network_id(BIND, platform="win32", run=run, shell=lambda: shell, arp=lambda: arp)


def interface(prefix: int = 24, alias: str = "eth0") -> Any:
    return lambda address: InterfaceInfo(alias, address, prefix, "192.168.1.0/24", "unknown", True)


class WindowsTest(unittest.TestCase):
    def test_the_id_is_built_from_the_profile_name_and_the_subnet(self) -> None:
        run = FakeRun(windows_reply())
        found = windows_id(run)
        assert found is not None
        self.assertRegex(found, r"^win:Mauler-s-House-5:192\.168\.1\.0_24:[0-9a-f]{12}$")
        self.assertIsNotNone(NETWORK_ID.fullmatch(found))
        command, args, env = run.calls[0]
        self.assertEqual((command, args[:3]), (SHELL, ["-NoProfile", "-NonInteractive", "-Command"]))
        self.assertIn("Get-NetConnectionProfile", args[3])
        self.assertEqual(env[netid._ADDRESS_ENV], BIND)  # pyright: ignore[reportPrivateUsage]
        self.assertNotIn(BIND, args[3])

    def test_the_id_is_stable_and_changes_with_the_name_or_the_subnet(self) -> None:
        first = windows_id(FakeRun(windows_reply()))
        self.assertEqual(first, windows_id(FakeRun(windows_reply())))
        self.assertNotEqual(first, windows_id(FakeRun(windows_reply(name="Mauler's House 6"))))
        self.assertNotEqual(first, windows_id(FakeRun(windows_reply(prefix=16))))

    def test_names_that_differ_only_in_unsafe_characters_get_different_ids(self) -> None:
        self.assertNotEqual(netid.windows_network_id("A B", "192.168.1.0/24"), netid.windows_network_id("A-B", "192.168.1.0/24"))

    def test_a_long_or_odd_name_still_fits_the_id_pattern(self) -> None:
        for name in ("x" * 300, "é" * 5, "!!!", "tab\there"):
            found = netid.windows_network_id(name, "10.0.0.0/8")
            assert found is not None
            self.assertIsNotNone(NETWORK_ID.fullmatch(found), name)

    def test_unreadable_answers_give_none(self) -> None:
        for run in (
            FakeRun(RunResult(1, "", "no such address")),
            FakeRun(RunResult(0, "not json", "")),
            FakeRun(RunResult(0, "[]", "")),
            FakeRun(windows_reply(address="192.168.1.99")),
            FakeRun(windows_reply(name="  ")),
            FakeRun(windows_reply(name="")),
            FakeRun(windows_reply(prefix=0)),
            FakeRun(windows_reply(prefix=True)),
            FakeRun(windows_reply(prefix="24")),
            FakeRun(OSError("no powershell")),
            FakeRun(TimeoutError("too slow")),
        ):
            self.assertIsNone(windows_id(run))

    def test_a_missing_powershell_gives_none(self) -> None:
        run = FakeRun()
        self.assertIsNone(windows_id(run, shell=None))
        self.assertEqual(run.calls, [])


def with_gateway(mac: str | None = "AA-BB-CC-DD-EE-FF", gateway: str = "192.168.1.1", **fields: Any) -> RunResult:
    return windows_reply(gateway=gateway, mac=mac if mac is not None else "", **fields)


class WindowsGatewayTest(unittest.TestCase):
    def test_the_script_reads_the_default_route_and_the_neighbour_entry(self) -> None:
        run = FakeRun(with_gateway())
        windows_id(run)
        script = run.calls[0][1][3]
        for part in ("Get-NetRoute", "0.0.0.0/0", "Get-NetNeighbor", "LinkLayerAddress"):
            self.assertIn(part, script)
        self.assertNotIn(BIND, script)

    def test_two_networks_with_the_same_name_and_subnet_but_different_gateway_macs_get_different_ids(self) -> None:
        first = windows_id(FakeRun(with_gateway("AA-BB-CC-DD-EE-FF")))
        second = windows_id(FakeRun(with_gateway("11-22-33-44-55-66")))
        assert first is not None and second is not None
        self.assertNotEqual(first, second)
        self.assertRegex(first, r"^win:Mauler-s-House-5:192\.168\.1\.0_24:[0-9a-f]{12}$")
        self.assertIsNotNone(NETWORK_ID.fullmatch(first))
        self.assertIsNone(netid.fallback_of(first))

    def test_the_id_is_stable_for_one_gateway_and_ignores_the_mac_notation(self) -> None:
        first = windows_id(FakeRun(with_gateway("AA-BB-CC-DD-EE-FF")))
        self.assertEqual(first, windows_id(FakeRun(with_gateway("aa:bb:cc:dd:ee:ff"))))
        self.assertEqual(first, windows_id(FakeRun(with_gateway("AA-BB-CC-DD-EE-FF", gateway="192.168.1.254"))))

    def test_the_id_carries_the_name_and_subnet_form_as_legacy(self) -> None:
        found = windows_id(FakeRun(with_gateway()))
        legacy = netid.windows_network_id("Mauler's House 5", "192.168.1.0/24")
        self.assertEqual(netid.legacy_of(found), legacy)
        self.assertNotEqual(found, legacy)

    def test_without_a_gateway_mac_the_id_falls_back_to_the_name_and_subnet_and_says_so(self) -> None:
        for reply in (windows_reply(), with_gateway(mac=None, gateway=""), with_gateway(mac="00-00-00-00-00-00", gateway="")):
            found = windows_id(FakeRun(reply))
            self.assertEqual(found, netid.windows_network_id("Mauler's House 5", "192.168.1.0/24"))
            self.assertIn("MAC address of the default gateway", netid.fallback_of(found) or "")
            self.assertIsNone(netid.legacy_of(found))

    def test_arp_supplies_the_mac_when_the_neighbour_entry_is_missing(self) -> None:
        run = FakeRun(with_gateway(mac=None), RunResult(0, ARP_TEXT_WINDOWS, ""))
        found = windows_id(run)
        self.assertEqual(found, windows_id(FakeRun(with_gateway("AA-BB-CC-DD-EE-FF"))))
        self.assertEqual(run.calls[1][0], ARP)
        self.assertEqual(run.calls[1][1], ["-a", "192.168.1.1", "-N", BIND])

    def test_an_arp_that_cannot_help_gives_the_fallback_id(self) -> None:
        fallback = netid.windows_network_id("Mauler's House 5", "192.168.1.0/24")
        for results, arp in (
            ((with_gateway(mac=None), RunResult(1, "", "")), ARP),
            ((with_gateway(mac=None), RunResult(0, "No ARP Entries Found.", "")), ARP),
            ((with_gateway(mac=None), RunResult(0, ARP_TEXT_WINDOWS.replace("192.168.1.1 ", "192.168.1.9 "), "")), ARP),
            ((with_gateway(mac=None), RunResult(0, ARP_TEXT_WINDOWS.replace("aa-bb-cc-dd-ee-ff", "00-00-00-00-00-00"), "")), ARP),
            ((with_gateway(mac=None), OSError("no arp")), ARP),
            ((with_gateway(mac=None), TimeoutError("slow")), ARP),
            ((with_gateway(mac=None),), None),
            ((with_gateway(mac=None, gateway="not an address"),), ARP),
        ):
            found = windows_id(FakeRun(*results), arp=arp)
            self.assertEqual(found, fallback)
            self.assertIsNotNone(netid.fallback_of(found))

    def test_an_old_format_entry_does_not_open_the_gate_when_the_gateway_mac_is_readable(self) -> None:
        found = windows_id(FakeRun(with_gateway()))
        legacy = netid.legacy_of(found)
        assert found is not None and legacy is not None
        self.assertFalse(netid.gate_state((), found).allowed)
        closed = netid.gate_state((legacy,), found)
        self.assertEqual((closed.allowed, closed.reason), (False, netid.LEGACY_ONLY))
        self.assertIn("net networks add", netid.LEGACY_ONLY)
        self.assertTrue(netid.gate_state((found,), found).allowed)
        self.assertEqual(netid.gate_state(("other",), found).reason, netid.NOT_ALLOWED)

    def test_an_old_format_entry_still_matches_the_fallback_id_it_was_built_for(self) -> None:
        fallback = windows_id(FakeRun(windows_reply()))
        assert fallback is not None
        self.assertIsNone(netid.legacy_of(fallback))
        self.assertTrue(netid.gate_state((str(fallback),), fallback).allowed)

    def test_an_explicit_command_can_still_upgrade_an_old_format_entry(self) -> None:
        found = windows_id(FakeRun(with_gateway()))
        legacy = netid.legacy_of(found)
        assert found is not None and legacy is not None
        self.assertEqual(netid.upgraded_allowed(("other", legacy), found), ("other", found))
        self.assertIsNone(netid.upgraded_allowed(("other", found), found))
        self.assertIsNone(netid.upgraded_allowed(("other",), found))
        self.assertIsNone(netid.upgraded_allowed((legacy,), windows_id(FakeRun(windows_reply()))))

    def test_a_new_format_entry_does_not_match_a_network_with_another_gateway(self) -> None:
        mine = windows_id(FakeRun(with_gateway("AA-BB-CC-DD-EE-FF")))
        other = windows_id(FakeRun(with_gateway("11-22-33-44-55-66")))
        assert mine is not None
        self.assertFalse(netid.gate_state((mine,), other).allowed)

    def test_the_fallback_id_does_not_match_a_stored_new_format_entry(self) -> None:
        stored = windows_id(FakeRun(with_gateway()))
        assert stored is not None
        self.assertFalse(netid.gate_state((stored,), windows_id(FakeRun(windows_reply()))).allowed)


def route_json(gateway: str | None = "192.168.1.1", dev: str = "eth0") -> RunResult:
    row: dict[str, Any] = {"dst": "default", "dev": dev}
    if gateway is not None:
        row["gateway"] = gateway
    return RunResult(0, json.dumps([row]), "")


def neigh_json(mac: str | None = "aa:bb:cc:dd:ee:ff", dst: str = "192.168.1.1") -> RunResult:
    row: dict[str, Any] = {"dst": dst, "dev": "eth0", "state": ["REACHABLE"]}
    if mac is not None:
        row["lladdr"] = mac
    return RunResult(0, json.dumps([row]), "")


def linux_id(run: FakeRun, prefix: int = 24, alias: str = "eth0") -> str | None:
    with mock.patch.object(netid.os.path, "isfile", return_value=True):
        return netid.current_network_id(BIND, platform="linux", run=run, interface=interface(prefix, alias))


class LinuxTest(unittest.TestCase):
    def test_the_id_is_the_gateway_mac_and_the_subnet(self) -> None:
        run = FakeRun(route_json(), neigh_json())
        found = linux_id(run)
        self.assertEqual(found, "aa:bb:cc:dd:ee:ff_192.168.1.0_24")
        assert found is not None
        self.assertIsNotNone(NETWORK_ID.fullmatch(found))
        self.assertEqual(run.calls[0][1], ["-j", "-4", "route", "show", "default"])
        self.assertEqual(run.calls[1][1], ["-j", "-4", "neigh", "show", "192.168.1.1"])

    def test_the_mac_is_lowercased(self) -> None:
        self.assertEqual(linux_id(FakeRun(route_json(), neigh_json("AA:BB:CC:DD:EE:FF"))), "aa:bb:cc:dd:ee:ff_192.168.1.0_24")

    def test_unknown_gateways_macs_and_tools_give_none(self) -> None:
        self.assertIsNone(linux_id(FakeRun(route_json(gateway=None))))
        self.assertIsNone(linux_id(FakeRun(route_json(dev="wlan0"))))
        self.assertIsNone(linux_id(FakeRun(route_json(), neigh_json(mac=None))))
        self.assertIsNone(linux_id(FakeRun(route_json(), neigh_json("00:00:00:00:00:00"))))
        self.assertIsNone(linux_id(FakeRun(route_json(), neigh_json("not-a-mac"))))
        self.assertIsNone(linux_id(FakeRun(route_json(), neigh_json(dst="192.168.1.2"))))
        self.assertIsNone(linux_id(FakeRun(RunResult(1, "", ""))))
        self.assertIsNone(linux_id(FakeRun(RunResult(0, "garbage", ""))))
        self.assertIsNone(linux_id(FakeRun(route_json(), RunResult(1, "", ""))))
        self.assertIsNone(linux_id(FakeRun(OSError("no ip"))))
        self.assertIsNone(linux_id(FakeRun(route_json(), TimeoutError("slow"))))

    def test_an_unknown_subnet_gives_none(self) -> None:
        self.assertIsNone(linux_id(FakeRun(route_json(), neigh_json()), prefix=32))

    def test_no_ip_tool_gives_none(self) -> None:
        with mock.patch.object(netid.os.path, "isfile", return_value=False):
            self.assertIsNone(netid.current_network_id(BIND, platform="linux", run=FakeRun(), interface=interface()))

    def test_an_address_the_system_does_not_know_gives_none(self) -> None:
        def unknown(_address: str) -> InterfaceInfo:
            raise InterfaceError("no such address")

        with mock.patch.object(netid.os.path, "isfile", return_value=True):
            self.assertIsNone(netid.current_network_id(BIND, platform="linux", run=FakeRun(), interface=unknown))


ROUTE_TEXT = """   route to: default
destination: default
       mask: default
    gateway: 192.168.1.1
  interface: en0
      flags: <UP,GATEWAY,DONE,STATIC,PRCLONING,GLOBAL>
"""
IFCONFIG_TEXT = """en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
	inet6 fe80::1%en0 prefixlen 64 scopeid 0x6
	inet 192.168.1.35 netmask 0xffffff00 broadcast 192.168.1.255
"""
ARP_TEXT = "? (192.168.1.1) at a:b:c:d:e:f on en0 ifscope [ethernet]\n"


def macos_id(*results: RunResult | Exception) -> str | None:
    with mock.patch.object(netid.os.path, "isfile", return_value=True):
        return netid.current_network_id(BIND, platform="darwin", run=FakeRun(*results))


class MacosTest(unittest.TestCase):
    def test_the_id_is_the_gateway_mac_and_the_subnet(self) -> None:
        found = macos_id(RunResult(0, ROUTE_TEXT, ""), RunResult(0, IFCONFIG_TEXT, ""), RunResult(0, ARP_TEXT, ""))
        self.assertEqual(found, "0a:0b:0c:0d:0e:0f_192.168.1.0_24")

    def test_unknown_parts_give_none(self) -> None:
        self.assertIsNone(macos_id(RunResult(1, "", "")))
        self.assertIsNone(macos_id(RunResult(0, "no gateway here", "")))
        self.assertIsNone(macos_id(RunResult(0, ROUTE_TEXT, ""), RunResult(0, "en0: nothing", ""), RunResult(0, ARP_TEXT, "")))
        self.assertIsNone(macos_id(RunResult(0, ROUTE_TEXT, ""), RunResult(0, IFCONFIG_TEXT, ""), RunResult(0, "(incomplete)", "")))
        self.assertIsNone(macos_id(RunResult(0, ROUTE_TEXT, ""), RunResult(0, IFCONFIG_TEXT, ""), RunResult(1, "", "")))
        self.assertIsNone(macos_id(OSError("no route")))

    def test_missing_tools_give_none(self) -> None:
        with mock.patch.object(netid.os.path, "isfile", return_value=False):
            self.assertIsNone(netid.current_network_id(BIND, platform="darwin", run=FakeRun()))


class GateTest(unittest.TestCase):
    def test_a_listed_network_is_allowed(self) -> None:
        state = netid.gate_state(("a", "b"), "b")
        self.assertEqual((state.allowed, state.network_id, state.reason), (True, "b", None))

    def test_an_unlisted_network_is_refused_with_the_documented_text(self) -> None:
        state = netid.gate_state(("a",), "b")
        self.assertEqual((state.allowed, state.network_id), (False, "b"))
        self.assertEqual(state.reason, "this network is not in network.allowedNetworks")

    def test_an_unknown_id_fails_closed_and_says_why(self) -> None:
        state = netid.gate_state(("a",), None)
        self.assertFalse(state.allowed)
        self.assertIn("can't be read", state.reason or "")

    def test_an_empty_list_allows_nothing(self) -> None:
        self.assertFalse(netid.gate_state((), "a").allowed)
        self.assertFalse(netid.gate_state((), None).allowed)

    def test_check_network_reads_through_the_given_function_and_fails_closed_on_errors(self) -> None:
        self.assertTrue(netid.check_network(("a",), BIND, lambda _bind: "a").allowed)

        def broken(_bind: str) -> str | None:
            raise OSError("no tool")

        self.assertFalse(netid.check_network(("a",), BIND, broken).allowed)


if __name__ == "__main__":
    unittest.main()

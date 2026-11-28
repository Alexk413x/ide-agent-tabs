from __future__ import annotations

import unittest
from unittest import mock

from ide_agent_tabs.net import transport
from ide_agent_tabs.net.interfaces import InterfaceError, InterfaceInfo
from ide_agent_tabs.net.pairing import PairingError
from ide_agent_tabs.net.policy import NetworkConfig
from ide_agent_tabs.net.transport import LanTransport, network_enabled, transport_for

INFO = InterfaceInfo("Ethernet", "192.168.1.10", 24, "192.168.1.0/24", "Private", True)


class ModeTest(unittest.TestCase):
    def test_enabled_modes_hold_lan_and_tailscale(self) -> None:
        self.assertEqual(transport.ENABLED_MODES, frozenset({"lan", "tailscale"}))

    def test_network_enabled_follows_the_mode(self) -> None:
        self.assertTrue(network_enabled(NetworkConfig(network="lan")))
        self.assertFalse(network_enabled(NetworkConfig()))
        self.assertFalse(network_enabled(NetworkConfig(network="off", bind="192.168.1.10")))

    def test_transport_for_returns_a_lan_transport_when_lan(self) -> None:
        self.assertIsInstance(transport_for(NetworkConfig(network="lan")), LanTransport)

    def test_transport_for_returns_none_when_off(self) -> None:
        self.assertIsNone(transport_for(NetworkConfig()))


class LanTransportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.lan = LanTransport()

    def test_bind_address_returns_an_allowed_bind(self) -> None:
        self.assertEqual(self.lan.bind_address(NetworkConfig(network="lan", bind="192.168.1.10")), "192.168.1.10")

    def test_bind_address_is_none_without_a_bind_or_with_a_refused_one(self) -> None:
        self.assertIsNone(self.lan.bind_address(NetworkConfig(network="lan")))
        self.assertIsNone(self.lan.bind_address(NetworkConfig(network="lan", bind="127.0.0.1")))

    def test_allows_bind_delegates_to_the_policy_check(self) -> None:
        self.assertTrue(self.lan.allows_bind("10.0.0.5"))
        self.assertFalse(self.lan.allows_bind("8.8.8.8"))
        self.assertFalse(self.lan.allows_bind("fe80::1%eth0"))

    def test_check_destination_delegates_to_the_pairing_check(self) -> None:
        self.assertEqual(self.lan.check_destination("192.168.1.20", 47830, "192.168.1.10", 24), "192.168.1.20")
        with self.assertRaises(PairingError):
            self.lan.check_destination("10.0.0.5", 47830, "192.168.1.10", 24)
        with mock.patch.object(transport, "check_lan_destination", return_value="x") as check:
            self.assertEqual(self.lan.check_destination("a", 1, "b", 2), "x")
        check.assert_called_once_with("a", 1, "b", 2)

    def test_check_connection_accepts_any_address(self) -> None:
        self.assertIsNone(self.lan.check_connection("192.168.1.20"))
        self.assertIsNone(self.lan.check_connection("8.8.8.8"))

    def test_firewall_scope_is_the_interface_subnet(self) -> None:
        self.assertEqual(self.lan.firewall_scope(INFO), "192.168.1.0/24")

    def test_require_ready_delegates_to_the_interface_check(self) -> None:
        with mock.patch.object(transport, "require_lan_ready", return_value=INFO) as ready:
            self.assertIs(self.lan.require_ready("192.168.1.10", True), INFO)
        ready.assert_called_once_with("192.168.1.10", allow_virtual=True)

    def test_require_ready_passes_the_error_through(self) -> None:
        with mock.patch.object(transport, "require_lan_ready", side_effect=InterfaceError("Public")), self.assertRaises(InterfaceError):
            self.lan.require_ready("192.168.1.10")


class WaitReadyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def wait(self, timeout_s: float) -> InterfaceInfo:
        return LanTransport().wait_ready("192.168.1.10", timeout_s, sleep=self.sleep, clock=self.clock)

    def test_returns_at_once_when_ready(self) -> None:
        with mock.patch.object(transport, "require_lan_ready", return_value=INFO) as ready:
            self.assertIs(self.wait(5.0), INFO)
        self.assertEqual((ready.call_count, self.sleeps), (1, []))

    def test_retries_until_the_interface_is_ready(self) -> None:
        results = [InterfaceError("down"), InterfaceError("down"), INFO]
        with mock.patch.object(transport, "require_lan_ready", side_effect=results) as ready:
            self.assertIs(self.wait(5.0), INFO)
        self.assertEqual((ready.call_count, self.sleeps), (3, [1.0, 1.0]))

    def test_raises_the_last_error_after_the_timeout(self) -> None:
        patcher = mock.patch.object(transport, "require_lan_ready", side_effect=InterfaceError("down"))
        with patcher as ready, self.assertRaises(InterfaceError):
            self.wait(2.0)
        self.assertEqual((ready.call_count, self.sleeps), (3, [1.0, 1.0]))

    def test_a_zero_timeout_tries_once(self) -> None:
        patcher = mock.patch.object(transport, "require_lan_ready", side_effect=InterfaceError("down"))
        with patcher as ready, self.assertRaises(InterfaceError):
            self.wait(0.0)
        self.assertEqual((ready.call_count, self.sleeps), (1, []))


if __name__ == "__main__":
    unittest.main()

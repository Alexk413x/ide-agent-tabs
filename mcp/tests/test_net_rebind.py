from __future__ import annotations

import contextlib
import json
import os
import socket
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest import mock

from ide_agent_tabs.net import listener
from ide_agent_tabs.net.interfaces import InterfaceInfo, private_interfaces
from ide_agent_tabs.net.listener import Listener, Options, read_listener
from ide_agent_tabs.net.policy import NetworkConfig
from net_support import LOOPBACK, TEST_NETWORK, FakeIngress, create_home, make_root, wait_until
from test_net_join import NEEDS

ALIAS = "localhost"
OTHER_NETWORK = "other-network"


def info(address: str, *, physical: bool = True) -> InterfaceInfo:
    return InterfaceInfo("Wi-Fi", address, 24, "192.168.1.0/24", "Private", physical)


class Machine:
    def __init__(self) -> None:
        self.gone = False
        self.candidates: list[InterfaceInfo] = [info(ALIAS)]
        self.networks: dict[str, str | None] = {}

    def present(self, address: str) -> bool:
        return not (self.gone and address == LOOPBACK)

    def network_id(self, address: str) -> str | None:
        return self.networks.get(address, TEST_NETWORK)


class Running:
    def __init__(
        self,
        test: unittest.TestCase,
        home: str,
        ingress_port: int,
        machine: Machine,
        *,
        bind: str = LOOPBACK,
        wait_s: float = 30.0,
        injected: bool = True,
    ) -> None:
        self.home = home
        self.ready = threading.Event()
        self.code: int | None = None
        self.options = Options(
            config=NetworkConfig(network="lan", bind=bind, port=0, allowed_networks=(TEST_NETWORK,)) if injected else None,
            bind_check=lambda _address: True,
            network_check=lambda _bind: False,
            network_id=machine.network_id,
            join_port=0,
            join_source_ok=lambda _address: True,
            server_port=ingress_port,
            ready=lambda _port: self.ready.set(),
            address_present=machine.present,
            candidates=lambda: list(machine.candidates),
            rebind_wait_s=wait_s,
        )
        self.listener = Listener(home, self.options)
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        test.addCleanup(self.stop)
        assert self.ready.wait(20), "the listener did not start"

    def _run(self) -> None:
        self.code = self.listener.run()
        self.ready.set()

    def stop(self) -> None:
        self.options.stop.set()
        self.thread.join(10)

    def log(self) -> str:
        path = listener.log_file(self.home)
        return Path(path).read_text(encoding="utf-8") if os.path.exists(path) else ""

    def saved_bind(self) -> Any:
        return json.loads(Path(self.home, "config.json").read_text(encoding="utf-8"))["network"].get("bind")

    def bound_to(self, address: str) -> bool:
        found = read_listener(self.home)
        return found is not None and found.bind == address


def accepts(port: int, address: str = LOOPBACK) -> bool:
    try:
        with socket.create_connection((address, port), timeout=2):
            return True
    except OSError:
        return False


@NEEDS
class RebindTest(unittest.TestCase):
    home: str

    @classmethod
    def setUpClass(cls) -> None:
        cls.home = create_home(make_root(cls), "a")

    def setUp(self) -> None:
        for name in ("config.json", os.path.join("net", "listener.json"), os.path.join("net", "listener.log")):
            with contextlib.suppress(OSError):
                os.remove(os.path.join(self.home, name))
        Path(self.home, "config.json").write_text(json.dumps({"network": {"network": "lan", "bind": LOOPBACK}}), encoding="utf-8")
        self.ingress = FakeIngress(self.home)
        self.addCleanup(self.ingress.close)
        self.machine = Machine()
        patch = mock.patch.multiple(listener, PROFILE_REFRESH_S=0.0, MONITOR_S=0.05, REBIND_RETRY_S=0.0, JOIN_RETRY_S=0.0)
        patch.start()
        self.addCleanup(patch.stop)

    def start(self, **changes: Any) -> Running:
        return Running(self, self.home, self.ingress.port, self.machine, **changes)

    def test_a_lost_address_is_replaced_by_the_address_on_the_allowed_network(self) -> None:
        running = self.start()
        info_before = read_listener(self.home)
        assert info_before is not None
        epoch = running.listener._address_epoch  # pyright: ignore[reportPrivateUsage]
        self.assertTrue(accepts(info_before.port))
        self.machine.gone = True
        self.assertTrue(wait_until(lambda: running.bound_to(ALIAS), 10), running.log())
        moved = read_listener(self.home)
        assert moved is not None
        self.assertEqual((moved.pid, moved.started_ms), (info_before.pid, info_before.started_ms))
        self.assertEqual(running.saved_bind(), ALIAS)
        self.assertTrue(accepts(moved.port, ALIAS))
        self.assertFalse(accepts(info_before.port))
        self.assertIsNone(running.code)
        self.assertTrue(running.listener._watch.allowed())  # pyright: ignore[reportPrivateUsage, reportOptionalMemberAccess]
        self.assertGreater(running.listener._address_epoch, epoch)  # pyright: ignore[reportPrivateUsage]
        self.assertIn(f"now listening on {ALIAS}", running.log())

    def test_the_join_server_and_the_beacons_follow_the_listener_to_the_new_address(self) -> None:
        running = self.start()
        moves: list[str] = []
        running.listener._beacons = mock.Mock(rebind=moves.append)  # pyright: ignore[reportPrivateUsage]
        running.listener._live = replace(running.listener._live, discoverable=True)  # pyright: ignore[reportPrivateUsage]
        self.assertTrue(wait_until(lambda: running.listener.join_port() is not None, 10), running.log())
        old_join = running.listener._join_sock  # pyright: ignore[reportPrivateUsage]
        self.machine.gone = True
        self.assertTrue(wait_until(lambda: moves == [ALIAS], 10), running.log())
        self.assertTrue(wait_until(lambda: running.listener._join_sock is not old_join, 10), running.log())  # pyright: ignore[reportPrivateUsage]
        self.assertTrue(wait_until(lambda: running.listener.join_port() is not None, 10), running.log())
        self.assertIn(f"serving joins on {ALIAS}", running.log())

    def test_the_listener_waits_for_an_address_and_stops_when_none_appears(self) -> None:
        self.machine.candidates = []
        running = self.start(wait_s=0.4)
        self.machine.gone = True
        self.assertTrue(wait_until(lambda: running.code is not None or not running.thread.is_alive(), 10), running.log())
        self.assertEqual(running.code, 0)
        self.assertIn("no address on an allowed network replaced it", running.log())
        self.assertIn("stopping:", running.log())

    def test_an_address_that_appears_while_the_listener_waits_is_used(self) -> None:
        self.machine.candidates = []
        running = self.start(wait_s=30.0)
        self.machine.gone = True
        self.assertTrue(wait_until(lambda: "no address on an allowed network replaced it" in running.log(), 10))
        self.assertFalse(running.listener._watch.allowed())  # pyright: ignore[reportPrivateUsage, reportOptionalMemberAccess]
        self.machine.candidates = [info(ALIAS)]
        self.assertTrue(wait_until(lambda: running.bound_to(ALIAS), 10), running.log())
        self.assertTrue(running.listener._watch.allowed())  # pyright: ignore[reportPrivateUsage, reportOptionalMemberAccess]
        self.assertIsNone(running.code)

    def test_an_address_on_a_network_that_is_not_allowed_is_not_used(self) -> None:
        self.machine.networks[ALIAS] = OTHER_NETWORK
        running = self.start(wait_s=0.4)
        self.machine.gone = True
        self.assertTrue(wait_until(lambda: running.code is not None, 10), running.log())
        self.assertEqual(running.saved_bind(), LOOPBACK)

    def test_an_address_whose_network_cannot_be_read_is_not_used(self) -> None:
        self.machine.networks[ALIAS] = None
        running = self.start(wait_s=0.4)
        self.machine.gone = True
        self.assertTrue(wait_until(lambda: running.code is not None, 10), running.log())
        self.assertEqual(running.saved_bind(), LOOPBACK)

    def test_a_bind_address_that_is_gone_at_start_is_replaced_before_listening(self) -> None:
        self.machine.gone = True
        running = self.start()
        moved = read_listener(self.home)
        assert moved is not None
        self.assertEqual(moved.bind, ALIAS)
        self.assertEqual(running.saved_bind(), ALIAS)
        self.assertTrue(accepts(moved.port, ALIAS))
        self.assertIn(f"using {ALIAS}", running.log())

    def patch_config(self) -> list[NetworkConfig]:
        config = [NetworkConfig(network="lan", bind=LOOPBACK, port=0, allowed_networks=(TEST_NETWORK,))]
        for patch in (
            mock.patch.object(listener, "load_network_config", lambda _home: (config[0], [])),
            mock.patch.object(listener, "CONFIG_RECHECK_S", 0.05),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        return config

    def test_a_bind_changed_by_a_command_while_the_old_address_is_gone_is_followed(self) -> None:
        config = self.patch_config()
        self.machine.candidates = []
        running = self.start(injected=False)
        self.machine.gone = True
        self.assertTrue(wait_until(lambda: "no address on an allowed network replaced it" in running.log(), 10))
        config[0] = replace(config[0], bind=ALIAS)
        self.assertTrue(wait_until(lambda: running.bound_to(ALIAS), 10), running.log())
        self.assertIsNone(running.code)
        self.assertTrue(running.listener._watch.allowed())  # pyright: ignore[reportPrivateUsage, reportOptionalMemberAccess]

    def test_a_bind_changed_by_a_command_while_the_old_address_is_present_still_stops_the_listener(self) -> None:
        config = self.patch_config()
        running = self.start(injected=False)
        config[0] = replace(config[0], bind=ALIAS)
        self.assertTrue(wait_until(lambda: running.code is not None, 10), running.log())
        self.assertIn("the network setting changed", running.log())


class PickAddressTest(unittest.TestCase):
    def pick(self, candidates: list[str], old: str, networks: dict[str, str | None] | None = None) -> str | None:
        found = networks or {}
        options = Options(
            candidates=lambda: [info(address) for address in candidates],
            network_id=lambda address: found.get(address, TEST_NETWORK),
        )
        return Listener(make_root(self), options)._pick_address((TEST_NETWORK,), old)  # pyright: ignore[reportPrivateUsage]

    def test_the_only_address_on_an_allowed_network_is_chosen(self) -> None:
        self.assertEqual(self.pick(["192.168.1.40"], "192.168.1.10"), "192.168.1.40")

    def test_an_address_on_another_subnet_of_an_allowed_network_is_chosen_when_it_is_the_only_one(self) -> None:
        self.assertEqual(self.pick(["10.0.5.2"], "192.168.1.10"), "10.0.5.2")

    def test_with_two_candidates_the_one_near_the_old_address_wins(self) -> None:
        self.assertEqual(self.pick(["10.0.5.2", "192.168.1.40"], "192.168.1.10"), "192.168.1.40")

    def test_two_candidates_that_are_both_near_or_both_far_are_not_guessed(self) -> None:
        self.assertIsNone(self.pick(["192.168.1.40", "192.168.1.41"], "192.168.1.10"))
        self.assertIsNone(self.pick(["10.0.5.2", "10.0.6.2"], "192.168.1.10"))

    def test_addresses_on_networks_that_are_not_allowed_or_unreadable_are_skipped(self) -> None:
        found: dict[str, str | None] = {"192.168.1.40": OTHER_NETWORK, "192.168.1.41": None}
        self.assertIsNone(self.pick(["192.168.1.40", "192.168.1.41"], "192.168.1.10", found))
        self.assertEqual(self.pick(["192.168.1.40", "192.168.1.42"], "192.168.1.10", found), "192.168.1.42")

    def test_an_ipv6_old_address_leaves_only_the_single_candidate_rule(self) -> None:
        self.assertEqual(self.pick(["192.168.1.40"], "fd00::5"), "192.168.1.40")
        self.assertIsNone(self.pick(["192.168.1.40", "192.168.1.41"], "fd00::5"))


class CandidateTest(unittest.TestCase):
    def test_only_private_physical_ipv4_addresses_qualify(self) -> None:
        described = {
            "192.168.1.5": info("192.168.1.5"),
            "10.0.0.8": info("10.0.0.8", physical=False),
            "172.20.0.2": info("172.20.0.2"),
        }

        def describe(address: str) -> InterfaceInfo:
            if address not in described:
                raise ValueError("unknown")
            return described[address]

        found = private_interfaces(
            ["127.0.0.1", "169.254.1.1", "8.8.8.8", "fd00::1", "192.168.1.5", "10.0.0.8", "172.20.0.2", "bad"], describe
        )
        self.assertEqual([i.address for i in found], ["192.168.1.5", "172.20.0.2"])


if __name__ == "__main__":
    unittest.main()

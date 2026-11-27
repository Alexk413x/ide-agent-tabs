from __future__ import annotations

import contextlib
import json
import os
import socket
import sys
import threading
import time
import unittest
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest import mock

from ide_agent_tabs.net import listener, pairing
from ide_agent_tabs.net.client import NetClientError, send_request
from ide_agent_tabs.net.hubjoin import HubJoiner, local_pair_identity
from ide_agent_tabs.net.listener import Listener, Options
from ide_agent_tabs.net.netid import tagged_network_id
from ide_agent_tabs.net.peers import save_peer
from ide_agent_tabs.net.policy import Hub, NetworkConfig, Policy, grant_for, load_policy, save_policy
from ide_agent_tabs.net.selfupdate import Install, JoinUpdater, UpdateCommand
from net_support import (
    LOOPBACK,
    FakeIngress,
    cert_of,
    create_home,
    device_of,
    make_root,
    reset_peers,
    trust_peer,
    wait_until,
)
from test_net_join import FAST, HUB_ID, NEEDS, make_hub, reset, set_name

HOME_NETWORK = "home-network"
OTHER_NETWORK = "other-network"
NOTICES: list[str] = []


class Cell:
    def __init__(self, config: NetworkConfig) -> None:
        self.config = config
        self.network: str | None = HOME_NETWORK

    def load(self, _home: str) -> tuple[NetworkConfig, list[str]]:
        return self.config, []

    def network_id(self, _bind: str) -> str | None:
        return self.network


class Running:
    def __init__(
        self,
        test: unittest.TestCase,
        home: str,
        cell: Cell,
        ingress_port: int,
        source_ok: Callable[[str], bool] = lambda _address: True,
        extra: dict[str, Any] | None = None,
    ) -> None:
        self.home = home
        self.ready = threading.Event()
        self.options = Options(
            bind_check=lambda _address: True,
            network_check=lambda _bind: False,
            network_id=cell.network_id,
            join_port=0,
            join_timeouts=FAST,
            join_source_ok=source_ok,
            notice=NOTICES.append,
            server_port=ingress_port,
            ready=lambda _port: self.ready.set(),
            **(extra or {}),
        )
        self.listener = Listener(home, self.options)
        self.code: int | None = None
        self.port = 0
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        test.addCleanup(self.stop)
        assert self.ready.wait(20), "the listener did not start"
        assert self.code is None, f"the listener refused to start with {self.code}"
        self.port = self.listener._sock.getsockname()[1]  # pyright: ignore[reportPrivateUsage, reportOptionalMemberAccess]

    def _run(self) -> None:
        self.code = self.listener.run()
        self.ready.set()

    def stop(self) -> None:
        self.options.stop.set()
        self.thread.join(10)

    def log(self) -> str:
        path = listener.log_file(self.home)
        return Path(path).read_text(encoding="utf-8") if os.path.exists(path) else ""

    def joins_on(self) -> bool:
        return self.listener.join_port() is not None


@NEEDS
class DiscoverableListenerTest(unittest.TestCase):
    a: str
    b: str
    c: str

    @classmethod
    def setUpClass(cls) -> None:
        root = make_root(cls)
        cls.a, cls.b, cls.c = (create_home(root, name) for name in ("a", "b", "c"))
        set_name(cls.a, "alpha")
        set_name(cls.b, "bravo")
        set_name(cls.c, "charlie")

    def setUp(self) -> None:
        NOTICES.clear()
        with contextlib.suppress(OSError):
            os.remove(listener.log_file(self.a))
        for home in (self.a, self.b, self.c):
            reset(home)
            reset_peers(home)
        with contextlib.suppress(OSError):
            os.remove(os.path.join(self.a, "config.json"))
        self.ingress = FakeIngress(self.a)
        self.addCleanup(self.ingress.close)
        self.listener_options: dict[str, Any] = {}
        patch = mock.patch.multiple(
            listener, PROFILE_REFRESH_S=0.0, MONITOR_S=0.05, CONFIG_RECHECK_S=0.05, JOIN_RETRY_S=0.0, NETWORK_ID_GRACE_S=0.0
        )
        patch.start()
        self.addCleanup(patch.stop)

    def start(self, source_ok: Callable[[str], bool] = lambda _address: True, **changes: Any) -> tuple[Running, Cell]:
        values: dict[str, Any] = {
            "network": "lan",
            "bind": LOOPBACK,
            "port": 0,
            "allowed_networks": (HOME_NETWORK,),
            "discoverable": True,
        }
        values.update(changes)
        cell = Cell(NetworkConfig(**values))
        loader = mock.patch.object(listener, "load_network_config", cell.load)
        loader.start()
        self.addCleanup(loader.stop)
        return Running(self, self.a, cell, self.ingress.port, source_ok, self.listener_options), cell

    def join(self, running: Running, **options: Any) -> pairing.PairResult:
        port = running.listener.join_port()
        assert port is not None
        return pairing.dial_join(
            LOOPBACK,
            port,
            device_of(self.a),
            local_pair_identity(self.b),
            HubJoiner(self.b),
            pairing.Limits(self.b),
            timeouts=FAST,
            **options,
        )

    def give_b_a_hub(self) -> None:
        save_policy(self.b, Policy((), 0, Hub(HUB_ID, "home")))

    def test_it_serves_joins_while_discoverable_is_on_and_a_join_saves_the_member(self) -> None:
        self.give_b_a_hub()
        running, _cell = self.start()
        self.assertTrue(wait_until(running.joins_on, 5))
        result = self.join(running)
        self.assertEqual(result.outcome, "paired")
        self.assertTrue(wait_until(lambda: grant_for(load_policy(self.a), device_of(self.b)) is not None, 5))
        peer = grant_for(load_policy(self.a), device_of(self.b))
        assert peer is not None
        self.assertTrue(peer.member)
        self.assertEqual(load_policy(self.a).hub, Hub(HUB_ID, "home"))
        self.assertIn("a device joined the hub", running.log())
        self.assertTrue(any("joined this machine's hub" in notice for notice in NOTICES))

    def updater(self, log: list[str], posted: list[str], calls: list[list[str]], restarts: list[str]) -> JoinUpdater:
        command = UpdateCommand((sys.executable, "plugin", "update", "ide-agent-tabs@market"), "tool plugin update ide-agent-tabs@market")

        def run(argv: list[str], **_kwargs: Any) -> Any:
            calls.append(argv)
            return mock.Mock(returncode=0, stdout="updated", stderr="")

        return JoinUpdater(
            self.a,
            "0.14.3",
            log.append,
            lambda _home, text: posted.append(text) or 1,
            command=lambda: command,
            fresh=lambda: Install("/plugins/0.15.0", "0.15.0"),
            runner=run,
            spawn=lambda _home, install, _port: restarts.append(install.root) or True,
            background=lambda job: job(),
        )

    def test_a_join_from_a_newer_machine_runs_the_unattended_update_and_notifies_the_sessions(self) -> None:
        self.give_b_a_hub()
        log: list[str] = []
        posted: list[str] = []
        calls: list[list[str]] = []
        restarts: list[str] = []
        self.listener_options = {"plugin_version": "0.14.3", "updater": self.updater(log, posted, calls, restarts)}
        running, _cell = self.start()
        self.assertTrue(wait_until(running.joins_on, 5))
        result = self.join(running, plugin_version="0.15.0")
        self.assertEqual(result.outcome, "paired")
        self.assertTrue(wait_until(lambda: restarts == ["/plugins/0.15.0"], 5), running.log())
        self.assertEqual(len(calls), 1)
        self.assertEqual(log[0].split(" command=")[0], f"self-update: attempt peer={device_of(self.b)}")
        self.assertEqual(log[1], "self-update: ok")
        self.assertIn("a join from a machine on version 0.15.0", posted[0])
        self.assertEqual(result.peer.plugin_version if result.peer else None, "0.14.3")

    def test_a_join_from_an_older_or_legacy_machine_runs_no_update(self) -> None:
        self.give_b_a_hub()
        log: list[str] = []
        posted: list[str] = []
        calls: list[list[str]] = []
        restarts: list[str] = []
        self.listener_options = {"plugin_version": "0.14.3", "updater": self.updater(log, posted, calls, restarts)}
        running, _cell = self.start()
        self.assertTrue(wait_until(running.joins_on, 5))
        self.assertEqual(self.join(running, plugin_version="0.14.2").outcome, "paired")
        self.assertTrue(wait_until(lambda: "a device joined the hub" in running.log(), 5))
        self.assertEqual((calls, log, posted, restarts), ([], [], [], []))

    def test_a_refused_join_connection_is_logged_with_its_address_and_reason(self) -> None:
        running, _cell = self.start(source_ok=lambda _address: False)
        self.assertTrue(wait_until(running.joins_on, 5))
        port = running.listener.join_port()
        assert port is not None
        with socket.create_connection((LOOPBACK, port), timeout=5):
            self.assertTrue(wait_until(lambda: "join connection from 127.0.0.1 refused" in running.log(), 5))
        self.assertIn("join connection from 127.0.0.1 refused: the address is not a private address", running.log())

    def reported_joins(self) -> bool | None:
        info = listener.read_listener(self.a)
        return None if info is None else info.serving_joins

    def test_the_listener_records_its_build_and_whether_it_serves_joins(self) -> None:
        running, cell = self.start()
        self.assertTrue(wait_until(lambda: self.reported_joins() is True, 5))
        info = listener.read_listener(self.a)
        assert info is not None
        build = listener.installed_build()
        self.assertEqual((info.version, info.build), (build.version, build.id))
        cell.config = replace(cell.config, discoverable=False)
        self.assertTrue(wait_until(lambda: self.reported_joins() is False, 5))
        self.assertFalse(running.joins_on())
        cell.config = replace(cell.config, discoverable=True)
        self.assertTrue(wait_until(lambda: self.reported_joins() is True, 5))

    def test_a_join_introduces_the_new_member_to_the_other_members_over_the_member_request(self) -> None:
        make_hub(self.a)
        save_peer(self.a, device_of(self.c), cert_of(self.c).read_text(encoding="ascii"), "192.168.1.33", "charlie", member=True)
        self.give_b_a_hub()
        running, _cell = self.start()
        sent: list[tuple[str, str, dict[str, Any]]] = []

        def request(_home: str, device_id: str, op: str, body: dict[str, Any], **_options: Any) -> dict[str, Any]:
            sent.append((device_id, op, body))
            return {"ok": True, "body": {"added": True}}

        running.options.member_request = request
        self.assertTrue(wait_until(running.joins_on, 5))
        self.assertEqual(self.join(running).outcome, "paired")
        self.assertTrue(wait_until(lambda: len(sent) == 1, 5), running.log())
        ((device_id, op, body),) = sent
        self.assertEqual((device_id, op), (device_of(self.c), "introduce"))
        self.assertEqual(body["hub"], HUB_ID)
        self.assertEqual(body["member"]["deviceId"], device_of(self.b))
        self.assertEqual(body["member"]["label"], "bravo")
        self.assertTrue(wait_until(lambda: "introduced" in running.log(), 5))

    def test_an_old_format_allowed_entry_does_not_open_the_gate_for_an_id_with_a_gateway_mac_and_is_not_rewritten(self) -> None:
        original = json.dumps({"network": {"allowedNetworks": [HOME_NETWORK, OTHER_NETWORK]}})
        Path(self.a, "config.json").write_text(original, encoding="utf-8")
        running, cell = self.start()
        self.assertTrue(wait_until(running.joins_on, 5))
        cell.network = tagged_network_id("new-" + HOME_NETWORK, legacy=HOME_NETWORK)
        self.assertTrue(wait_until(lambda: not running.joins_on(), 5), running.log())
        self.assertIn("network gate closed: this network is listed only by its older id", running.log())
        time.sleep(0.5)
        self.assertEqual(Path(self.a, "config.json").read_text(encoding="utf-8"), original)
        self.assertNotIn("is now", running.log())
        self.assertFalse(running.joins_on())

    def test_a_network_id_that_fell_back_to_the_name_and_subnet_is_logged_once(self) -> None:
        running, cell = self.start()
        self.assertTrue(wait_until(running.joins_on, 5))
        note = "the MAC address of the default gateway can't be read, so the network id is built from the profile name and the subnet only"
        cell.network = tagged_network_id(HOME_NETWORK, fallback=note)
        self.assertTrue(wait_until(lambda: f"network id: {note}" in running.log(), 5))
        time.sleep(0.3)
        self.assertEqual(running.log().count(f"network id: {note}"), 1)
        self.assertTrue(running.joins_on())

    def test_it_serves_no_joins_while_discoverable_is_off(self) -> None:
        running, _cell = self.start(discoverable=False)
        self.assertFalse(wait_until(running.joins_on, 1))
        self.assertNotIn("serving joins", running.log())

    def test_turning_discoverable_off_stops_serving_joins_and_on_starts_again(self) -> None:
        self.give_b_a_hub()
        running, cell = self.start()
        self.assertTrue(wait_until(running.joins_on, 5))
        cell.config = replace(cell.config, discoverable=False)
        self.assertTrue(wait_until(lambda: not running.joins_on(), 5))
        self.assertIn("stopped serving joins", running.log())
        self.assertEqual(running.code, None)
        cell.config = replace(cell.config, discoverable=True)
        self.assertTrue(wait_until(running.joins_on, 5))
        self.assertEqual(self.join(running).outcome, "paired")

    def test_a_network_that_is_not_allowed_serves_no_joins_and_drops_members(self) -> None:
        trust_peer(self.a, self.b, "laptop")
        trust_peer(self.b, self.a, "desktop")
        running, cell = self.start()
        self.assertTrue(wait_until(running.joins_on, 5))
        self.assertTrue(send_request(self.b, device_of(self.a), "inventory", {}, timeout=10, port=running.port)["ok"])
        cell.network = OTHER_NETWORK
        self.assertTrue(wait_until(lambda: not running.joins_on(), 5))
        self.assertIn("network gate closed: this network is not in network.allowedNetworks", running.log())
        with self.assertRaises(NetClientError):
            send_request(self.b, device_of(self.a), "inventory", {}, timeout=10, port=running.port)
        cell.network = HOME_NETWORK
        self.assertTrue(wait_until(running.joins_on, 5))
        self.assertTrue(send_request(self.b, device_of(self.a), "inventory", {}, timeout=10, port=running.port)["ok"])

    def test_an_unreadable_network_id_fails_closed_and_says_why(self) -> None:
        trust_peer(self.a, self.b, "laptop")
        trust_peer(self.b, self.a, "desktop")
        running, _cell = self.start()
        self.assertTrue(wait_until(running.joins_on, 5))
        _cell.network = None
        self.assertTrue(wait_until(lambda: not running.joins_on(), 5))
        self.assertIn("can't be read", running.log())
        with self.assertRaises(NetClientError):
            send_request(self.b, device_of(self.a), "inventory", {}, timeout=10, port=running.port)

    def test_a_network_id_that_briefly_can_not_be_read_keeps_the_gate_open(self) -> None:
        trust_peer(self.a, self.b, "laptop")
        trust_peer(self.b, self.a, "desktop")
        with mock.patch.object(listener, "NETWORK_ID_GRACE_S", 60.0):
            running, cell = self.start()
            self.assertTrue(wait_until(running.joins_on, 5))
            cell.network = None
            self.assertTrue(wait_until(lambda: "the gate keeps the last id for up to 60 s" in running.log(), 5))
            time.sleep(0.5)
            self.assertTrue(running.joins_on())
            self.assertTrue(send_request(self.b, device_of(self.a), "inventory", {}, timeout=10, port=running.port)["ok"])
            cell.network = HOME_NETWORK
            self.assertTrue(wait_until(lambda: "the id of this network can be read again" in running.log(), 5))
        self.assertNotIn("network gate closed", running.log())
        self.assertNotIn("stopped serving joins", running.log())

    def test_a_network_id_that_stays_unreadable_past_the_grace_period_closes_the_gate(self) -> None:
        with mock.patch.object(listener, "NETWORK_ID_GRACE_S", 0.5):
            running, cell = self.start()
            self.assertTrue(wait_until(running.joins_on, 5))
            cell.network = None
            self.assertTrue(wait_until(lambda: not running.joins_on(), 5))
        log = running.log()
        self.assertLess(log.index("the gate keeps the last id"), log.index("network gate closed: the id of this network can't be read"))

    def test_a_changed_address_never_keeps_the_last_network_id(self) -> None:
        cell = Cell(NetworkConfig(network="lan", bind=LOOPBACK, allowed_networks=(HOME_NETWORK,)))
        gate = Listener(self.a, Options(network_id=cell.network_id))
        gate._live = cell.config  # pyright: ignore[reportPrivateUsage]
        gate._refresh_gate(force=True)  # pyright: ignore[reportPrivateUsage]
        self.assertTrue(gate._gate.allowed)  # pyright: ignore[reportPrivateUsage]
        cell.network = None
        with mock.patch.object(listener, "NETWORK_ID_GRACE_S", 60.0):
            gate._refresh_gate(force=True)  # pyright: ignore[reportPrivateUsage]
        self.assertFalse(gate._gate.allowed)  # pyright: ignore[reportPrivateUsage]
        self.assertNotIn("the gate keeps the last id", Path(listener.log_file(self.a)).read_text(encoding="utf-8"))

    def test_removing_the_network_from_the_allowed_list_closes_the_gate_at_once(self) -> None:
        running, cell = self.start()
        self.assertTrue(wait_until(running.joins_on, 5))
        cell.config = replace(cell.config, allowed_networks=())
        self.assertTrue(wait_until(lambda: not running.joins_on(), 5))

    def test_an_empty_allowed_list_opens_nothing_even_when_discoverable(self) -> None:
        running, _cell = self.start(allowed_networks=())
        self.assertFalse(wait_until(running.joins_on, 1))

    def test_the_listener_stops_the_join_socket_when_it_stops(self) -> None:
        running, _cell = self.start()
        self.assertTrue(wait_until(running.joins_on, 5))
        port = running.listener.join_port()
        running.stop()
        self.assertIsNone(running.listener.join_port())
        with socket.socket() as probe:
            probe.settimeout(1)
            self.assertNotEqual(probe.connect_ex((LOOPBACK, port or 1)), 0)


if __name__ == "__main__":
    unittest.main()

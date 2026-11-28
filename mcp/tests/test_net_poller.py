from __future__ import annotations

import json
import threading
import time
import unittest
from collections.abc import Mapping
from typing import Any

from ide_agent_tabs.net import poller, wire
from ide_agent_tabs.net.client import NetClientError
from ide_agent_tabs.net.poller import Poller
from ide_agent_tabs.net.store import NetStore
from ide_agent_tabs.shared.server import SharedServer
from net_support import wait_until
from outbound_support import Ticks, peer_grant, write_config, write_policy
from shared_support import free_port
from store_host import StoreHost
from support import temp_home

SESSION = {"id": "tab-r", "name": "api", "agent": "claude", "status": "idle"}
INVENTORY = {"re": "r", "ok": True, "body": {"sessions": [SESSION]}}


def peer_id(index: int) -> str:
    return f"PEER{index:02d}"


def peers(count: int) -> list[Any]:
    return [peer_grant(peer_id(i), f"peer-{i}", f"192.168.1.{10 + i}") for i in range(count)]


class FakeClock:
    def __init__(self) -> None:
        self.at = 1000.0

    def __call__(self) -> float:
        return self.at


class Fetch:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any], float]] = []
        self.replies: dict[str, Any] = {}
        self.gate: threading.Event | None = None
        self.active = 0
        self.most = 0
        self._lock = threading.Lock()

    def __call__(self, home: str, peer: str, op: str, body: Mapping[str, Any], **kwargs: Any) -> dict[str, Any]:
        with self._lock:
            self.calls.append((peer, op, dict(body), kwargs["timeout"]))
            self.active += 1
            self.most = max(self.most, self.active)
        try:
            if self.gate is not None:
                self.gate.wait(10)
            reply = self.replies.get(peer, INVENTORY)
            if isinstance(reply, Exception):
                raise reply
            return reply
        finally:
            with self._lock:
                self.active -= 1

    def asked(self) -> list[str]:
        with self._lock:
            return [call[0] for call in self.calls]


class PollerCase(unittest.TestCase):
    def setUp(self) -> None:
        self.home = temp_home(self)
        write_config(self.home, network="lan", bind="192.168.1.5")
        write_policy(self.home, peers(2))
        self.clock = FakeClock()
        self.ticks = Ticks()
        self.fetch = Fetch()
        self.logs: list[str] = []

    def make(self, **over: Any) -> Poller:
        fields: dict[str, Any] = {"fetch": self.fetch, "clock": self.clock, "now_ms": self.ticks, "log": self.logs.append}
        fields.update(over)
        made = Poller(self.home, **fields)
        self.addCleanup(made.stop)
        return made

    def cached(self, peer: str) -> tuple[list[wire.RemoteSession], int] | None:
        store = NetStore(self.home)
        try:
            found = store.get_inventory(peer)
        finally:
            store.close()
        return None if found is None else (wire.parse_inventory(json.loads(found[0])), found[1])


class RefreshTest(PollerCase):
    def test_the_first_pass_asks_every_peer_for_its_inventory_and_stores_the_reply(self) -> None:
        made = self.make()
        self.assertEqual(sorted(made.poll_once()), [peer_id(0), peer_id(1)])
        self.assertEqual(sorted(self.fetch.asked()), [peer_id(0), peer_id(1)])
        for _peer, op, body, timeout in self.fetch.calls:
            self.assertEqual((op, body), ("inventory", {}))
            self.assertLessEqual(timeout, 30)
        sessions, fetched = self.cached(peer_id(0)) or ([], 0)
        self.assertEqual([(s.id, s.name, s.agent, s.status) for s in sessions], [("tab-r", "api", "claude", "idle")])
        self.assertEqual(fetched, self.ticks())

    def test_a_members_folders_are_kept_beside_its_sessions(self) -> None:
        folders = ["C:\\Projects", "/home/me/code"]
        self.fetch.replies[peer_id(0)] = {"re": "r", "ok": True, "body": {"sessions": [SESSION], "folders": folders}}
        made = self.make()
        made.poll_once()
        store = NetStore(self.home)
        try:
            members = store.get_inventory(peer_id(0))
            others = store.get_inventory(peer_id(1))
        finally:
            store.close()
        assert members is not None and others is not None
        self.assertEqual(wire.parse_inventory_folders(json.loads(members[0])), folders)
        self.assertIsNone(wire.parse_inventory_folders(json.loads(others[0])))

    def test_an_inventory_with_invalid_folders_is_refused_and_marks_the_peer_offline(self) -> None:
        self.fetch.replies[peer_id(0)] = {"re": "r", "ok": True, "body": {"sessions": [SESSION], "folders": ["relative"]}}
        made = self.make()
        made.poll_once()
        self.assertIsNone(self.cached(peer_id(0)))
        self.assertTrue(made.is_failed(peer_id(0)))

    def test_a_peer_is_asked_at_most_once_in_sixty_seconds(self) -> None:
        made = self.make()
        made.poll_once()
        self.assertEqual(made.poll_once(), [])
        self.clock.at += 59.9
        self.assertEqual(made.poll_once(), [])
        self.assertEqual(len(self.fetch.calls), 2)
        self.clock.at += 0.2
        self.assertEqual(len(made.poll_once()), 2)
        self.assertEqual(len(self.fetch.calls), 4)

    def test_an_unanswered_peer_is_offline_and_keeps_its_last_inventory(self) -> None:
        made = self.make()
        made.poll_once()
        before = self.cached(peer_id(0))
        self.ticks.at += 90_000
        self.clock.at += 60
        self.fetch.replies[peer_id(0)] = NetClientError("timed out")
        made.poll_once()
        self.assertTrue(made.is_failed(peer_id(0)))
        self.assertFalse(made.is_failed(peer_id(1)))
        self.assertEqual(self.cached(peer_id(0)), before)
        self.ticks.at += 90_000
        self.clock.at += 60
        self.fetch.replies.pop(peer_id(0))
        made.poll_once()
        self.assertFalse(made.is_failed(peer_id(0)))
        after = self.cached(peer_id(0))
        assert after is not None and before is not None
        self.assertGreater(after[1], before[1])

    def test_a_refusal_or_an_invalid_inventory_marks_the_peer_offline_and_stores_nothing(self) -> None:
        bad_bodies: list[Any] = [
            {"re": "r", "ok": False, "refused": True},
            {"re": "r", "ok": True, "body": {"sessions": [{**SESSION, "extra": "x"}]}},
            {"re": "r", "ok": True, "body": {"sessions": [SESSION, SESSION]}},
            {"re": "r", "ok": True, "body": {"sessions": [{**SESSION, "agent": "bash"}]}},
            {"re": "r", "ok": True, "body": {"sessions": [{**SESSION, "name": "a\nb"}]}},
            {"re": "r", "ok": True, "body": {"sessions": "none"}},
            {"re": "r", "ok": True, "body": {}},
        ]
        for reply in bad_bodies:
            with self.subTest(reply=reply):
                made = self.make()
                self.clock.at += 60
                self.fetch.replies = {peer_id(0): reply, peer_id(1): reply}
                made.poll_once()
                self.assertTrue(made.is_failed(peer_id(0)))
                self.assertIsNone(self.cached(peer_id(0)))

    def test_an_unexpected_error_marks_the_peer_offline_and_is_logged_by_type_only(self) -> None:
        made = self.make()
        self.fetch.replies[peer_id(0)] = RuntimeError("secret path C:/Users/someone")
        made.poll_once()
        self.assertTrue(made.is_failed(peer_id(0)))
        self.assertTrue(any("RuntimeError" in line for line in self.logs))
        self.assertFalse(any("secret" in line for line in self.logs))

    def test_only_trusted_peers_are_asked_and_a_removed_peer_is_forgotten(self) -> None:
        made = self.make()
        write_policy(self.home, peers(1))
        self.assertEqual(made.poll_once(), [peer_id(0)])
        write_policy(self.home, [])
        self.clock.at += 60
        self.assertEqual(made.poll_once(), [])
        self.assertEqual(self.fetch.asked(), [peer_id(0)])

    def test_nothing_is_asked_while_the_network_is_off(self) -> None:
        made = self.make()
        for network in ({"network": "off"}, {}):
            write_config(self.home, **network)
            self.assertEqual(made.poll_once(), [])
        self.assertEqual(self.fetch.calls, [])

    def test_a_broken_policy_asks_nobody(self) -> None:
        made = self.make()
        with open(self.home + "/net/policy.json", "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertEqual(made.poll_once(), [])


class LimitsTest(PollerCase):
    def run_round(self, made: Poller) -> threading.Thread:
        thread = threading.Thread(target=made.poll_once, daemon=True)
        thread.start()
        return thread

    def test_one_request_is_outstanding_per_peer(self) -> None:
        made = self.make()
        self.fetch.gate = threading.Event()
        thread = self.run_round(made)
        self.assertTrue(wait_until(lambda: len(self.fetch.asked()) == 2))
        self.clock.at += 600
        self.assertEqual(made.poll_once(), [], "a peer with a request in flight is not asked again")
        made.nudge(peer_id(0))
        self.assertEqual(len(self.fetch.asked()), 2)
        self.fetch.gate.set()
        thread.join(5)
        self.assertEqual(len(self.fetch.asked()), 2)

    def test_at_most_eight_requests_run_at_once(self) -> None:
        write_policy(self.home, peers(10))
        made = self.make()
        self.fetch.gate = threading.Event()
        thread = self.run_round(made)
        self.assertTrue(wait_until(lambda: self.fetch.active == 8))
        time.sleep(0.2)
        self.assertEqual(self.fetch.active, 8)
        self.fetch.gate.set()
        thread.join(5)
        self.assertEqual(self.fetch.most, 8)
        self.assertEqual(sorted(self.fetch.asked()), sorted(peer_id(i) for i in range(10)))

    def test_a_round_ends_at_its_deadline_and_the_peers_it_never_reached_wait_for_the_next(self) -> None:
        write_policy(self.home, peers(6))
        made = self.make(max_concurrent=2, deadline_s=0.3, clock=time.monotonic)
        self.fetch.gate = threading.Event()
        started = time.monotonic()
        attempted = made.poll_once()
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(len(attempted), 2)
        self.assertEqual(len(self.fetch.asked()), 2)
        self.fetch.gate.set()
        self.assertTrue(wait_until(lambda: self.fetch.active == 0))
        self.assertEqual(len(made.poll_once()), 4, "only the peers never asked are due")
        self.assertEqual(sorted(set(self.fetch.asked())), sorted(peer_id(i) for i in range(6)))

    def test_the_request_gets_the_time_left_in_the_round(self) -> None:
        made = self.make(deadline_s=12)
        made.poll_once()
        self.assertTrue(all(0.1 <= call[3] <= 12 for call in self.fetch.calls))


class ThreadTest(PollerCase):
    def start(self, **over: Any) -> Poller:
        made = self.make(clock=time.monotonic, **over)
        made.start()
        return made

    def test_a_member_saved_by_another_process_is_asked_within_the_policy_check(self) -> None:
        write_policy(self.home, peers(1))
        made = self.start(policy_check_s=0.05)
        self.assertTrue(wait_until(lambda: self.fetch.asked() == [peer_id(0)]))
        write_policy(self.home, peers(2))
        self.assertTrue(wait_until(lambda: self.fetch.asked() == [peer_id(0), peer_id(1)]))
        write_policy(self.home, [peer_grant(peer_id(0), "peer-0", "192.168.1.99"), peers(2)[1]])
        time.sleep(0.3)
        self.assertEqual(len(self.fetch.asked()), 2, "a changed policy never asks a known peer again inside a minute")
        self.assertTrue(made.running)

    def test_the_loop_asks_at_start_and_a_nudge_brings_a_new_peer_in_early(self) -> None:
        write_policy(self.home, peers(1))
        made = self.start(policy_check_s=60.0)
        self.assertTrue(wait_until(lambda: self.fetch.asked() == [peer_id(0)]))
        write_policy(self.home, peers(2))
        time.sleep(0.2)
        self.assertEqual(self.fetch.asked(), [peer_id(0)], "nothing is due for a minute")
        poller.nudge(self.home, peer_id(1))
        self.assertTrue(wait_until(lambda: self.fetch.asked() == [peer_id(0), peer_id(1)]))
        made.nudge(peer_id(0))
        time.sleep(0.2)
        self.assertEqual(len(self.fetch.asked()), 2, "a nudge never makes a peer's second request inside a minute")

    def test_a_running_poller_is_found_by_home_and_a_stopped_one_is_not(self) -> None:
        self.fetch.replies[peer_id(0)] = NetClientError("timed out")
        made = self.start()
        self.assertTrue(wait_until(lambda: poller.is_failed(self.home, peer_id(0))))
        self.assertFalse(poller.is_failed(self.home, peer_id(1)))
        made.stop()
        self.assertFalse(poller.is_failed(self.home, peer_id(0)))
        poller.nudge(self.home, peer_id(0))
        self.assertFalse(made.running)

    def test_a_poll_that_raises_is_logged_once_and_the_loop_goes_on(self) -> None:
        def broken(_home: str) -> Any:
            raise RuntimeError("config unreadable")

        made = self.start(load_config=broken)
        self.assertTrue(wait_until(lambda: any("RuntimeError" in line for line in self.logs)))
        self.assertTrue(made.running)


class ServerTest(PollerCase):
    def server(self) -> SharedServer:
        server = SharedServer(self.home, free_port(), StoreHost(self.home), log=lambda _m: None)
        self.addCleanup(server.stop, "test over")
        return server

    def threads(self) -> list[str]:
        return [t.name for t in threading.enumerate() if t.name == "agent-tabs-net-poll"]

    def test_no_poller_and_no_thread_while_the_network_is_off(self) -> None:
        write_policy(self.home, [])
        for network in ({"network": "off"}, {}):
            write_config(self.home, **network)
            server = self.server()
            self.assertFalse(server.sync_net())
            self.assertIsNone(server._poller)
            self.assertEqual(self.threads(), [])

    def test_the_poller_starts_when_the_network_is_lan_and_stops_when_it_goes_off(self) -> None:
        write_policy(self.home, [])
        server = self.server()
        server.sync_net()
        self.assertEqual(len(self.threads()), 1)
        server.sync_net()
        self.assertEqual(len(self.threads()), 1, "a second sync starts no second poller")
        write_config(self.home, network="off")
        server.sync_net()
        self.assertTrue(wait_until(lambda: self.threads() == []))

    def test_stopping_the_server_stops_the_poller_and_a_stopped_server_starts_none(self) -> None:
        write_policy(self.home, [])
        server = self.server()
        server.sync_net()
        server.stop("done")
        self.assertTrue(wait_until(lambda: self.threads() == []))
        server.sync_net()
        self.assertEqual(self.threads(), [])

    def test_the_timers_start_the_poller_once_and_a_busy_check_keeps_it(self) -> None:
        write_policy(self.home, [])
        server = self.server()
        server.start_timers(idle_s=0.05, state_s=60)
        self.assertTrue(wait_until(lambda: len(self.threads()) == 1))
        time.sleep(0.2)
        self.assertEqual(len(self.threads()), 1)


if __name__ == "__main__":
    unittest.main()

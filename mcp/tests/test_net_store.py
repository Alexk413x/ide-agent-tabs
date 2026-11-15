from __future__ import annotations

import os
import sqlite3
import threading
import unittest

from ide_agent_tabs.net import store as net_store
from ide_agent_tabs.net.store import ConflictError, Launch, NetStore, Reservation, db_path
from support import temp_home

NOW = 1_800_000_000_000
HOUR = 3600 * 1000
DAY = 24 * HOUR


class StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        self.home = temp_home(self)
        self.store = self.open_store()

    def open_store(self) -> NetStore:
        store = NetStore(self.home)
        self.addCleanup(store.close)
        return store

    def open_request(self, request_id: str = "r1", peer: str = "p1", digest: str = "d1", generation: int = 0) -> Reservation:
        return self.store.reserve(peer, request_id, digest, "open", generation, NOW)

    def state(self, request_id: str, peer: str = "p1") -> str:
        found = self.store.get_request(peer, request_id)
        assert found is not None
        return found.state


class GenerationTest(StoreCase):
    def test_starts_at_zero_and_bumps(self) -> None:
        self.assertEqual(self.store.generation(), 0)
        self.assertEqual(self.store.bump_generation(), 1)
        self.assertEqual(self.store.bump_generation(), 2)
        self.assertEqual(self.store.generation(), 2)

    def test_persists_across_instances(self) -> None:
        self.store.bump_generation()
        self.assertEqual(self.open_store().generation(), 1)

    def test_uses_full_synchronous_and_wal(self) -> None:
        sql = self.store._sql  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(sql.execute("PRAGMA synchronous").fetchone()[0], 2)
        self.assertEqual(str(sql.execute("PRAGMA journal_mode").fetchone()[0]).lower(), "wal")
        self.assertTrue(os.path.exists(net_store.db_path(self.home)))

    def test_bump_refuses_pending_reservations_but_not_dispatched_ones(self) -> None:
        self.open_request("pending")
        self.open_request("sent")
        self.assertTrue(self.store.dispatch_handoff("p1", "sent", 0, lambda: True))
        self.store.bump_generation()
        self.assertEqual(self.state("pending"), "refused")
        self.assertEqual(self.state("sent"), "dispatched")


class ReserveTest(StoreCase):
    def test_new_reservation_is_pending(self) -> None:
        found = self.open_request()
        self.assertEqual((found.state, found.op, found.generation, found.created_ms, found.tab_id), ("pending", "open", 0, NOW, None))
        self.assertFalse(found.replayed)

    def test_same_id_and_digest_returns_the_existing_record(self) -> None:
        first = self.open_request(generation=0)
        self.store.dispatch_handoff("p1", "r1", 0, lambda: True)
        again = self.store.reserve("p1", "r1", "d1", "open", 5, NOW + 10)
        self.assertTrue(again.replayed)
        self.assertEqual(again.state, "dispatched")
        self.assertEqual((again.generation, again.created_ms), (first.generation, first.created_ms))

    def test_different_digest_conflicts(self) -> None:
        self.open_request()
        with self.assertRaises(ConflictError):
            self.store.reserve("p1", "r1", "other", "open", 0, NOW)

    def test_different_op_conflicts(self) -> None:
        self.open_request()
        with self.assertRaises(ConflictError):
            self.store.reserve("p1", "r1", "d1", "close", 0, NOW)

    def test_same_request_id_from_two_peers_is_independent(self) -> None:
        self.open_request(peer="p1", digest="a")
        self.open_request(peer="p2", digest="b")
        self.assertEqual(self.state("r1", "p2"), "pending")

    def test_unknown_op_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            self.store.reserve("p1", "r1", "d1", "send", 0, NOW)

    def test_a_close_reservation_does_not_count_as_an_open(self) -> None:
        self.store.reserve("p1", "c1", "d", "close", 0, NOW)
        self.assertEqual(self.store.count_active("p1", set()), 0)
        self.assertEqual(self.store.opens_since("p1", 0), 0)


class DispatchTest(StoreCase):
    def test_dispatch_records_dispatched(self) -> None:
        self.open_request()
        self.assertTrue(self.store.dispatch_handoff("p1", "r1", 0, lambda: True))
        self.assertEqual(self.state("r1"), "dispatched")
        self.assertEqual(self.open_store().get_request("p1", "r1").state, "dispatched")  # type: ignore[union-attr]

    def test_dispatch_runs_once(self) -> None:
        self.open_request()
        self.assertTrue(self.store.dispatch_handoff("p1", "r1", 0, lambda: True))
        self.assertFalse(self.store.dispatch_handoff("p1", "r1", 0, lambda: True))

    def test_generation_bump_after_reserve_blocks_dispatch(self) -> None:
        self.open_request(generation=0)
        self.store.bump_generation()
        self.assertFalse(self.store.dispatch_handoff("p1", "r1", 1, lambda: True))
        self.assertNotEqual(self.state("r1"), "dispatched")

    def test_stale_caller_generation_blocks_dispatch(self) -> None:
        self.open_request(generation=0)
        self.store.bump_generation()
        self.assertFalse(self.store.dispatch_handoff("p1", "r1", 0, lambda: True))
        self.assertEqual(self.state("r1"), "refused")

    def test_a_reservation_made_under_an_old_generation_never_dispatches(self) -> None:
        self.store.bump_generation()
        self.open_request(generation=0)
        self.assertFalse(self.store.dispatch_handoff("p1", "r1", 1, lambda: True))
        self.assertEqual(self.state("r1"), "refused")

    def test_failed_check_refuses_and_runs_inside_the_decision(self) -> None:
        self.open_request()
        self.assertFalse(self.store.dispatch_handoff("p1", "r1", 0, lambda: False))
        self.assertEqual(self.state("r1"), "refused")

    def test_raising_check_leaves_the_record_pending(self) -> None:
        self.open_request()

        def boom() -> bool:
            raise RuntimeError("policy unreadable")

        with self.assertRaises(RuntimeError):
            self.store.dispatch_handoff("p1", "r1", 0, boom)
        self.assertEqual(self.state("r1"), "pending")
        self.assertTrue(self.store.dispatch_handoff("p1", "r1", 0, lambda: True))

    def test_unknown_record_is_not_dispatched(self) -> None:
        self.assertFalse(self.store.dispatch_handoff("p1", "nope", 0, lambda: True))

    def test_bump_cannot_interleave_with_a_check(self) -> None:
        self.open_request()
        inside = threading.Event()
        release = threading.Event()
        bumped = threading.Event()

        def check() -> bool:
            inside.set()
            release.wait(5)
            return True

        result: list[bool] = []
        t = threading.Thread(target=lambda: result.append(self.store.dispatch_handoff("p1", "r1", 0, check)))
        t.start()
        self.assertTrue(inside.wait(5))
        b = threading.Thread(target=lambda: (self.store.bump_generation(), bumped.set()))
        b.start()
        self.assertFalse(bumped.wait(0.3))
        release.set()
        t.join(5)
        b.join(5)
        self.assertEqual(result, [True])
        self.assertTrue(bumped.is_set())
        self.assertEqual(self.state("r1"), "dispatched")

    def test_complete_and_refuse_transitions(self) -> None:
        self.open_request("a")
        self.open_request("b")
        self.assertFalse(self.store.complete("p1", "a", "tab-1"))
        self.assertTrue(self.store.dispatch_handoff("p1", "a", 0, lambda: True))
        self.assertTrue(self.store.complete("p1", "a", "tab-1"))
        found = self.store.get_request("p1", "a")
        assert found is not None
        self.assertEqual((found.state, found.tab_id), ("completed", "tab-1"))
        self.assertTrue(self.store.refuse("p1", "b"))
        self.assertEqual(self.state("b"), "refused")
        self.assertFalse(self.store.refuse("p1", "a"))
        self.assertFalse(self.store.complete("p1", "b", "tab-2"))

    def test_a_dispatched_launch_that_fails_can_be_refused(self) -> None:
        self.open_request()
        self.store.dispatch_handoff("p1", "r1", 0, lambda: True)
        self.assertTrue(self.store.refuse("p1", "r1"))
        self.assertEqual(self.state("r1"), "refused")


class ReconcileTest(StoreCase):
    def dispatched(self, request_id: str, tab_id: str | None, op: str = "open") -> None:
        self.store.reserve("p1", request_id, request_id, op, 0, NOW, tab_id)
        self.assertTrue(self.store.dispatch_handoff("p1", request_id, 0, lambda: True))

    def test_dispatched_open_is_completed_when_its_tab_is_live_else_uncertain(self) -> None:
        self.dispatched("live", "t-live")
        self.dispatched("gone", "t-gone")
        self.dispatched("untracked", None)
        changed = self.store.reconcile({"t-live"})
        self.assertEqual(
            sorted((r.request_id, r.state) for r in changed), [("gone", "uncertain"), ("live", "completed"), ("untracked", "uncertain")]
        )
        self.assertEqual(self.state("live"), "completed")
        self.assertEqual(self.state("gone"), "uncertain")
        self.assertEqual(self.store.opened_by("t-live"), "p1")
        self.assertIsNone(self.store.opened_by("t-gone"))

    def test_reconcile_never_dispatches_again(self) -> None:
        self.dispatched("gone", "t-gone")
        self.store.reconcile(set())
        self.assertFalse(self.store.dispatch_handoff("p1", "gone", 0, lambda: True))
        again = self.store.reserve("p1", "gone", "gone", "open", 0, NOW)
        self.assertEqual((again.replayed, again.state), (True, "uncertain"))

    def test_pending_records_are_refused_at_start(self) -> None:
        self.open_request_pending()
        changed = self.store.reconcile(set())
        self.assertEqual([(r.request_id, r.state) for r in changed], [("r1", "refused")])

    def open_request_pending(self) -> None:
        self.store.reserve("p1", "r1", "d", "open", 0, NOW)

    def test_dispatched_close_is_completed_when_its_tab_is_gone(self) -> None:
        self.dispatched("closed", "t-1", "close")
        self.dispatched("still", "t-2", "close")
        self.store.reconcile({"t-2"})
        self.assertEqual((self.state("closed"), self.state("still")), ("completed", "uncertain"))

    def test_settled_records_are_left_alone(self) -> None:
        self.dispatched("a", "t-a")
        self.store.complete("p1", "a", "t-a")
        self.assertEqual(self.store.reconcile(set()), [])
        self.assertEqual(self.state("a"), "completed")

    def test_assign_tab(self) -> None:
        self.open_request_pending()
        self.assertTrue(self.store.assign_tab("p1", "r1", "t-9"))
        found = self.store.get_request("p1", "r1")
        assert found is not None
        self.assertEqual(found.tab_id, "t-9")
        self.assertFalse(self.store.assign_tab("p1", "missing", "t-9"))


class CapacityTest(StoreCase):
    def test_count_active_counts_pending_dispatched_and_live_completed(self) -> None:
        self.store.reserve("p1", "uncertain", "uncertain", "open", 0, NOW)
        self.store.dispatch_handoff("p1", "uncertain", 0, lambda: True)
        self.store.reconcile(set())
        self.assertEqual(self.state("uncertain"), "uncertain")
        for request_id in ("pending", "sent", "done-live", "done-dead", "refused"):
            self.store.reserve("p1", request_id, request_id, "open", 0, NOW)
        self.store.reserve("p2", "other", "other", "open", 0, NOW)
        for request_id in ("sent", "done-live", "done-dead"):
            self.store.dispatch_handoff("p1", request_id, 0, lambda: True)
        self.store.complete("p1", "done-live", "t-live")
        self.store.complete("p1", "done-dead", "t-dead")
        self.store.refuse("p1", "refused")
        self.assertEqual(self.store.count_active("p1", {"t-live"}), 3)
        self.assertEqual(self.store.count_active("p1", set()), 2)
        self.assertEqual(self.store.count_active(None, {"t-live"}), 4)
        self.assertEqual(self.store.count_active("nobody", {"t-live"}), 0)

    def test_opens_since_ignores_refused_and_old_records(self) -> None:
        self.store.reserve("p1", "old", "o", "open", 0, NOW - 2 * HOUR)
        self.store.reserve("p1", "new", "n", "open", 0, NOW - HOUR // 2)
        self.store.reserve("p1", "gone", "g", "open", 0, NOW - 10)
        self.store.refuse("p1", "gone")
        self.store.reserve("p2", "other", "x", "open", 0, NOW)
        self.assertEqual(self.store.opens_since("p1", NOW - HOUR), 1)
        self.assertEqual(self.store.opens_since("p1", 0), 2)

    def reserve_open(
        self, request_id: str, peer: str = "p1", peer_cap: int = 10, global_cap: int = 10, hourly: int = 100
    ) -> Reservation | None:
        return self.store.reserve_open(peer, request_id, request_id, 0, NOW, peer_cap, global_cap, hourly, set())

    def test_reserve_open_enforces_each_limit(self) -> None:
        self.assertIsNotNone(self.reserve_open("a", peer_cap=1))
        self.assertIsNone(self.reserve_open("b", peer_cap=1))
        self.assertIsNotNone(self.reserve_open("c", peer="p2", peer_cap=1, global_cap=2))
        self.assertIsNone(self.reserve_open("d", peer="p3", peer_cap=1, global_cap=2))
        self.assertIsNotNone(self.reserve_open("e", peer="p4", hourly=1))
        self.assertIsNone(self.reserve_open("f", peer="p4", hourly=1))

    def test_a_refused_limit_leaves_nothing_behind(self) -> None:
        self.reserve_open("a", peer_cap=1)
        self.assertIsNone(self.reserve_open("b", peer_cap=1))
        self.assertIsNone(self.store.get_request("p1", "b"))

    def test_replay_skips_the_limits_and_returns_the_stored_state(self) -> None:
        first = self.reserve_open("a", peer_cap=1)
        again = self.reserve_open("a", peer_cap=1)
        assert first is not None and again is not None
        self.assertTrue(again.replayed)
        with self.assertRaises(ConflictError):
            self.store.reserve_open("p1", "a", "different", 0, NOW, 1, 1, 1, set())

    def test_live_completed_tabs_hold_capacity_until_they_close(self) -> None:
        self.assertIsNotNone(self.store.reserve_open("p1", "a", "a", 0, NOW, 1, 5, 100, set(), "t-1"))
        self.store.dispatch_handoff("p1", "a", 0, lambda: True)
        self.store.complete("p1", "a", "t-1")
        self.assertIsNone(self.store.reserve_open("p1", "b", "b", 0, NOW, 1, 5, 100, {"t-1"}))
        self.assertIsNotNone(self.store.reserve_open("p1", "b", "b", 0, NOW, 1, 5, 100, set()))

    def test_concurrent_reserve_open_in_one_store_cannot_exceed_limits(self) -> None:
        self.assertEqual(self.race([self.store] * 8, peer_cap=3, global_cap=100, hourly=100), 3)

    def test_concurrent_reserve_open_across_connections_cannot_exceed_limits(self) -> None:
        stores = [self.open_store() for _ in range(8)]
        self.assertEqual(self.race(stores, peer_cap=3, global_cap=100, hourly=100), 3)

    def test_concurrent_global_and_hourly_limits(self) -> None:
        stores = [self.open_store() for _ in range(8)]
        self.assertEqual(self.race(stores, peer_cap=100, global_cap=2, hourly=100, peers=True), 2)
        self.assertEqual(self.race(stores, peer_cap=100, global_cap=100, hourly=1, prefix="h"), 1)

    def race(self, stores: list[NetStore], peer_cap: int, global_cap: int, hourly: int, peers: bool = False, prefix: str = "r") -> int:
        start = threading.Barrier(len(stores))
        results: list[Reservation | None] = []
        errors: list[BaseException] = []
        guard = threading.Lock()

        def run(index: int, store: NetStore) -> None:
            try:
                start.wait(10)
                peer = f"peer-{index}" if peers else "p-race"
                found = store.reserve_open(peer, f"{prefix}{index}", f"{prefix}{index}", 0, NOW, peer_cap, global_cap, hourly, set())
                with guard:
                    results.append(found)
            except BaseException as e:  # noqa: BLE001
                with guard:
                    errors.append(e)

        threads = [threading.Thread(target=run, args=(i, s)) for i, s in enumerate(stores)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), len(stores))
        return sum(1 for r in results if r is not None)


class ReplyGrantTest(StoreCase):
    def grant(self, now: int = NOW) -> None:
        self.store.add_reply_grant("local-1", "gen-a", "peer-1", "remote-1", "m-1", now)

    def check(self, now: int = NOW + 1, change: dict[str, str] | None = None) -> bool:
        args = {
            "peer": "peer-1",
            "remote_session": "remote-1",
            "local_session": "local-1",
            "local_generation": "gen-a",
            "reply_to": "m-1",
            **(change or {}),
        }
        return self.store.check_reply(
            args["peer"], args["remote_session"], args["local_session"], args["local_generation"], args["reply_to"], now
        )

    def test_matching_unexpired_grant_is_accepted(self) -> None:
        self.grant()
        self.assertTrue(self.check())
        self.assertTrue(self.check(NOW + DAY - 1))

    def test_expires_after_24_hours(self) -> None:
        self.grant()
        self.assertFalse(self.check(NOW + DAY))
        self.assertFalse(self.check(NOW + 2 * DAY))

    def test_every_tuple_member_must_match(self) -> None:
        self.grant()
        for field, value in (
            ("peer", "peer-2"),
            ("remote_session", "remote-2"),
            ("local_session", "local-2"),
            ("local_generation", "gen-b"),
            ("reply_to", "m-2"),
        ):
            with self.subTest(field=field):
                self.assertFalse(self.check(change={field: value}))

    def test_no_grant_and_clock_before_the_grant(self) -> None:
        self.assertFalse(self.check())
        self.grant(NOW)
        self.assertFalse(self.check(NOW - 1))

    def test_a_grant_is_not_consumed_by_a_check(self) -> None:
        self.grant()
        self.assertTrue(self.check())
        self.assertTrue(self.check())

    def test_regranting_refreshes_the_window(self) -> None:
        self.grant(NOW)
        self.grant(NOW + DAY - 5)
        self.assertTrue(self.check(NOW + DAY + 10))

    def test_prune_removes_expired_grants(self) -> None:
        self.grant(NOW)
        self.store.add_reply_grant("local-1", "gen-a", "peer-1", "remote-1", "m-2", NOW + DAY)
        self.store.prune(NOW + DAY + 1)
        rows = self.store._sql.execute("SELECT message_id FROM reply_grants").fetchall()  # pyright: ignore[reportPrivateUsage]
        self.assertEqual([r[0] for r in rows], ["m-2"])


class InventoryTest(StoreCase):
    def test_round_trip_and_replace(self) -> None:
        self.assertIsNone(self.store.get_inventory("p1"))
        self.store.put_inventory("p1", '[{"id":"a"}]', NOW)
        self.assertEqual(self.store.get_inventory("p1"), ('[{"id":"a"}]', NOW))
        self.store.put_inventory("p1", "[]", NOW + 5)
        self.assertEqual(self.store.get_inventory("p1"), ("[]", NOW + 5))
        self.assertIsNone(self.store.get_inventory("p2"))

    def test_delete(self) -> None:
        self.store.put_inventory("p1", "[]", NOW)
        self.store.delete_inventory("p1")
        self.assertIsNone(self.store.get_inventory("p1"))


class PruneAndOwnerTest(StoreCase):
    def test_prune_deletes_records_older_than_seven_days(self) -> None:
        week = 7 * DAY
        self.store.reserve("p1", "old", "o", "open", 0, NOW - week - 1)
        self.store.reserve("p1", "edge", "e", "open", 0, NOW - week)
        self.store.reserve("p1", "new", "n", "open", 0, NOW)
        self.assertEqual(self.store.prune(NOW), 1)
        self.assertIsNone(self.store.get_request("p1", "old"))
        self.assertIsNotNone(self.store.get_request("p1", "edge"))
        self.assertIsNotNone(self.store.get_request("p1", "new"))

    def test_a_pruned_id_can_be_used_again_with_a_new_digest(self) -> None:
        self.store.reserve("p1", "old", "o", "open", 0, NOW - 8 * DAY)
        self.store.prune(NOW)
        self.assertFalse(self.store.reserve("p1", "old", "different", "open", 0, NOW).replayed)

    def test_opened_by_names_the_peer_whose_completed_open_made_the_tab(self) -> None:
        self.store.reserve("p1", "a", "a", "open", 0, NOW, "t-1")
        self.assertIsNone(self.store.opened_by("t-1"))
        self.store.dispatch_handoff("p1", "a", 0, lambda: True)
        self.assertIsNone(self.store.opened_by("t-1"))
        self.store.complete("p1", "a", "t-1")
        self.assertEqual(self.store.opened_by("t-1"), "p1")
        self.assertIsNone(self.store.opened_by("t-2"))

    def test_a_close_for_a_tab_does_not_make_the_closer_its_owner(self) -> None:
        self.store.reserve("p2", "c", "c", "close", 0, NOW, "t-1")
        self.store.dispatch_handoff("p2", "c", 0, lambda: True)
        self.store.complete("p2", "c", "t-1")
        self.assertIsNone(self.store.opened_by("t-1"))

    def test_a_refused_open_owns_nothing(self) -> None:
        self.store.reserve("p1", "a", "a", "open", 0, NOW, "t-1")
        self.store.refuse("p1", "a")
        self.assertIsNone(self.store.opened_by("t-1"))


class LaunchTest(StoreCase):
    def test_a_begun_launch_is_listed_until_it_ends(self) -> None:
        self.store.begin_launch("p1", "r1", "/work/proj", NOW)
        self.assertEqual(self.store.inflight_launches(NOW), [Launch("p1", "r1", "/work/proj", NOW)])
        self.store.end_launch("p1", "r1")
        self.assertEqual(self.store.inflight_launches(NOW), [])

    def test_ending_a_launch_that_is_not_there_is_harmless(self) -> None:
        self.store.end_launch("p1", "missing")

    def test_a_launch_older_than_the_maximum_age_is_not_listed(self) -> None:
        self.store.begin_launch("p1", "old", "/a", NOW - 120_001)
        self.store.begin_launch("p1", "edge", "/b", NOW - 120_000)
        self.assertEqual([x.request_id for x in self.store.inflight_launches(NOW)], ["edge"])
        self.assertEqual(sorted(x.request_id for x in self.store.inflight_launches(NOW, max_age_ms=500_000)), ["edge", "old"])

    def test_launches_are_keyed_by_peer_and_request_id(self) -> None:
        self.store.begin_launch("p1", "r1", "/a", NOW)
        self.store.begin_launch("p2", "r1", "/b", NOW)
        self.store.end_launch("p1", "r1")
        self.assertEqual([x.peer for x in self.store.inflight_launches(NOW)], ["p2"])

    def test_prune_drops_launches_past_the_maximum_age(self) -> None:
        self.store.begin_launch("p1", "old", "/a", NOW - 200_000)
        self.store.begin_launch("p1", "new", "/b", NOW)
        self.store.prune(NOW)
        self.assertEqual([x.request_id for x in self.store.inflight_launches(NOW, max_age_ms=10**9)], ["new"])


class MigrationTest(unittest.TestCase):
    def test_a_database_from_before_launches_keeps_its_data(self) -> None:
        home = temp_home(self)
        os.makedirs(os.path.dirname(db_path(home)))
        sql = sqlite3.connect(db_path(home), isolation_level=None)
        for step in net_store.MIGRATIONS[:2]:
            for statement in step:
                sql.execute(statement)
        sql.execute("PRAGMA user_version=2")
        sql.execute("INSERT INTO remote_tabs (tab_id, peer, created_ms) VALUES ('t-1', 'p1', 5)")
        sql.close()
        store = NetStore(home)
        self.addCleanup(store.close)
        self.assertEqual(store.remote_tab_peer("t-1"), "p1")
        store.begin_launch("p1", "r1", "/a", NOW)
        self.assertEqual(len(store.inflight_launches(NOW)), 1)
        sql = sqlite3.connect(db_path(home))
        self.addCleanup(sql.close)
        self.assertEqual(sql.execute("PRAGMA user_version").fetchone()[0], net_store.SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()

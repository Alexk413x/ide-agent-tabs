from __future__ import annotations

import contextlib
import os
import sqlite3
import threading
import time
from collections.abc import Collection
from dataclasses import dataclass
from typing import Any, Callable, TypeVar

from ..clock import now_ms as current_ms
from ..files import ensure_private_dir, file_lock
from ..messaging.db import is_busy, run_tx

T = TypeVar("T")

DB_FILE = "net.db"
SCHEMA_VERSION = 3
BUSY_TIMEOUT_MS = 200
DEADLINE_MS = 5_000
REQUEST_TTL_MS = 7 * 24 * 3600 * 1000
REPLY_GRANT_TTL_MS = 24 * 3600 * 1000
HOUR_MS = 3600 * 1000
LAUNCH_MAX_AGE_MS = 120_000

PENDING = "pending"
DISPATCHED = "dispatched"
COMPLETED = "completed"
REFUSED = "refused"
UNCERTAIN = "uncertain"
OPS = ("open", "close")

MIGRATIONS = (
    (
        "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
        "INSERT INTO meta (key, value) VALUES ('generation', '0')",
        """CREATE TABLE requests (
    peer       TEXT    NOT NULL,
    request_id TEXT    NOT NULL,
    digest     TEXT    NOT NULL,
    op         TEXT    NOT NULL CHECK (op IN ('open', 'close')),
    state      TEXT    NOT NULL CHECK (state IN ('pending', 'dispatched', 'completed', 'refused', 'uncertain')),
    tab_id     TEXT,
    generation INTEGER NOT NULL,
    created_ms INTEGER NOT NULL,
    updated_ms INTEGER NOT NULL,
    PRIMARY KEY (peer, request_id)
  )""",
        "CREATE INDEX requests_age ON requests(created_ms)",
        "CREATE INDEX requests_tab ON requests(tab_id) WHERE tab_id IS NOT NULL",
        """CREATE TABLE reply_grants (
    local_session    TEXT    NOT NULL,
    local_generation TEXT    NOT NULL,
    peer             TEXT    NOT NULL,
    remote_session   TEXT    NOT NULL,
    message_id       TEXT    NOT NULL,
    created_ms       INTEGER NOT NULL,
    PRIMARY KEY (local_session, local_generation, peer, remote_session, message_id)
  )""",
        "CREATE INDEX reply_grants_age ON reply_grants(created_ms)",
        """CREATE TABLE inventory (
    peer          TEXT PRIMARY KEY,
    sessions_json TEXT    NOT NULL,
    fetched_ms    INTEGER NOT NULL
  )""",
    ),
    (
        """CREATE TABLE remote_tabs (
    tab_id     TEXT    PRIMARY KEY,
    peer       TEXT    NOT NULL,
    created_ms INTEGER NOT NULL
  )""",
    ),
    (
        """CREATE TABLE launches (
    peer       TEXT    NOT NULL,
    request_id TEXT    NOT NULL,
    folder     TEXT    NOT NULL,
    started_ms INTEGER NOT NULL,
    PRIMARY KEY (peer, request_id)
  )""",
    ),
)


class NetStoreError(Exception):
    pass


class StoreBusyError(NetStoreError):
    def __init__(self) -> None:
        super().__init__("the network store is busy; try again")


class ConflictError(NetStoreError):
    pass


@dataclass(frozen=True)
class Reservation:
    peer: str
    request_id: str
    digest: str
    op: str
    state: str
    tab_id: str | None
    generation: int
    created_ms: int
    replayed: bool = False


@dataclass(frozen=True)
class Launch:
    peer: str
    request_id: str
    folder: str
    started_ms: int


def db_path(home: str) -> str:
    return os.path.join(home, "net", DB_FILE)


def _is_open_race(e: BaseException) -> bool:
    # Windows reports SQLITE_IOERR when processes open a new database at once and race to set up its WAL files.
    return is_busy(e) or (isinstance(e, sqlite3.OperationalError) and "disk i/o error" in str(e).lower())


def _jitter_s() -> float:
    return (10 + os.urandom(1)[0] % 41) / 1000


def _retry(work: Callable[[], T], deadline: float, retryable: Callable[[BaseException], bool] = is_busy) -> T:
    while True:
        try:
            return work()
        except sqlite3.Error as e:
            if not retryable(e):
                raise
            if current_ms() + 10 >= deadline:
                raise StoreBusyError() from e
            time.sleep(_jitter_s())


def _user_version(sql: sqlite3.Connection) -> int:
    row = sql.execute("PRAGMA user_version").fetchone()
    return int(row[0]) if row is not None else 0


def _setup(sql: sqlite3.Connection) -> None:
    sql.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    mode = sql.execute("PRAGMA journal_mode").fetchone()
    if str(mode[0] if mode else "").lower() != "wal":
        sql.execute("PRAGMA journal_mode=WAL")
    sql.execute("PRAGMA synchronous=FULL")
    if _user_version(sql) >= SCHEMA_VERSION:
        return

    def migrate() -> None:
        start = _user_version(sql)
        for step in MIGRATIONS[start:SCHEMA_VERSION]:
            for statement in step:
                sql.execute(statement)
        sql.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    run_tx(sql, migrate)


def _reservation(row: sqlite3.Row, replayed: bool = False) -> Reservation:
    return Reservation(
        row["peer"],
        row["request_id"],
        row["digest"],
        row["op"],
        row["state"],
        row["tab_id"],
        row["generation"],
        row["created_ms"],
        replayed,
    )


class NetStore:
    def __init__(self, home: str, deadline_ms: int = DEADLINE_MS) -> None:
        self._deadline_ms = deadline_ms
        self._lock = threading.Lock()
        file = os.path.abspath(db_path(home))
        ensure_private_dir(os.path.dirname(file))
        os.close(os.open(file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600))
        fresh = os.stat(file).st_size == 0
        self._sql = sqlite3.connect(file, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None, check_same_thread=False)
        self._sql.row_factory = sqlite3.Row
        deadline = current_ms() + deadline_ms
        try:
            if fresh:
                with file_lock(f"{file}.init"):
                    _retry(lambda: _setup(self._sql), deadline, _is_open_race)
            else:
                _retry(lambda: _setup(self._sql), deadline, _is_open_race)
        except BaseException:
            self._sql.close()
            raise

    def close(self) -> None:
        with self._lock, contextlib.suppress(sqlite3.Error):
            self._sql.close()

    def _tx(self, work: Callable[[], T]) -> T:
        deadline = current_ms() + self._deadline_ms

        def attempt() -> T:
            if not self._lock.acquire(timeout=max(0.0, (deadline - current_ms()) / 1000)):
                raise StoreBusyError()
            try:
                return run_tx(self._sql, work)
            finally:
                self._lock.release()

        return _retry(attempt, deadline)

    def _read(self, work: Callable[[], T]) -> T:
        deadline = current_ms() + self._deadline_ms

        def attempt() -> T:
            if not self._lock.acquire(timeout=max(0.0, (deadline - current_ms()) / 1000)):
                raise StoreBusyError()
            try:
                return work()
            finally:
                self._lock.release()

        return _retry(attempt, deadline)

    def _one(self, text: str, *args: Any) -> sqlite3.Row | None:
        return self._sql.execute(text, args).fetchone()

    def _all(self, text: str, *args: Any) -> list[sqlite3.Row]:
        return self._sql.execute(text, args).fetchall()

    def _run(self, text: str, *args: Any) -> int:
        return self._sql.execute(text, args).rowcount

    def _generation(self) -> int:
        row = self._one("SELECT value FROM meta WHERE key = 'generation'")
        return int(row["value"]) if row is not None else 0

    def generation(self) -> int:
        return self._read(self._generation)

    def bump_generation(self) -> int:
        def work() -> int:
            self._run("UPDATE meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) WHERE key = 'generation'")
            self._run("UPDATE requests SET state = 'refused', updated_ms = ? WHERE state = 'pending'", current_ms())
            return self._generation()

        return self._tx(work)

    def _request(self, peer: str, request_id: str) -> sqlite3.Row | None:
        return self._one("SELECT * FROM requests WHERE peer = ? AND request_id = ?", peer, request_id)

    def get_request(self, peer: str, request_id: str) -> Reservation | None:
        row = self._read(lambda: self._request(peer, request_id))
        return None if row is None else _reservation(row)

    def _reserve(self, peer: str, request_id: str, digest: str, op: str, generation: int, now: int, tab_id: str | None) -> Reservation:
        if op not in OPS:
            raise ValueError(f"op must be one of {OPS}")
        row = self._request(peer, request_id)
        if row is not None:
            if row["digest"] != digest or row["op"] != op:
                raise ConflictError("the request id was used for a different request")
            return _reservation(row, True)
        self._run(
            "INSERT INTO requests (peer, request_id, digest, op, state, tab_id, generation, created_ms, updated_ms)"
            " VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?)",
            peer,
            request_id,
            digest,
            op,
            tab_id,
            generation,
            now,
            now,
        )
        created = self._request(peer, request_id)
        assert created is not None
        return _reservation(created)

    def reserve(
        self, peer: str, request_id: str, digest: str, op: str, generation: int, now_ms: int, tab_id: str | None = None
    ) -> Reservation:
        return self._tx(lambda: self._reserve(peer, request_id, digest, op, generation, now_ms, tab_id))

    def _count_active(self, peer: str | None, live: Collection[str]) -> int:
        where, args = ("", ()) if peer is None else (" AND peer = ?", (peer,))
        row = self._one(f"SELECT COUNT(*) AS n FROM requests WHERE op = 'open' AND state IN ('pending', 'dispatched'){where}", *args)
        total = int(row["n"]) if row is not None else 0
        done = self._all(f"SELECT tab_id FROM requests WHERE op = 'open' AND state = 'completed'{where}", *args)
        return total + sum(1 for r in done if r["tab_id"] in live)

    def count_active(self, peer: str | None, live_tab_ids: Collection[str]) -> int:
        return self._read(lambda: self._count_active(peer, live_tab_ids))

    def _opens_since(self, peer: str, since_ms: int) -> int:
        row = self._one(
            "SELECT COUNT(*) AS n FROM requests WHERE op = 'open' AND state != 'refused' AND peer = ? AND created_ms >= ?", peer, since_ms
        )
        return int(row["n"]) if row is not None else 0

    def opens_since(self, peer: str, since_ms: int) -> int:
        return self._read(lambda: self._opens_since(peer, since_ms))

    def reserve_open(
        self,
        peer: str,
        request_id: str,
        digest: str,
        generation: int,
        now_ms: int,
        max_tabs_peer: int,
        max_tabs_global: int,
        opens_per_hour: int,
        live_tab_ids: Collection[str],
        tab_id: str | None = None,
    ) -> Reservation | None:
        def work() -> Reservation | None:
            if self._request(peer, request_id) is None and (
                self._count_active(peer, live_tab_ids) >= max_tabs_peer
                or self._count_active(None, live_tab_ids) >= max_tabs_global
                or self._opens_since(peer, now_ms - HOUR_MS) >= opens_per_hour
            ):
                return None
            return self._reserve(peer, request_id, digest, "open", generation, now_ms, tab_id)

        return self._tx(work)

    def assign_tab(self, peer: str, request_id: str, tab_id: str) -> bool:
        def work() -> bool:
            return (
                self._run(
                    "UPDATE requests SET tab_id = ?, updated_ms = ?"
                    " WHERE peer = ? AND request_id = ? AND state IN ('pending', 'dispatched')",
                    tab_id,
                    current_ms(),
                    peer,
                    request_id,
                )
                == 1
            )

        return self._tx(work)

    def dispatch_handoff(self, peer: str, request_id: str, current_generation: int, check: Callable[[], bool]) -> bool:
        # check runs inside the store lock and transaction; it must not call back into this store.
        def work() -> bool:
            row = self._request(peer, request_id)
            if row is None or row["state"] != PENDING:
                return False
            now = current_ms()
            if row["generation"] != current_generation or self._generation() != current_generation or not check():
                self._run("UPDATE requests SET state = 'refused', updated_ms = ? WHERE peer = ? AND request_id = ?", now, peer, request_id)
                return False
            self._run("UPDATE requests SET state = 'dispatched', updated_ms = ? WHERE peer = ? AND request_id = ?", now, peer, request_id)
            return True

        return self._tx(work)

    def _finish(self, peer: str, request_id: str, states: tuple[str, ...], state: str, tab_id: str | None) -> bool:
        marks = ", ".join("?" for _ in states)
        return (
            self._run(
                "UPDATE requests SET state = ?, tab_id = COALESCE(?, tab_id), updated_ms = ?"
                f" WHERE peer = ? AND request_id = ? AND state IN ({marks})",
                state,
                tab_id,
                current_ms(),
                peer,
                request_id,
                *states,
            )
            == 1
        )

    def complete(self, peer: str, request_id: str, tab_id: str) -> bool:
        return self._tx(lambda: self._finish(peer, request_id, (DISPATCHED,), COMPLETED, tab_id))

    def refuse(self, peer: str, request_id: str) -> bool:
        return self._tx(lambda: self._finish(peer, request_id, (PENDING, DISPATCHED), REFUSED, None))

    def reconcile(self, live_tab_ids: Collection[str]) -> list[Reservation]:
        def work() -> list[Reservation]:
            now = current_ms()
            changed: list[Reservation] = []
            for row in self._all("SELECT * FROM requests WHERE state IN ('pending', 'dispatched') ORDER BY created_ms"):
                live = row["tab_id"] is not None and row["tab_id"] in live_tab_ids
                if row["state"] == PENDING:
                    state = REFUSED
                elif row["op"] == "open":
                    state = COMPLETED if live else UNCERTAIN
                else:
                    state = UNCERTAIN if live else COMPLETED
                self._run(
                    "UPDATE requests SET state = ?, updated_ms = ? WHERE peer = ? AND request_id = ?",
                    state,
                    now,
                    row["peer"],
                    row["request_id"],
                )
                changed.append(_reservation(self._request(row["peer"], row["request_id"]) or row))
            return changed

        return self._tx(work)

    def prune(self, now_ms: int) -> int:
        def work() -> int:
            self._run("DELETE FROM reply_grants WHERE created_ms <= ?", now_ms - REPLY_GRANT_TTL_MS)
            self._run("DELETE FROM launches WHERE started_ms < ?", now_ms - LAUNCH_MAX_AGE_MS)
            return self._run("DELETE FROM requests WHERE created_ms < ?", now_ms - REQUEST_TTL_MS)

        return self._tx(work)

    def add_reply_grant(
        self, local_session: str, local_generation: str, peer: str, remote_session: str, message_id: str, now_ms: int
    ) -> None:
        def work() -> None:
            self._run(
                "INSERT OR REPLACE INTO reply_grants (local_session, local_generation, peer, remote_session, message_id, created_ms)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                local_session,
                local_generation,
                peer,
                remote_session,
                message_id,
                now_ms,
            )

        self._tx(work)

    def check_reply(self, peer: str, remote_session: str, local_session: str, local_generation: str, reply_to: str, now_ms: int) -> bool:
        def work() -> bool:
            row = self._one(
                "SELECT created_ms FROM reply_grants WHERE local_session = ? AND local_generation = ? AND peer = ? AND remote_session = ?"
                " AND message_id = ?",
                local_session,
                local_generation,
                peer,
                remote_session,
                reply_to,
            )
            return row is not None and 0 <= now_ms - row["created_ms"] < REPLY_GRANT_TTL_MS

        return self._read(work)

    def put_inventory(self, peer: str, sessions_json: str, now_ms: int) -> None:
        self._tx(
            lambda: self._run(
                "INSERT OR REPLACE INTO inventory (peer, sessions_json, fetched_ms) VALUES (?, ?, ?)", peer, sessions_json, now_ms
            )
        )

    def get_inventory(self, peer: str) -> tuple[str, int] | None:
        row = self._read(lambda: self._one("SELECT sessions_json, fetched_ms FROM inventory WHERE peer = ?", peer))
        return None if row is None else (row["sessions_json"], int(row["fetched_ms"]))

    def delete_inventory(self, peer: str) -> None:
        self._tx(lambda: self._run("DELETE FROM inventory WHERE peer = ?", peer))

    def opened_by(self, tab_id: str) -> str | None:
        row = self._read(
            lambda: self._one(
                "SELECT peer FROM requests WHERE op = 'open' AND state = 'completed' AND tab_id = ? ORDER BY created_ms DESC LIMIT 1",
                tab_id,
            )
        )
        return None if row is None else str(row["peer"])

    def record_remote_tab(self, tab_id: str, peer: str, now_ms: int) -> None:
        self._tx(lambda: self._run("INSERT OR REPLACE INTO remote_tabs (tab_id, peer, created_ms) VALUES (?, ?, ?)", tab_id, peer, now_ms))

    def remote_tab_peer(self, tab_id: str) -> str | None:
        row = self._read(lambda: self._one("SELECT peer FROM remote_tabs WHERE tab_id = ?", tab_id))
        return None if row is None else str(row["peer"])

    def begin_launch(self, peer: str, request_id: str, folder: str, started_ms: int) -> None:
        self._tx(
            lambda: self._run(
                "INSERT OR REPLACE INTO launches (peer, request_id, folder, started_ms) VALUES (?, ?, ?, ?)",
                peer,
                request_id,
                folder,
                started_ms,
            )
        )

    def end_launch(self, peer: str, request_id: str) -> None:
        self._tx(lambda: self._run("DELETE FROM launches WHERE peer = ? AND request_id = ?", peer, request_id))

    def inflight_launches(self, now_ms: int, max_age_ms: int = LAUNCH_MAX_AGE_MS) -> list[Launch]:
        rows = self._read(
            lambda: self._all("SELECT peer, request_id, folder, started_ms FROM launches WHERE ? - started_ms <= ?", now_ms, max_age_ms)
        )
        return [Launch(r["peer"], r["request_id"], r["folder"], int(r["started_ms"])) for r in rows]

    def find_reply_grant(self, peer: str, local_session: str, local_generation: str, reply_to: str, now_ms: int) -> str | None:
        def work() -> str | None:
            row = self._one(
                "SELECT remote_session, created_ms FROM reply_grants WHERE local_session = ? AND local_generation = ? AND peer = ?"
                " AND message_id = ? ORDER BY created_ms DESC LIMIT 1",
                local_session,
                local_generation,
                peer,
                reply_to,
            )
            if row is None or not 0 <= now_ms - row["created_ms"] < REPLY_GRANT_TTL_MS:
                return None
            return str(row["remote_session"])

        return self._read(work)

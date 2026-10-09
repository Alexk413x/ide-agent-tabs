from __future__ import annotations

import contextlib
import os
import posixpath
import re
import sqlite3
import sys
import threading
import time
from typing import Any, Callable, TypeVar

from ..clock import now_ms
from ..files import ensure_private_dir, file_lock, remove_tree

T = TypeVar("T")

DB_FILE = "messages.db"
WAKE_DIR = "wake"
FILE_MAILBOX_DIR = "mail"
SCHEMA_VERSION = 1
BUSY_TIMEOUT_MS = 200
SHARED_BUSY_TIMEOUT_MS = 0
TOOL_DEADLINE_MS = 5_000
HOOK_DEADLINE_MS = 1_000
CLEAN_DEADLINE_MS = 1_000
JOURNAL_SIZE_LIMIT = 4 * 1024 * 1024

CORRUPT_MESSAGE = (
    "the message store is corrupt; Agent Tabs moves it aside and starts a new one at the next start or hourly cleanup, "
    "and unread messages in it can be lost"
)

# The text Node's build runs, byte for byte: SQLite keeps each CREATE statement's text in sqlite_master.
# Migrations stay additive (new tables, columns with defaults, indexes): an older build keeps using a database
# that a newer build migrated, with no version check.
MIGRATIONS = (
    """CREATE TABLE messages (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    id         TEXT    NOT NULL CHECK (id GLOB 'm-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'),
    route      TEXT    NOT NULL CHECK (route IN ('agent-tabs', 'native')),
    owner      TEXT,
    direction  TEXT    CHECK (direction IN ('sent', 'received')),
    from_id    TEXT, from_name TEXT, from_agent TEXT, from_path TEXT,
    to_id      TEXT, to_name   TEXT, to_agent   TEXT, to_path   TEXT,
    text       TEXT    NOT NULL CHECK (length(text) <= 32000),
    reply_to   TEXT,
    sent_at    TEXT    NOT NULL,
    sent_ms    INTEGER NOT NULL,
    digest     TEXT,
    delivery   TEXT,
    state      TEXT    CHECK (state IN ('unread', 'held', 'read')),
    claim      TEXT,
    state_ms   INTEGER,
    CHECK ((route = 'agent-tabs') = (state IS NOT NULL)),
    CHECK ((route = 'native') = (owner IS NOT NULL AND direction IS NOT NULL))
  );
  CREATE UNIQUE INDEX messages_id_mail   ON messages(id) WHERE route = 'agent-tabs';
  CREATE UNIQUE INDEX messages_id_native ON messages(owner, id) WHERE route = 'native';
  CREATE INDEX messages_inbox   ON messages(to_id, state, seq) WHERE route = 'agent-tabs';
  CREATE INDEX messages_outbox  ON messages(from_id, sent_ms);
  CREATE INDEX messages_to      ON messages(to_id, sent_ms);
  CREATE INDEX messages_names   ON messages(from_name, to_name);
  CREATE INDEX messages_owner   ON messages(owner, sent_ms) WHERE route = 'native';
  CREATE INDEX messages_reply   ON messages(reply_to) WHERE reply_to IS NOT NULL;
  CREATE INDEX messages_age     ON messages(sent_ms);
  CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);""",
)

NETWORK_FS_TYPES = frozenset({"nfs", "nfs4", "cifs", "smb3", "smbfs", "9p"})


class MailError(Exception):
    pass


class StoreBusyError(MailError):
    def __init__(self) -> None:
        super().__init__("the message store is busy; try again")


def _message(e: BaseException) -> str:
    return str(e).lower()


def is_busy(e: BaseException) -> bool:
    return isinstance(e, sqlite3.OperationalError) and ("database is locked" in _message(e) or "database table is locked" in _message(e))


def is_full(e: BaseException) -> bool:
    return isinstance(e, sqlite3.Error) and "database or disk is full" in _message(e)


def is_constraint(e: BaseException) -> bool:
    return isinstance(e, sqlite3.IntegrityError)


# Windows reports SQLITE_IOERR when processes open a new database at once and race to set up its WAL files.
def _is_open_race(e: BaseException) -> bool:
    return is_busy(e) or (isinstance(e, sqlite3.OperationalError) and "disk i/o error" in _message(e))


def is_corrupt(e: BaseException) -> bool:
    return isinstance(e, sqlite3.DatabaseError) and (
        "database disk image is malformed" in _message(e) or "file is not a database" in _message(e)
    )


def _unescape_mount(path: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), path)


def _linux_fs_type(folder: str, mountinfo: str) -> str | None:
    target = os.path.realpath(folder) if sys.platform.startswith("linux") else posixpath.normpath(folder)
    best: tuple[int, str] | None = None
    for line in mountinfo.splitlines():
        head, sep, tail = line.partition(" - ")
        fields = head.split()
        if not sep or len(fields) < 5:
            continue
        point = _unescape_mount(fields[4])
        inside = target == point or target.startswith(point.rstrip("/") + "/")
        if inside and (best is None or len(point) >= best[0]):
            kind = tail.split()
            best = (len(point), kind[0] if kind else "")
    return None if best is None else best[1]


def _read_mountinfo() -> str:
    with open("/proc/self/mountinfo", encoding="utf-8", errors="replace") as f:
        return f.read()


def network_home_reason(home: str, platform: str = sys.platform, mountinfo: Callable[[], str] = _read_mountinfo) -> str | None:
    advice = (
        f"the Agent Tabs home {home} is on a network file system, where the message store can't lock; "
        "set IDE_AGENT_TABS_HOME to a local folder"
    )
    if platform == "win32":
        p = home.replace("/", "\\")
        return advice if re.match(r"^\\\\\?\\UNC\\", p, re.IGNORECASE) or re.match(r"^\\\\[^?.\\]", p) else None
    if not platform.startswith("linux"):
        return None
    try:
        kind = _linux_fs_type(home, mountinfo())
    except OSError:
        return None
    return advice if kind is not None and kind.split(".")[0] in NETWORK_FS_TYPES else None


def db_path(home: str) -> str:
    return os.path.join(home, DB_FILE)


def wake_dir(home: str) -> str:
    return os.path.join(home, WAKE_DIR)


class Db:
    def __init__(self, home: str, file: str, sql: sqlite3.Connection, ino: int) -> None:
        self.home = home
        self.file = file
        self.sql = sql
        self.ino = ino
        self.lock = threading.RLock()

    def one(self, text: str, *args: Any) -> sqlite3.Row | None:
        return self.sql.execute(text, args).fetchone()

    def all(self, text: str, *args: Any) -> list[sqlite3.Row]:
        return self.sql.execute(text, args).fetchall()

    def run(self, text: str, *args: Any) -> int:
        return self.sql.execute(text, args).rowcount


_busy_timeout_ms = BUSY_TIMEOUT_MS
_connections: dict[str, Db] = {}
_connecting = threading.Lock()
open_stats = {"opened": 0}


def _jitter_s() -> float:
    return (10 + os.urandom(1)[0] % 41) / 1000


def set_busy_timeout(ms: int) -> None:
    global _busy_timeout_ms
    _busy_timeout_ms = ms
    for db in list(_connections.values()):
        with db.lock, contextlib.suppress(sqlite3.Error):
            db.sql.execute(f"PRAGMA busy_timeout={int(ms)}")


def retry_busy(work: Callable[[], T], deadline: float, label: str | None = None, retryable: Callable[[BaseException], bool] = is_busy) -> T:
    while True:
        try:
            return work()
        except sqlite3.Error as e:
            if not retryable(e):
                raise
            if now_ms() + 10 >= deadline:
                if label is not None:
                    sys.stderr.write(f"ide-agent-tabs: {label} gave up waiting for the message store lock\n")
                raise StoreBusyError() from e
            time.sleep(_jitter_s())


def run_tx(sql: sqlite3.Connection, work: Callable[[], T]) -> T:
    sql.execute("BEGIN IMMEDIATE")
    try:
        result = work()
        sql.execute("COMMIT")
        return result
    except BaseException:
        with contextlib.suppress(sqlite3.Error):
            sql.execute("ROLLBACK")
        raise


def _statements(script: str) -> list[str]:
    out: list[str] = []
    pending = ""
    for piece in script.split(";"):
        pending += piece + ";"
        if sqlite3.complete_statement(pending):
            text = pending[:-1].lstrip()
            if text:
                out.append(text)
            pending = ""
    if pending.strip(" \n;"):
        out.append(pending.rstrip(";").lstrip())
    return out


def _user_version(sql: sqlite3.Connection) -> int:
    row = sql.execute("PRAGMA user_version").fetchone()
    return int(row[0]) if row is not None else 0


def _setup(sql: sqlite3.Connection) -> None:
    sql.execute(f"PRAGMA busy_timeout={int(_busy_timeout_ms)}")
    mode = sql.execute("PRAGMA journal_mode").fetchone()
    if str(mode[0] if mode else "").lower() != "wal":
        sql.execute("PRAGMA journal_mode=WAL")
    sql.execute("PRAGMA synchronous=NORMAL")
    sql.execute(f"PRAGMA journal_size_limit={JOURNAL_SIZE_LIMIT}")
    if _user_version(sql) >= SCHEMA_VERSION:
        return

    def migrate() -> None:
        start = _user_version(sql)
        if start >= SCHEMA_VERSION:
            return
        for step in MIGRATIONS[start:SCHEMA_VERSION]:
            for statement in _statements(step):
                sql.execute(statement)
        sql.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    run_tx(sql, migrate)


def _connect(home: str, file: str, deadline: float) -> Db:
    reason = network_home_reason(home)
    if reason is not None:
        raise MailError(reason)
    ensure_private_dir(home)
    ensure_private_dir(wake_dir(home))
    os.close(os.open(file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600))
    fresh = os.stat(file).st_size == 0
    sql = sqlite3.connect(file, timeout=_busy_timeout_ms / 1000, isolation_level=None, check_same_thread=False)
    sql.row_factory = sqlite3.Row
    open_stats["opened"] += 1

    def ready() -> None:
        retry_busy(lambda: _setup(sql), deadline, "open", _is_open_race)

    try:
        if fresh:
            with file_lock(f"{file}.init"):
                ready()
        else:
            ready()
    except BaseException as e:
        sql.close()
        if is_corrupt(e):
            raise MailError(CORRUPT_MESSAGE) from e
        raise
    mailbox = os.path.join(home, FILE_MAILBOX_DIR)
    if os.path.isdir(mailbox):
        remove_tree(mailbox)
    return Db(home, file, sql, os.stat(file).st_ino)


def open_db(home: str, deadline_ms: float = TOOL_DEADLINE_MS) -> Db:
    file = os.path.abspath(db_path(home))
    known = _connections.get(file)
    if known is not None:
        return known
    with _connecting:
        known = _connections.get(file)
        if known is not None:
            return known
        db = _connect(os.path.dirname(file), file, now_ms() + deadline_ms)
        _connections[file] = db
        return db


def close_db(home: str) -> None:
    file = os.path.abspath(db_path(home))
    db = _connections.pop(file, None)
    if db is not None:
        with db.lock, contextlib.suppress(sqlite3.Error):
            db.sql.close()


def close_all_dbs() -> None:
    for file in list(_connections):
        db = _connections.pop(file, None)
        if db is not None:
            with db.lock, contextlib.suppress(sqlite3.Error):
                db.sql.close()


def _guarded(home: str, work: Callable[[Db], T], in_tx: bool, deadline_ms: float, label: str | None) -> T:
    deadline = now_ms() + deadline_ms
    db = open_db(home, deadline_ms)

    def attempt() -> T:
        if not db.lock.acquire(timeout=max(0.0, (deadline - now_ms()) / 1000)):
            raise StoreBusyError()
        try:
            return run_tx(db.sql, lambda: work(db)) if in_tx else work(db)
        finally:
            db.lock.release()

    try:
        return retry_busy(attempt, deadline, label)
    except sqlite3.Error as e:
        if is_corrupt(e):
            close_db(home)
            raise MailError(CORRUPT_MESSAGE) from e
        raise


def tx(home: str, work: Callable[[Db], T], deadline_ms: float = TOOL_DEADLINE_MS, label: str | None = None) -> T:
    return _guarded(home, work, True, deadline_ms, label)


def query(home: str, work: Callable[[Db], T], deadline_ms: float = TOOL_DEADLINE_MS, label: str | None = None) -> T:
    return _guarded(home, work, False, deadline_ms, label)


def quick_check(home: str) -> bool:
    try:
        return query(home, lambda db: all(row[0] == "ok" for row in db.all("PRAGMA quick_check")), CLEAN_DEADLINE_MS)
    except StoreBusyError:
        return True
    except MailError as e:
        if str(e) == CORRUPT_MESSAGE:
            return False
        raise


def same_file(home: str) -> bool:
    file = os.path.abspath(db_path(home))
    db = _connections.get(file)
    if db is None:
        return True
    try:
        ino: int | None = os.stat(file).st_ino
    except OSError:
        ino = None
    if ino == db.ino:
        return True
    close_db(home)
    return False


def set_aside_corrupt(home: str, now: int | None = None) -> None:
    now = now_ms() if now is None else now
    close_db(home)
    file = db_path(home)
    with file_lock(f"{file}.recover"):
        try:
            broken = sqlite3.connect(file, isolation_level=None)
            try:
                target = os.path.join(home, f"messages.recovered-{now}.db").replace("'", "''")
                broken.execute(f"VACUUM INTO '{target}'")
            finally:
                broken.close()
        except sqlite3.Error:
            pass
        for suffix in ("-shm", "-wal", ""):
            try:
                os.replace(f"{file}{suffix}", os.path.join(home, f"messages.corrupt-{now}.db{suffix}"))
            except FileNotFoundError:
                continue
            except PermissionError as e:
                raise MailError(f"{CORRUPT_MESSAGE}; another process still has it open") from e


def ensure_healthy(home: str, now: int | None = None) -> bool:
    if quick_check(home):
        return True
    set_aside_corrupt(home, now)
    return False

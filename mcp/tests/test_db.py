from __future__ import annotations

import os
import sqlite3
import sys
import threading
import unittest

from ide_agent_tabs.messaging import db
from support import temp_home


def count(home: str) -> int:
    return db.query(home, lambda d: int(d.one("SELECT count(*) FROM messages")[0]))  # type: ignore[index]


class OpenTest(unittest.TestCase):
    def test_the_first_open_creates_a_wal_database_at_the_current_schema_and_a_wake_folder(self) -> None:
        home = temp_home(self)
        d = db.open_db(home)
        self.assertEqual(d.one("PRAGMA journal_mode")[0], "wal")  # type: ignore[index]
        self.assertEqual(d.one("PRAGMA user_version")[0], db.SCHEMA_VERSION)  # type: ignore[index]
        self.assertEqual(d.one("PRAGMA journal_size_limit")[0], db.JOURNAL_SIZE_LIMIT)  # type: ignore[index]
        self.assertTrue(os.path.isdir(db.wake_dir(home)))
        if sys.platform != "win32":
            self.assertEqual(os.stat(db.db_path(home)).st_mode & 0o777, 0o600)

    def test_one_connection_per_database_however_many_threads_open_it(self) -> None:
        home = temp_home(self)
        before = db.open_stats["opened"]
        opened: list[db.Db] = []
        threads = [threading.Thread(target=lambda: opened.append(db.open_db(home))) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len({id(d) for d in opened}), 1)
        self.assertEqual(db.open_stats["opened"] - before, 1)

    def test_the_old_file_mailbox_is_deleted(self) -> None:
        home = temp_home(self)
        os.makedirs(os.path.join(home, "mail", "tab-b", "new"))
        db.open_db(home)
        self.assertFalse(os.path.exists(os.path.join(home, "mail")))

    def test_a_newer_schema_is_used_as_it_is(self) -> None:
        home = temp_home(self)
        db.open_db(home).sql.execute(f"PRAGMA user_version={db.SCHEMA_VERSION + 4}")
        db.close_db(home)
        db.tx(home, lambda d: d.run("INSERT INTO meta (key, value) VALUES ('a', '1')"))
        self.assertEqual(db.query(home, lambda d: d.one("PRAGMA user_version")[0]), db.SCHEMA_VERSION + 4)  # type: ignore[index]

    def test_migration_statements_split_like_sqlite_exec(self) -> None:
        statements = db._statements(db.MIGRATIONS[0])
        self.assertEqual(len(statements), 11)
        self.assertTrue(all(s.startswith("CREATE ") and not s.endswith(";") for s in statements))


class TransactionTest(unittest.TestCase):
    def test_commits_and_rolls_back_on_error(self) -> None:
        home = temp_home(self)
        db.tx(home, lambda d: d.run("INSERT INTO meta (key, value) VALUES ('kept', '1')"))

        def failing(d: db.Db) -> None:
            d.run("INSERT INTO meta (key, value) VALUES ('dropped', '1')")
            raise db.MailError("no")

        with self.assertRaises(db.MailError):
            db.tx(home, failing)
        self.assertEqual(db.query(home, lambda d: [r[0] for r in d.all("SELECT key FROM meta")]), ["kept"])

    def test_a_busy_store_is_retried_until_the_deadline_then_reported_busy(self) -> None:
        home = temp_home(self)
        db.open_db(home)
        other = sqlite3.connect(db.db_path(home), isolation_level=None)
        self.addCleanup(other.close)
        other.execute("BEGIN IMMEDIATE")
        with self.assertRaises(db.StoreBusyError):
            db.tx(home, lambda d: d.run("INSERT INTO meta (key, value) VALUES ('x', '1')"), deadline_ms=400)
        other.execute("ROLLBACK")
        db.tx(home, lambda d: d.run("INSERT INTO meta (key, value) VALUES ('x', '1')"))

    def test_a_corrupt_database_fails_clearly_and_the_health_check_moves_it_aside(self) -> None:
        home = temp_home(self)
        with open(db.db_path(home), "w", encoding="utf-8") as f:
            f.write("this is not a database" * 200)
        with self.assertRaisesRegex(db.MailError, "corrupt"):
            count(home)
        self.assertFalse(db.ensure_healthy(home, 1234))
        self.assertIn("messages.corrupt-1234.db", os.listdir(home))
        self.assertEqual(count(home), 0)
        self.assertTrue(db.ensure_healthy(home))


class NetworkHomeTest(unittest.TestCase):
    def test_a_home_on_a_network_file_system_is_refused(self) -> None:
        self.assertRegex(
            db.network_home_reason("\\\\server\\share\\.ide-agent-tabs", "win32") or "", "network file system.*IDE_AGENT_TABS_HOME"
        )
        self.assertIsNotNone(db.network_home_reason("//server/share/x", "win32"))
        self.assertIsNotNone(db.network_home_reason("\\\\?\\UNC\\server\\share\\x", "win32"))
        self.assertIsNone(db.network_home_reason("C:\\Users\\you\\.ide-agent-tabs", "win32"))
        self.assertIsNone(db.network_home_reason("\\\\?\\C:\\Users\\you", "win32"))
        self.assertIsNone(db.network_home_reason("/Volumes/share", "darwin"))

    def test_linux_reads_the_file_system_type_of_the_longest_mount(self) -> None:
        def mounts(kind: str) -> str:
            return f"22 1 8:1 / / rw - ext4 /dev/sda1 rw\n40 22 0:50 / /home/you rw shared:1 - {kind} server:/home rw\n"

        for kind in ("nfs", "nfs4", "cifs", "smb3", "9p"):
            self.assertIsNotNone(db.network_home_reason("/home/you/.ide-agent-tabs", "linux", lambda k=kind: mounts(k)))
        self.assertIsNone(db.network_home_reason("/home/you/.ide-agent-tabs", "linux", lambda: mounts("ext4")))
        self.assertIsNone(db.network_home_reason("/home/yours", "linux", lambda: mounts("nfs")))

        def broken() -> str:
            raise OSError("no proc")

        self.assertIsNone(db.network_home_reason("/home/you", "linux", broken))


if __name__ == "__main__":
    unittest.main()

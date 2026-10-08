from __future__ import annotations

import os
import re
import sys
import threading
import time
import unittest

from ide_agent_tabs import files
from ide_agent_tabs.home import agent_tabs_home
from support import temp_home


class AtomicWriteTest(unittest.TestCase):
    def test_writes_replaces_and_leaves_no_temp_file(self) -> None:
        home = temp_home(self)
        target = os.path.join(home, "deep", "state.json")
        files.write_atomically(target, "one")
        files.write_atomically(target, "café")
        with open(target, "rb") as f:
            self.assertEqual(f.read(), "café".encode())
        self.assertEqual(os.listdir(os.path.dirname(target)), ["state.json"])

    @unittest.skipIf(sys.platform == "win32", "POSIX modes")
    def test_private_modes(self) -> None:
        home = temp_home(self)
        target = os.path.join(home, "sub", "f")
        files.write_atomically(target, b"x")
        self.assertEqual(os.stat(target).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(os.path.dirname(target)).st_mode & 0o777, 0o700)

    def test_new_private_file_refuses_to_overwrite(self) -> None:
        home = temp_home(self)
        target = os.path.join(home, "once")
        files.write_new_private_file(target, "a")
        with self.assertRaises(FileExistsError):
            files.write_new_private_file(target, "b")

    def test_read_text_if_exists(self) -> None:
        home = temp_home(self)
        self.assertIsNone(files.read_text_if_exists(os.path.join(home, "missing")))
        files.write_atomically(os.path.join(home, "f"), "text")
        self.assertEqual(files.read_text_if_exists(os.path.join(home, "f")), "text")

    def test_remove_stale_files_keeps_fresh_and_unmatched(self) -> None:
        home = temp_home(self)
        for name in ("old.tmp", "new.tmp", "old.json"):
            files.write_atomically(os.path.join(home, name), "")
        old = time.time() - 120
        os.utime(os.path.join(home, "old.tmp"), (old, old))
        os.utime(os.path.join(home, "old.json"), (old, old))
        files.remove_stale_files(home, [".tmp"], 60_000)
        self.assertEqual(sorted(os.listdir(home)), ["new.tmp", "old.json"])


class FileLockTest(unittest.TestCase):
    def test_token_is_pid_and_16_hex_digits(self) -> None:
        self.assertRegex(files.new_lock_token(), rf"^{os.getpid()} [0-9a-f]{{16}}$")

    def test_lock_file_exists_while_held_and_holds_the_token(self) -> None:
        home = temp_home(self)
        target = os.path.join(home, "x")
        with files.file_lock(target):
            text = files.read_text_if_exists(target + ".lock")
            self.assertIsNotNone(text)
            assert text is not None
            self.assertTrue(re.fullmatch(rf"{os.getpid()} [0-9a-f]{{16}}", text))
        self.assertFalse(os.path.exists(target + ".lock"))

    def test_threads_take_turns(self) -> None:
        home = temp_home(self)
        target = os.path.join(home, "count")
        files.write_atomically(target, "0")

        def bump() -> None:
            for _ in range(20):
                with files.file_lock(target):
                    n = int(files.read_text_if_exists(target) or "0")
                    time.sleep(0.001)
                    with open(target, "w", encoding="utf-8") as f:
                        f.write(str(n + 1))

        threads = [threading.Thread(target=bump) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(files.read_text_if_exists(target), "80")

    def test_a_dead_owner_frees_the_lock_at_once(self) -> None:
        home = temp_home(self)
        target = os.path.join(home, "x")
        files.write_new_private_file(target + ".lock", "999999 0123456789abcdef")
        started = time.monotonic()
        with files.file_lock(target, alive=lambda pid: False):
            pass
        self.assertLess(time.monotonic() - started, 1)

    def test_a_live_owner_keeps_the_lock_until_it_is_10_seconds_old(self) -> None:
        home = temp_home(self)
        target = os.path.join(home, "x")
        lock = target + ".lock"
        files.write_new_private_file(lock, f"{os.getpid()} 0123456789abcdef")
        with self.assertRaises(TimeoutError), files.file_lock(target, timeout_ms=200):
            pass
        old = time.time() - 9
        os.utime(lock, (old, old))
        with self.assertRaises(TimeoutError), files.file_lock(target, timeout_ms=200):
            pass
        old = time.time() - 11
        os.utime(lock, (old, old))
        with files.file_lock(target, timeout_ms=200):
            pass

    def test_a_lock_without_a_pid_counts_only_by_age(self) -> None:
        home = temp_home(self)
        target = os.path.join(home, "x")
        files.write_new_private_file(target + ".lock", "garbage")
        with self.assertRaises(TimeoutError), files.file_lock(target, timeout_ms=100, alive=lambda pid: False):
            pass

    def test_release_keeps_a_lock_another_holder_took_over(self) -> None:
        home = temp_home(self)
        target = os.path.join(home, "x")
        lock = target + ".lock"
        with files.file_lock(target):
            os.remove(lock)
            files.write_new_private_file(lock, "1 ffffffffffffffff")
        self.assertEqual(files.read_text_if_exists(lock), "1 ffffffffffffffff")


class HomeTest(unittest.TestCase):
    def test_the_environment_overrides_the_default_home(self) -> None:
        self.assertEqual(agent_tabs_home({"IDE_AGENT_TABS_HOME": "/elsewhere"}), "/elsewhere")
        self.assertEqual(agent_tabs_home({"IDE_AGENT_TABS_HOME": ""}), os.path.join(os.path.expanduser("~"), ".ide-agent-tabs"))


if __name__ == "__main__":
    unittest.main()

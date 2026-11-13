from __future__ import annotations

import os
import time
import unittest

from ide_agent_tabs import files
from support import py_worker, start_together, temp_home


class LockProcessesTest(unittest.TestCase):
    def test_holders_in_six_processes_exclude_each_other(self) -> None:
        home = temp_home(self)
        counter = os.path.join(home, "counter")
        files.write_atomically(counter, "0")
        args = {"file": counter, "rounds": 25}
        workers = [py_worker(self, "lockCount", args) for _ in range(6)]
        start_together(workers)
        for w in workers:
            self.assertEqual(w.result(), {"rounds": 25})
        self.assertEqual(files.read_text_if_exists(counter), str(6 * 25))
        self.assertFalse(os.path.exists(counter + ".lock"))

    def test_a_waiter_in_another_process_waits_for_the_holder(self) -> None:
        home = temp_home(self)
        target = os.path.join(home, "x")
        holder = py_worker(self, "lockHold", {"file": target})
        holder.line("locked")
        waiter = py_worker(self, "lockOnce", {"file": target, "timeoutMs": 400})
        start_together([waiter])
        blocked = waiter.result()
        self.assertFalse(blocked["ok"], blocked)
        waiter = py_worker(self, "lockOnce", {"file": target})
        start_together([waiter])
        time.sleep(0.3)
        holder.send("release")
        self.assertEqual(holder.result(), {"released": True})
        freed = waiter.result()
        self.assertTrue(freed["ok"], freed)
        self.assertGreaterEqual(freed["ms"], 200)

    def test_a_process_breaks_a_lock_another_left_behind(self) -> None:
        home = temp_home(self)
        target = os.path.join(home, "x")
        holder = py_worker(self, "lockHold", {"file": target})
        holder.line("locked")
        holder.close()
        self.assertTrue(os.path.exists(target + ".lock"))
        waiter = py_worker(self, "lockOnce", {"file": target, "timeoutMs": 2_000})
        start_together([waiter])
        freed = waiter.result()
        self.assertTrue(freed["ok"], freed)
        self.assertLess(freed["ms"], 1_000)

    def test_a_process_breaks_a_live_owners_lock_after_10_seconds(self) -> None:
        home = temp_home(self)
        target = os.path.join(home, "x")
        lock = target + ".lock"
        waiter = py_worker(self, "lockOnce", {"file": target, "timeoutMs": 5_000})
        waiter.line("ready")
        files.write_new_private_file(lock, f"{os.getpid()} 0123456789abcdef")
        old = time.time() - 9.6
        os.utime(lock, (old, old))
        waiter.send()
        freed = waiter.result()
        self.assertTrue(freed["ok"], freed)
        self.assertGreaterEqual(freed["ms"], 200)
        self.assertFalse(os.path.exists(lock))


if __name__ == "__main__":
    unittest.main()

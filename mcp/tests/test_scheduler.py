from __future__ import annotations

import threading
import unittest

from ide_agent_tabs.scheduler import Scheduler


class SchedulerLogTest(unittest.TestCase):
    def test_a_failing_job_logs_each_new_error_once_and_keeps_running(self) -> None:
        logged: list[str] = []
        errors = ["disk full", "disk full", "disk full", "locked", None, "locked"]
        done = threading.Event()
        runs: list[int] = []

        def work() -> None:
            runs.append(1)
            if len(runs) > len(errors):
                done.set()
                return
            error = errors[len(runs) - 1]
            if error is not None:
                raise OSError(error)

        scheduler = Scheduler(log=logged.append)
        self.addCleanup(scheduler.stop)
        job = scheduler.every(0.001, work)
        self.assertTrue(done.wait(5))
        job.cancel()
        self.assertEqual(len(logged), 3)
        self.assertTrue(all(line.startswith("scheduled job ") for line in logged))
        self.assertEqual([line.rsplit(": ", 1)[1] for line in logged], ["disk full", "locked", "locked"])

    def test_a_scheduler_without_a_log_swallows_job_failures(self) -> None:
        ran = threading.Event()
        scheduler = Scheduler()
        self.addCleanup(scheduler.stop)

        def fail() -> None:
            raise RuntimeError("boom")

        scheduler.soon(fail)
        scheduler.soon(ran.set)
        self.assertTrue(ran.wait(5))


if __name__ == "__main__":
    unittest.main()

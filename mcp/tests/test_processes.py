from __future__ import annotations

import os
import subprocess
import sys
import unittest

from ide_agent_tabs import processes


class LivenessTest(unittest.TestCase):
    def test_this_process_is_alive(self) -> None:
        self.assertTrue(processes.pid_alive(os.getpid()))

    def test_non_positive_pids_are_not(self) -> None:
        self.assertFalse(processes.pid_alive(0))
        self.assertFalse(processes.pid_alive(-1))

    def test_an_exited_child_is_not_alive_and_a_running_one_survives_the_check(self) -> None:
        child = subprocess.Popen([sys.executable, "-I", "-S", "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE)
        try:
            for _ in range(3):
                self.assertTrue(processes.pid_alive(child.pid))
            self.assertIsNone(child.poll(), "the liveness check must not end the process")
        finally:
            assert child.stdin is not None
            child.stdin.close()
            child.wait()
        self.assertFalse(processes.pid_alive(child.pid))


class RunTest(unittest.TestCase):
    def test_round_trips_utf8_through_a_child(self) -> None:
        code = "import sys; d = sys.stdin.buffer.read(); sys.stdout.buffer.write(d); sys.stderr.buffer.write(d)"
        text = "café 中 \U0001f600"
        result = processes.run(sys.executable, ["-I", "-S", "-c", code], input=text)
        self.assertEqual(result, processes.RunResult(0, text, text))

    def test_reports_the_exit_code(self) -> None:
        self.assertEqual(processes.run(sys.executable, ["-I", "-S", "-c", "raise SystemExit(3)"]).code, 3)

    def test_times_out(self) -> None:
        with self.assertRaises(TimeoutError):
            processes.run(sys.executable, ["-I", "-S", "-c", "import time; time.sleep(5)"], timeout=0.3)

    def test_flags_are_windows_only(self) -> None:
        if sys.platform == "win32":
            self.assertEqual(processes.child_flags(), processes.CREATE_NO_WINDOW)
            self.assertEqual(processes.detached_flags(), 0x08000000 | 0x00000200 | 0x01000000)
        else:
            self.assertEqual(processes.child_flags(), 0)
            self.assertEqual(processes.detached_flags(), 0)


if __name__ == "__main__":
    unittest.main()

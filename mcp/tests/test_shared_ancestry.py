from __future__ import annotations

import os
import subprocess
import sys
import time
import unittest

from ide_agent_tabs.shared.ancestry import (
    ProcessInfo,
    find_agent,
    find_agent_process,
    is_skipped_name,
    parse_linux_stat,
    parse_ps_table,
    process_lookup,
)
from support import SRC, temp_home


def table(*rows: tuple[int, int, str, float]) -> dict[int, ProcessInfo]:
    return {pid: ProcessInfo(pid, ppid, name, start) for pid, ppid, name, start in rows}


class FindAgentTest(unittest.TestCase):
    def test_the_agent_is_the_nearest_ancestor_that_is_neither_a_shell_nor_the_py_launcher(self) -> None:
        t = table(
            (10, 9, "python.exe", 500),
            (9, 8, "py.exe", 400),
            (8, 7, "cmd.exe", 300),
            (7, 6, "claude.exe", 200),
            (6, 1, "explorer.exe", 100),
        )
        self.assertEqual(find_agent(t.get, 10), t[7])
        t = table((10, 9, "python3", 500), (9, 8, "-bash", 400), (8, 7, "node", 300), (7, 1, "claude", 200))
        self.assertEqual(find_agent(t.get, 10), t[8], "a nested session running as node is the agent, not the outer claude")

    def test_the_walk_stops_at_a_missing_parent_a_reused_pid_or_a_cycle(self) -> None:
        self.assertIsNone(find_agent(table((10, 9, "python", 500), (9, 8, "sh", 400)).get, 10))
        self.assertIsNone(find_agent(table((10, 9, "python", 500), (9, 1, "claude", 9_000)).get, 10), "a parent younger than its child")
        self.assertIsNotNone(find_agent(table((10, 9, "python", 500), (9, 1, "claude", 1_900)).get, 10), "within the start-time slack")
        self.assertIsNone(find_agent(table((10, 10, "python", 500)).get, 10))
        self.assertIsNone(find_agent(table((10, 9, "python", 500), (9, 10, "sh", 400)).get, 10))
        self.assertIsNone(find_agent({}.get, 10))

    def test_shell_and_launcher_names_match_with_paths_and_login_dashes(self) -> None:
        for name in ("cmd.exe", "CMD.EXE", "/bin/bash", "-zsh", "C:\\Windows\\py.exe", "pyw.exe", "pwsh", "env", "conhost.exe"):
            self.assertTrue(is_skipped_name(name), name)
        for name in ("claude.exe", "node", "python.exe", "python3", "codex", "pyright"):
            self.assertFalse(is_skipped_name(name), name)


class ParseTest(unittest.TestCase):
    def test_linux_stat_lines_with_spaces_and_parentheses_in_the_name(self) -> None:
        fields = ["S", "42"] + ["0"] * 17 + ["250"]
        info = parse_linux_stat(77, f"77 (we (ird) name) {' '.join(fields)} 0 0", 1_000_000, 10)
        self.assertEqual(info, ProcessInfo(77, 42, "we (ird) name", 1_002_500))
        self.assertIsNone(parse_linux_stat(77, "garbage", 0, 10))

    def test_ps_lines_read_lstart_as_local_time(self) -> None:
        text = "  501   1 Thu Oct  8 10:00:00 2026 /Applications/Claude Code.app/claude\n 600 501 Thu Oct  8 10:00:01 2026 -zsh\nbad line\n"
        parsed = parse_ps_table(text)
        self.assertEqual(sorted(parsed), [501, 600])
        self.assertEqual(parsed[501].name, "/Applications/Claude Code.app/claude")
        expected = time.mktime(time.strptime("Thu Oct 8 10:00:00 2026", "%a %b %d %H:%M:%S %Y")) * 1000
        self.assertEqual(parsed[501].start_ms, expected)
        self.assertEqual(parsed[600].start_ms - parsed[501].start_ms, 1000)


class RealProcessTest(unittest.TestCase):
    def test_this_process_has_a_start_time_and_its_parent_in_the_table(self) -> None:
        lookup = process_lookup()
        me = lookup(os.getpid())
        assert me is not None
        self.assertEqual(me.ppid, os.getppid())
        self.assertLess(abs(me.start_ms - time.time() * 1000), 24 * 3600 * 1000 * 30)

    def test_a_child_run_through_a_shell_finds_this_process(self) -> None:
        folder = temp_home(self, "iat-chain-")
        script = os.path.join(folder, "probe.py")
        with open(script, "w", encoding="utf-8") as f:
            f.write("import sys\nsys.path.insert(0, sys.argv[1])\nfrom ide_agent_tabs.shared.ancestry import find_agent_process\n")
            f.write("found = find_agent_process()\nprint(found.pid if found else 0)\n")
        args = [sys.executable, "-I", "-S", script, SRC]
        if sys.platform == "win32":
            command: list[str] = ["cmd.exe", "/d", "/s", "/c", subprocess.list2cmdline(args)]
        else:
            command = ["/bin/sh", "-c", " ".join(f"'{a}'" for a in args) + "; true"]
        out = subprocess.run(command, capture_output=True, timeout=60, check=True)
        self.assertEqual(int(out.stdout.decode().strip()), os.getpid())

    def test_the_lookup_from_this_process_names_a_live_ancestor(self) -> None:
        found = find_agent_process()
        if found is not None:
            self.assertNotEqual(found.pid, os.getpid())


if __name__ == "__main__":
    unittest.main()

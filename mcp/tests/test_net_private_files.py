from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from ide_agent_tabs.net.private_files import PrivateFileError, check_private, ensure_private_dir, make_private_file

WINDOWS = sys.platform == "win32"
USERS_SID = "S-1-5-32-545"


def icacls(*arguments: str) -> None:
    exe = os.path.join(os.environ["SystemRoot"], "System32", "icacls.exe")
    result = subprocess.run([exe, *arguments], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


def make_junction(link: Path, target: Path) -> None:
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


class PrivateFilesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.base = Path(os.path.realpath(tempfile.mkdtemp(prefix="iat-priv-")))
        self.addCleanup(shutil.rmtree, str(self.base), True)

    def private_dir(self, name: str = "net") -> Path:
        folder = self.base / name
        ensure_private_dir(folder)
        return folder

    def test_ensure_creates_nested_folders_that_pass_the_check(self) -> None:
        folder = self.base / "a" / "b" / "net"
        ensure_private_dir(folder)
        self.assertTrue(folder.is_dir())
        self.assertEqual(check_private(folder), [])

    def test_ensure_is_idempotent(self) -> None:
        folder = self.private_dir()
        ensure_private_dir(folder)
        self.assertEqual(check_private(folder), [])

    def test_ensure_refuses_a_file_in_the_way(self) -> None:
        file = self.base / "net"
        file.write_text("x")
        with self.assertRaises(OSError):
            ensure_private_dir(file)

    def test_make_private_file_passes_the_check(self) -> None:
        file = self.private_dir() / "key.pem"
        file.write_text("secret")
        make_private_file(file)
        self.assertEqual(check_private(file), [])
        self.assertEqual(file.read_text(), "secret")

    def test_make_private_file_refuses_a_missing_file_and_a_folder(self) -> None:
        folder = self.private_dir()
        for target in (folder / "missing", folder):
            with self.subTest(target=target.name), self.assertRaises(PrivateFileError):
                make_private_file(target)

    def test_check_reports_a_missing_path(self) -> None:
        self.assertEqual(len(check_private(self.base / "missing")), 1)

    def test_check_refuses_a_path_outside_the_root(self) -> None:
        folder = self.private_dir()
        other = self.base / "other"
        other.mkdir()
        self.assertTrue(check_private(folder, root=other))

    def test_check_with_a_root_accepts_a_clean_chain(self) -> None:
        folder = self.private_dir()
        file = folder / "peers.json"
        file.write_text("{}")
        make_private_file(file)
        self.assertEqual(check_private(file, root=self.base), [])
        self.assertEqual(check_private(folder, root=folder), [])

    def test_check_reports_a_special_file(self) -> None:
        if WINDOWS:
            self.skipTest("no special file types to create")
        assert sys.platform != "win32"
        fifo = self.private_dir() / "pipe"
        os.mkfifo(fifo, 0o600)
        self.assertTrue(check_private(fifo))

    @unittest.skipUnless(WINDOWS, "Windows access lists")
    def test_windows_reports_an_extra_entry_on_a_folder_and_repairs_it(self) -> None:
        folder = self.private_dir()
        icacls(str(folder), "/grant", f"*{USERS_SID}:R")
        problems = check_private(folder)
        self.assertEqual(len(problems), 1)
        self.assertIn(USERS_SID, problems[0])
        ensure_private_dir(folder)
        self.assertEqual(check_private(folder), [])

    @unittest.skipUnless(WINDOWS, "Windows access lists")
    def test_windows_reports_an_extra_entry_on_a_file_and_repairs_it(self) -> None:
        file = self.private_dir() / "policy.json"
        file.write_text("{}")
        make_private_file(file)
        icacls(str(file), "/grant", f"*{USERS_SID}:R")
        problems = check_private(file)
        self.assertEqual(len(problems), 1)
        self.assertIn(USERS_SID, problems[0])
        make_private_file(file)
        self.assertEqual(check_private(file), [])

    @unittest.skipUnless(WINDOWS, "Windows access lists")
    def test_windows_reports_a_folder_that_inherits_access(self) -> None:
        folder = self.private_dir()
        icacls(str(folder), "/inheritance:e")
        problems = check_private(folder)
        self.assertTrue(any("inherits" in problem for problem in problems), problems)
        ensure_private_dir(folder)
        self.assertEqual(check_private(folder), [])

    @unittest.skipUnless(WINDOWS, "Windows access lists")
    def test_windows_allows_only_the_user_system_and_administrators(self) -> None:
        folder = self.private_dir()
        result = subprocess.run(
            [os.path.join(os.environ["SystemRoot"], "System32", "icacls.exe"), str(folder)], capture_output=True, text=True, check=False
        )
        entries = [line for line in result.stdout.splitlines() if "(" in line]
        self.assertEqual(len(entries), 3, result.stdout)
        self.assertTrue(all("(OI)(CI)(F)" in entry for entry in entries), result.stdout)
        self.assertEqual(sum("SYSTEM" in entry or "Administrators" in entry for entry in entries), 2, result.stdout)

    @unittest.skipUnless(WINDOWS, "Windows access lists")
    def test_windows_refuses_a_system_folder_with_other_owner_and_entries(self) -> None:
        self.assertTrue(check_private(Path(os.environ["SystemRoot"])))

    @unittest.skipUnless(WINDOWS, "Windows junctions")
    def test_windows_refuses_a_junction(self) -> None:
        target = self.private_dir("target")
        junction = self.base / "junction"
        make_junction(junction, target)
        self.addCleanup(os.rmdir, junction)
        problems = check_private(junction)
        self.assertEqual(len(problems), 1)
        self.assertIn("reparse", problems[0])
        with self.assertRaises(PrivateFileError):
            ensure_private_dir(junction)
        self.assertTrue(target.exists())

    @unittest.skipUnless(WINDOWS, "Windows junctions")
    def test_windows_refuses_a_leaf_below_a_junction_when_the_chain_is_checked(self) -> None:
        target = self.private_dir("target")
        leaf = target / "net"
        ensure_private_dir(leaf)
        junction = self.base / "junction"
        make_junction(junction, target)
        self.addCleanup(os.rmdir, junction)
        through = junction / "net"
        self.assertEqual(check_private(through), [])
        problems = check_private(through, root=self.base)
        self.assertEqual(len(problems), 1)
        self.assertIn(str(junction), problems[0])

    def test_refuses_a_symbolic_link(self) -> None:
        target = self.private_dir("target")
        link = self.base / "link"
        try:
            os.symlink(target, link, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"cannot create symbolic links: {error}")
        self.addCleanup(os.remove if not WINDOWS else os.rmdir, link)
        problems = check_private(link)
        self.assertEqual(len(problems), 1)
        with self.assertRaises(PrivateFileError):
            ensure_private_dir(link)

    def test_refuses_a_file_symbolic_link(self) -> None:
        folder = self.private_dir()
        real = folder / "real"
        real.write_text("x")
        make_private_file(real)
        link = folder / "link"
        try:
            os.symlink(real, link)
        except OSError as error:
            self.skipTest(f"cannot create symbolic links: {error}")
        self.assertEqual(len(check_private(link)), 1)
        with self.assertRaises(PrivateFileError):
            make_private_file(link)

    @unittest.skipIf(WINDOWS, "POSIX modes")
    def test_posix_modes_and_owner(self) -> None:
        assert sys.platform != "win32"
        folder = self.private_dir()
        self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
        file = folder / "key.pem"
        file.write_text("secret")
        file.chmod(0o644)
        self.assertEqual(len(check_private(file)), 1)
        make_private_file(file)
        self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)
        self.assertEqual(check_private(file), [])
        self.assertEqual(os.stat(file).st_uid, os.getuid())

    @unittest.skipIf(WINDOWS, "POSIX modes")
    def test_posix_reports_a_loose_folder_and_repairs_it(self) -> None:
        folder = self.base / "loose"
        folder.mkdir(mode=0o755)
        folder.chmod(0o755)
        self.assertEqual(len(check_private(folder)), 1)
        ensure_private_dir(folder)
        self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
        for mode in (0o770, 0o707, 0o750):
            with self.subTest(mode=oct(mode)):
                folder.chmod(mode)
                self.assertEqual(len(check_private(folder)), 1)

    @unittest.skipIf(WINDOWS, "POSIX modes")
    def test_posix_reports_a_folder_owned_by_someone_else(self) -> None:
        assert sys.platform != "win32"
        if os.getuid() == 0:
            self.skipTest("root owns everything it can reach")
        self.assertTrue(check_private(Path("/etc")))


if __name__ == "__main__":
    unittest.main()

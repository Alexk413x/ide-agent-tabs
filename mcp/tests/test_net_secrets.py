from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from ide_agent_tabs.net.private_files import check_private, ensure_private_dir
from ide_agent_tabs.net.secrets import SecretsError, load_passphrase, new_passphrase, store_passphrase

WINDOWS = sys.platform == "win32"
STORED = "passphrase.dpapi" if WINDOWS else "passphrase"


class NewPassphraseTest(unittest.TestCase):
    def test_is_64_ascii_hex_characters_and_differs_each_time(self) -> None:
        first, second = new_passphrase(), new_passphrase()
        self.assertEqual(len(first), 64)
        self.assertEqual(first, first.decode("ascii").lower().encode("ascii"))
        int(first, 16)
        self.assertNotEqual(first, second)


class StoreTest(unittest.TestCase):
    def setUp(self) -> None:
        base = Path(os.path.realpath(tempfile.mkdtemp(prefix="iat-secret-")))
        self.addCleanup(shutil.rmtree, str(base), True)
        self.folder = base / "net"
        ensure_private_dir(self.folder)

    def test_round_trip(self) -> None:
        passphrase = new_passphrase()
        store_passphrase(self.folder, passphrase)
        self.assertEqual(load_passphrase(self.folder), passphrase)
        self.assertEqual(sorted(entry.name for entry in self.folder.iterdir()), [STORED])
        self.assertEqual(check_private(self.folder / STORED), [])

    def test_creates_the_folder_when_it_is_missing(self) -> None:
        folder = self.folder / "nested"
        store_passphrase(folder, b"abc")
        self.assertEqual(check_private(folder), [])
        self.assertEqual(load_passphrase(folder), b"abc")

    def test_a_second_store_replaces_the_first(self) -> None:
        store_passphrase(self.folder, b"first")
        store_passphrase(self.folder, b"second")
        self.assertEqual(load_passphrase(self.folder), b"second")
        self.assertEqual(sorted(entry.name for entry in self.folder.iterdir()), [STORED])

    def test_refuses_an_empty_passphrase(self) -> None:
        with self.assertRaises(SecretsError):
            store_passphrase(self.folder, b"")
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_a_missing_file_is_file_not_found(self) -> None:
        with self.assertRaises(FileNotFoundError):
            load_passphrase(self.folder)

    @unittest.skipUnless(WINDOWS, "DPAPI")
    def test_windows_file_holds_a_dpapi_blob_not_the_passphrase(self) -> None:
        passphrase = new_passphrase()
        store_passphrase(self.folder, passphrase)
        content = (self.folder / "passphrase.dpapi").read_bytes()
        self.assertNotIn(passphrase, content)
        self.assertEqual(content[:4], b"\x01\x00\x00\x00")

    @unittest.skipUnless(WINDOWS, "DPAPI")
    def test_windows_refuses_a_corrupted_blob(self) -> None:
        store_passphrase(self.folder, new_passphrase())
        stored = self.folder / "passphrase.dpapi"
        content = bytearray(stored.read_bytes())
        content[-5] ^= 0xFF
        stored.write_bytes(bytes(content))
        with self.assertRaises(SecretsError):
            load_passphrase(self.folder)

    @unittest.skipUnless(WINDOWS, "DPAPI")
    def test_windows_refuses_a_plaintext_file(self) -> None:
        (self.folder / "passphrase.dpapi").write_bytes(b"plain text, not a blob")
        with self.assertRaises(SecretsError):
            load_passphrase(self.folder)

    @unittest.skipUnless(WINDOWS, "Windows access lists")
    def test_windows_refuses_a_file_with_an_extra_access_entry(self) -> None:
        store_passphrase(self.folder, new_passphrase())
        exe = os.path.join(os.environ["SystemRoot"], "System32", "icacls.exe")
        result = subprocess.run([exe, str(self.folder / STORED), "/grant", "*S-1-5-32-545:R"], capture_output=True, check=False)
        self.assertEqual(result.returncode, 0)
        with self.assertRaisesRegex(SecretsError, "S-1-5-32-545"):
            load_passphrase(self.folder)

    @unittest.skipIf(WINDOWS, "POSIX modes")
    def test_posix_file_is_mode_0600_with_the_raw_passphrase(self) -> None:
        store_passphrase(self.folder, b"abc")
        stored = self.folder / "passphrase"
        self.assertEqual(stat.S_IMODE(stored.stat().st_mode), 0o600)
        self.assertEqual(stored.read_bytes(), b"abc")

    @unittest.skipIf(WINDOWS, "POSIX modes")
    def test_posix_refuses_a_file_readable_by_others(self) -> None:
        store_passphrase(self.folder, b"abc")
        (self.folder / "passphrase").chmod(0o644)
        with self.assertRaises(SecretsError):
            load_passphrase(self.folder)

    @unittest.skipIf(WINDOWS, "POSIX modes")
    def test_posix_refuses_a_folder_open_to_others(self) -> None:
        store_passphrase(self.folder, b"abc")
        self.folder.chmod(0o755)
        with self.assertRaises(SecretsError):
            load_passphrase(self.folder)


if __name__ == "__main__":
    unittest.main()

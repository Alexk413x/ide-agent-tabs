from __future__ import annotations

import os
import sys
import unittest

from ide_agent_tabs.editor_clis import EditorCli, find_editor_clis, resolve_editor_cli
from ide_agent_tabs.installed import find_on_path, is_cmd_shim, is_installed
from support import temp_home


def touch(path: str) -> None:
    with open(path, "w", encoding="utf-8"):
        pass


class InstalledTest(unittest.TestCase):
    def setUp(self) -> None:
        self.home = temp_home(self, "iat-installed-")
        self.bin = os.path.join(self.home, "bin")
        os.mkdir(self.bin)
        for name in ("posix-cli", "npm-cli.cmd", "shim-cli.ps1", "native-cli.exe", "old-cli.bat", "tool.exe"):
            touch(os.path.join(self.bin, name))

    def test_installed_means_the_command_is_on_path_with_windows_extensions_on_windows(self) -> None:
        path_var = os.pathsep.join([os.path.join(self.home, "missing"), self.bin])
        for command in ("posix-cli", "npm-cli", "shim-cli", "native-cli", "old-cli", "native-cli.exe"):
            with self.subTest(command=command):
                self.assertTrue(is_installed(command, path_var, True))
        self.assertTrue(is_installed("posix-cli", path_var, False))
        self.assertFalse(is_installed("npm-cli", path_var, False))
        self.assertFalse(is_installed("absent", path_var, True))
        self.assertTrue(is_installed(os.path.join(self.bin, "posix-cli"), "", False))
        self.assertTrue(is_installed(os.path.join(self.bin, "npm-cli"), "", True))
        self.assertFalse(is_installed(os.path.join(self.bin, "absent"), path_var, False))
        self.assertFalse(is_installed(f"bin{os.sep}posix-cli", path_var, False))
        self.assertFalse(is_installed("bin/posix-cli", path_var, False))

    def test_finds_an_executable_on_path_and_skips_blank_quoted_and_invalid_entries(self) -> None:
        path_var = os.pathsep.join(["", "  ", os.path.join(self.home, "no", "such"), "bad<>|dir", f'"{self.bin}"'])
        self.assertEqual(find_on_path(path_var, "tool.exe"), os.path.join(self.bin, "tool.exe"))
        self.assertIsNone(find_on_path(path_var, "absent.exe"))

    def test_finds_the_microsoft_store_pwsh_alias(self) -> None:
        windows_apps = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WindowsApps")
        try:
            os.lstat(os.path.join(windows_apps, "pwsh.exe"))
        except OSError:
            self.skipTest("Store pwsh not installed")
        self.assertEqual(find_on_path(windows_apps, "pwsh.exe"), os.path.join(windows_apps, "pwsh.exe"))

    def test_a_command_is_a_cmd_shim_when_path_finds_its_cmd_or_bat_before_an_exe_or_finds_nothing(self) -> None:
        a, b = os.path.join(self.home, "a"), os.path.join(self.home, "b")
        os.mkdir(a)
        os.mkdir(b)
        touch(os.path.join(a, "codex.cmd"))
        touch(os.path.join(b, "codex.exe"))
        touch(os.path.join(b, "claude.exe"))
        path_var = os.pathsep.join([a, b])
        self.assertTrue(is_cmd_shim("codex", path_var))
        self.assertFalse(is_cmd_shim("claude", path_var))
        self.assertTrue(is_cmd_shim("missing", path_var))


class EditorCliTest(unittest.TestCase):
    def test_finds_an_editor_cli_in_a_linux_fallback_location_when_path_misses_it(self) -> None:
        fallback = "/snap/bin/code"
        found = find_editor_clis("linux", {"PATH": ""}, "/home/a", lambda p: p == fallback)
        self.assertEqual(found, [EditorCli("code", fallback)])

    def test_finds_editor_clis_on_path_first_then_in_install_locations(self) -> None:
        bin_dir = temp_home(self, "iat-clis-")
        windows = sys.platform == "win32"
        touch(os.path.join(bin_dir, "code.cmd" if windows else "code"))
        if windows:
            touch(os.path.join(bin_dir, "cursor"))
        fallback = "C:\\Users\\a\\AppData\\Local\\Programs\\Antigravity IDE\\bin\\antigravity-ide.cmd"
        platform = "win32" if windows else sys.platform
        found = find_editor_clis(
            platform, {"PATH": bin_dir, "LOCALAPPDATA": "C:\\Users\\a\\AppData\\Local"}, bin_dir, lambda p: p == fallback
        )
        expected = [EditorCli("code", os.path.join(bin_dir, "code.cmd" if windows else "code"))]
        if windows:
            expected.append(EditorCli("antigravity-ide", fallback))
        self.assertEqual(found, expected)

    def test_resolves_an_editor_cli_named_by_the_user(self) -> None:
        env = {"PATH": ""}

        def exists(p: str) -> bool:
            return p == "D:\\VS Code\\bin\\code.cmd"

        self.assertEqual(
            resolve_editor_cli("D:\\VS Code\\bin\\code", "win32", env, "C:\\Users\\a", exists),
            EditorCli("code", "D:\\VS Code\\bin\\code.cmd"),
        )
        self.assertIsNone(resolve_editor_cli("..\\code", "win32", env, "C:\\Users\\a", exists))
        self.assertIsNone(resolve_editor_cli("code", "win32", env, "C:\\Users\\a", exists))


if __name__ == "__main__":
    unittest.main()

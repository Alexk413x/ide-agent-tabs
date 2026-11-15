from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

from ide_agent_tabs.net import shim
from ide_agent_tabs.net.private_files import check_private
from ide_agent_tabs.net.shim import ShimError
from net_support import make_root

INTERPRETER = r"C:\Program Files\Python312\python.exe"
ENTRY = r"C:\Users\Me\plugin\launch\agent_tabs.py"


class Registry:
    def __init__(self, value: str, kind: int = 2) -> None:
        self.value = value
        self.kind = kind
        self.writes: list[tuple[str, int]] = []
        self.broadcasts = 0

    def read(self) -> tuple[str, int]:
        return self.value, self.kind

    def write(self, value: str, kind: int) -> None:
        self.writes.append((value, kind))
        self.value = value

    def broadcast(self) -> None:
        self.broadcasts += 1


class SourceTest(unittest.TestCase):
    def test_cmd_runs_the_interpreter_with_isolated_flags_and_passes_arguments_only_through_percent_star(self) -> None:
        text = shim.cmd_source(INTERPRETER, ENTRY)
        self.assertEqual(text, f'@"{INTERPRETER}" -I -S "{ENTRY}" %*\r\n')
        self.assertEqual(text.count("%"), 1)

    def test_cmd_refuses_a_percent_sign_in_a_path(self) -> None:
        with self.assertRaises(ShimError):
            shim.cmd_source(r"C:\100%\python.exe", ENTRY)
        with self.assertRaises(ShimError):
            shim.cmd_source(INTERPRETER, r"C:\%TEMP%\agent_tabs.py")

    def test_ps1_fixes_the_executable_and_leading_arguments_as_literals(self) -> None:
        text = shim.ps1_source(INTERPRETER, ENTRY)
        self.assertTrue(text.startswith(f"$exe = '{INTERPRETER}'\n$lead = @('-I', '-S', '{ENTRY}')\n"))
        self.assertIn("@($lead) + @($args)", text)
        self.assertIn("exit $process.ExitCode", text)
        self.assertNotIn("Get-Command", text)

    def test_ps1_doubles_single_quotes_in_a_path(self) -> None:
        text = shim.ps1_source(r"C:\O'Neil\python.exe", ENTRY)
        self.assertIn(r"$exe = 'C:\O''Neil\python.exe'", text)

    def test_sh_execs_the_interpreter_by_absolute_path_with_quoted_arguments(self) -> None:
        text = shim.sh_source("/usr/bin/python3", "/opt/my plugin/launch/agent_tabs.py")
        self.assertEqual(text, "#!/bin/sh\nexec /usr/bin/python3 -I -S '/opt/my plugin/launch/agent_tabs.py' \"$@\"\n")

    def test_sh_quotes_a_path_that_holds_a_single_quote_or_a_command(self) -> None:
        text = shim.sh_source("/usr/bin/python3", "/o'p/$(calc)/agent_tabs.py")
        self.assertIn("'/o'\"'\"'p/$(calc)/agent_tabs.py'", text)


class InstallTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = make_root(self, "iat-shim-")
        self.home = os.path.join(self.root, "home")
        self.interpreter = os.path.join(self.root, "tools", "python.exe")
        self.entry = os.path.join(self.root, "plugin", "agent_tabs.py")

    def install(self) -> Path:
        return shim.install_shim(self.home, self.interpreter, self.entry)

    def test_the_three_shims_are_written_to_the_bin_folder(self) -> None:
        folder = self.install()
        self.assertEqual(folder, Path(self.home) / "bin")
        self.assertEqual(sorted(os.listdir(folder)), ["agent-tabs", "agent-tabs.cmd", "agent-tabs.ps1"])
        contents = {name: (folder / name).read_bytes().decode("utf-8") for name in os.listdir(folder)}
        self.assertEqual(contents["agent-tabs.cmd"], shim.cmd_source(self.interpreter, self.entry))
        self.assertEqual(contents["agent-tabs.ps1"], shim.ps1_source(self.interpreter, self.entry))
        self.assertEqual(contents["agent-tabs"], shim.sh_source(self.interpreter, self.entry))
        self.assertIn("\r\n", contents["agent-tabs.cmd"])
        self.assertNotIn("\r", contents["agent-tabs"])

    def test_the_folder_and_files_follow_the_private_file_rules(self) -> None:
        folder = self.install()
        self.assertEqual(check_private(folder), [])
        for name in os.listdir(folder):
            self.assertEqual(check_private(folder / name), [], msg=name)

    @unittest.skipIf(sys.platform == "win32", "POSIX mode bits")
    def test_the_sh_shim_is_executable_by_its_owner_only(self) -> None:
        folder = self.install()
        self.assertEqual(os.stat(folder / "agent-tabs").st_mode & 0o777, 0o700)

    def test_installing_again_replaces_the_files(self) -> None:
        self.install()
        self.interpreter = os.path.join(self.root, "other", "python.exe")
        folder = self.install()
        self.assertIn(self.interpreter, (folder / "agent-tabs.cmd").read_text(encoding="utf-8"))
        self.assertEqual(len(os.listdir(folder)), 3)

    def test_a_foreign_file_in_the_folder_is_refused_and_left_alone(self) -> None:
        folder = Path(self.home) / "bin"
        folder.mkdir(parents=True)
        (folder / "git.exe").write_bytes(b"")
        with self.assertRaises(ShimError) as caught:
            self.install()
        self.assertIn("git.exe", str(caught.exception))
        self.assertTrue((folder / "git.exe").exists())
        self.assertNotIn("agent-tabs.cmd", os.listdir(folder))

    def test_a_temporary_file_left_by_a_crash_is_removed(self) -> None:
        folder = self.install()
        leftover = folder / "agent-tabs.cmd.123.456.tmp"
        leftover.write_bytes(b"")
        self.install()
        self.assertFalse(leftover.exists())

    def test_paths_that_are_not_absolute_or_hold_quotes_are_refused_before_writing(self) -> None:
        for interpreter, entry in (
            ("python.exe", self.entry),
            (self.interpreter, "agent_tabs.py"),
            ('C:\\a"b\\python.exe', self.entry),
            (self.interpreter, "C:\\a\nb\\agent_tabs.py"),
        ):
            with self.assertRaises(ShimError):
                shim.install_shim(self.home, interpreter, entry)
        self.assertFalse(os.path.exists(os.path.join(self.home, "bin")))


class PathContainsTest(unittest.TestCase):
    def test_the_process_path_is_searched_with_normalised_entries(self) -> None:
        folder = os.path.join(os.sep, "home", "me", "bin")
        text = os.pathsep.join(["/usr/bin", folder + os.sep, ""])
        self.assertTrue(shim.path_contains(folder, text))
        self.assertFalse(shim.path_contains(folder, os.pathsep.join(["/usr/bin", "/home/me/bin2"])))
        self.assertFalse(shim.path_contains(folder, ""))

    def test_on_windows_the_saved_user_path_counts_too(self) -> None:
        folder = r"C:\Users\Me\.ide-agent-tabs\bin"
        registry = Registry(r"C:\Tools;%USERPROFILE%\x;" + folder)
        self.assertTrue(shim.path_contains(folder, None, "win32", registry.read))
        self.assertFalse(shim.path_contains(folder, None, "win32", Registry(r"C:\Tools").read))

    def test_a_registry_that_cannot_be_read_counts_as_absent(self) -> None:
        def broken() -> tuple[str, int]:
            raise OSError("denied")

        self.assertFalse(shim.path_contains(r"C:\x", None, "win32", broken))


class AppendTest(unittest.TestCase):
    folder = r"C:\Users\Me\.ide-agent-tabs\bin"

    def append(self, registry: Registry, folder: str | None = None) -> str:
        return shim.append_to_user_path(folder or self.folder, "win32", registry.read, registry.write, registry.broadcast)

    def test_the_folder_is_appended_never_prepended_and_the_change_is_broadcast(self) -> None:
        registry = Registry(r"C:\Tools;%USERPROFILE%\bin")
        message = self.append(registry)
        self.assertEqual(registry.writes, [(r"C:\Tools;%USERPROFILE%\bin;" + self.folder, 2)])
        self.assertEqual(registry.broadcasts, 1)
        self.assertIn("Appended", message)

    def test_a_trailing_semicolon_does_not_double(self) -> None:
        registry = Registry(r"C:\Tools;")
        self.append(registry)
        self.assertEqual(registry.writes[0][0], r"C:\Tools;" + self.folder)

    def test_an_empty_path_becomes_the_folder(self) -> None:
        registry = Registry("", 1)
        self.append(registry)
        self.assertEqual(registry.writes, [(self.folder, 1)])

    def test_the_value_type_is_kept(self) -> None:
        registry = Registry(r"C:\Tools", 1)
        self.append(registry)
        self.assertEqual(registry.writes[0][1], 1)

    def test_a_folder_that_is_already_there_changes_nothing(self) -> None:
        registry = Registry(r"C:\Tools;" + self.folder)
        message = self.append(registry)
        self.assertEqual(registry.writes, [])
        self.assertEqual(registry.broadcasts, 0)
        self.assertIn("already", message)

    def test_a_folder_that_cannot_be_a_path_entry_is_refused(self) -> None:
        for folder in (r"C:\a;b", 'C:\\a"b', "C:\\a\nb"):
            registry = Registry("")
            with self.assertRaises(ShimError, msg=repr(folder)):
                self.append(registry, folder)
            self.assertEqual(registry.writes, [])

    def test_other_systems_get_the_line_to_add(self) -> None:
        text = shim.append_to_user_path("/home/me/.ide-agent-tabs/bin", "linux", shell="/bin/zsh")
        self.assertIn("~/.zshrc", text)
        self.assertIn('export PATH="$PATH":/home/me/.ide-agent-tabs/bin', text)
        self.assertIn("~/.bashrc", shim.append_to_user_path("/x", "linux", shell="/usr/bin/bash"))
        self.assertIn("~/.profile", shim.append_to_user_path("/x", "darwin", shell="/bin/fish"))

    def test_the_profile_line_quotes_a_folder_with_a_space(self) -> None:
        self.assertEqual(shim.profile_line("/home/my user/bin"), "export PATH=\"$PATH\":'/home/my user/bin'")


if __name__ == "__main__":
    unittest.main()

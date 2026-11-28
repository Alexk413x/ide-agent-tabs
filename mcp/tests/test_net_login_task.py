from __future__ import annotations

import json
import os
import plistlib
import subprocess
import sys
import unittest
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from ide_agent_tabs.net import login_task
from ide_agent_tabs.net.login_task import AclEntry, Environment, LoginTaskError
from ide_agent_tabs.net.private_files import check_private, ensure_private_dir
from ide_agent_tabs.processes import RunResult
from net_support import make_root

NS = "{http://schemas.microsoft.com/windows/2004/02/mit/task}"
SID = "S-1-5-21-111-222-333-1001"
WINDOWS = r"C:\Windows"
READ_ONLY = 0x1200A9
MODIFY = 0x1301BF


class FakeRun:
    def __init__(self, *results: RunResult) -> None:
        self.calls: list[tuple[str, list[str]]] = []
        self.files: list[bytes] = []
        self.private: list[list[str]] = []
        self.results = list(results)

    def __call__(self, command: str, args: Sequence[str] = (), **_options: Any) -> RunResult:
        self.calls.append((command, list(args)))
        if "/XML" in args and args[0] == "/Create":
            path = Path(args[args.index("/XML") + 1])
            self.files.append(path.read_bytes())
            self.private.append(check_private(path))
        return self.results.pop(0) if self.results else RunResult(0, "", "")


class Sandbox:
    def __init__(self, test: unittest.TestCase) -> None:
        root = make_root(test, "iat-login-")
        self.home = os.path.join(root, "home")
        self.tools = os.path.join(root, "tools")
        self.install = os.path.join(root, "install")
        self.user_home = os.path.join(root, "user")
        for folder in (self.tools, self.install, self.user_home):
            os.makedirs(folder)
        self.interpreter = os.path.join(self.tools, "python.exe")
        self.launch_script = os.path.join(self.install, "agent_tabs.py")
        Path(self.interpreter).write_bytes(b"")
        Path(self.launch_script).write_text("pass\n", encoding="utf-8")
        ensure_private_dir(Path(self.home))
        self.run = FakeRun()
        self.entries: list[AclEntry] | None = [AclEntry(0, 0, MODIFY, SID)]

    def env(self, platform: str, **overrides: Any) -> Environment:
        values: dict[str, Any] = {
            "platform": platform,
            "interpreter": self.interpreter,
            "launch_script": self.launch_script,
            "user_home": self.user_home,
            "run": self.run,
            "windows_directory": lambda: WINDOWS,
            "user_sid": lambda: SID,
            "acl_entries": lambda _path: self.entries,
            "uid": lambda: 501,
            "stat": lambda _path: os.stat_result((0o40700, 0, 0, 1, 501, 0, 0, 0, 0, 0)),
        }
        values.update(overrides)
        return Environment(**values)


class TaskXmlTest(unittest.TestCase):
    def parsed(self, **overrides: str) -> ElementTree.Element:
        values = {
            "sid": SID,
            "interpreter": r"C:\Python 3\python.exe",
            "launcher": r"C:\Users\Me & Co\.ide-agent-tabs\net\launcher.py",
            "working_directory": r"C:\Users\Me & Co\.ide-agent-tabs\net",
        }
        values.update(overrides)
        text = login_task.task_xml(**values)
        self.assertTrue(text.startswith('<?xml version="1.0" encoding="UTF-16"?>'))
        return ElementTree.fromstring(text.split("?>", 1)[1])

    def value(self, root: ElementTree.Element, path: str) -> str | None:
        return root.findtext("/".join(NS + part for part in path.split("/")))

    def test_the_principal_runs_as_the_current_user_without_elevation_or_a_password(self) -> None:
        root = self.parsed()
        self.assertEqual(self.value(root, "Principals/Principal/UserId"), SID)
        self.assertEqual(self.value(root, "Principals/Principal/LogonType"), "InteractiveToken")
        self.assertEqual(self.value(root, "Principals/Principal/RunLevel"), "LeastPrivilege")
        text = ElementTree.tostring(root, encoding="unicode")
        for forbidden in ("Password", "S-1-5-18", "SYSTEM", "HighestAvailable", "ServiceAccount", "S4U"):
            self.assertNotIn(forbidden, text)

    def test_the_trigger_is_a_logon_of_the_current_user(self) -> None:
        root = self.parsed()
        triggers = root.find(f"{NS}Triggers")
        assert triggers is not None
        self.assertEqual([child.tag for child in triggers], [f"{NS}LogonTrigger"])
        self.assertEqual(self.value(root, "Triggers/LogonTrigger/UserId"), SID)
        self.assertEqual(self.value(root, "Triggers/LogonTrigger/Enabled"), "true")

    def test_the_settings_keep_one_instance_and_never_stop_it(self) -> None:
        root = self.parsed()
        self.assertEqual(self.value(root, "Settings/MultipleInstancesPolicy"), "IgnoreNew")
        self.assertEqual(self.value(root, "Settings/DisallowStartIfOnBatteries"), "false")
        self.assertEqual(self.value(root, "Settings/ExecutionTimeLimit"), "PT0S")

    def test_the_action_names_absolute_paths_and_escapes_them(self) -> None:
        root = self.parsed()
        self.assertEqual(self.value(root, "Actions/Exec/Command"), r"C:\Python 3\python.exe")
        self.assertEqual(
            self.value(root, "Actions/Exec/Arguments"),
            r'-I -S "C:\Users\Me & Co\.ide-agent-tabs\net\launcher.py" lan-start',
        )
        self.assertEqual(self.value(root, "Actions/Exec/WorkingDirectory"), r"C:\Users\Me & Co\.ide-agent-tabs\net")
        self.assertEqual(len(root.findall(f"{NS}Actions/{NS}Exec")), 1)


class AccessTest(unittest.TestCase):
    def test_a_broad_group_with_write_rights_is_found(self) -> None:
        for sid, name in (("S-1-5-32-545", "Users"), ("S-1-5-11", "Authenticated Users"), ("S-1-1-0", "Everyone")):
            self.assertEqual(login_task.writable_by_others([AclEntry(0, 0, MODIFY, sid)]), [f"{name} can change it"], sid)

    def test_read_and_execute_is_fine(self) -> None:
        entries = [AclEntry(0, 0, READ_ONLY, "S-1-5-32-545"), AclEntry(0, 0, 0x1F01FF, SID)]
        self.assertEqual(login_task.writable_by_others(entries), [])

    def test_each_single_write_right_counts(self) -> None:
        for right in (0x2, 0x4, 0x40, 0x10000, 0x40000, 0x80000, 0x10000000, 0x40000000):
            self.assertEqual(len(login_task.writable_by_others([AclEntry(0, 0, right, "S-1-1-0")])), 1, hex(right))

    def test_an_entry_that_applies_only_to_children_does_not_count(self) -> None:
        self.assertEqual(login_task.writable_by_others([AclEntry(0, 0x8 | 0x3, MODIFY, "S-1-5-11")]), [])

    def test_a_deny_entry_does_not_count_and_no_list_is_refused(self) -> None:
        self.assertEqual(login_task.writable_by_others([AclEntry(1, 0, MODIFY, "")]), [])
        self.assertEqual(len(login_task.writable_by_others(None)), 1)

    @unittest.skipUnless(sys.platform == "win32", "reads a Windows access list")
    def test_the_real_reader_sees_a_group_added_to_a_folder(self) -> None:
        from ide_agent_tabs.net import private_files

        folder = Path(make_root(self, "iat-acl-"))
        entries = login_task._acl_entries(str(folder))  # pyright: ignore[reportPrivateUsage]
        assert entries is not None
        self.assertIn("S-1-5-18", [entry.sid for entry in entries])
        self.assertEqual(login_task.writable_by_others(entries), [])
        private_files._icacls(folder, ["/grant", "*S-1-5-11:(OI)(CI)M"])  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(login_task.writable_by_others(login_task._acl_entries(str(folder))), ["Authenticated Users can change it"])  # pyright: ignore[reportPrivateUsage]


class LauncherTest(unittest.TestCase):
    def test_the_launcher_is_private_and_names_the_install(self) -> None:
        box = Sandbox(self)
        path = login_task.refresh_launcher(box.home, box.launch_script)
        self.assertEqual(path, Path(box.home) / "net" / "launcher.py")
        self.assertEqual(check_private(path), [])
        text = path.read_text(encoding="utf-8")
        self.assertIn(repr(box.launch_script), text)
        self.assertIn(repr(box.home), text)

    def test_the_launcher_is_rewritten_for_a_new_install(self) -> None:
        box = Sandbox(self)
        login_task.refresh_launcher(box.home, box.launch_script)
        other = os.path.join(box.install, "next", "agent_tabs.py")
        os.makedirs(os.path.dirname(other))
        Path(other).write_text("pass\n", encoding="utf-8")
        path = login_task.refresh_launcher(box.home, other)
        self.assertIn(repr(other), path.read_text(encoding="utf-8"))
        self.assertNotIn(repr(box.launch_script), path.read_text(encoding="utf-8"))
        self.assertEqual(check_private(path), [])

    def test_a_missing_or_unsafe_launch_script_is_refused(self) -> None:
        box = Sandbox(self)
        for script in (os.path.join(box.install, "missing.py"), "agent_tabs.py", 'C:\\a"b\\agent_tabs.py'):
            with self.assertRaises(LoginTaskError, msg=script):
                login_task.refresh_launcher(box.home, script)
        self.assertFalse(login_task.launcher_path(box.home).exists())

    def test_the_launcher_runs_net_lan_start_with_the_recorded_home(self) -> None:
        box = Sandbox(self)
        Path(box.launch_script).write_text(
            "import json, os, sys\nprint(json.dumps([sys.argv[1:], os.environ['IDE_AGENT_TABS_HOME']]))\n", encoding="utf-8"
        )
        path = login_task.refresh_launcher(box.home, box.launch_script)
        done = subprocess.run([sys.executable, "-I", "-S", str(path), "lan-start"], capture_output=True, text=True, check=False)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout), [["net", "lan", "start"], box.home])

    def test_the_launcher_ignores_any_other_argument(self) -> None:
        box = Sandbox(self)
        path = login_task.refresh_launcher(box.home, box.launch_script)
        for args in ([], ["on"], ["lan-start", "extra"]):
            done = subprocess.run([sys.executable, "-I", "-S", str(path), *args], capture_output=True, text=True, check=False)
            self.assertEqual(done.returncode, 2, args)


class SyncLauncherTest(unittest.TestCase):
    def setUp(self) -> None:
        self.box = Sandbox(self)
        self.old = os.path.join(self.box.install, "0.11.0", "agent_tabs.py")
        os.makedirs(os.path.dirname(self.old))
        Path(self.old).write_text("pass\n", encoding="utf-8")

    def target(self) -> login_task.LauncherTarget:
        return login_task.launcher_target(login_task.launcher_path(self.box.home).read_text(encoding="utf-8"))

    def write_unversioned(self, script: str) -> None:
        text = login_task.launcher_source(self.box.home, script)
        text = "".join(line for line in text.splitlines(keepends=True) if not line.startswith("VERSION = "))
        login_task.launcher_path(self.box.home).write_text(text, encoding="utf-8")

    def test_the_launcher_records_its_version(self) -> None:
        login_task.refresh_launcher(self.box.home, self.box.launch_script, "0.14.3")
        self.assertEqual(self.target(), login_task.LauncherTarget("0.14.3", self.box.launch_script))

    def test_no_launcher_means_no_login_task_and_nothing_is_written(self) -> None:
        self.assertIsNone(login_task.sync_launcher(self.box.home, self.box.env("win32"), "0.14.3"))
        self.assertFalse(login_task.launcher_path(self.box.home).exists())

    def test_a_launcher_of_an_older_version_is_rewritten_to_the_installed_plugin(self) -> None:
        login_task.refresh_launcher(self.box.home, self.old, "0.11.0")
        message = login_task.sync_launcher(self.box.home, self.box.env("win32"), "0.14.3")
        self.assertIsNotNone(message)
        assert message is not None
        self.assertIn("0.14.3", message)
        self.assertIn(self.old, message)
        self.assertEqual(self.target(), login_task.LauncherTarget("0.14.3", self.box.launch_script))
        self.assertEqual(check_private(login_task.launcher_path(self.box.home)), [])

    def test_a_launcher_that_records_no_version_is_rewritten(self) -> None:
        login_task.refresh_launcher(self.box.home, self.old)
        self.write_unversioned(self.old)
        self.assertEqual(self.target().version, None)
        self.assertIsNotNone(login_task.sync_launcher(self.box.home, self.box.env("win32"), "0.14.3"))
        self.assertEqual(self.target(), login_task.LauncherTarget("0.14.3", self.box.launch_script))

    def test_a_launcher_whose_script_is_gone_is_rewritten_even_when_newer(self) -> None:
        login_task.refresh_launcher(self.box.home, self.old, "0.15.0")
        os.remove(self.old)
        self.assertIsNotNone(login_task.sync_launcher(self.box.home, self.box.env("win32"), "0.14.3"))
        self.assertEqual(self.target(), login_task.LauncherTarget("0.14.3", self.box.launch_script))

    def test_a_current_or_newer_launcher_is_left_alone(self) -> None:
        for version in ("0.14.3", "0.15.0"):
            login_task.refresh_launcher(self.box.home, self.old, version)
            before = login_task.launcher_path(self.box.home).read_bytes()
            self.assertIsNone(login_task.sync_launcher(self.box.home, self.box.env("win32"), "0.14.3"), version)
            self.assertEqual(login_task.launcher_path(self.box.home).read_bytes(), before)

    def test_an_install_folder_others_can_change_is_refused_and_the_launcher_kept(self) -> None:
        login_task.refresh_launcher(self.box.home, self.old, "0.11.0")
        self.box.entries = [AclEntry(0, 0, MODIFY, SID), AclEntry(0, 0, MODIFY, "S-1-1-0")]
        with self.assertRaises(LoginTaskError):
            login_task.sync_launcher(self.box.home, self.box.env("win32"), "0.14.3")
        self.assertEqual(self.target(), login_task.LauncherTarget("0.11.0", self.old))

    def test_a_hostile_launcher_text_names_no_target(self) -> None:
        text = "LAUNCH_SCRIPT = __import__('os').getcwd()\nVERSION = 99\n"
        self.assertEqual(login_task.launcher_target(text), login_task.LauncherTarget(None, None))


class WindowsTaskTest(unittest.TestCase):
    SCHTASKS = os.path.join(WINDOWS, "System32", "schtasks.exe")

    def test_register_creates_the_task_from_a_private_xml_file_and_deletes_it(self) -> None:
        box = Sandbox(self)
        message = login_task.register(box.home, box.env("win32"))
        self.assertEqual(len(box.run.calls), 1)
        command, args = box.run.calls[0]
        self.assertEqual(command, self.SCHTASKS)
        self.assertEqual((args[0], args[1], args[3], args[4], args[5]), ("/Create", "/XML", "/TN", login_task.TASK_NAME, "/F"))
        xml_path = Path(args[2])
        self.assertEqual(xml_path.parent, Path(box.home) / "net")
        self.assertFalse(xml_path.exists())
        self.assertEqual(box.run.private, [[]])
        root = ElementTree.fromstring(box.run.files[0].decode("utf-16").split("?>", 1)[1])
        self.assertEqual(root.findtext(f"{NS}Actions/{NS}Exec/{NS}Command"), box.interpreter)
        launcher = str(login_task.launcher_path(box.home))
        self.assertEqual(root.findtext(f"{NS}Actions/{NS}Exec/{NS}Arguments"), f'-I -S "{launcher}" lan-start')
        self.assertEqual(root.findtext(f"{NS}Actions/{NS}Exec/{NS}WorkingDirectory"), os.path.join(box.home, "net"))
        self.assertIn(login_task.TASK_NAME, message)
        self.assertEqual(check_private(login_task.launcher_path(box.home)), [])

    def test_register_refuses_an_interpreter_folder_that_users_can_write(self) -> None:
        box = Sandbox(self)
        box.entries = [AclEntry(0, 0, MODIFY, "S-1-5-32-545")]
        with self.assertRaises(LoginTaskError) as caught:
            login_task.register(box.home, box.env("win32"))
        self.assertIn("Users can change it", str(caught.exception))
        self.assertIn(box.tools, str(caught.exception))
        self.assertEqual(box.run.calls, [])

    def test_register_refuses_an_install_folder_that_everyone_can_write(self) -> None:
        box = Sandbox(self)
        folders: list[str] = []

        def entries(path: str) -> list[AclEntry]:
            folders.append(path)
            return [AclEntry(0, 0, MODIFY, "S-1-1-0")] if path == box.install else []

        with self.assertRaises(LoginTaskError) as caught:
            login_task.register(box.home, box.env("win32", acl_entries=entries))
        self.assertIn("Everyone can change it", str(caught.exception))
        self.assertEqual(sorted(folders), sorted([box.tools, box.install]))
        self.assertEqual(box.run.calls, [])

    def test_register_refuses_a_missing_or_unsafe_interpreter(self) -> None:
        box = Sandbox(self)
        for interpreter in (os.path.join(box.tools, "missing.exe"), "python.exe", 'C:\\a"b\\python.exe'):
            with self.assertRaises(LoginTaskError, msg=interpreter):
                login_task.register(box.home, box.env("win32", interpreter=interpreter))
        self.assertEqual(box.run.calls, [])

    def test_register_reports_a_schtasks_failure_and_still_deletes_the_file(self) -> None:
        box = Sandbox(self)
        box.run.results = [RunResult(1, "", "ERROR: Access is denied.")]
        with self.assertRaises(LoginTaskError) as caught:
            login_task.register(box.home, box.env("win32"))
        self.assertIn("exited 1", str(caught.exception))
        self.assertIn("Access is denied", str(caught.exception))
        self.assertEqual([p.name for p in (Path(box.home) / "net").glob("login-task-*")], [])

    def test_remove_deletes_the_task_and_the_launcher(self) -> None:
        box = Sandbox(self)
        login_task.refresh_launcher(box.home, box.launch_script)
        message = login_task.remove(box.home, box.env("win32"))
        self.assertEqual(box.run.calls, [(self.SCHTASKS, ["/Delete", "/TN", login_task.TASK_NAME, "/F"])])
        self.assertIn("Removed", message)
        self.assertFalse(login_task.launcher_path(box.home).exists())

    def test_remove_without_a_task_is_not_an_error(self) -> None:
        box = Sandbox(self)
        box.run.results = [RunResult(1, "", "ERROR: The system cannot find the file specified."), RunResult(1, "", "not found")]
        self.assertEqual(login_task.remove(box.home, box.env("win32")), "No login task is registered.")

    def test_remove_reports_a_task_that_remains(self) -> None:
        box = Sandbox(self)
        box.run.results = [RunResult(1, "", "ERROR: Access is denied."), RunResult(0, "", "")]
        with self.assertRaises(LoginTaskError) as caught:
            login_task.remove(box.home, box.env("win32"))
        self.assertIn("remains", str(caught.exception))

    def test_status_reports_the_action_target(self) -> None:
        box = Sandbox(self)
        definition = login_task.task_xml(
            sid=SID, interpreter=box.interpreter, launcher="C:\\h\\net\\launcher.py", working_directory="C:\\h\\net"
        )
        box.run.results = [RunResult(0, definition, "")]
        text = login_task.status(box.home, box.env("win32"))
        self.assertIn(login_task.TASK_NAME, text)
        self.assertIn(f'{box.interpreter} -I -S "C:\\h\\net\\launcher.py" lan-start', text)
        self.assertEqual(box.run.calls[0][1], ["/Query", "/TN", login_task.TASK_NAME, "/XML"])

    def test_status_without_a_task_and_with_a_hostile_definition(self) -> None:
        box = Sandbox(self)
        box.run.results = [RunResult(1, "", "ERROR")]
        self.assertEqual(login_task.status(box.home, box.env("win32")), "none")
        box.run.results = [RunResult(0, '<!DOCTYPE x [<!ENTITY a "b">]><Task xmlns="x"/>', "")]
        self.assertIn("target unreadable", login_task.status(box.home, box.env("win32")))


class MacosTest(unittest.TestCase):
    def test_the_launch_agent_holds_only_program_arguments(self) -> None:
        data = plistlib.loads(
            login_task.launch_agent_plist(interpreter="/usr/bin/python3", launcher="/h/net/launcher.py", working_directory="/h/net")
        )
        self.assertEqual(data["ProgramArguments"], ["/usr/bin/python3", "-I", "-S", "/h/net/launcher.py", "lan-start"])
        self.assertEqual(data["Label"], login_task.AGENT_LABEL)
        self.assertTrue(data["RunAtLoad"])
        self.assertEqual(data["WorkingDirectory"], "/h/net")
        self.assertNotIn("EnvironmentVariables", data)
        self.assertNotIn("UserName", data)

    def test_register_writes_the_plist_and_bootstraps_it(self) -> None:
        box = Sandbox(self)
        login_task.register(box.home, box.env("darwin"))
        plist = Path(box.user_home) / "Library" / "LaunchAgents" / f"{login_task.AGENT_LABEL}.plist"
        data = plistlib.loads(plist.read_bytes())
        self.assertEqual(data["ProgramArguments"][:4], [box.interpreter, "-I", "-S", str(login_task.launcher_path(box.home))])
        if sys.platform != "win32":
            self.assertEqual(plist.stat().st_mode & 0o777, 0o600)
        self.assertEqual(
            box.run.calls,
            [
                ("/bin/launchctl", ["bootout", f"gui/501/{login_task.AGENT_LABEL}"]),
                ("/bin/launchctl", ["bootstrap", "gui/501", str(plist)]),
            ],
        )

    def test_remove_boots_out_and_deletes_the_plist(self) -> None:
        box = Sandbox(self)
        login_task.register(box.home, box.env("darwin"))
        box.run.calls.clear()
        login_task.remove(box.home, box.env("darwin"))
        self.assertEqual(box.run.calls, [("/bin/launchctl", ["bootout", f"gui/501/{login_task.AGENT_LABEL}"])])
        self.assertFalse((Path(box.user_home) / "Library" / "LaunchAgents" / f"{login_task.AGENT_LABEL}.plist").exists())
        self.assertEqual(login_task.remove(box.home, box.env("darwin")), "No login task is registered.")

    def test_status_reads_the_plist(self) -> None:
        box = Sandbox(self)
        self.assertEqual(login_task.status(box.home, box.env("darwin")), "none")
        login_task.register(box.home, box.env("darwin"))
        self.assertIn(f"{box.interpreter} -I -S", login_task.status(box.home, box.env("darwin")))

    def test_a_world_writable_interpreter_folder_or_a_foreign_owner_is_refused(self) -> None:
        box = Sandbox(self)
        for mode, uid in ((0o40777, 501), (0o40755, 502)):
            fake = os.stat_result((mode, 0, 0, 1, uid, 0, 0, 0, 0, 0))
            with self.assertRaises(LoginTaskError, msg=oct(mode)):
                login_task.register(box.home, box.env("darwin", stat=lambda _path, fake=fake: fake))
        self.assertEqual(box.run.calls, [])


class LinuxTest(unittest.TestCase):
    def test_the_unit_has_absolute_quoted_paths(self) -> None:
        text = login_task.systemd_unit(
            interpreter="/opt/py 3/bin/python3", launcher="/h/a%b$c/net/launcher.py", working_directory="/h/a%b$c/net"
        )
        self.assertIn('ExecStart="/opt/py 3/bin/python3" "-I" "-S" "/h/a%%b$$c/net/launcher.py" "lan-start"\n', text)
        self.assertIn("WorkingDirectory=/h/a%%b$c/net\n", text)
        self.assertIn("WantedBy=default.target", text)
        self.assertNotIn("Environment", text)

    def test_register_writes_the_unit_and_enables_it(self) -> None:
        box = Sandbox(self)
        login_task.register(box.home, box.env("linux"))
        unit = Path(box.user_home) / ".config" / "systemd" / "user" / login_task.UNIT_NAME
        self.assertIn(f"ExecStart={login_task._systemd_word(box.interpreter)}", unit.read_text(encoding="utf-8"))
        if sys.platform != "win32":
            self.assertEqual(unit.stat().st_mode & 0o777, 0o600)
        self.assertEqual(box.run.calls, [("/usr/bin/systemctl", ["--user", "enable", login_task.UNIT_NAME])])

    def test_register_reports_a_systemctl_failure(self) -> None:
        box = Sandbox(self)
        box.run.results = [RunResult(1, "", "Failed to connect to bus")]
        with self.assertRaises(LoginTaskError) as caught:
            login_task.register(box.home, box.env("linux"))
        self.assertIn("Failed to connect", str(caught.exception))

    def test_remove_disables_and_deletes_the_unit(self) -> None:
        box = Sandbox(self)
        login_task.register(box.home, box.env("linux"))
        box.run.calls.clear()
        login_task.remove(box.home, box.env("linux"))
        self.assertEqual(box.run.calls, [("/usr/bin/systemctl", ["--user", "disable", login_task.UNIT_NAME])])
        self.assertFalse((Path(box.user_home) / ".config" / "systemd" / "user" / login_task.UNIT_NAME).exists())

    def test_status_reads_the_unit(self) -> None:
        box = Sandbox(self)
        login_task.register(box.home, box.env("linux"))
        self.assertIn("systemd user unit", login_task.status(box.home, box.env("linux")))


if __name__ == "__main__":
    unittest.main()

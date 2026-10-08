from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import unittest

from config_support import SERVER_SCRIPT, make_source, write
from ide_agent_tabs.profiles import codex_python
from ide_agent_tabs.server_copy import (
    CURRENT_FILE,
    bundled_build,
    copy_dir,
    current_build,
    plugin_source,
    read_current,
    read_python,
    refresh_server_copy,
    server_copy_path,
    write_python,
)
from support import temp_home


def run_python(*args: str, env: dict[str, str] | None = None) -> str:
    done = subprocess.run([sys.executable, "-I", "-S", *args], capture_output=True, encoding="utf-8", env=env, check=False, timeout=60)
    if done.returncode != 0:
        raise AssertionError(done.stderr)
    return done.stdout


class ServerCopy(unittest.TestCase):
    def test_a_build_folder_stubs_and_a_pointer_are_written_once(self) -> None:
        root = temp_home(self)
        source = make_source(root)
        home = os.path.join(root, "home")
        build = refresh_server_copy(source, home)
        self.assertEqual(build, bundled_build(source))
        assert build is not None
        self.assertTrue(build.startswith("0.9.0-"))
        folder = copy_dir(home)
        self.assertEqual(sorted(os.listdir(folder)), sorted([build, "current.json", "launch"]))
        self.assertEqual(
            sorted(os.listdir(os.path.join(folder, build, "launch"))),
            ["agent-launch.fish", "agent-launch.ps1", "agent-launch.sh", "agent_hook.py", "mcp_server.py"],
        )
        self.assertEqual(sorted(os.listdir(os.path.join(folder, "launch"))), ["agent_hook.py", "mcp_server.py"])
        self.assertEqual(read_current(home), {"build": build, "version": "0.9.0"})
        self.assertIsNone(refresh_server_copy(source, home), "an unchanged source copies nothing")

    def test_the_stub_runs_the_current_build_with_its_arguments(self) -> None:
        root = temp_home(self)
        home = os.path.join(root, "home")
        build = refresh_server_copy(make_source(root), home)
        assert build is not None
        target_file, argv = ast.literal_eval(run_python(server_copy_path(home, sys.platform), "a b", "c"))
        self.assertEqual(os.path.normcase(target_file), os.path.normcase(os.path.join(copy_dir(home), build, "launch", "mcp_server.py")))
        self.assertEqual(argv, ["a b", "c"])
        os.remove(os.path.join(copy_dir(home), CURRENT_FILE))
        done = subprocess.run(
            [sys.executable, "-I", "-S", server_copy_path(home, sys.platform)], capture_output=True, encoding="utf-8", check=False
        )
        self.assertEqual(done.returncode, 1)
        self.assertIn("incomplete", done.stderr)

    def test_an_update_switches_builds_and_keeps_only_the_previous_one(self) -> None:
        root = temp_home(self)
        home = os.path.join(root, "home")
        builds = []
        for n in range(3):
            builds.append(refresh_server_copy(make_source(os.path.join(root, str(n)), server=SERVER_SCRIPT + f"# {n}\n"), home))
        self.assertEqual(len(set(builds)), 3)
        self.assertEqual(current_build(home), builds[2])
        self.assertEqual(sorted(n for n in os.listdir(copy_dir(home)) if n not in ("launch", "current.json")), sorted(builds[1:]))

    def test_an_older_plugin_never_replaces_a_newer_copy(self) -> None:
        root = temp_home(self)
        home = os.path.join(root, "home")
        newer = refresh_server_copy(make_source(os.path.join(root, "a"), "0.10.0"), home)
        self.assertIsNone(refresh_server_copy(make_source(os.path.join(root, "b"), "0.9.0", server="# old\n"), home))
        self.assertEqual(current_build(home), newer)
        newest = refresh_server_copy(make_source(os.path.join(root, "c"), "0.10.1"), home)
        self.assertEqual(current_build(home), newest)

    def test_the_real_package_reads_its_version_and_launch_scripts_from_the_copy(self) -> None:
        home = os.path.join(temp_home(self), "home")
        source = plugin_source()
        build = refresh_server_copy(source, home)
        assert build is not None
        src = os.path.join(copy_dir(home), build, "src")
        code = (
            f"import sys; sys.path.insert(0, {src!r})\n"
            "from ide_agent_tabs.version import PACKAGE_VERSION\n"
            "from ide_agent_tabs.list_ides_cli import scripts_dir\n"
            "print(PACKAGE_VERSION); print(scripts_dir())"
        )
        version, scripts = run_python("-c", code).splitlines()
        self.assertEqual(version, source.version)
        self.assertNotEqual(version, "0")
        self.assertEqual(os.path.normcase(scripts), os.path.normcase(os.path.join(copy_dir(home), build, "launch")))
        self.assertTrue(os.path.isfile(os.path.join(scripts, "agent-launch.ps1")))


class PythonFile(unittest.TestCase):
    def test_python_json_records_the_interpreter_and_codex_tabs_read_it(self) -> None:
        home = temp_home(self)
        windows = sys.platform == "win32"
        self.assertEqual(codex_python(home, windows), ("py", "-3") if windows else ("python3",))
        self.assertTrue(write_python(home, sys.executable))
        self.assertFalse(write_python(home, sys.executable))
        self.assertEqual(read_python(home), sys.executable)
        self.assertEqual(codex_python(home, windows), (sys.executable,))
        write(os.path.join(home, "mcp", "python.json"), json.dumps({"python": os.path.join(home, "missing", "python")}))
        self.assertEqual(codex_python(home, windows), ("py", "-3") if windows else ("python3",))
        write(os.path.join(home, "mcp", "python.json"), json.dumps({"python": sys.executable + "'"}))
        self.assertEqual(codex_python(home, False), ("python3",))


if __name__ == "__main__":
    unittest.main()

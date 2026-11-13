from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import unittest

from support import temp_home
from test_shared_launch import PLUGIN, clean_env, shells

LAUNCH = os.path.join(PLUGIN, "mcp", "launch")
WINDOWS = sys.platform == "win32"
STUB = """\
import json
import os
import sys

data = sys.stdin.buffer.read().decode("utf-8") if os.environ.get("STUB_STDIN") else ""
print(json.dumps({"argv": sys.argv[1:], "stdin": data}))
sys.exit(int(os.environ.get("STUB_EXIT", "0")))
"""
NO_PYTHON = "Agent Tabs needs Python 3.9 or later. Install it from https://www.python.org/downloads/ and run the command again."
TRICKY_ARGS = ["sync-ides", "--install", "two words", 'say "hi"', "trailing\\", "C:\\a b\\", "", "caf\u00e9", "it's", 'a\\\\"b']


def hook_commands() -> dict[str, str]:
    with open(os.path.join(PLUGIN, "hooks", "hooks.json"), encoding="utf-8") as f:
        hooks = json.load(f)["hooks"]["SessionStart"]
    return {entry["matcher"]: entry["hooks"][0]["command"] for entry in hooks}


def stub_plugin(test: unittest.TestCase) -> str:
    """A plugin folder whose agent_tabs.py only reports its arguments and stdin, so no real sync runs."""
    root = temp_home(test, "iat-cli-launch-")
    launch = os.path.join(root, "mcp", "launch")
    os.makedirs(launch)
    for name in ("sync-hook.ps1", "agent-tabs.ps1", "agent-tabs"):
        shutil.copyfile(os.path.join(LAUNCH, name), os.path.join(launch, name))
    with open(os.path.join(launch, "agent_tabs.py"), "w", encoding="utf-8", newline="\n") as f:
        f.write(STUB)
    return root.replace("\\", "/")


def power_shells() -> list[tuple[str, str]]:
    found = [(name, shutil.which(name)) for name in ("powershell", "pwsh")] if WINDOWS else []
    return [(name, path) for name, path in found if path is not None]


def bare_path(test: unittest.TestCase, store_stub: bool) -> str:
    """A PATH with no Python on it, or with only a Microsoft Store style stub under WindowsApps."""
    folder = temp_home(test, "iat-cli-path-")
    if not store_stub:
        return folder
    apps = os.path.join(folder, "WindowsApps")
    os.makedirs(apps)
    whoami = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "whoami.exe")
    for name in ("python3.exe", "python.exe"):
        shutil.copyfile(whoami, os.path.join(apps, name))
    return apps


class SyncHookShellTest(unittest.TestCase):
    def test_the_sync_hook_sources_one_polyglot_script(self) -> None:
        self.assertEqual(hook_commands()["startup"], '. "${CLAUDE_PLUGIN_ROOT}/mcp/launch/sync-hook.ps1"')
        self.assertTrue(os.path.exists(os.path.join(LAUNCH, "sync-hook.ps1")))

    def run_hook(self, shell: list[str], root: str, home: str, **extra: str) -> subprocess.CompletedProcess[bytes]:
        command = hook_commands()["startup"].replace("${CLAUDE_PLUGIN_ROOT}", root)
        env = clean_env(home, CLAUDE_PLUGIN_ROOT=root, **extra)
        return subprocess.run([*shell, command], input=b"{}", env=env, capture_output=True, timeout=60, check=False)

    def test_the_hook_runs_sync_ides_hook_and_keeps_its_exit_code_in_every_shell(self) -> None:
        found = shells()
        if not found:
            self.skipTest("no shell")
        for name, shell in found:
            for code in (0, 3):
                with self.subTest(shell=name, code=code):
                    root = stub_plugin(self)
                    done = self.run_hook(shell, root, temp_home(self), STUB_EXIT=str(code))
                    # `powershell -Command` reports a failed script as 1, whatever code the script exited with.
                    expected = 1 if code and name != "sh" else code
                    self.assertEqual(done.returncode, expected, done.stderr)
                    self.assertEqual(json.loads(done.stdout), {"argv": ["sync-ides", "--hook"], "stdin": ""})

    def test_a_missing_python_is_not_a_hook_error_in_any_shell(self) -> None:
        found = shells()
        if not found:
            self.skipTest("no shell")
        for name, shell in found:
            for store_stub in (False, True) if WINDOWS else (False,):
                with self.subTest(shell=name, store_stub=store_stub):
                    root = stub_plugin(self)
                    path = bare_path(self, store_stub)
                    done = self.run_hook(shell, root, temp_home(self), PATH=path)
                    self.assertEqual((done.returncode, done.stdout, done.stderr), (0, b"", b""))


@unittest.skipUnless(power_shells(), "no PowerShell")
class PowerShellLauncherTest(unittest.TestCase):
    def run_launcher(
        self, exe: str, root: str, args: list[str], home: str, command: bool = False, **extra: str
    ) -> subprocess.CompletedProcess[bytes]:
        script = os.path.join(root, "mcp", "launch", "agent-tabs.ps1")
        base = [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass"]
        if command:
            # -EncodedCommand, because Windows PowerShell strips the double quotes of a -Command argument.
            quoted = " ".join("'" + a + "'" for a in args)
            encoded = base64.b64encode(f"& '{script}' {quoted}; exit $LASTEXITCODE".encode("utf-16-le")).decode("ascii")
            line = [*base, "-EncodedCommand", encoded]
        else:
            line = [*base, "-File", script, *args]
        return subprocess.run(line, env=clean_env(home, **extra), capture_output=True, timeout=60, check=False)

    def test_arguments_with_spaces_and_quotes_arrive_intact(self) -> None:
        for name, exe in power_shells():
            with self.subTest(shell=name, form="file"):
                done = self.run_launcher(exe, stub_plugin(self), TRICKY_ARGS, temp_home(self))
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertEqual(json.loads(done.stdout)["argv"], TRICKY_ARGS)
            with self.subTest(shell=name, form="command"):
                args = ["list-ides", "two words", 'say "hi"', "trailing\\", "caf\u00e9"]
                done = self.run_launcher(exe, stub_plugin(self), args, temp_home(self), command=True)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertEqual(json.loads(done.stdout)["argv"], args)

    def test_a_piped_request_arrives_as_utf8(self) -> None:
        text = '{"question":"caf\u00e9 \u2014 \u4e2d\u6587","options":["a","b"]}\n'
        for name, exe in power_shells():
            with self.subTest(shell=name):
                root = stub_plugin(self)
                request = os.path.join(temp_home(self), "request.json")
                with open(request, "w", encoding="utf-8", newline="") as f:
                    f.write(text)
                script = os.path.join(root, "mcp", "launch", "agent-tabs.ps1")
                command = f"Get-Content -Raw -Encoding UTF8 -LiteralPath '{request}' | & '{script}' jev choose; exit $LASTEXITCODE"
                done = subprocess.run(
                    [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", command],
                    env=clean_env(temp_home(self), STUB_STDIN="1"),
                    capture_output=True,
                    timeout=60,
                    check=False,
                )
                self.assertEqual(done.returncode, 0, done.stderr)
                reply = json.loads(done.stdout)
                self.assertEqual(reply["argv"], ["jev", "choose"])
                self.assertEqual(reply["stdin"], text)

    def test_the_exit_code_of_agent_tabs_is_kept(self) -> None:
        for name, exe in power_shells():
            for code in (1, 2, 42):
                with self.subTest(shell=name, code=code):
                    done = self.run_launcher(exe, stub_plugin(self), ["list-ides"], temp_home(self), STUB_EXIT=str(code))
                    self.assertEqual(done.returncode, code, done.stderr)

    def test_without_python_it_prints_the_json_error_and_exits_127(self) -> None:
        with open(os.path.join(LAUNCH, "agent-tabs"), encoding="utf-8") as f:
            match = re.search(r"echo '(\{\"error\".*?\})' >&2", f.read())
        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(json.loads(match.group(1)), {"error": NO_PYTHON})
        for name, exe in power_shells():
            for store_stub in (False, True):
                with self.subTest(shell=name, store_stub=store_stub):
                    path = bare_path(self, store_stub)
                    done = self.run_launcher(exe, stub_plugin(self), ["list-ides"], temp_home(self), PATH=path)
                    self.assertEqual(done.returncode, 127, done.stderr)
                    self.assertEqual(done.stdout, b"")
                    self.assertEqual(done.stderr.decode("utf-8").strip(), match.group(1))


if __name__ == "__main__":
    unittest.main()

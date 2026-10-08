from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import unittest
from typing import Any

from ide_agent_tabs.profiles import AgentProfile, launch_of
from ide_agent_tabs.spec import launch_spec, posix_spec, power_shell_spec
from ide_agent_tabs.terminals.shell import LoginShell, argv_mode_command
from support import ROOT, temp_home

LAUNCH_DIR = os.path.join(ROOT, "claude-plugin", "dist", "launch")
WINDOWS = sys.platform == "win32"

PROBE = """import json, os, sys
with open(os.environ["PROBE_OUT"], "w", encoding="utf-8") as f:
    json.dump({
        "args": sys.argv[1:],
        "cwd": os.getcwd(),
        "id": os.environ.get("IDE_AGENT_TABS_ID"),
        "agent": os.environ.get("IDE_AGENT_TABS_AGENT"),
        "spec": os.environ.get("IDE_AGENT_TABS_SPEC"),
        "extra": os.environ.get("IAT_EXTRA"),
    }, f)
"""

PROMPT = "Say \"hi\" & run $(whoami); `tick` 'quote' --flag é ✓ 🙂 2024-01-01T00:00:00\nsecond line"
CALLER_ARGS = [
    "--plugin-dir",
    "C:\\Program Files\\a b",
    "say \"hi\" $(whoami) `t` 'q'",
    "é ✓",
    "2026-09-26",
    "",
    "C:\\dir with space\\",
    'x\\"y z',
    '{"a": 1}',
]
EXTRA = "value with spaces & $(x)"


def on_path(name: str) -> str | None:
    for folder in os.environ.get("PATH", "").split(os.pathsep):
        candidate = os.path.join(folder.strip('"'), name)
        if folder.strip() and os.path.isfile(candidate):
            return candidate
    return None


def find_bash() -> str | None:
    if not WINDOWS:
        return "/bin/bash" if os.path.exists("/bin/bash") else None
    for folder in os.environ.get("PATH", "").split(os.pathsep):
        candidate = os.path.join(folder.strip('"'), "bash.exe")
        # System32 and WindowsApps hold the WSL bash, which can't run a Windows Python.
        if folder.strip() and os.path.isfile(candidate) and not re.search(r"System32|WindowsApps", candidate, re.IGNORECASE):
            return candidate
    return None


BASH_ENV = {**os.environ, "MSYS_NO_PATHCONV": "1", "MSYS2_ARG_CONV_EXCL": "*"}


class LauncherTest(unittest.TestCase):
    def setUp(self) -> None:
        self.work = temp_home(self, "iat launch ")
        self.probe = os.path.join(self.work, "probe.py")
        with open(self.probe, "w", encoding="utf-8") as f:
            f.write(PROBE)
        self.profile = AgentProfile("probe", "Probe", sys.executable, ("-I", "-S", self.probe), "--prompt")

    def launch(self, out: str, with_prompt: bool) -> Any:
        return launch_of(self.profile, PROMPT if with_prompt else None, CALLER_ARGS, {"PROBE_OUT": out, "IAT_EXTRA": EXTRA})

    def expected(self, with_prompt: bool) -> dict[str, Any]:
        return {
            "args": [*CALLER_ARGS, *(["--prompt", PROMPT] if with_prompt else [])],
            "id": "tab-1",
            "agent": "probe",
            "spec": None,
            "extra": EXTRA,
        }

    def read_probe(self, out: str) -> dict[str, Any]:
        with open(out, encoding="utf-8") as f:
            got: dict[str, Any] = json.load(f)
        return got

    def run_power_shell(self, shell: str) -> None:
        exe = on_path(shell) if WINDOWS else None
        if exe is None:
            self.skipTest(f"{shell} not found")
        for with_prompt in (True, False):
            with self.subTest(with_prompt=with_prompt):
                out = os.path.join(self.work, f"ps-{shell}-{with_prompt}.json")
                spec_file = os.path.join(self.work, f"ps-{shell}-{with_prompt}.spec.json")
                pid_file = os.path.join(self.work, f"ps-{shell}-{with_prompt}.pid")
                with open(spec_file, "w", encoding="utf-8") as f:
                    f.write(power_shell_spec(launch_spec("tab-1", self.work, self.launch(out, with_prompt), pid_file)))
                launcher = os.path.join(LAUNCH_DIR, "agent-launch.ps1")
                result = subprocess.run(
                    [exe, "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", launcher, spec_file],
                    capture_output=True,
                    check=False,
                    encoding="utf-8",
                    errors="replace",
                    timeout=120,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                got = self.read_probe(out)
                self.assertEqual(os.path.normcase(os.path.realpath(got.pop("cwd"))), os.path.normcase(os.path.realpath(self.work)))
                self.assertEqual(got, self.expected(with_prompt))
                self.assertFalse(os.path.exists(spec_file), "the launcher deletes its spec")
                with open(pid_file, encoding="utf-8") as f:
                    self.assertRegex(f.read(), r"\A\d+\Z")

    def test_the_power_shell_launcher_passes_the_prompt_and_args_intact_in_pwsh(self) -> None:
        self.run_power_shell("pwsh.exe")

    def test_the_power_shell_launcher_passes_the_prompt_and_args_intact_in_windows_power_shell(self) -> None:
        self.run_power_shell("powershell.exe")

    def test_the_posix_launcher_passes_the_prompt_and_args_intact(self) -> None:
        bash = find_bash()
        if bash is None:
            self.skipTest("no bash found")
        for with_prompt in (True, False):
            with self.subTest(with_prompt=with_prompt):
                out = os.path.join(self.work, f"sh-{with_prompt}.json")
                spec_file = os.path.join(self.work, f"sh-{with_prompt}.spec")
                with open(spec_file, "wb") as f:
                    f.write(posix_spec(launch_spec("tab-1", self.work, self.launch(out, with_prompt))))
                env = {**BASH_ENV, "IDE_AGENT_TABS_LAUNCHER": os.path.join(LAUNCH_DIR, "agent-launch.sh"), "IDE_AGENT_TABS_SPEC": spec_file}
                result = subprocess.run(
                    [bash, "--noprofile", "--norc", "-c", '. "$IDE_AGENT_TABS_LAUNCHER"'],
                    env=env,
                    capture_output=True,
                    check=False,
                    encoding="utf-8",
                    errors="replace",
                    timeout=120,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                got = self.read_probe(out)
                got.pop("cwd")
                self.assertEqual(got, self.expected(with_prompt))
                self.assertFalse(os.path.exists(spec_file), "the launcher deletes its spec")

    def test_the_posix_launcher_runs_in_argv_mode_and_writes_its_shell_pid(self) -> None:
        bash = find_bash()
        if bash is None:
            self.skipTest("no bash found")
        out = os.path.join(self.work, "argv.json")
        spec_file = os.path.join(self.work, "argv.spec")
        pid_file = os.path.join(self.work, "argv.pid")
        with open(spec_file, "wb") as f:
            f.write(posix_spec(launch_spec("tab-1", self.work, self.launch(out, True), pid_file)))
        command = argv_mode_command(LoginShell("/bin/bash", "posix"), os.path.join(LAUNCH_DIR, "agent-launch.sh"), spec_file)
        script, positional = command[4], command[5:]
        once = re.sub(r"; exec .*\Z", lambda _: '; printf %s "$$"', script)
        result = subprocess.run(
            [bash, "--noprofile", "--norc", "-c", once, *positional],
            env=BASH_ENV,
            capture_output=True,
            check=False,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        got = self.read_probe(out)
        got.pop("cwd")
        self.assertEqual(got, self.expected(True))
        self.assertFalse(os.path.exists(spec_file), "the launcher deletes its spec")
        with open(pid_file, encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), result.stdout)


if __name__ == "__main__":
    unittest.main()

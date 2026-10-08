from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import unittest

from fake_ide import fake_ide
from ide_agent_tabs.clock import iso, now_ms
from support import MCP, SRC, require_node, temp_home

REAL_IDES_ENV = "IDE_AGENT_TABS_REAL_IDES"
PY_CLI = "import sys; sys.path.insert(0, sys.argv[1]); from ide_agent_tabs.list_ides_cli import cli_list_ides; sys.exit(cli_list_ides(sys.argv[2:]))"
KEEP = ("SYSTEMROOT", "SystemRoot", "WINDIR", "windir", "COMSPEC", "ComSpec", "TEMP", "TMP", "TMPDIR")


def sandbox_env(home: str, root: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k in KEEP}
    env.update(
        {
            "IDE_AGENT_TABS_HOME": home,
            "PATH": os.path.join(root, "bin"),
            "HOME": root,
            "USERPROFILE": root,
            "LOCALAPPDATA": os.path.join(root, "local"),
            "ProgramFiles": os.path.join(root, "programs"),
            "SHELL": "/bin/sh",
        }
    )
    return env


def run_both(env: dict[str, str], args: list[str]) -> tuple[subprocess.CompletedProcess[bytes], subprocess.CompletedProcess[bytes]]:
    node = shutil.which("node")
    assert node is not None
    node_run = subprocess.run(
        [node, "--import", "tsx", "src/main.ts", "list-ides", *args], cwd=MCP, env=env, capture_output=True, timeout=120, check=False
    )
    py_run = subprocess.run([sys.executable, "-I", "-S", "-c", PY_CLI, SRC, *args], env=env, capture_output=True, timeout=120, check=False)
    return node_run, py_run


def write_detection(home: str) -> None:
    detection = {
        "version": 1,
        "detectedAt": iso(now_ms()),
        "platform": sys.platform,
        "terminals": [],
        "shells": [
            {"path": "C:\\Program Files\\PowerShell\\7\\pwsh.exe", "label": "PowerShell 7.5.0 (MSI)", "version": "7.5.0", "source": "msi"}
        ],
        "ori": None,
    }
    with open(os.path.join(home, "detected.json"), "w", encoding="utf-8") as f:
        json.dump(detection, f)


class ListIdesInteropTest(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        require_node(self)

    def test_python_prints_what_node_prints(self) -> None:
        home = temp_home(self)
        root = temp_home(self, "iat-sandbox-")
        write_detection(home)
        work = temp_home(self, "iat-work-")
        ide = fake_ide(self, projects=[{"name": "app", "path": work, "focused": True}, {"name": 7, "path": None}])
        ide.register(home, "jetbrains-1", started_at=5)
        refused = fake_ide(self, product="Visual Studio Code", token="right")
        refused.register(home, "vscode-2", token="wrong")
        with open(os.path.join(home, "endpoints", "broken.json"), "w", encoding="utf-8") as f:
            f.write("{")
        env = sandbox_env(home, root)
        node_run, py_run = run_both(env, [])
        self.assertEqual(node_run.returncode, 0, node_run.stderr.decode("utf-8", "replace"))
        self.assertEqual((py_run.returncode, py_run.stdout.decode("utf-8")), (0, node_run.stdout.decode("utf-8")))

    def test_arguments_fail_alike(self) -> None:
        home = temp_home(self)
        node_run, py_run = run_both(sandbox_env(home, temp_home(self, "iat-sandbox-")), ["extra"])
        self.assertEqual((node_run.returncode, py_run.returncode), (1, 1))
        self.assertIn("list-ides takes no arguments.", json.loads(node_run.stderr)["error"])
        self.assertIn("list-ides takes no arguments.", json.loads(py_run.stderr)["error"])

    @unittest.skipUnless(os.environ.get(REAL_IDES_ENV) == "1", f"reads this machine's IDE installs; set {REAL_IDES_ENV}=1")
    def test_real_machine_matches_node(self) -> None:
        home = temp_home(self)
        write_detection(home)
        env = dict(os.environ, IDE_AGENT_TABS_HOME=home)
        node_run, py_run = run_both(env, [])
        self.assertEqual(node_run.returncode, 0, node_run.stderr.decode("utf-8", "replace"))
        self.assertEqual(py_run.stdout.decode("utf-8"), node_run.stdout.decode("utf-8"))


if __name__ == "__main__":
    unittest.main()

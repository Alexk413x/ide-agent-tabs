from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import unittest

from ide_agent_tabs.jev.cli import CliIo, run_jev_cli
from jev_support import StubTypeSafe
from support import ROOT, require_node, temp_home

LAUNCH = os.path.join(ROOT, "claude-plugin", "mcp", "launch")
ENTRY = os.path.join(LAUNCH, "agent_tabs.py")
POSIX_LAUNCHER = os.path.join(LAUNCH, "agent-tabs")
NODE_CLI = os.path.join(ROOT, "claude-plugin", "dist", "mcp-server.mjs")
PASSED_ENV = ("SYSTEMROOT", "SystemRoot", "WINDIR", "TEMP", "TMP", "PATH", "Path", "HOME", "USERPROFILE", "LOCALAPPDATA", "APPDATA")


def find_sh() -> str | None:
    found = shutil.which("sh")
    if found is None and sys.platform == "win32":
        git_sh = os.path.join(os.environ.get("ProgramFiles", ""), "Git", "bin", "sh.exe")
        found = git_sh if os.path.isfile(git_sh) else None
    return found


def base_env(home: str, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k in PASSED_ENV}
    env["IDE_AGENT_TABS_HOME"] = home
    env.update(extra)
    return env


def enable_jev(home: str, jev: object) -> None:
    with open(os.path.join(home, "config.json"), "w", encoding="utf-8") as f:
        json.dump({"jev": jev}, f)


def run(command: list[str], env: dict[str, str], stdin: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, input=stdin, env=env, capture_output=True, encoding="utf-8", timeout=60, check=False)


def py_cli(*args: str) -> list[str]:
    return [sys.executable, "-I", "-S", ENTRY, *args]


class Dispatch(unittest.TestCase):
    def test_unknown_command_prints_usage(self) -> None:
        home = temp_home(self)
        for args in ([], ["bogus"]):
            done = run(py_cli(*args), base_env(home))
            self.assertEqual(done.returncode, 2)
            self.assertEqual(
                json.loads(done.stderr),
                {
                    "error": "Usage: agent-tabs list-ides | jev <status|ask|choose|check|rank|route> | server status|stop [--port <port>] | sync-ides <--hook|--status|--install|--agents|--register|--unregister> [<editor cli or agent>...]"
                },
            )

    def test_jev_usage_and_off(self) -> None:
        home = temp_home(self)
        done = run(py_cli("jev", "status"), base_env(home))
        self.assertEqual(done.returncode, 1)
        path = os.path.join(home, "config.json")
        self.assertEqual(json.loads(done.stderr), {"error": f'Jev is off. Set "jev": {{"enabled": true}} in {path}.'})
        for args in (["jev"], ["jev", "nope"], ["jev", "status", "x"]):
            done = run(py_cli(*args), base_env(home))
            self.assertEqual(done.returncode, 1)
            self.assertTrue(json.loads(done.stderr)["error"].startswith("Usage: agent-tabs jev <status|"), done.stderr)

    def test_stdin_errors(self) -> None:
        out: list[str] = []
        err: list[str] = []
        io = CliIo(lambda: "{not json", out.append, err.append)
        self.assertEqual(run_jev_cli(["choose"], object(), "off", io), 1)  # type: ignore[arg-type]
        self.assertTrue(json.loads(err[0])["error"].startswith("stdin is not JSON: "))
        err.clear()
        io = CliIo(lambda: "  ", out.append, err.append)
        self.assertEqual(run_jev_cli(["route"], object(), "off", io), 1)  # type: ignore[arg-type]
        self.assertEqual(json.loads(err[0]), {"error": "✖ Invalid input: expected string, received undefined\n  → at task"})
        self.assertEqual(out, [])


class NodeParity(unittest.TestCase):
    stub: StubTypeSafe

    @classmethod
    def setUpClass(cls) -> None:
        cls.stub = StubTypeSafe()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.stub.close()

    def both(
        self, args: list[str], env: dict[str, str], stdin: str = ""
    ) -> tuple[subprocess.CompletedProcess[str], subprocess.CompletedProcess[str]]:
        require_node(self)
        node = shutil.which("node")
        assert node is not None
        return run(py_cli(*args), env, stdin), run([node, NODE_CLI, *args], env, stdin)

    def test_jev_status_matches_node(self) -> None:
        home = temp_home(self)
        enable_jev(home, {"enabled": True, "sure": 0.9, "tiers": {"codex": "Quick", "claude:opus": "Hard"}})
        ledger = os.path.join(home, "jev")
        os.makedirs(ledger)
        with open(os.path.join(ledger, "ledger.jsonl"), "w", encoding="utf-8") as f:
            f.write('{"at":"2020-01-01T00:00:00.000Z","tool":"jev_check","model":"jev-old","input_tokens":5,"ok":true}\n')
        py, node = self.both(["jev", "status"], base_env(home, TYPESAFE_API_KEY="tsk-x"))
        self.assertEqual((py.returncode, py.stdout, py.stderr), (node.returncode, node.stdout, node.stderr))
        self.assertEqual(json.loads(py.stdout)["model"], "jev-old")

    def test_jev_off_matches_node(self) -> None:
        home = temp_home(self)
        enable_jev(home, {"enabled": True, "sure": 2})
        py, node = self.both(["jev", "status"], base_env(home))
        self.assertEqual((py.returncode, py.stdout, py.stderr), (node.returncode, node.stdout, node.stderr))

    def test_jev_choose_matches_node(self) -> None:
        home = temp_home(self)
        enable_jev(home, {"enabled": True})
        answer = {"pick": {"type": "choice", "choice": "b", "confidence": 0.8, "probabilities": {"a": 0.1, "b": 0.8, "none": 0.1}}}
        self.stub.set({"status": 200, "body": json.dumps({"model": "jev-9", "answers": answer, "usage": {"input_tokens": 77}})})
        request = json.dumps(
            {"instruction": "Which?", "options": [{"id": "a", "description": "A"}, {"id": "b", "description": "B"}], "state": "café 😀"}
        )
        env = base_env(home, TYPESAFE_API_KEY="tsk-x", TYPESAFE_BASE_URL=self.stub.url)
        py, node = self.both(["jev", "choose"], env, request)
        self.assertEqual((py.returncode, py.stdout, py.stderr), (node.returncode, node.stdout, node.stderr))
        self.assertEqual(self.stub.seen[0]["body"], self.stub.seen[1]["body"])
        self.assertEqual(json.loads(py.stdout)["band"], "unsure")

    def test_validation_errors_match_node(self) -> None:
        home = temp_home(self)
        enable_jev(home, {"enabled": True})
        request = json.dumps({"query": "", "items": [{"id": "", "text": 5}], "top": 0.5})
        py, node = self.both(["jev", "rank"], base_env(home, TYPESAFE_API_KEY="tsk-x"), request)
        self.assertEqual((py.returncode, py.stdout, py.stderr), (node.returncode, node.stdout, node.stderr))


@unittest.skipIf(find_sh() is None, "no POSIX sh")
class PosixLauncher(unittest.TestCase):
    def sh(self) -> str:
        found = find_sh()
        assert found is not None
        return found

    def test_launcher_runs_the_cli(self) -> None:
        home = temp_home(self)
        enable_jev(home, {"enabled": True})
        done = run([self.sh(), POSIX_LAUNCHER, "jev", "status"], base_env(home, TYPESAFE_API_KEY="tsk-x"))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout)["key"], "env")

    def test_launcher_skips_store_stubs(self) -> None:
        home = temp_home(self)
        stubs = os.path.join(temp_home(self), "WindowsApps")
        os.makedirs(stubs)
        for name in ("python3", "python"):
            path = os.path.join(stubs, name)
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write("#!/bin/sh\necho stub >&2\nexit 99\n")
            os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
        real = os.path.join(temp_home(self), "real")
        os.makedirs(real)
        link = os.path.join(real, "python3")
        with open(link, "w", encoding="utf-8", newline="\n") as f:
            f.write(f'#!/bin/sh\nexec "{sys.executable.replace(os.sep, "/")}" "$@"\n')
        os.chmod(link, os.stat(link).st_mode | stat.S_IEXEC)
        env = base_env(home)
        env["PATH"] = os.pathsep.join([stubs, real, os.path.dirname(self.sh())])
        env.pop("Path", None)
        env["OS"] = ""
        done = run([self.sh(), POSIX_LAUNCHER, "server", "nope"], env)
        self.assertEqual(done.returncode, 2, done.stderr)
        self.assertNotIn("stub", done.stderr)


if __name__ == "__main__":
    unittest.main()

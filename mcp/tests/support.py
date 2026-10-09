from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections.abc import Sequence
from typing import Any

TESTS = os.path.dirname(os.path.abspath(__file__))
MCP = os.path.dirname(TESTS)
ROOT = os.path.dirname(MCP)
SRC = os.path.join(ROOT, "claude-plugin", "mcp", "src")
PACKAGE = os.path.join(SRC, "ide_agent_tabs")
FIXTURES = os.path.join(TESTS, "fixtures")
PY_WORKER = os.path.join(TESTS, "py_worker.py")
# The released 0.8.0 Node build, kept to test against servers and hooks of 0.8.0 that still run during an update.
NODE_080 = os.path.join(TESTS, "node080")
NODE_WORKERS = {"interop": os.path.join(NODE_080, "interop-worker.mjs"), "stress": os.path.join(NODE_080, "stress-worker.mjs")}
INTEROP_ENV = "IDE_AGENT_TABS_INTEROP"
WORKER_TIMEOUT_S = 180.0


def temp_home(test: unittest.TestCase, prefix: str = "iat-py-") -> str:
    folder = os.path.realpath(tempfile.mkdtemp(prefix=prefix))
    # Windows can refuse the delete while a closing handle lingers; a leftover temp folder must not fail the run.
    test.addCleanup(shutil.rmtree, folder, True)
    test.addCleanup(close_store, folder)
    return folder


def close_store(home: str) -> None:
    from ide_agent_tabs.messaging import db

    db.close_db(home)


def js_fixtures() -> dict[str, Any]:
    with open(os.path.join(FIXTURES, "js.json"), encoding="utf-8") as f:
        return json.load(f)


NODE_080_MINIMUM = (22, 13)
_node_version: list[tuple[int, ...]] = []


def node_missing() -> str | None:
    node = shutil.which("node")
    if node is None:
        return "node is not on PATH"
    if not _node_version:
        done = subprocess.run([node, "--version"], capture_output=True, encoding="utf-8", timeout=30, check=False)
        _node_version.append(tuple(int(p) for p in done.stdout.strip().lstrip("v").split(".")[:2] if p.isdigit()))
    if _node_version[0] < NODE_080_MINIMUM:
        return f"the 0.8.0 build needs Node {'.'.join(map(str, NODE_080_MINIMUM))} or later"
    return None


def require_node(test: unittest.TestCase) -> None:
    reason = node_missing()
    if reason is None:
        return
    if os.environ.get(INTEROP_ENV) == "1":
        test.fail(f"{INTEROP_ENV}=1 but {reason}")
    test.skipTest(reason)


class Worker:
    def __init__(self, command: Sequence[str], label: str) -> None:
        self.label = label
        self.child = subprocess.Popen(
            list(command),
            cwd=MCP,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            encoding="utf-8",
            errors="replace",
        )
        self._lines: list[str] = []
        self._closed = False
        self._cond = threading.Condition()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        assert self.child.stdout is not None
        for raw in self.child.stdout:
            line = raw.rstrip("\r\n")
            if line:
                with self._cond:
                    self._lines.append(line)
                    self._cond.notify_all()
        with self._cond:
            self._closed = True
            self._cond.notify_all()

    def line(self, prefix: str, timeout: float = WORKER_TIMEOUT_S) -> str:
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                for i, line in enumerate(self._lines):
                    if line.startswith(prefix):
                        return self._lines.pop(i)
                if self._closed:
                    raise RuntimeError(f"{self.label} exited before printing {prefix!r}")
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError(f"{self.label} printed no {prefix!r} within {timeout:g} s")
                self._cond.wait(left)

    def send(self, text: str = "go") -> None:
        assert self.child.stdin is not None
        self.child.stdin.write(text + "\n")
        self.child.stdin.flush()

    def result(self, timeout: float = WORKER_TIMEOUT_S) -> Any:
        return json.loads(self.line("{", timeout))

    def close(self) -> None:
        if self.child.poll() is None:
            self.child.kill()
        self.child.wait()
        for stream in (self.child.stdin, self.child.stdout):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass


def node_worker(test: unittest.TestCase, mode: str, args: dict[str, Any], script: str = "interop") -> Worker:
    node = shutil.which("node")
    assert node is not None
    w = Worker([node, NODE_WORKERS[script], mode, json.dumps(args)], f"node {mode}")
    test.addCleanup(w.close)
    return w


def py_worker(test: unittest.TestCase, mode: str, args: dict[str, Any]) -> Worker:
    w = Worker([sys.executable, "-I", "-S", PY_WORKER, mode, json.dumps(args)], f"python {mode}")
    test.addCleanup(w.close)
    return w


def start_together(workers: Sequence[Worker]) -> None:
    for w in workers:
        w.line("ready")
    for w in workers:
        w.send()


def percentile(values: Sequence[float], p: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return float("nan")
    return ordered[min(len(ordered) - 1, int(p / 100 * len(ordered)))]

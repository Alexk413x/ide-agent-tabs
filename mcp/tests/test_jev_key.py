from __future__ import annotations

import os
import sys
import unittest
import uuid
from typing import Any

from ide_agent_tabs.jev.key import KeyDeps, KeyStore, StoredCredential, look_up_key, read_windows_credential
from ide_agent_tabs.processes import RunResult

REAL_STORE_ENV = "IDE_AGENT_TABS_REAL_CREDENTIALS"


class FakeBackends(unittest.TestCase):
    def test_env_key_wins_and_is_trimmed(self) -> None:
        result = look_up_key(
            KeyDeps(env={"TYPESAFE_API_KEY": " k \n"}, platform="win32", read_credential=lambda _: self.fail("store read"))
        )
        self.assertEqual((result.found.key, result.found.source) if result.found else None, ("k", "env"))

    def test_windows_reads_both_targets_in_order(self) -> None:
        asked: list[str] = []

        def read(target: str) -> StoredCredential | None:
            asked.append(target)
            return StoredCredential(target, None, "k2".encode("utf-16-le")) if target == "api_key@typesafe" else None

        result = look_up_key(KeyDeps(env={}, platform="win32", read_credential=read))
        self.assertEqual(asked, ["typesafe", "api_key@typesafe"])
        self.assertEqual(result.found.key if result.found else None, "k2")

    def test_windows_read_failure_is_reported(self) -> None:
        def read(target: str) -> StoredCredential | None:
            raise OSError(5, "Access is denied")

        result = look_up_key(KeyDeps(env={}, platform="win32", read_credential=read))
        self.assertIsNone(result.found)
        self.assertIn(
            "(CredReadW failed for typesafe: [Errno 5] Access is denied; CredReadW failed for api_key@typesafe:", result.missing or ""
        )

    def test_mac_and_linux_commands(self) -> None:
        calls: list[tuple[str, list[str], float]] = []

        def runner(command: str, args: Any, env: Any, timeout: float) -> RunResult:
            calls.append((command, list(args), timeout))
            return RunResult(0, "key\n", "")

        look_up_key(KeyDeps(env={}, platform="darwin", run_command=runner))
        look_up_key(KeyDeps(env={}, platform="linux", run_command=runner))
        self.assertEqual(
            calls,
            [
                ("security", ["find-generic-password", "-s", "typesafe", "-a", "api_key", "-w"], 15.0),
                ("secret-tool", ["lookup", "service", "typesafe", "username", "api_key"], 15.0),
            ],
        )

    def test_command_timeout_is_reported(self) -> None:
        def runner(command: str, args: Any, env: Any, timeout: float) -> RunResult:
            raise TimeoutError(f"{command} did not finish within 15 s")

        result = look_up_key(KeyDeps(env={}, platform="linux", run_command=runner))
        self.assertIn("(secret-tool did not finish within 15 s)", result.missing or "")

    def test_store_caches_a_found_key_only(self) -> None:
        answers = [RunResult(1, "", ""), RunResult(0, "k", ""), RunResult(0, "other", "")]
        store = KeyStore(KeyDeps(env={}, platform="linux", run_command=lambda *_: answers.pop(0)))
        self.assertIsNone(store.look_up().found)
        self.assertEqual([store.look_up().found.key for _ in range(2)], ["k", "k"])  # type: ignore[union-attr]
        self.assertEqual(len(answers), 1)


@unittest.skipUnless(sys.platform == "win32", "Windows Credential Manager")
class WindowsCredentialManager(unittest.TestCase):
    def test_a_missing_target_reads_as_none(self) -> None:
        self.assertIsNone(read_windows_credential(f"ide-agent-tabs-test-{uuid.uuid4().hex}"))

    @unittest.skipUnless(os.environ.get(REAL_STORE_ENV) == "1", f"set {REAL_STORE_ENV}=1 to read the real TypeSafe credential")
    def test_reads_the_real_store(self) -> None:
        result = look_up_key(KeyDeps(env={}, platform="win32"))
        self.assertEqual(result.found.source if result.found else None, "credential-store", result.missing)


if __name__ == "__main__":
    unittest.main()

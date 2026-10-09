from __future__ import annotations

import base64
import os
import sys
import time
import unittest
from typing import Any

from ide_agent_tabs.jev import ledger as ledger_module
from ide_agent_tabs.jev.client import NO_BODY, JevError, http_message
from ide_agent_tabs.jev.inputs import InputError, parse_input
from ide_agent_tabs.jev.key import KeyDeps, StoredCredential, decode_blob, look_up_key, pick_windows_credential
from ide_agent_tabs.jev.ledger import cost_usd, ledger_path, summarize_ledger
from ide_agent_tabs.jev.service import Jev, JevDeps, start_jev
from ide_agent_tabs.jev.settings import parse_jev_settings, read_jev_config
from ide_agent_tabs.jev.tools import run_tool
from ide_agent_tabs.jsjson import parse, stringify
from ide_agent_tabs.processes import RunResult
from jev_support import StubTypeSafe, expand, jev_fixtures
from support import temp_home

FIXTURES = jev_fixtures()


def settings_json(value: Any) -> dict[str, Any]:
    s = parse_jev_settings(value)
    return {"enabled": s.enabled, "sure": s.sure, "tiers": s.tiers, "pricePerMillionInput": s.price_per_million_input}


def mask(text: str, home: str) -> str:
    escaped = stringify(home + os.sep)[1:-1]
    for prefix in (home + os.sep, escaped):
        text = text.replace(prefix, "<home>/")
    return text.replace(stringify(home)[1:-1], "<home>").replace(home, "<home>")


def portable(text: str) -> str:
    return text.replace("\\\\", "/")


class SettingsParity(unittest.TestCase):
    def test_settings_match_node(self) -> None:
        for case in FIXTURES["settings"]:
            with self.subTest(input=case["input"]):
                if "error" in case:
                    with self.assertRaises(ValueError) as caught:
                        parse_jev_settings(case["input"])
                    self.assertEqual(str(caught.exception), case["error"])
                else:
                    self.assertEqual(stringify(settings_json(case["input"])), stringify(case["settings"]))

    def test_config_file_warnings(self) -> None:
        home = temp_home(self)
        self.assertEqual(read_jev_config(home)[1], [])
        path = os.path.join(home, "config.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"jev": {"enabled": "yes"}}')
        settings, warnings = read_jev_config(home)
        self.assertFalse(settings.enabled)
        self.assertEqual(warnings, [f"Ignoring jev in {path}, so Jev is off: jev.enabled must be true or false"])
        with open(path, "w", encoding="utf-8") as f:
            f.write("[]")
        self.assertEqual(read_jev_config(home)[1], [f"Ignoring {path}: config.json must hold a JSON object"])
        started = start_jev(home, {}, "linux")
        self.assertIsNone(started.jev)
        self.assertEqual(
            started.off, f'Jev is off. Set "jev": {{"enabled": true}} in {path}. Ignoring {path}: config.json must hold a JSON object'
        )


class LedgerParity(unittest.TestCase):
    def test_costs_round_like_to_fixed(self) -> None:
        for case in FIXTURES["costs"]:
            with self.subTest(case=case):
                self.assertEqual(stringify(cost_usd(case["tokens"], case["price"])), stringify(case["cost"]))

    def test_summary_matches_node(self) -> None:
        case = FIXTURES["ledger"]
        home = temp_home(self)
        os.makedirs(os.path.dirname(ledger_path(home)))
        with open(ledger_path(home), "w", encoding="utf-8", newline="") as f:
            f.write(case["text"])
        self.assertEqual(stringify(summarize_ledger(home, case["price"], case["now"])), stringify(case["summary"]))

    def test_append_writes_one_line_per_entry(self) -> None:
        home = temp_home(self)
        ledger_module.append_ledger(home, {"at": "x", "ok": True})
        ledger_module.append_ledger(home, {"at": "y", "ok": False})
        with open(ledger_path(home), "rb") as f:
            self.assertEqual(f.read(), b'{"at":"x","ok":true}\n{"at":"y","ok":false}\n')


class KeyParity(unittest.TestCase):
    def test_blobs_decode_like_node(self) -> None:
        for case in FIXTURES["blobs"]:
            with self.subTest(blob=case["blob"]):
                self.assertEqual(decode_blob(base64.b64decode(case["blob"])), case["key"])

    def test_windows_pick_matches_node(self) -> None:
        for case in FIXTURES["credentials"]:
            entries = [StoredCredential(e["target"], e["user"], base64.b64decode(e["blob"])) for e in case["entries"]]
            with self.subTest(entries=case["entries"]):
                self.assertEqual(pick_windows_credential(entries), case["key"])

    def test_lookups_match_node(self) -> None:
        for case in FIXTURES["keys"]:
            with self.subTest(case=case):
                calls: list[tuple[str, list[str]]] = []

                def runner(
                    command: str, args: Any, env: Any, timeout: float, case: dict[str, Any] = case, calls: list[Any] = calls
                ) -> RunResult:
                    calls.append((command, list(args)))
                    if case.get("enoent"):
                        raise FileNotFoundError(command)
                    return RunResult(case.get("code", 0), case.get("stdout", ""), "")

                stored = {
                    e["target"]: StoredCredential(e["target"], e["user"], base64.b64decode(e["blob"])) for e in case.get("entries", [])
                }
                env = {"TYPESAFE_API_KEY": case["env"]} if "env" in case else {}
                result = look_up_key(KeyDeps(env=env, platform=case["platform"], run_command=runner, read_credential=stored.get))
                found = None if result.found is None else {"key": result.found.key, "source": result.found.source}
                self.assertEqual(found, case["found"])
                self.assertEqual(result.missing, case["missing"])
                if "env" in case:
                    self.assertEqual(calls, [])


class InputParity(unittest.TestCase):
    def test_validation_matches_zod(self) -> None:
        for case in FIXTURES["validation"]:
            with self.subTest(tool=case["tool"], input=stringify(case["input"])[:200]):
                if "error" in case:
                    with self.assertRaises(InputError) as caught:
                        parse_input(case["tool"], case["input"])
                    self.assertEqual(str(caught.exception), case["error"])
                else:
                    self.assertEqual(stringify(parse_input(case["tool"], case["input"])), stringify(case["parsed"]))


class HttpMessageParity(unittest.TestCase):
    def test_messages_match_node(self) -> None:
        key = FIXTURES["key"]
        for case in FIXTURES["describe"]:
            body = NO_BODY if case["body"].get("undefined") else case["body"]["value"]
            with self.subTest(status=case["status"], body=body):
                self.assertEqual(http_message(case["status"], body).replace(key, "***"), case["message"])


class ToolParity(unittest.TestCase):
    stub: StubTypeSafe

    @classmethod
    def setUpClass(cls) -> None:
        cls.stub = StubTypeSafe()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.stub.close()

    def run_case(self, case: dict[str, Any]) -> None:
        home = temp_home(self)
        self.stub.set(case["response"])
        env = {"TYPESAFE_BASE_URL": self.stub.url, "IDE_AGENT_TABS_AGENT": "codex", "IDE_AGENT_TABS_ID": "tab-1"}
        if not case["noKey"]:
            env["TYPESAFE_API_KEY"] = FIXTURES["key"]
        jev = Jev(
            JevDeps(
                settings=parse_jev_settings({"enabled": True, **case["settings"]}),
                home=home,
                env=env,
                platform="linux",
                profiles=lambda: case["profiles"],
                run_command=lambda *_: RunResult(1, "", ""),
            )
        )
        try:
            output: dict[str, Any] = {"output": portable(mask(stringify(run_tool(jev, case["tool"], expand(case["input"])), 2), home))}
        except JevError as e:
            output = {"error": mask(e.message, home)}
            if e.status is not None:
                output["status"] = e.status
        expected = {k: case[k] for k in ("output", "error", "status") if k in case}
        if "output" in expected:
            expected["output"] = portable(expected["output"])
        self.assertEqual(output, expected)
        seen = [{**s, "headers": {k: v for k, v in s["headers"].items() if v is not None}} for s in self.stub.seen]
        want = [{**s, "headers": {k: v for k, v in s["headers"].items() if v is not None}} for s in case["requests"]]
        self.assertEqual(seen, want)
        entries: list[str] = []
        if os.path.exists(ledger_path(home)):
            with open(ledger_path(home), encoding="utf-8") as f:
                for line in f.read().splitlines():
                    entry = parse(line)
                    entry.pop("at")
                    entries.append(stringify(entry))
        self.assertEqual(entries, [stringify(e) for e in case["ledger"]])

    def test_tools_match_node(self) -> None:
        for case in FIXTURES["tools"]:
            with self.subTest(case=case["name"]):
                self.run_case(case)


class Timing(unittest.TestCase):
    def test_status_needs_no_network(self) -> None:
        home = temp_home(self)
        jev = Jev(JevDeps(settings=parse_jev_settings({"enabled": True}), home=home, env={"TYPESAFE_API_KEY": "k"}, platform=sys.platform))
        started = time.monotonic()
        self.assertEqual(jev.status()["key"], "env")
        self.assertLess(time.monotonic() - started, 1.0)


if __name__ == "__main__":
    unittest.main()

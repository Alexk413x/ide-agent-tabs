from __future__ import annotations

import json
import unittest
from typing import Any

from ide_agent_tabs.clock import parse_iso
from support import node_worker, py_worker, require_node, temp_home

T0 = parse_iso("2026-10-07T12:00:00Z") or 0
ID = "tab-parity-1"


def mail(at: int, n: int) -> dict[str, Any]:
    return {
        "op": "send",
        "out": {"from": {"id": "abcdef0123456", "agent": "codex", "path": "/w"}, "to": ID, "text": f"note {n}"},
        "now": at,
    }


def step(at: int, cli: str, event: str, data: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    return [{"op": "hook", "id": ID, "cli": cli, "event": event, "input": data or {}, "now": at}, {"op": "presenceText", "id": ID}]


def scenario() -> list[dict[str, Any]]:
    ops: list[dict[str, Any]] = []
    at = T0
    plan: list[tuple[str, str, dict[str, Any]] | int] = [
        ("claude", "SessionStart", {"source": "startup", "session_id": "a", "model": "claude-opus-5-5"}),
        ("claude", "UserPromptSubmit", {"session_id": "a", "effort": {"level": "high"}}),
        1,
        ("claude", "PostToolUse", {"session_id": "a"}),
        ("claude", "PostToolUse", {"session_id": "a"}),
        ("claude", "Notification", {"session_id": "a", "notification_type": "permission_prompt"}),
        ("claude", "Stop", {"session_id": "child"}),
        ("claude", "Stop", {"session_id": "a"}),
        ("claude", "Stop", {"session_id": "a", "stop_hook_active": True}),
        ("claude", "Stop", {"session_id": "a", "stop_hook_active": True}),
        ("claude", "Stop", {"session_id": "a", "stop_hook_active": True}),
        ("claude", "Notification", {"session_id": "a", "notification_type": "idle_prompt"}),
        2,
        ("claude", "SessionStart", {"source": "clear", "session_id": "b"}),
        ("claude", "PostToolUseFailure", {"session_id": "b", "is_interrupt": True}),
        ("claude", "StopFailure", {"session_id": "b"}),
        ("codex", "UserPromptSubmit", {"session_id": "b"}),
        ("agy", "PreInvocation", {"conversationId": "c", "invocationNum": 0, "modelName": "gemini-3-pro"}),
        ("agy", "Stop", {"conversationId": "c"}),
        ("vim", "Stop", {}),
    ]
    for item in plan:
        at += 1_000
        if isinstance(item, int):
            ops.append(mail(at, item))
        else:
            ops.extend(step(at, *item))
    return ops


def normalized(results: list[Any]) -> list[Any]:
    out: list[Any] = []
    for r in results:
        if isinstance(r, str) and r.startswith("{"):
            presence = json.loads(r)
            if "reminded" in presence:
                presence["reminded"] = len(presence["reminded"])
            out.append(list(presence.items()))
        elif isinstance(r, dict) and "id" in r and str(r["id"]).startswith("m-"):
            out.append("sent")
        else:
            out.append(r)
    return out


class HookParityTest(unittest.TestCase):
    def setUp(self) -> None:
        require_node(self)

    def test_node_and_python_hooks_print_the_same_and_write_the_same_presence(self) -> None:
        ops = scenario()
        runs = {}
        for name, spawn in (("node", node_worker), ("python", py_worker)):
            home = temp_home(self, "iat-hook-parity-")
            results = spawn(self, "store", {"home": home, "ops": ops}).result()["results"]
            errors = [r for r in results if "error" in r]
            self.assertEqual(errors, [], name)
            runs[name] = normalized([r["ok"] for r in results])
        self.assertEqual(runs["python"], runs["node"])
        outputs = [r for r in runs["node"] if isinstance(r, dict)]
        self.assertTrue(any("hookSpecificOutput" in o for o in outputs))
        self.assertTrue(any(o.get("decision") == "block" for o in outputs))


if __name__ == "__main__":
    unittest.main()

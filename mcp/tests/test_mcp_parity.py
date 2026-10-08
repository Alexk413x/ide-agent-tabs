from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
import unittest
from typing import Any

from jev_support import StubTypeSafe, jev_fixtures
from mcp_client import NODE_SERVER, McpProcess, result_json
from support import require_node

KEEP = ("SYSTEMROOT", "SystemRoot", "WINDIR", "windir", "COMSPEC", "ComSpec", "TEMP", "TMP", "TMPDIR")
TAB_A = "tab-parity-a"
TAB_B = "tab-parity-b"
TAB_C = "tab-parity-c"
THREAD_B = "019a-parity-thread"
EMOJI = "\U0001f600"
LONG_TEXT = "x" * 199 + EMOJI + "y" * 250
_IDS = re.compile(r"\b([mch])-([0-9a-f]{16}|[0-9a-f]{12})\b|\bs-[0-9a-f]{12}\b")
_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z")
_VOLATILE_KEYS = {"pid", "modBeat", "cost_usd"}


class Normalizer:
    def __init__(self, root: str) -> None:
        self.roots = sorted({root, root.replace("\\", "/"), os.path.realpath(root)}, key=len, reverse=True)
        self.ids: dict[str, str] = {}

    def _id(self, match: re.Match[str]) -> str:
        text = match.group(0)
        if text not in self.ids:
            self.ids[text] = f"<{text[0]}{len([k for k in self.ids if k[0] == text[0]]) + 1}>"
        return self.ids[text]

    def text(self, value: str) -> str:
        for root in self.roots:
            value = value.replace(root, "<root>")
        value = _TIME.sub("<time>", value)
        return _IDS.sub(self._id, value)

    def value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self.value(v) for v in value]
        if isinstance(value, dict):
            return {k: ("<n>" if k in _VOLATILE_KEYS and isinstance(v, (int, float)) else self.value(v)) for k, v in value.items()}
        return value

    def reply(self, reply: dict[str, Any] | None) -> Any:
        if reply is None:
            return None
        out = dict(reply)
        result = out.get("result")
        if isinstance(result, dict) and isinstance(result.get("content"), list):
            content = []
            for item in result["content"]:
                text = item.get("text")
                try:
                    parsed = json.loads(text) if isinstance(text, str) else None
                except ValueError:
                    parsed = None
                content.append({**item, "text": {"json": parsed}} if isinstance(parsed, (dict, list)) else item)
            out["result"] = {**result, "content": content}
        if isinstance(result, dict) and "capabilities" in result:
            tools = dict(result["capabilities"].get("tools") or {})
            tools.pop("listChanged", None)
            out["result"] = {**result, "capabilities": {**result["capabilities"], "tools": tools}}
        out.pop("id", None)
        return self.value(out)


def sandbox_env(root: str, home: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k in KEEP}
    env.update(
        {
            "IDE_AGENT_TABS_HOME": home,
            "PATH": os.path.join(root, "bin"),
            "HOME": root,
            "USERPROFILE": root,
            "LOCALAPPDATA": os.path.join(root, "local"),
            "APPDATA": os.path.join(root, "roaming"),
            "ProgramFiles": os.path.join(root, "programs"),
            "SHELL": "/bin/sh",
            "CODEX_HOME": os.path.join(root, "codex"),
            "CLAUDE_CONFIG_DIR": os.path.join(root, "claude"),
        }
    )
    return env


class Run:
    def __init__(self, impl: str, root: str, stub: StubTypeSafe) -> None:
        self.impl = impl
        self.root = root
        self.home = os.path.join(root, "home")
        self.work = os.path.join(root, "parity-work")
        for folder in ("bin", "codex", "claude", self.home, self.work):
            os.makedirs(os.path.join(root, folder), exist_ok=True)
        with open(os.path.join(root, "codex", "config.toml"), "w", encoding="utf-8") as f:
            f.write('model = "gpt-5.5-codex"\nmodel_reasoning_effort = "high"\n')
        with open(os.path.join(self.home, "config.json"), "w", encoding="utf-8") as f:
            json.dump({"jev": {"enabled": True}}, f)
        self.env = sandbox_env(root, self.home)
        self.env.update({"TYPESAFE_API_KEY": jev_fixtures()["key"], "TYPESAFE_BASE_URL": stub.url})
        self.norm = Normalizer(root)
        self.log: list[tuple[str, Any]] = []
        self.servers: dict[str, McpProcess] = {}

    def server(self, name: str, tab: str, extra: dict[str, str] | None = None) -> McpProcess:
        env = {**self.env, "IDE_AGENT_TABS_ID": tab, **(extra or {})}
        process = McpProcess(self.impl, env, self.work, os.path.join(self.root, f"{name}.stderr"))
        self.servers[name] = process
        return process

    def record(self, label: str, value: Any) -> None:
        self.log.append((label, value))

    def reply(self, label: str, reply: dict[str, Any] | None) -> None:
        self.record(label, self.norm.reply(reply))

    def presence(self, label: str, session_id: str) -> None:
        path = os.path.join(self.home, "sessions", f"{session_id}.json")
        try:
            with open(path, encoding="utf-8") as f:
                self.record(label, self.norm.value(json.load(f)))
        except OSError:
            self.record(label, None)

    def close(self) -> None:
        for process in self.servers.values():
            process.close()


def scenario(run: Run, stub: StubTypeSafe) -> None:
    a = run.server("a", TAB_A)
    b = run.server("b", TAB_B)
    c = run.server("c", TAB_C, {"IDE_AGENT_TABS_AGENT": "gemini"})
    run.reply("initialize a", a.initialize("claude-code", "2025-06-18"))
    run.reply("initialize b", b.initialize("codex-mcp-client", "2025-03-26"))
    run.reply("initialize c", c.initialize("other-client", "1999-01-01"))
    for name, process in (("a", a), ("b", b), ("c", c)):
        run.reply(f"tools/list {name}", process.request("tools/list"))
        run.reply(f"first read {name}", process.call("read_messages", {}))
    # Each server records its client's agent after the initialized notification, without a reply to wait for.
    time.sleep(0.5)
    run.reply("ping", a.request("ping"))
    run.reply("unknown method", a.request("resources/list"))
    run.reply("hook tool hidden from claude", a.call("agent_tabs_hook", {"event": "Stop"}))
    run.reply("unknown tool", a.call("no_such_tool", {}))
    invalid: list[tuple[str, Any]] = [
        ("send_message", None),
        ("send_message", {"to": "x"}),
        ("send_message", {"to": 5, "text": "", "replyTo": None}),
        ("send_message", {"to": "x", "text": EMOJI * 32_000}),
        ("wait_for_message", {"timeout": 1.5}),
        ("wait_for_message", {"timeout": 601, "from": 3}),
        ("wait_for_message", {"timeout": 1e20}),
        ("open_tab", {"path": "/x", "model": "bad model", "via": "x", "env": {"A": 1, "2": True}, "args": ["a", 5], "focus": "yes"}),
        ("open_tab", {"prompt": "p" * 30_001}),
        ("agent_tabs_mod", {"op": "nope"}),
        ("agent_tabs_mod", {"op": "history", "names": ["n"] * 9, "offset": -1, "agents": [{"names": "x"}, 5]}),
        ("agent_tabs_mod", {"op": "presence", "state": "asleep", "agentColor": "x" * 17}),
        ("resume_tab", {"id": ""}),
        ("handoff", {"goal": "g"}),
        ("list_tabs", {"ide": None}),
        ("jev_choose", {}),
        ("jev_rank", {"query": "q", "items": [{"id": "", "text": 1}], "top": 1e20}),
        ("jev_ask", {"state": "s", "questions": {"q": {"type": "wrong"}}}),
    ]
    for name, arguments in invalid:
        run.reply(f"invalid {name} {json.dumps(arguments)[:60]}", a.call(name, arguments))
    run.reply("list_sessions a", a.call("list_sessions", {}))
    run.reply(
        "mod presence a",
        a.call(
            "agent_tabs_mod",
            {
                "op": "presence",
                "driver": True,
                "nativeName": "alpha",
                "state": "idle",
                "model": "claude-opus-5-5",
                "effort": "high",
                "agentType": "reviewer",
                "agentColor": "blue",
                "session": "0123abcd-claude",
            },
        ),
    )
    run.presence("presence a after mod", TAB_A)
    run.reply("mod presence bad model", a.call("agent_tabs_mod", {"op": "presence", "model": "a\nb"}))
    run.reply(
        "hook b prompt", b.call("agent_tabs_hook", {"event": "UserPromptSubmit", "session_id": THREAD_B, "turn_id": "t1", "extra": 1})
    )
    b_id = f"codex-{THREAD_B}"
    run.presence("presence b after hook", b_id)
    run.presence("old presence b", TAB_B)
    listed = result_json(a.call("list_sessions", {}))
    run.record("list_sessions after rename", run.norm.value(listed))
    b_name = next(s["name"] for s in listed["sessions"] if s["id"] == b_id)
    a_name = next(s["name"] for s in listed["sessions"] if s["id"] == TAB_A)
    run.reply("send to self", a.call("send_message", {"to": a_name, "text": "me"}))
    run.reply("send to nobody", a.call("send_message", {"to": "no-such-session", "text": "hi"}))
    run.reply("send bad name", a.call("send_message", {"to": "not a name!", "text": "hi"}))
    run.reply("send bad replyTo", a.call("send_message", {"to": b_name, "text": "hi", "replyTo": "m-1"}))
    run.reply("send empty text", a.call("send_message", {"to": b_name, "text": " \n "}))
    run.reply("send a to b", a.call("send_message", {"to": b_name, "text": f"ping {EMOJI}"}))
    run.reply("send a to b again", a.call("send_message", {"to": b_name, "text": f"ping {EMOJI}"}))
    run.reply("hook b stop", b.call("agent_tabs_hook", {"event": "Stop", "session_id": THREAD_B}))
    read = b.call("read_messages", {})
    run.reply("read b", read)
    first = result_json(read)["messages"][0]["id"]
    run.reply("reply b to a", b.call("send_message", {"to": TAB_A, "text": "pong", "replyTo": first}))
    run.reply("unread a", a.call("agent_tabs_mod", {"op": "unread"}))
    take = a.call("agent_tabs_mod", {"op": "take"})
    run.reply("take a", take)
    claim = result_json(take)["claim"]
    run.reply("ack a", a.call("agent_tabs_mod", {"op": "ack", "claim": claim}))
    run.reply("ack a again", a.call("agent_tabs_mod", {"op": "ack", "claim": claim}))
    run.reply("release missing claim", a.call("agent_tabs_mod", {"op": "release"}))

    waiting = a.start("tools/call", {"name": "wait_for_message", "arguments": {"timeout": 30, "from": b_id}})
    quick = a.start("tools/call", {"name": "agent_tabs_mod", "arguments": {"op": "unread"}})
    run.reply("unread while a waits", a.response(quick))
    run.reply("b sends late", b.call("send_message", {"to": TAB_A, "text": "late"}))
    run.reply("wait a returns late", a.response(waiting))

    cancelled = a.start("tools/call", {"name": "wait_for_message", "arguments": {"timeout": 30}})
    time.sleep(0.5)
    a.notify("notifications/cancelled", {"requestId": cancelled, "reason": "test"})
    run.reply("ping after cancel", a.request("ping"))
    run.reply("b sends after cancel", b.call("send_message", {"to": TAB_A, "text": "after cancel"}))
    run.reply("cancelled wait sends nothing", a.response(cancelled, timeout=1.5))
    run.reply("read a keeps the message", a.call("read_messages", {}))

    for direction, text, at in (("sent", LONG_TEXT, 1_790_000_000_000.5), ("received", f"short {EMOJI}", 1_790_000_001_000)):
        run.reply(
            f"mod log {direction}",
            a.call("agent_tabs_mod", {"op": "log", "direction": direction, "peer": "beta", "text": text, "at": at}),
        )
    run.reply("mod log bad peer", a.call("agent_tabs_mod", {"op": "log", "direction": "sent", "peer": "a\tb", "text": "t"}))
    run.reply("mod log no text", a.call("agent_tabs_mod", {"op": "log", "direction": "sent", "peer": "beta"}))
    history = a.call("agent_tabs_mod", {"op": "history", "session": TAB_A, "names": ["alpha"]})
    run.reply("mod history", history)
    long_id = next(m["id"] for m in result_json(history)["messages"] if m["textLength"] == len(LONG_TEXT) + 1)
    for offset in (0, 199, 200, 10_000):
        run.reply(
            f"mod message offset {offset}",
            a.call("agent_tabs_mod", {"op": "message", "session": TAB_A, "names": ["alpha"], "id": long_id, "offset": offset}),
        )
    run.reply("mod message missing", a.call("agent_tabs_mod", {"op": "message", "names": ["alpha"], "id": "m-0000000000000000"}))
    run.reply("mod history before", a.call("agent_tabs_mod", {"op": "history", "names": ["alpha"], "before": "2026-01-01T00:00:00.000Z"}))
    run.reply("mod history nobody", a.call("agent_tabs_mod", {"op": "history", "names": ["a\nb"]}))
    run.reply("mod history bad session", a.call("agent_tabs_mod", {"op": "history", "session": "-bad"}))
    run.reply(
        "mod counts",
        a.call(
            "agent_tabs_mod",
            {
                "op": "counts",
                "agents": [{"session": TAB_A, "names": []}, {"names": ["beta"]}, {"names": []}, {"session": "?", "names": []}],
            },
        ),
    )
    run.reply("mod sessions", a.call("agent_tabs_mod", {"op": "sessions"}))
    run.reply("mod settings", a.call("agent_tabs_mod", {"op": "settings"}))
    run.reply("mod send", a.call("agent_tabs_mod", {"op": "send", "to": TAB_C, "text": "to c"}))
    run.reply("mod send missing to", a.call("agent_tabs_mod", {"op": "send", "text": "x"}))

    run.reply("c wait timeout 0", c.call("wait_for_message", {"timeout": 0}))
    run.reply("c read", c.call("read_messages", {}))
    run.reply("c wait bad from", c.call("wait_for_message", {"timeout": 0, "from": "-x"}))
    run.presence("presence c", TAB_C)

    missing = os.path.join(run.root, "missing-folder")
    run.reply("handoff without goal", a.call("handoff", {"path": run.work}))
    run.reply(
        "handoff to missing folder",
        a.call("handoff", {"path": missing, "goal": "Finish the port", "next": "Run the suite", "files": ["a.py", " "]}),
    )
    run.reply("close unknown tab", a.call("close_tab", {"id": "tab-nope"}))
    run.reply("list_tabs", a.call("list_tabs", {}))
    run.reply("open_tab missing folder", a.call("open_tab", {"path": missing}))
    run.reply("open_tab relative folder", a.call("open_tab", {"path": "relative"}))
    run.reply("closed_sessions", a.call("closed_sessions", {}))
    run.reply("resume unknown", a.call("resume_tab", {"id": "zzzz"}))
    run.reply("list_agents", a.call("list_agents"))

    check = next(t for t in jev_fixtures()["tools"] if t["tool"] == "jev_check" and t["response"])
    stub.set(check["response"])
    run.reply("jev_check", a.call("jev_check", check["input"]))
    stub.set({"status": 401, "body": json.dumps({"error": "bad key"})})
    run.reply("jev_check rejected", a.call("jev_check", check["input"]))

    raw_id = a.new_id()
    a.send_raw(b"not json\n\n" + json.dumps({"jsonrpc": "2.0", "id": raw_id, "method": "ping"}).encode() + b"\r\n")
    run.reply("ping after a bad line", a.response(raw_id))
    run.reply("call without params", a.request("tools/call"))
    run.reply("call with array arguments", a.request("tools/call", {"name": "list_tabs", "arguments": [1]}))

    for name, process in run.servers.items():
        process.notifications("notifications/tools/list_changed")
        run.record(f"exit {name}", process.close())
    sessions = os.path.join(run.home, "sessions")
    run.record("sessions left", sorted(os.listdir(sessions)) if os.path.isdir(sessions) else [])
    handoffs = os.path.join(run.home, "handoffs")
    run.record("handoff files", sorted(os.path.splitext(n)[1] for n in os.listdir(handoffs)) if os.path.isdir(handoffs) else [])


class McpParityTest(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        require_node(self)
        if not os.path.exists(NODE_SERVER):
            self.fail(f"{NODE_SERVER} is missing; run npm run build in mcp/")
        self.stub = StubTypeSafe()
        self.addCleanup(self.stub.close)

    def transcript(self, impl: str) -> list[tuple[str, Any]]:
        root = os.path.realpath(tempfile.mkdtemp(prefix=f"iat-parity-{impl}-"))
        self.addCleanup(shutil.rmtree, root, True)
        run = Run(impl, root, self.stub)
        try:
            scenario(run, self.stub)
        finally:
            run.close()
        return run.log

    def test_node_and_python_servers_answer_a_scripted_session_the_same(self) -> None:
        node = self.transcript("node")
        python = self.transcript("python")
        self.assertEqual([label for label, _ in python], [label for label, _ in node])
        for (label, expected), (_, actual) in zip(node, python):
            with self.subTest(step=label):
                self.assertEqual(actual, expected)

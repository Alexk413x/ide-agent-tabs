from __future__ import annotations

import json
import os
import threading
import unittest
from collections.abc import Collection, Mapping
from typing import Any

from ide_agent_tabs.mcp_tools import Call, ToolDeps, Tools
from ide_agent_tabs.messaging.messaging import Messaging
from ide_agent_tabs.net.origin import OriginChecker
from ide_agent_tabs.net.outbound import Outbound, SessionNet, close_all
from ide_agent_tabs.net.policy import Hub, PeerGrant, Policy, save_policy
from ide_agent_tabs.net.store import NetStore
from support import session, temp_home

PEER = "AAAAAAA-BBBBBBB-CCCCCCC-DDDDDDD-EEEEEEE-FFFFFFF-GGGGGGG-HHHHHHH"
OTHER = "BBBBBBB-CCCCCCC-DDDDDDD-EEEEEEE-FFFFFFF-GGGGGGG-HHHHHHH-AAAAAAA"
MESSAGE_ID = "m-0123456789abcdef"
START = 1_800_000_000_000


class Ticks:
    def __init__(self) -> None:
        self.at = START

    def __call__(self) -> int:
        return self.at


class FakeRequest:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.reply: dict[str, Any] = {"re": "r", "ok": True, "body": {"id": MESSAGE_ID}}
        self.replies: list[dict[str, Any]] = []
        self.error: Exception | None = None

    def __call__(self, home: str, peer: str, op: str, body: Mapping[str, Any], **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"home": home, "peer": peer, "op": op, "body": dict(body), **kwargs})
        if self.error is not None:
            raise self.error
        return self.replies.pop(0) if self.replies else self.reply


class FakeService:
    def __init__(self) -> None:
        self.opened: list[dict[str, Any]] = []

    def open_tab(self, given: Mapping[str, Any], wait: str = "full", on_progress: Any = None) -> dict[str, Any]:
        self.opened.append(dict(given))
        return {"id": "tab-new"}


class FakeHandoffs:
    def __init__(self) -> None:
        self.started: list[dict[str, Any]] = []

    def start(self, given: Mapping[str, Any]) -> dict[str, Any]:
        self.started.append(dict(given))
        return {"id": "handoff-1"}

    def check_close(self, target: str | None, caller_id: str) -> None:
        return None


class FakeResumes:
    def __init__(self) -> None:
        self.resumed: list[dict[str, Any]] = []

    def resume(self, given: Mapping[str, Any]) -> dict[str, Any]:
        self.resumed.append(dict(given))
        return {"resumed": True}

    def list(self) -> dict[str, Any]:
        return {"sessions": []}


def write_config(home: str, **network: Any) -> None:
    with open(os.path.join(home, "config.json"), "w", encoding="utf-8") as f:
        json.dump({"network": network} if network else {}, f)


def write_policy(home: str, peers: Collection[PeerGrant]) -> None:
    save_policy(home, Policy(tuple(peers), 0))


def write_member_policy(home: str, peers: Collection[PeerGrant]) -> None:
    save_policy(home, Policy(tuple(peers), 0, Hub("0123456789abcdef0123456789abcdef", "home")))


def peer_grant(device_id: str = PEER, label: str = "laptop", address: str = "192.168.1.9", member: bool = False) -> PeerGrant:
    return PeerGrant(device_id=device_id, label=label, address=address, member=member)


class NetCase(unittest.TestCase):
    def setUp(self) -> None:
        self.home = temp_home(self)
        self.addCleanup(close_all)
        self.folder = os.path.join(self.home, "work")
        os.makedirs(self.folder)
        self.enable()
        write_policy(self.home, [peer_grant()])
        self.clock = Ticks()
        self.request = FakeRequest()
        self.nudges: list[tuple[str, str]] = []
        self.net = self.make_outbound()
        self.messaging = self.make_session("tab-a")
        self.service = FakeService()
        self.handoffs = FakeHandoffs()
        self.resumes = FakeResumes()

    def enable(self, **over: Any) -> None:
        fields: dict[str, Any] = {"network": "lan", "bind": "192.168.1.5", "outboundFolders": [self.folder]}
        fields.update(over)
        write_config(self.home, **fields)

    def make_outbound(self, **over: Any) -> Outbound:
        fields: dict[str, Any] = {
            "now_ms": self.clock,
            "request": self.request,
            "nudge": lambda home, peer: self.nudges.append((home, peer)),
            "origin": OriginChecker(self.home, self.clock),
        }
        fields.update(over)
        outbound = Outbound(self.home, **fields)
        self.addCleanup(outbound.close)
        return outbound

    def make_session(self, tab: str, pid: int = 1, **over: Any) -> Messaging:
        return session(self, self.home, tab, "claude", pid, cwd=self.folder, **over)

    def store(self) -> NetStore:
        store = NetStore(self.home)
        self.addCleanup(store.close)
        return store

    def record_remote_tab(self, tab: str, peer: str = PEER) -> None:
        self.store().record_remote_tab(tab, peer, self.clock())

    def call(self, name: str, arguments: dict[str, Any] | None = None, messaging: Messaging | None = None) -> tuple[bool, str]:
        caller = messaging or self.messaging
        deps = ToolDeps(self.service, None, caller, self.handoffs, self.resumes, SessionNet(self.net, caller.caller))
        result = Tools(lambda: deps, False).call("claude-code", name, arguments, Call({}, threading.Event(), lambda *_a: None))
        return bool(result.get("isError")), str(result["content"][0]["text"])

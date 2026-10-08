from __future__ import annotations

import threading
from typing import Any, Callable

from ide_agent_tabs.clock import now_iso
from ide_agent_tabs.jsjson import stringify
from ide_agent_tabs.messaging import store
from ide_agent_tabs.messaging.db import MailError
from ide_agent_tabs.messaging.sessions import HEARTBEAT_MS, MAIL_VERSION, Presence, live_sessions, read_presence, update_presence
from ide_agent_tabs.processes import pid_alive
from ide_agent_tabs.shared.host import Binding, BoundSession, Catalog, Progress

HTTP_MAX_WAIT_S = 240


def _text(value: Any, error: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {"content": [{"type": "text", "text": value if isinstance(value, str) else stringify(value)}]}
    if error:
        out["isError"] = True
    return out


# A stand-in for P7's tool layer: the shared server's tests and stress run need sessions with presence files
# and the messaging tools over the real message store, not the rest of the tool set.
class StoreSession:
    def __init__(self, host: StoreHost, binding: Binding) -> None:
        self.host = host
        self.binding = binding
        self._id = binding.id
        self.started_at = now_iso()
        self.calls: list[str] = []
        tab = binding.tab
        taken = False
        if tab is not None:

            def claim(current: Presence | None) -> Presence | None:
                nonlocal taken
                pid = current.get("pid") if current else None
                if current is not None and pid is not None and pid != binding.pid and host.alive(pid):
                    taken = True
                    return current
                return self.presence()

            self._id = tab
            update_presence(host.home, tab, claim)
        if tab is None or taken:
            self._id = binding.id
            update_presence(host.home, self._id, lambda _c: self.presence())

    @property
    def id(self) -> str:
        return self._id

    def presence(self) -> Presence:
        b = self.binding
        p: Presence = {"id": self._id, "agent": b.agent, "path": b.cwd, "pid": b.pid}
        if b.pid_start is not None:
            p["pidStart"] = b.pid_start
        p.update({"startedAt": self.started_at, "state": "unknown", "beatMs": HEARTBEAT_MS, "mail": MAIL_VERSION})
        return p

    def call(self, name: str, arguments: dict[str, Any], progress: Progress | None, cancel: threading.Event) -> dict[str, Any]:
        self.calls.append(name)
        home = self.host.home
        try:
            if name == "list_sessions":
                sessions = [
                    {"id": p["id"], "agent": p.get("agent"), "path": p.get("path"), "self": p["id"] == self._id}
                    for p in live_sessions(home, self.host.alive)
                ]
                return _text({"sessions": sessions})
            if name == "send_message":
                sender = {"id": self._id, "agent": self.binding.agent, "path": self.binding.cwd}
                sent = store.send_message(home, {"from": sender, "to": arguments["to"], "text": arguments["text"]})
                return _text({"id": sent["id"], "to": arguments["to"]})
            if name == "read_messages":
                batch = store.take_batch(home, self._id, count=store.MAX_UNREAD, chars=store.MAX_READ_CHARS)
                return _text({"messages": batch["messages"], "remaining": batch["remaining"]})
            if name == "wait_for_message":
                timeout = min(float(arguments.get("timeoutSeconds", HTTP_MAX_WAIT_S)), HTTP_MAX_WAIT_S)
                got = store.wait_for_message(home, self._id, None, timeout * 1000, cancel)
                self.host.waits_done.append("cancelled" if cancel.is_set() else "returned")
                return _text({"message": got} if got is not None else {"timedOut": True})
        except (MailError, KeyError) as e:
            return _text(f"{name}: {e}", error=True)
        return _text(f"unknown tool {name}", error=True)

    def end(self) -> None:
        self.host.ended.append(self._id)
        own = read_presence(self.host.home, self._id)
        if own is not None and own.get("pid") == self.binding.pid:
            update_presence(self.host.home, self._id, lambda _c: None)

    def release(self) -> None:
        self.host.released.append(self._id)


class StoreHost(Catalog):
    def __init__(self, home: str, alive: Callable[[int], bool] = pid_alive) -> None:
        super().__init__(home)
        self.alive = alive
        self.bound: list[StoreSession] = []
        self.ended: list[str] = []
        self.released: list[str] = []
        self.waits_done: list[str] = []
        self.closed = False

    def bind(self, binding: Binding) -> BoundSession:
        session = StoreSession(self, binding)
        self.bound.append(session)
        return session

    def close(self) -> None:
        self.closed = True

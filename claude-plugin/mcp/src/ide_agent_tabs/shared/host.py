from __future__ import annotations

import threading
from typing import Any, NamedTuple, Protocol


class Binding(NamedTuple):
    id: str
    agent: str
    pid: int
    cwd: str
    tab: str | None = None
    pid_start: int | None = None


class BoundSession(Protocol):
    @property
    def id(self) -> str: ...

    def call(self, name: str, arguments: dict[str, Any], cancel: threading.Event) -> dict[str, Any]: ...

    def end(self) -> None: ...

    def release(self) -> None: ...


# What the shared server needs from the tool layer: the tool list and instructions a client sees, and one
# bound session per Claude Code process. end() records the session as closed and removes its presence;
# release() keeps the presence for the next server, as a build handover does.
class ToolHost(Protocol):
    def tools_for(self, agent: str) -> list[dict[str, Any]]: ...

    def instructions(self) -> str: ...

    def bind(self, binding: Binding) -> BoundSession: ...

    def close(self) -> None: ...

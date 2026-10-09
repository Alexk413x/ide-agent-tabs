from __future__ import annotations

import json
import os
import threading
from typing import Any, Callable, NamedTuple, Optional, Protocol

from ..jsjson import parse

CATALOG_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "catalog.json")
JEV_PREFIX = "jev_"

Progress = Callable[[float, Optional[float], Optional[str]], None]


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

    def call(self, name: str, arguments: dict[str, Any], progress: Progress | None, cancel: threading.Event) -> dict[str, Any]: ...

    def end(self) -> None: ...

    def release(self) -> None: ...


# What the shared server needs from the tool layer: the tool list and instructions a client sees, and one
# bound session per Claude Code process. end() records the session as closed and removes its presence;
# release() keeps the presence for the next server, as a build handover does.
class ToolHost(Protocol):
    def tools_for(self, agent: str) -> list[dict[str, Any]]: ...

    def instructions_for(self, agent: str) -> str: ...

    def bind(self, binding: Binding) -> BoundSession: ...

    def close(self) -> None: ...


def _load_catalog() -> dict[str, Any]:
    with open(CATALOG_FILE, encoding="utf-8") as f:
        return json.load(f)


def jev_enabled(home: str) -> bool:
    try:
        with open(os.path.join(home, "config.json"), encoding="utf-8") as f:
            config = parse(f.read())
    except (OSError, ValueError):
        return False
    jev = config.get("jev") if isinstance(config, dict) else None
    return isinstance(jev, dict) and jev.get("enabled") is True


class Catalog:
    def __init__(self, home: str, catalog: dict[str, Any] | None = None) -> None:
        self.home = home
        self.catalog = _load_catalog() if catalog is None else catalog

    def tools_for(self, agent: str) -> list[dict[str, Any]]:
        only: dict[str, list[str]] = self.catalog.get("only", {})
        jev = jev_enabled(self.home)
        return [
            tool
            for tool in self.catalog["tools"]
            if agent in only.get(tool["name"], [agent]) and (jev or not str(tool["name"]).startswith(JEV_PREFIX))
        ]

    def instructions_for(self, agent: str) -> str:
        texts: dict[str, str] = self.catalog.get("instructions", {})
        return texts.get("jev" if jev_enabled(self.home) else "plain", "")

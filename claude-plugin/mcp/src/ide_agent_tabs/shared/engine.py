from __future__ import annotations

import threading
from typing import Any, Callable

from .host import Binding, BoundSession, Catalog, Progress, ToolHost

NOT_BUILT = "Agent Tabs: this build of the shared server has no tool layer yet"


class _Unbuilt:
    def __init__(self, binding: Binding) -> None:
        self._id = binding.id

    @property
    def id(self) -> str:
        return self._id

    def call(self, name: str, arguments: dict[str, Any], progress: Progress | None, cancel: threading.Event) -> dict[str, Any]:
        return {"isError": True, "content": [{"type": "text", "text": NOT_BUILT}]}

    def end(self) -> None:
        return None

    def release(self) -> None:
        return None


class CatalogHost(Catalog):
    def bind(self, binding: Binding) -> BoundSession:
        return _Unbuilt(binding)

    def close(self) -> None:
        return None


def load_host(home: str, log: Callable[[str], None]) -> ToolHost:
    return CatalogHost(home)

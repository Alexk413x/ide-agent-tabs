from __future__ import annotations

import os
from typing import Any, Callable

from .files import file_lock, read_text_if_exists, write_atomically
from .jsjson import is_number, js_trim, parse, stringify
from .terminals.driver import TerminalTab

TABS_FILE = "terminal-tabs.json"


def _is_tab(t: Any) -> bool:
    return (
        isinstance(t, dict)
        and isinstance(t.get("id"), str)
        and isinstance(t.get("terminal"), str)
        and isinstance(t.get("agent"), str)
        and isinstance(t.get("path"), str)
        and is_number(t.get("createdAt"))
    )


def parse_tabs(text: str | None) -> list[TerminalTab]:
    if text is None or js_trim(text) == "":
        return []
    try:
        value = parse(text)
    except (ValueError, RecursionError):
        return []
    tabs = value.get("tabs") if isinstance(value, dict) else None
    if not isinstance(tabs, list):
        return []
    return [t for t in tabs if _is_tab(t)]


class TabStore:
    def __init__(self, home: str) -> None:
        self.file = os.path.join(home, TABS_FILE)

    def read(self) -> list[TerminalTab]:
        return parse_tabs(read_text_if_exists(self.file))

    def update(self, change: Callable[[list[TerminalTab]], list[TerminalTab]]) -> list[TerminalTab]:
        with file_lock(self.file):
            updated = change(self.read())
            write_atomically(self.file, stringify({"tabs": updated}, 2) + "\n")
            return updated

    def add(self, tab: TerminalTab) -> list[TerminalTab]:
        return self.update(lambda tabs: [*(t for t in tabs if t["id"] != tab["id"]), tab])

    def remove(self, ids: set[str]) -> list[TerminalTab]:
        return self.update(lambda tabs: [t for t in tabs if t["id"] not in ids])

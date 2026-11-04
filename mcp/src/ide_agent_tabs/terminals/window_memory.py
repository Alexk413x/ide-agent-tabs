from __future__ import annotations

import os
from typing import Any

from ..files import file_lock, read_text_if_exists, write_atomically
from ..jsjson import entries, parse, stringify

WINDOWS_FILE = "terminal-windows.json"
DEDICATED_NAME = "agent-tabs"

RememberedWindow = dict[str, str]


def _parse(text: str | None) -> dict[str, RememberedWindow]:
    if text is None:
        return {}
    try:
        value: Any = parse(text)
    except (ValueError, RecursionError):
        return {}
    if not isinstance(value, dict):
        return {}
    return {
        name: w
        for name, w in entries(value)
        if isinstance(w, dict) and isinstance(w.get("id"), str) and ("socket" not in w or isinstance(w.get("socket"), str))
    }


def read_window(home: str, terminal: str) -> RememberedWindow | None:
    try:
        text = read_text_if_exists(os.path.join(home, WINDOWS_FILE))
    except OSError:
        text = None
    return _parse(text).get(terminal)


def remember_window(home: str, terminal: str, window: RememberedWindow) -> None:
    file = os.path.join(home, WINDOWS_FILE)
    with file_lock(file):
        all_windows = _parse(read_text_if_exists(file))
        all_windows[terminal] = window
        write_atomically(file, stringify(all_windows, 2) + "\n")

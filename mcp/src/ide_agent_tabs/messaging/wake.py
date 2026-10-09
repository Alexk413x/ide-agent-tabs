from __future__ import annotations

import contextlib
import os
import sqlite3
import threading
from collections.abc import Collection
from typing import Callable

from ..clock import now_ms
from ..files import mtime_ms
from .db import Db, MailError, query, wake_dir

POLL_MS = 100

Listener = Callable[[], None]


class _Folder:
    def __init__(self) -> None:
        self.listeners: dict[str, set[Listener]] = {}
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None
        self.version: int | None = None


_folders: dict[str, _Folder] = {}
_lock = threading.Lock()
_skip_local_files = False


def skip_wake_files_for_local_waiters(skip: bool) -> None:
    global _skip_local_files
    _skip_local_files = skip


def wake_path(home: str, session_id: str) -> str:
    return os.path.join(wake_dir(home), session_id)


def _key(home: str) -> str:
    return os.path.abspath(wake_dir(home))


def _dispatch(folder: _Folder, session_id: str | None) -> None:
    with _lock:
        if session_id is None:
            targets = [listener for group in folder.listeners.values() for listener in group]
        else:
            targets = list(folder.listeners.get(session_id, ()))
    for listener in targets:
        listener()


def notify_local(home: str, session_id: str) -> None:
    folder = _folders.get(_key(home))
    if folder is not None:
        _dispatch(folder, session_id)


def signal_wake(home: str, session_id: str) -> None:
    folder = _folders.get(_key(home))
    local = folder is not None and session_id in folder.listeners
    notify_local(home, session_id)
    if local and _skip_local_files:
        return
    with contextlib.suppress(OSError):
        fd = os.open(wake_path(home, session_id), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(str(now_ms()).encode("ascii"))


def data_version(home: str) -> int:
    def read(db: Db) -> int:
        row = db.one("PRAGMA data_version")
        return int(row[0]) if row is not None else 0

    return query(home, read)


def _read_version(home: str) -> int | None:
    try:
        return data_version(home)
    except (MailError, sqlite3.Error, OSError):
        return None


def _poll(home: str, folder: _Folder, poll_ms: int) -> None:
    while not folder.stop.wait(poll_ms / 1000):
        current = _read_version(home)
        if current is None:
            continue
        if folder.version is not None and current != folder.version:
            _dispatch(folder, None)
        folder.version = current


def on_wake(home: str, session_id: str, listener: Listener, poll_ms: int = POLL_MS) -> Callable[[], None]:
    key = _key(home)
    with _lock:
        folder = _folders.get(key)
        if folder is None:
            folder = _Folder()
            _folders[key] = folder
        if folder.thread is None:
            # The waiter checks the store after this returns, so a commit after this baseline always dispatches.
            folder.version = _read_version(home)
            folder.thread = threading.Thread(target=_poll, args=(home, folder, poll_ms), name="agent-tabs-wake", daemon=True)
            folder.thread.start()
        folder.listeners.setdefault(session_id, set()).add(listener)
    own = folder

    def stop() -> None:
        with _lock:
            group = own.listeners.get(session_id)
            if group is not None:
                group.discard(listener)
                if not group:
                    del own.listeners[session_id]
            if not own.listeners:
                own.stop.set()
                if _folders.get(key) is own:
                    del _folders[key]

    return stop


def remove_stale_wakes(home: str, live: Collection[str], cutoff_ms: float) -> None:
    folder = wake_dir(home)
    try:
        names = os.listdir(folder)
    except OSError:
        return
    for name in names:
        if name in live:
            continue
        path = os.path.join(folder, name)
        if not os.path.isfile(path):
            continue
        mtime = mtime_ms(path)
        if mtime is not None and mtime < cutoff_ms:
            with contextlib.suppress(OSError):
                os.remove(path)

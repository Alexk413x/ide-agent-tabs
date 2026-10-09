from __future__ import annotations

import contextlib
import errno
import os
import sys
import time
from collections.abc import Iterator, Sequence
from typing import Callable

from .clock import now_ms
from .liveness import pid_alive
from .winapi import open_shared_read

LOCK_STALE_MS = 10_000
LOCK_WAIT_MS = 15_000
LOCK_POLL_S = 0.025
_RENAME_ATTEMPTS = 20


def ensure_private_dir(path: str) -> None:
    # A failed mkdir of an existing folder costs about 5 ms on Windows, more than the check.
    if not os.path.isdir(path):
        os.makedirs(path, mode=0o700, exist_ok=True)
    if sys.platform != "win32":
        os.chmod(path, 0o700)


def _write_new(path: str, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def _bytes(content: str | bytes) -> bytes:
    return content.encode("utf-8") if isinstance(content, str) else content


def write_new_private_file(path: str, content: str | bytes) -> None:
    ensure_private_dir(os.path.dirname(os.path.abspath(path)))
    _write_new(path, _bytes(content))


# Windows refuses to rename over a file while another process has it open, such as a reader of the same
# file; the reader closes it within milliseconds.
def replace_retrying(source: str, target: str) -> None:
    for attempt in range(_RENAME_ATTEMPTS + 1):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if sys.platform != "win32" or attempt >= _RENAME_ATTEMPTS:
                raise
            time.sleep(0.025)


def write_atomically(path: str, content: str | bytes) -> None:
    ensure_private_dir(os.path.dirname(os.path.abspath(path)))
    temp = f"{path}.{os.getpid()}.{now_ms()}.tmp"
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(_bytes(content))
        replace_retrying(temp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(temp)
        raise


def read_bytes(path: str) -> bytes:
    with os.fdopen(open_shared_read(path), "rb") as f:
        return f.read()


def read_text_if_exists(path: str) -> str | None:
    try:
        return read_bytes(path).decode("utf-8", "replace")
    except FileNotFoundError:
        return None


def remove_file(path: str) -> None:
    for attempt in range(_RENAME_ATTEMPTS + 1):
        try:
            os.remove(path)
            return
        except FileNotFoundError:
            return
        except PermissionError:
            if sys.platform != "win32" or attempt >= _RENAME_ATTEMPTS:
                raise
            time.sleep(0.025)


def remove_tree(path: str) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)


def mtime_ms(path: str) -> float | None:
    try:
        return os.stat(path).st_mtime_ns / 1e6
    except OSError:
        return None


def remove_stale_files(folder: str, suffixes: Sequence[str], max_age_ms: float, now: float | None = None) -> None:
    now = now_ms() if now is None else now
    try:
        names = os.listdir(folder)
    except OSError:
        return
    for name in names:
        if not any(name.endswith(s) for s in suffixes):
            continue
        path = os.path.join(folder, name)
        mtime = mtime_ms(path)
        if mtime is not None and now - mtime > max_age_ms:
            with contextlib.suppress(OSError):
                os.remove(path)


def _lock_owner(text: str | None) -> int | None:
    head, sep, _ = (text or "").partition(" ")
    if not sep or not head.isdigit() or not head.isascii():
        return None
    pid = int(head)
    return pid if 0 < pid <= 2**53 - 1 else None


def _abandoned(lock: str, alive: Callable[[int], bool]) -> str | None:
    try:
        text = read_text_if_exists(lock)
    except OSError:
        text = None
    mtime = mtime_ms(lock)
    if text is None or mtime is None:
        return None
    owner = _lock_owner(text)
    dead = owner is not None and not alive(owner)
    return text if dead or now_ms() - mtime > LOCK_STALE_MS else None


def _remove_if_still(lock: str, text: str) -> None:
    try:
        current = read_text_if_exists(lock)
    except OSError:
        return
    if current == text:
        with contextlib.suppress(OSError):
            remove_file(lock)


def new_lock_token() -> str:
    return f"{os.getpid()} {os.urandom(8).hex()}"


@contextlib.contextmanager
def file_lock(path: str, timeout_ms: float = LOCK_WAIT_MS, alive: Callable[[int], bool] = pid_alive) -> Iterator[None]:
    lock = f"{path}.lock"
    token = new_lock_token()
    ensure_private_dir(os.path.dirname(os.path.abspath(path)))
    deadline = now_ms() + timeout_ms
    while True:
        try:
            _write_new(lock, token.encode("utf-8"))
            break
        except FileExistsError:
            pass
        except PermissionError:
            # Windows answers access denied, not "exists", while another process's delete of the lock is pending.
            if sys.platform != "win32":
                raise
        except OSError as e:
            if e.errno != errno.EEXIST:
                raise
        abandoned = _abandoned(lock, alive)
        if abandoned is not None:
            _remove_if_still(lock, abandoned)
            continue
        if now_ms() > deadline:
            raise TimeoutError(f"timed out waiting for {lock}")
        time.sleep(LOCK_POLL_S)
    try:
        yield
    finally:
        _remove_if_still(lock, token)

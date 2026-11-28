from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..clock import iso
from ..files import file_lock, replace_retrying
from .private_files import PrivateFileError, check_private, ensure_private_dir, make_private_file

AUDIT_FILE = "audit.jsonl"
MAX_BYTES = 5 * 1024 * 1024
ADMITTED = "admitted"
REFUSED = "refused"


class AuditError(Exception):
    pass


@dataclass(frozen=True)
class AuditRecord:
    peer: str
    request_id: str | None
    op: str | None
    decision: str
    outcome: str
    reason: str
    generation: int | None
    tab: str | None = None


def audit_path(home: str) -> str:
    return os.path.join(home, "net", AUDIT_FILE)


class Audit:
    def __init__(self, home: str, now_ms: Callable[[], int], max_bytes: int = MAX_BYTES) -> None:
        self._path = audit_path(home)
        self._now_ms = now_ms
        self._max_bytes = max_bytes
        self._lock = threading.Lock()
        self._folder_ready = False

    def _ready(self) -> None:
        if not self._folder_ready:
            ensure_private_dir(Path(os.path.dirname(self._path)))
            self._folder_ready = True

    def _open(self) -> int:
        created = not os.path.exists(self._path)
        fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0), 0o600)
        if created:
            try:
                make_private_file(Path(self._path))
            except BaseException:
                os.close(fd)
                raise
        elif check_private(Path(self._path)):
            os.close(fd)
            raise PrivateFileError(f"{self._path} is not private")
        return fd

    def check(self) -> None:
        with self._lock:
            try:
                self._ready()
                os.close(self._open())
            except OSError as e:
                raise AuditError(f"the audit log can't be written: {e}") from e

    def _rotate(self, incoming: int) -> None:
        try:
            size = os.path.getsize(self._path)
        except OSError:
            return
        if size > 0 and size + incoming > self._max_bytes:
            replace_retrying(self._path, f"{self._path}.1")

    def write(self, record: AuditRecord) -> None:
        line = json.dumps(
            {
                "time": iso(self._now_ms()),
                "peer": record.peer,
                "id": record.request_id,
                "op": record.op,
                "decision": record.decision,
                "outcome": record.outcome,
                "reason": record.reason,
                "generation": record.generation,
                "tab": record.tab,
            },
            separators=(",", ":"),
        )
        data = (line + "\n").encode("utf-8")
        with self._lock:
            try:
                self._ready()
                with file_lock(self._path):
                    self._rotate(len(data))
                    fd = self._open()
                    with os.fdopen(fd, "ab") as f:
                        f.write(data)
            except OSError as e:
                raise AuditError(f"the audit log can't be written: {e}") from e

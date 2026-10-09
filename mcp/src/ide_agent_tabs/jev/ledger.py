from __future__ import annotations

import math
import os
import sys
import time
from typing import Any

from ..clock import now_ms, parse_iso
from ..files import ensure_private_dir, read_text_if_exists
from ..jsjson import parse, stringify, trim
from ..winapi import FILE_ATTRIBUTE_NORMAL, FILE_SHARE_ALL, INVALID_HANDLE_VALUE, kernel32
from .settings import is_number

_FILE_APPEND_DATA = 0x0004
_OPEN_ALWAYS = 4


def ledger_path(home: str) -> str:
    return os.path.join(home, "jev", "ledger.jsonl")


# An append-only handle makes each write land at the end of the file even while another process appends too;
# the C runtime's O_APPEND on Windows seeks and then writes, so two writers can overwrite each other's line.
def _open_append(path: str) -> int:
    if sys.platform != "win32":
        return os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    import ctypes
    import msvcrt

    handle = kernel32().CreateFileW(path, _FILE_APPEND_DATA, FILE_SHARE_ALL, None, _OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, None)
    if handle is None or handle == ctypes.c_void_p(INVALID_HANDLE_VALUE).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return msvcrt.open_osfhandle(handle, os.O_WRONLY | os.O_BINARY)
    except BaseException:
        kernel32().CloseHandle(handle)
        raise


def append_ledger(home: str, entry: dict[str, Any]) -> None:
    path = ledger_path(home)
    ensure_private_dir(os.path.dirname(path))
    data = (stringify(entry) + "\n").encode("utf-8", "surrogatepass")
    fd = _open_append(path)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def to_fixed(value: float, digits: int) -> float:
    if not math.isfinite(value):
        return value
    from decimal import ROUND_HALF_UP, Decimal

    return float(Decimal(value).quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP))


def cost_usd(input_tokens: float, price_per_million_input: float) -> float:
    return to_fixed(input_tokens * price_per_million_input / 1e6, 8)


def _local_day(ms: float | None) -> tuple[int, int, int] | None:
    if ms is None:
        return None
    try:
        t = time.localtime(ms / 1000)
    except (OverflowError, OSError, ValueError):
        return None
    return (t.tm_year, t.tm_mon, t.tm_mday)


def _entries_of(text: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for line in text.split("\n"):
        if trim(line) == "":
            continue
        try:
            e = parse(line)
        except ValueError:
            continue
        if isinstance(e, dict) and isinstance(e.get("at"), str) and isinstance(e.get("ok"), bool):
            entries.append(e)
    return entries


def summarize_ledger(home: str, price_per_million_input: float, now: float | None = None) -> dict[str, Any]:
    try:
        text = read_text_if_exists(ledger_path(home)) or ""
    except OSError:
        text = ""
    entries = _entries_of(text)
    today_key = _local_day(now_ms() if now is None else now)
    today = [e for e in entries if _local_day(parse_iso(e["at"])) == today_key]
    tokens = sum(e["input_tokens"] for e in today if is_number(e.get("input_tokens")) and math.isfinite(e["input_tokens"]))
    answered = [e for e in entries if e["ok"]]
    last = answered[-1].get("model") if answered else None
    return {
        "last_model": last,
        "today": {
            "calls": len(today),
            "failed": sum(1 for e in today if not e["ok"]),
            "input_tokens": tokens,
            "cost_usd": cost_usd(tokens, price_per_million_input),
        },
    }

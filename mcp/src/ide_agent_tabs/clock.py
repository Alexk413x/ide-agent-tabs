from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_ISO = re.compile(
    r"(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?"
    r"(?:[Tt ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d+))?)?)?"
    r"([Zz]|[+-]\d{2}:\d{2})?"
)


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def iso(ms: int) -> str:
    at = _EPOCH + timedelta(milliseconds=ms)
    return f"{at.year:04d}-{at.month:02d}-{at.day:02d}T{at.hour:02d}:{at.minute:02d}:{at.second:02d}.{at.microsecond // 1000:03d}Z"


def now_iso() -> str:
    return iso(now_ms())


def parse_iso(text: str) -> int | None:
    match = _ISO.fullmatch(text)
    if match is None:
        return None
    year, month, day, hour, minute, second, fraction, zone = match.groups()
    has_time = hour is not None
    m = int(month or 1)
    d = int(day or 1)
    h = int(hour or 0)
    mi = int(minute or 0)
    s = int(second or 0)
    ms = int((fraction or "")[:3].ljust(3, "0"))
    if not (1 <= m <= 12 and 1 <= d <= 31 and mi <= 59 and s <= 59):
        return None
    if h > 24 or (h == 24 and (mi or s or ms)):
        return None
    try:
        start = datetime(int(year), m, 1, tzinfo=timezone.utc)
    except ValueError:
        return None
    wall = start + timedelta(days=d - 1, hours=h, minutes=mi, seconds=s, milliseconds=ms)
    if zone is None and has_time:
        seconds = time.mktime((wall.year, wall.month, wall.day, wall.hour, wall.minute, wall.second, 0, 0, -1))
        return int(seconds) * 1000 + ms
    offset = timedelta(0)
    if zone is not None and zone not in ("Z", "z"):
        sign = -1 if zone[0] == "-" else 1
        offset = sign * timedelta(hours=int(zone[1:3]), minutes=int(zone[4:6]))
    at = wall - offset
    return (at - _EPOCH) // timedelta(milliseconds=1)

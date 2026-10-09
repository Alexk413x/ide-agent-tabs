from __future__ import annotations

import contextlib
import os
import re
from typing import Callable, NamedTuple, Union

from .clock import now_ms
from .files import mtime_ms, read_bytes
from .jsjson import is_finite, is_number, is_safe_integer, js_string, parse
from .liveness import pid_alive

PROTOCOL_VERSION = 1
ENDPOINTS_DIR = "endpoints"
ENDPOINT_BEATS_MISSED = 5

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
_JSON_SUFFIX = re.compile(r"\.json\Z", re.IGNORECASE)
_TRAILING_SLASHES = re.compile(r"/+\Z")
_URL = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*)://(?:[^@/?#]*@)?(\[[0-9A-Fa-f:.]*\]|[^/?#:\[\]]*)(?::(\d*))?([/?#].*)?", re.DOTALL)


class Endpoint(NamedTuple):
    id: str
    file: str
    ide: str
    product: str
    version: str
    pid: int
    url: str
    token: str
    started_at: float
    beat_ms: int | None = None


class Skip(NamedTuple):
    reason: str
    warn: bool


Parsed = Union[Endpoint, Skip]


class Registry(NamedTuple):
    endpoints: list[Endpoint]
    warnings: list[str]


class SplitUrl(NamedTuple):
    scheme: str
    host: str
    port: int
    path: str


def split_url(url: str) -> SplitUrl | None:
    match = _URL.fullmatch(url.strip())
    if match is None:
        return None
    scheme, host, port, rest = match.groups()
    scheme = scheme.lower()
    host = host.lower()
    if host.startswith("["):
        host = host[1:-1]
    number = int(port) if port else (443 if scheme == "https" else 80)
    if number > 65535:
        return None
    return SplitUrl(scheme, host, number, rest or "")


def is_loopback_url(url: str) -> bool:
    parts = split_url(url)
    return parts is not None and parts.scheme in ("http", "https") and parts.host in _LOOPBACK_HOSTS


def _str(obj: dict[str, object], key: str) -> str | None:
    value = obj.get(key)
    return value if isinstance(value, str) else None


def parse_endpoint(text: str, file: str, started_at: float) -> Parsed:
    name = os.path.basename(file)
    try:
        value = parse(text)
    except (ValueError, RecursionError):
        return Skip(f"{name} is not JSON", True)
    if not isinstance(value, dict):
        return Skip(f"{name} must hold a JSON object", True)
    protocol = value.get("protocol")
    if not (is_number(protocol) and protocol == PROTOCOL_VERSION):
        shown = "undefined" if "protocol" not in value else js_string(protocol)
        return Skip(f"{name} uses protocol {shown}", False)
    ide = _str(value, "ide")
    url = _str(value, "url")
    token = _str(value, "token")
    pid = _positive_int(value.get("pid"))
    if not ide or not url or not token or pid is None:
        return Skip(f"{name} lacks ide, url, token or pid", True)
    if not is_loopback_url(url):
        return Skip(f"{name} has a non-loopback url", True)
    stamp = value.get("startedAt")
    product = _str(value, "product")
    return Endpoint(
        id=_JSON_SUFFIX.sub("", name),
        file=file,
        ide=ide,
        product=product if product is not None else ide,
        version=_str(value, "version") or "",
        pid=pid,
        url=_TRAILING_SLASHES.sub("", url),
        token=token,
        started_at=stamp if isinstance(stamp, (int, float)) and is_finite(stamp) and stamp > 0 else started_at,
        beat_ms=_positive_int(value.get("beatMs")),
    )


def _positive_int(value: object) -> int | None:
    if isinstance(value, (int, float)) and is_safe_integer(value) and value > 0:
        return int(value)
    return None


def read_registry(home: str, alive: Callable[[int], bool] = pid_alive, now: float | None = None) -> Registry:
    now = now_ms() if now is None else now
    folder = os.path.join(home, ENDPOINTS_DIR)
    try:
        names = os.listdir(folder)
    except OSError:
        return Registry([], [])
    endpoints: list[Endpoint] = []
    warnings: list[str] = []
    for name in sorted(n for n in names if n.endswith(".json")):
        file = os.path.join(folder, name)
        try:
            text = read_bytes(file).decode("utf-8", "replace")
        except OSError:
            continue
        mtime = mtime_ms(file)
        if mtime is None:
            continue
        parsed = parse_endpoint(text, file, mtime)
        if isinstance(parsed, Skip):
            if parsed.warn:
                warnings.append(f"Skipping {parsed.reason}")
            continue
        # A pid alone can name a new process once Windows reuses it; an IDE that beats proves it still runs.
        silent = parsed.beat_ms is not None and now - mtime > parsed.beat_ms * ENDPOINT_BEATS_MISSED
        if silent or not alive(parsed.pid):
            with contextlib.suppress(OSError):
                os.remove(file)
            continue
        endpoints.append(parsed)
    return Registry(endpoints, warnings)

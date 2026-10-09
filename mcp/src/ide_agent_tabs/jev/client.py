from __future__ import annotations

import functools
import math
import os
import re
import sys
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Callable, Union

from ..jsjson import JS_SPACE, number, parse, stringify, trim, utf16_len, utf16_slice
from .settings import JEV_MODEL

MAX_CHOICE_OPTIONS = 255
MIN_SCORE_LEVELS = 2
MAX_SCORE_LEVELS = 10
MAX_REQUEST_CHARS = 200_000
CALL_LIMIT_MS = 30_000
ATTEMPT_TIMEOUT_MS = 10_000
MAX_RETRIES = 2
BACKOFF_INITIAL_MS = 500
BACKOFF_MAX_MS = 5_000
BACKOFF_JITTER = 0.25
MAX_RETRY_AFTER_MS = 60_000
SDK_VERSION = "0.6.0"
DEFAULT_BASE_URL = "https://api.typesafe.ai"
BASE_URL_ENV = "TYPESAFE_BASE_URL"
SYSTEM_ONE_PATH = "/v1/systemone"
_MAX_RAW_BODY_IN_MESSAGE = 200
_READ_CHUNK = 65536

if TYPE_CHECKING:
    import ssl
    from email.message import Message

Status = Union[int, str]


class JevError(Exception):
    def __init__(self, message: str, status: Status | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def check_request(state: Any, questions: dict[str, Any]) -> None:
    if not questions:
        raise JevError("A Jev request needs at least one question.")
    for name, q in questions.items():
        kind = q.get("type") if isinstance(q, dict) else None
        criteria = q.get("criteria") if isinstance(q, dict) else None
        if kind == "choice":
            count = len(criteria) if isinstance(criteria, dict) else 0
            if count < 2 or count > MAX_CHOICE_OPTIONS:
                raise JevError(f'Choice question "{name}" has {count} options; Jev takes 2 to {MAX_CHOICE_OPTIONS}.')
        elif kind == "score":
            count = len(criteria) if isinstance(criteria, list) else 0
            if count < MIN_SCORE_LEVELS or count > MAX_SCORE_LEVELS:
                raise JevError(f'Score question "{name}" has {count} levels; Jev takes {MIN_SCORE_LEVELS} to {MAX_SCORE_LEVELS}.')
    chars = utf16_len(stringify({"state": state, "questions": questions}))
    if chars > MAX_REQUEST_CHARS:
        raise JevError(f"The request is {chars} characters; Jev takes at most {MAX_REQUEST_CHARS}. Send less state.")


def _scrub(text: str, key: str) -> str:
    return text if key == "" else text.replace(key, "***")


def _js_string(value: Any) -> str:
    if value is None:
        return ""
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return number(value) if math.isfinite(value) else ("NaN" if math.isnan(value) else ("Infinity" if value > 0 else "-Infinity"))
    if isinstance(value, list):
        return ",".join(_js_string(v) for v in value)
    return "[object Object]"


def _validation_errors(errors: list[Any]) -> str | None:
    parts: list[str] = []
    for e in errors:
        if not isinstance(e, dict) or not isinstance(e.get("msg"), str):
            continue
        loc = e.get("loc")
        where = ".".join(_js_string(x) for x in loc if x != "body") if isinstance(loc, list) else ""
        parts.append(f"{where}: {e['msg']}" if where else e["msg"])
    return "; ".join(parts) if parts else None


def _extract_message(body: Any) -> str | None:
    if isinstance(body, str):
        return body or None
    if not isinstance(body, dict):
        return None
    error = body.get("error")
    message = body.get("message")
    detail = body.get("detail")
    if isinstance(error, str):
        return error
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        return error["message"]
    if isinstance(message, str):
        return message
    if isinstance(detail, str):
        return detail
    if isinstance(detail, dict) and isinstance(detail.get("message"), str):
        return detail["message"]
    if isinstance(detail, list):
        return _validation_errors(detail)
    return None


class _Missing:
    pass


NO_BODY: Any = _Missing()


def describe_error(status: int, body: Any) -> str:
    detail = _extract_message(body)
    if detail:
        return f"{status} {detail}"
    if body is NO_BODY:
        return f"{status} status code (no body)"
    raw = body if isinstance(body, str) else stringify(body)
    return f"{status} {utf16_slice(raw, 0, _MAX_RAW_BODY_IN_MESSAGE)}…" if utf16_len(raw) > _MAX_RAW_BODY_IN_MESSAGE else f"{status} {raw}"


def _detail(status: int, body: Any) -> str:
    text = re.sub(f"^{status}[{re.escape(JS_SPACE)}]*", "", describe_error(status, body), count=1)
    return "" if text == "" else f": {text}"


def http_message(status: int, body: Any) -> str:
    if status == 401:
        return "TypeSafe rejected the API key (HTTP 401). Check the key."
    if status == 403:
        return "TypeSafe refused access for this API key (HTTP 403)."
    if status in (400, 422):
        return f"TypeSafe refused the request as invalid (HTTP {status}){_detail(status, body)}"
    if status == 429:
        return "TypeSafe rate-limited the request (HTTP 429). Try again later."
    if status == 529:
        return "TypeSafe is overloaded (HTTP 529). Try again later."
    return f"TypeSafe answered HTTP {status}{_detail(status, body)}"


def timeout_error() -> JevError:
    return JevError(f"Jev did not answer within {CALL_LIMIT_MS // 1000} s.", "timeout")


_RADIX = {"x": (16, "[0-9a-fA-F]+"), "o": (8, "[0-7]+"), "b": (2, "[01]+")}


def js_to_number(text: str) -> float:
    s = trim(text)
    if s == "":
        return 0.0
    if len(s) > 2 and s[0] == "0" and s[1].lower() in _RADIX:
        base, digits = _RADIX[s[1].lower()]
        if not re.fullmatch(digits, s[2:]):
            return math.nan
        try:
            return float(int(s[2:], base))
        except OverflowError:
            return math.inf
    if re.fullmatch(r"[+-]?Infinity", s):
        return -math.inf if s[0] == "-" else math.inf
    if not re.fullmatch(r"[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", s):
        return math.nan
    return float(s)


def _header(headers: Message, name: str) -> str | None:
    values = headers.get_all(name)
    return None if values is None else ", ".join(values)


def parse_retry_after(headers: Message, now_ms: float) -> float | None:
    raw_ms = _header(headers, "retry-after-ms")
    if raw_ms is not None:
        ms = js_to_number(raw_ms)
        if math.isfinite(ms) and ms >= 0:
            return ms
    raw = _header(headers, "retry-after")
    if raw is None:
        return None
    seconds = js_to_number(raw)
    if math.isfinite(seconds):
        return seconds * 1000 if seconds >= 0 else None
    from email.utils import parsedate_to_datetime

    try:
        at = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        return None
    if at is None or at.tzinfo is None:
        return None
    return max(0.0, at.timestamp() * 1000 - now_ms)


def _random() -> float:
    import random

    return random.random()


def retry_delay_ms(attempt: int, headers: Message | None, rand: Callable[[], float] = _random) -> float:
    if headers is not None:
        after = parse_retry_after(headers, time.time() * 1000)
        if after is not None and after <= MAX_RETRY_AFTER_MS:
            return after
    exponential = min(BACKOFF_INITIAL_MS * 2**attempt, BACKOFF_MAX_MS)
    return math.floor(exponential * (1 - rand() * BACKOFF_JITTER) + 0.5)


def parse_body(raw: bytes) -> Any:
    text = raw.decode("utf-8", "replace")
    text = text.removeprefix("\N{ZERO WIDTH NO-BREAK SPACE}")
    if text == "":
        return NO_BODY
    try:
        return parse(text)
    except ValueError:
        return text


def _node_arch() -> str:
    import platform

    machine = platform.machine().lower()
    return {"amd64": "x64", "x86_64": "x64", "aarch64": "arm64", "arm64": "arm64", "x86": "ia32", "i386": "ia32", "i686": "ia32"}.get(
        machine, machine
    )


def runtime_header() -> str:
    v = sys.version_info
    return f"python/{v.major}.{v.minor}.{v.micro} ({sys.platform}; {_node_arch()})"


def base_url_of(env: Mapping[str, str]) -> str:
    given = env.get(BASE_URL_ENV) or None
    return re.sub(r"/+$", "", given or DEFAULT_BASE_URL)


@functools.cache
def _tls_context() -> ssl.SSLContext:
    import ssl

    context = ssl.create_default_context()
    # python.org builds for macOS ship without CA certificates until the user runs "Install Certificates.command";
    # the system bundle at /etc/ssl/cert.pem covers that case.
    if sys.platform == "darwin" and context.cert_store_stats().get("x509_ca", 0) == 0 and os.path.exists("/etc/ssl/cert.pem"):
        context.load_verify_locations("/etc/ssl/cert.pem")
    return context


class _AttemptTimeout(Exception):
    pass


class _Response:
    def __init__(self, status: int, headers: Message, raw: bytes) -> None:
        self.status = status
        self.headers = headers
        self.raw = raw


def _attempt(url: str, method: str, headers: dict[str, str], body: bytes, timeout_s: float) -> _Response:
    import http.client
    import socket
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    host = parts.hostname or ""
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    ends = time.monotonic() + timeout_s
    if parts.scheme == "https":
        conn: http.client.HTTPConnection = http.client.HTTPSConnection(host, parts.port, timeout=timeout_s, context=_tls_context())
    elif parts.scheme == "http":
        conn = http.client.HTTPConnection(host, parts.port, timeout=timeout_s)
    else:
        raise OSError(f"unsupported URL scheme {parts.scheme!r}")
    try:
        conn.connect()
        sock = conn.sock
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        chunks: list[bytes] = []
        while True:
            left = ends - time.monotonic()
            if left <= 0:
                raise _AttemptTimeout()
            if sock is not None:
                sock.settimeout(left)
            chunk = response.read(_READ_CHUNK)
            if not chunk:
                break
            chunks.append(chunk)
        return _Response(response.status, response.msg, b"".join(chunks))
    except socket.timeout as e:
        raise _AttemptTimeout() from e
    finally:
        conn.close()


Transport = Callable[[str, str, dict[str, str], bytes, float], _Response]


def call_jev(
    api_key: str,
    base_url: str,
    state: Any,
    questions: dict[str, Any],
    *,
    transport: Transport = _attempt,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    rand: Callable[[], float] = _random,
    limit_ms: float = CALL_LIMIT_MS,
    attempt_ms: float = ATTEMPT_TIMEOUT_MS,
) -> dict[str, Any]:
    try:
        return _call(api_key, base_url, state, questions, transport, sleep, clock, rand, limit_ms, attempt_ms)
    except JevError as e:
        raise JevError(_scrub(e.message, api_key), e.status) from None
    except Exception as e:  # noqa: BLE001
        raise JevError(_scrub(f"Jev request failed: {e}", api_key), "error") from None


def _call(
    api_key: str,
    base_url: str,
    state: Any,
    questions: dict[str, Any],
    transport: Transport,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
    rand: Callable[[], float],
    limit_ms: float,
    attempt_ms: float,
) -> dict[str, Any]:
    import http.client

    url = base_url + SYSTEM_ONE_PATH
    body = stringify({"state": state, "questions": questions, "model": JEV_MODEL}).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "User-Agent": f"typesafe-sdk/{SDK_VERSION}",
        "X-TypeSafe-SDK": f"typesafe-sdk/{SDK_VERSION}",
        "X-TypeSafe-Runtime": runtime_header(),
        "Content-Type": "application/json",
    }
    deadline = clock() + limit_ms / 1000
    attempt = 0
    while True:
        retries_left = MAX_RETRIES - attempt
        left = deadline - clock()
        if left <= 0:
            raise timeout_error()
        per_attempt = min(attempt_ms / 1000, left)
        sent = headers if attempt == 0 else {**headers, "X-TypeSafe-Retry-Count": str(attempt)}
        try:
            response = transport(url, "POST", sent, body, per_attempt)
        except _AttemptTimeout:
            if per_attempt < attempt_ms / 1000 or retries_left <= 0:
                raise timeout_error() from None
            _back_off(attempt, None, deadline, sleep, clock, rand)
            attempt += 1
            continue
        except (OSError, http.client.HTTPException):
            if retries_left <= 0:
                raise JevError("Could not reach TypeSafe: Connection error: fetch failed", "connection") from None
            _back_off(attempt, None, deadline, sleep, clock, rand)
            attempt += 1
            continue
        if 200 <= response.status < 300:
            result = parse_body(response.raw)
            return result if isinstance(result, dict) else {}
        error_body = parse_body(response.raw)
        if retries_left <= 0 or not (response.status in (408, 429) or 500 <= response.status < 600):
            raise JevError(http_message(response.status, error_body), response.status)
        _back_off(attempt, response.headers, deadline, sleep, clock, rand)
        attempt += 1


def _back_off(
    attempt: int,
    headers: Message | None,
    deadline: float,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
    rand: Callable[[], float],
) -> None:
    delay = retry_delay_ms(attempt, headers, rand) / 1000
    if clock() + delay >= deadline:
        raise timeout_error()
    sleep(delay)

from __future__ import annotations

import json
import math
import re
from typing import Any

MAX_SAFE_INTEGER = 2**53 - 1
_MAX_ARRAY_INDEX = 2**32 - 2

_ESCAPES = {'"': '\\"', "\\": "\\\\", "\b": "\\b", "\f": "\\f", "\n": "\\n", "\r": "\\r", "\t": "\\t"}
_SPECIAL = re.compile('[\ud800-\udbff][\udc00-\udfff]|["\\\\\x00-\x1f\ud800-\udfff]')


def _escape(match: re.Match[str]) -> str:
    text = match.group()
    if len(text) == 2:
        return chr(0x10000 + ((ord(text[0]) - 0xD800) << 10) + (ord(text[1]) - 0xDC00))
    known = _ESCAPES.get(text)
    return known if known is not None else f"\\u{ord(text):04x}"


def quote(text: str) -> str:
    return '"' + _SPECIAL.sub(_escape, text) + '"'


def number(value: float) -> str:
    if isinstance(value, int):
        if -MAX_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER:
            return str(value)
        try:
            value = float(value)
        except OverflowError:
            return "null"
    if math.isnan(value) or math.isinf(value):
        return "null"
    if value == 0:
        return "0"
    mantissa, _, exponent = repr(abs(value)).partition("e")
    whole, _, fraction = mantissa.partition(".")
    all_digits = whole + fraction
    stripped = all_digits.lstrip("0")
    point = len(whole) + (int(exponent) if exponent else 0) - (len(all_digits) - len(stripped))
    digits = stripped.rstrip("0")
    k = len(digits)
    if k <= point <= 21:
        text = digits + "0" * (point - k)
    elif 0 < point <= 21:
        text = f"{digits[:point]}.{digits[point:]}"
    elif -6 < point <= 0:
        text = "0." + "0" * -point + digits
    else:
        e = point - 1
        sign = "+" if e >= 0 else "-"
        text = (digits if k == 1 else f"{digits[0]}.{digits[1:]}") + f"e{sign}{abs(e)}"
    return "-" + text if value < 0 else text


def _is_index(key: str) -> bool:
    return key.isdigit() and key.isascii() and (key == "0" or key[0] != "0") and int(key) <= _MAX_ARRAY_INDEX


def _ordered_keys(obj: dict[Any, Any]) -> list[tuple[str, Any]]:
    items: list[tuple[str, Any]] = []
    for key, value in obj.items():
        if isinstance(key, bool) or not isinstance(key, (str, int)):
            raise TypeError(f"object keys must be strings, not {type(key).__name__}")
        items.append((str(key), value))
    indices = sorted((item for item in items if _is_index(item[0])), key=lambda item: int(item[0]))
    return indices + [item for item in items if not _is_index(item[0])]


def _write(value: Any, indent: str, step: str, out: list[str]) -> None:
    if value is None:
        out.append("null")
    elif value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif isinstance(value, str):
        out.append(quote(value))
    elif isinstance(value, (int, float)):
        out.append(number(value))
    elif isinstance(value, (list, tuple)):
        if not value:
            out.append("[]")
            return
        inner = indent + step
        out.append("[")
        for i, item in enumerate(value):
            if i:
                out.append(",")
            if step:
                out.append("\n" + inner)
            _write(item, inner, step, out)
        if step:
            out.append("\n" + indent)
        out.append("]")
    elif isinstance(value, dict):
        if not value:
            out.append("{}")
            return
        inner = indent + step
        out.append("{")
        for i, (key, item) in enumerate(_ordered_keys(value)):
            if i:
                out.append(",")
            if step:
                out.append("\n" + inner)
            out.append(quote(key) + (": " if step else ":"))
            _write(item, inner, step, out)
        if step:
            out.append("\n" + indent)
        out.append("}")
    else:
        raise TypeError(f"{type(value).__name__} is not JSON serializable")


def stringify(value: Any, indent: int = 0) -> str:
    out: list[str] = []
    _write(value, "", " " * min(indent, 10), out)
    return "".join(out)


def _no_constant(name: str) -> Any:
    raise ValueError(f"{name} is not valid JSON")


def parse(text: str) -> Any:
    return json.loads(text, parse_constant=_no_constant)


def utf16_len(text: str) -> int:
    return len(text) + sum(1 for c in text if ord(c) > 0xFFFF)


def _units(text: str) -> bytes:
    return text.encode("utf-16-le", "surrogatepass")


def utf16_slice(text: str, start: int, end: int | None = None) -> str:
    units = _units(text)
    length = len(units) // 2

    def clamp(i: int) -> int:
        return max(0, length + i) if i < 0 else min(i, length)

    first = clamp(start)
    last = length if end is None else clamp(end)
    if last <= first:
        return ""
    return units[2 * first : 2 * last].decode("utf-16-le", "surrogatepass")


def well_formed(text: str) -> str:
    return _units(text).decode("utf-16-le", "replace")

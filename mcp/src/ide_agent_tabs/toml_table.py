from __future__ import annotations

import re
from typing import Union

from .jsjson import parse, quote

TomlValue = Union[str, int, "list[str]", "dict[str, str]"]
_EOL = re.compile(r"\r?\n")
_HEADER_LINE = re.compile(r"^\s*\[")
_TRAILING = re.compile(r"^\s*(?:#.*)?$")
_KEY_LINE = re.compile(r"^\s*([A-Za-z0-9_-]+)\s*=", re.MULTILINE)


def _key(k: str) -> str:
    e = re.escape(k)
    return f"(?:{e}|\"{e}\"|'{e}')"


def toml_header(section: str, name: str) -> re.Pattern[str]:
    return re.compile(rf"^\s*\[\s*{_key(section)}\s*\.\s*{_key(name)}\s*(\.[^\]]*)?\]\s*(?:#.*)?$")


def split_lines(text: str) -> list[str]:
    return _EOL.split(text)


def toml_blocks(lines: list[str], section: str, name: str) -> list[tuple[int, int]]:
    header = toml_header(section, name)
    blocks: list[tuple[int, int]] = []
    i = 0
    while i < len(lines):
        if not header.search(lines[i]):
            i += 1
            continue
        end = i + 1
        while end < len(lines) and not _HEADER_LINE.search(lines[end]):
            end += 1
        while end > i + 1 and _TRAILING.search(lines[end - 1]):
            end -= 1
        blocks.append((i, end))
        i = end
    return blocks


def toml_string(s: str) -> str:
    return quote(s)


def _value(v: TomlValue) -> str:
    if isinstance(v, int):
        return str(v)
    if isinstance(v, str):
        return toml_string(v)
    if isinstance(v, list):
        return f"[{', '.join(toml_string(s) for s in v)}]"
    return "{ " + ", ".join(f"{k} = {toml_string(s)}" for k, s in v.items()) + " }"


def toml_table(section: str, name: str, values: dict[str, TomlValue]) -> list[str]:
    return [f"[{section}.{name}]", *(f"{k} = {_value(v)}" for k, v in values.items())]


# Only the [section.name] form can be found and replaced; any other spelling of the same table would be a
# duplicate key once a second one is added.
def with_toml_table(text: str | None, file: str, section: str, name: str, table: list[str] | None) -> str | None:
    source = text or ""
    eol = "\r\n" if "\r\n" in source else "\n"
    lines = split_lines(source)
    blocks = toml_blocks(lines, section, name)
    inside = {i for s, e in blocks for i in range(s, e)}
    kept = [line for i, line in enumerate(lines) if i not in inside]
    other = re.compile(rf"(?:^|[\s.\[]){_key(name)}\s*(?:[.=\]])")
    if any(not re.match(r"^\s*#", line) and other.search(line) for line in kept):
        raise ValueError(f"{file} names {name} in a form other than a [{section}.{name}] table; edit it by hand")
    if blocks:
        out: list[str] = []
        starts = {s: (s, e) for s, e in blocks}
        i = 0
        while i < len(lines):
            block = starts.get(i)
            if block is None:
                out.append(lines[i])
                i += 1
                continue
            if table is not None and block == blocks[0]:
                out.extend(table)
            elif out and out[-1].strip() == "" and (block[1] >= len(lines) or lines[block[1]].strip() == ""):
                out.pop()
            i = block[1]
        joined = eol.join(out)
        return None if joined == source else joined
    if table is None:
        return None
    body = re.sub(r"(?:\r?\n)+\Z", "", source)
    return f"{body}{'' if body == '' else eol + eol}{eol.join(table)}{eol}"


def _parse_string(src: str, i: int) -> tuple[str, int] | None:
    if i < len(src) and src[i] == "'":
        close = src.find("'", i + 1)
        return None if close < 0 else (src[i + 1 : close], close + 1)
    if i >= len(src) or src[i] != '"':
        return None
    j = i + 1
    while j < len(src) and src[j] != '"':
        j += 2 if src[j] == "\\" else 1
    try:
        value = parse(src[i : j + 1])
    except ValueError:
        return None
    return (value, j + 1) if isinstance(value, str) else None


def _skip_space(src: str, i: int) -> int:
    while i < len(src):
        if src[i].isspace() or src[i] == ",":
            i += 1
        elif src[i] == "#":
            while i < len(src) and src[i] != "\n":
                i += 1
        else:
            break
    return i


def _parse_value(src: str, i: int) -> str | list[str] | None:
    i = _skip_space(src, i)
    if i >= len(src) or src[i] != "[":
        found = _parse_string(src, i)
        return None if found is None else found[0]
    items: list[str] = []
    i = _skip_space(src, i + 1)
    while i < len(src) and src[i] != "]":
        item = _parse_string(src, i)
        if item is None:
            return None
        items.append(item[0])
        i = _skip_space(src, item[1])
    return items


def read_toml_table(text: str | None, section: str, name: str) -> dict[str, str | list[str] | None] | None:
    if text is None:
        return None
    lines = split_lines(text)
    header = toml_header(section, name)
    main = next(
        (b for b in toml_blocks(lines, section, name) if (m := header.search(lines[b[0]])) is not None and m.group(1) is None), None
    )
    if main is None:
        return None
    body = "\n".join(lines[main[0] + 1 : main[1]])
    return {m.group(1): _parse_value(body, m.end()) for m in _KEY_LINE.finditer(body)}

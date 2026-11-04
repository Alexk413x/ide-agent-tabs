from __future__ import annotations

import re
from typing import Any, Callable, NamedTuple

_EOL = re.compile(r"\r?\n")
_INDICATORS = set(",[]{}#&*!|>'\"%@`")
_NULL = re.compile(r"(?:~|null|Null|NULL|)\Z")
_TRUE = re.compile(r"(?:true|True|TRUE)\Z")
_FALSE = re.compile(r"(?:false|False|FALSE)\Z")
_INT = re.compile(r"[-+]?[0-9]+\Z")
_OCT = re.compile(r"0o[0-7]+\Z")
_HEX = re.compile(r"0x[0-9a-fA-F]+\Z")
_FLOAT = re.compile(r"[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)(?:[eE][-+]?[0-9]+)?\Z")
_INF = re.compile(r"[-+]?\.(?:inf|Inf|INF)\Z")
_NAN = re.compile(r"\.(?:nan|NaN|NAN)\Z")
_DOUBLE_ESCAPES = {
    "0": "\0", "a": "\a", "b": "\b", "t": "\t", "\t": "\t", "n": "\n", "v": "\v", "f": "\f", "r": "\r", "e": "\x1b",
    " ": " ", '"': '"', "/": "/", "\\": "\\", "N": "\x85", "_": "\xa0", "L": " ", "P": " ",
}  # fmt: skip
_HEX_ESCAPES = {"x": 2, "u": 4, "U": 8}
_NEEDS_DOUBLE = re.compile("[\x00-\x08\x0b-\x1f\x7f\x85  ]")


class YamlError(ValueError):
    pass


class _Unsupported(Exception):
    def __init__(self, what: str, line: int) -> None:
        super().__init__(what)
        self.what = what
        self.line = line


class Node(NamedTuple):
    kind: str
    value: Any
    items: list[Item]
    start: int
    end: int
    indent: int


class Item(NamedTuple):
    key: Any
    node: Node | None
    start: int
    end: int
    indent: int


def _resolve(plain: str) -> Any:
    if _NULL.match(plain):
        return None
    if _TRUE.match(plain):
        return True
    if _FALSE.match(plain):
        return False
    if _INT.match(plain):
        return int(plain)
    if _OCT.match(plain) or _HEX.match(plain):
        return int(plain, 0)
    if _FLOAT.match(plain):
        return float(plain)
    if _INF.match(plain):
        return float("-inf") if plain.startswith("-") else float("inf")
    if _NAN.match(plain):
        return float("nan")
    return plain


def _is_plain_start(text: str) -> bool:
    if not text:
        return False
    if text[0] in "-?:":
        return len(text) > 1 and text[1] not in " \t"
    return text[0] not in _INDICATORS


def _double_quoted(text: str, i: int, line: int) -> tuple[str, int]:
    out: list[str] = []
    j = i + 1
    while j < len(text):
        c = text[j]
        if c == '"':
            return "".join(out), j + 1
        if c != "\\":
            out.append(c)
            j += 1
            continue
        e = text[j + 1 : j + 2]
        if e in _DOUBLE_ESCAPES:
            out.append(_DOUBLE_ESCAPES[e])
            j += 2
        elif e in _HEX_ESCAPES:
            digits = text[j + 2 : j + 2 + _HEX_ESCAPES[e]]
            if len(digits) != _HEX_ESCAPES[e] or not re.fullmatch(r"[0-9a-fA-F]+", digits):
                raise _Unsupported("a bad escape in a double-quoted string", line)
            out.append(chr(int(digits, 16)))
            j += 2 + len(digits)
        else:
            raise _Unsupported("a bad escape in a double-quoted string", line)
    raise _Unsupported("a quoted string that continues on the next line", line)


def _single_quoted(text: str, i: int, line: int) -> tuple[str, int]:
    out: list[str] = []
    j = i + 1
    while j < len(text):
        if text[j] == "'":
            if text[j + 1 : j + 2] == "'":
                out.append("'")
                j += 2
                continue
            return "".join(out), j + 1
        out.append(text[j])
        j += 1
    raise _Unsupported("a quoted string that continues on the next line", line)


def _rest_is_blank(text: str, j: int, line: int) -> None:
    rest = text[j:]
    if rest.strip() and not re.match(r"[ \t]+#", rest):
        raise _Unsupported("text after a quoted string", line)


def _flow_scalar(text: str, line: int) -> Any:
    item = text.strip()
    if item.startswith('"'):
        value, end = _double_quoted(item, 0, line)
    elif item.startswith("'"):
        value, end = _single_quoted(item, 0, line)
    else:
        if not item or item[0] in _INDICATORS:
            raise _Unsupported("a nested or empty item in a flow collection", line)
        return _resolve(item)
    if item[end:].strip():
        raise _Unsupported("text after a quoted string", line)
    return value


def _split_flow(body: str, line: int) -> list[str]:
    parts: list[str] = []
    current = ""
    quote = ""
    for c in body:
        if quote:
            current += c
            if c == quote:
                quote = ""
            continue
        if c in "\"'":
            quote = c
            current += c
        elif c in "[]{}":
            raise _Unsupported("a nested flow collection", line)
        elif c == ",":
            parts.append(current)
            current = ""
        else:
            current += c
    if quote:
        raise _Unsupported("an unclosed quote in a flow collection", line)
    parts.append(current)
    if parts and not parts[-1].strip():
        parts.pop()
    return parts


def _flow(text: str, line: int) -> tuple[str, Any]:
    close = "]" if text[0] == "[" else "}"
    end = text.rfind(close)
    if end < 0:
        raise _Unsupported("a flow collection that continues on the next line", line)
    _rest_is_blank(text, end + 1, line)
    parts = _split_flow(text[1:end], line)
    if close == "]":
        return "seq", [_flow_scalar(p, line) for p in parts]
    mapping: dict[Any, Any] = {}
    for part in parts:
        key, sep, value = part.partition(": ")
        if not sep:
            raise _Unsupported("a flow mapping entry without ': '", line)
        mapping[_flow_scalar(key, line)] = _flow_scalar(value, line)
    return "map", mapping


def _inline(text: str, line: int) -> tuple[str, Any]:
    if text.startswith('"'):
        value, end = _double_quoted(text, 0, line)
        _rest_is_blank(text, end, line)
        return "scalar", value
    if text.startswith("'"):
        value, end = _single_quoted(text, 0, line)
        _rest_is_blank(text, end, line)
        return "scalar", value
    if text[0] in "[{":
        return _flow(text, line)
    if text[0] in "|>":
        raise _Unsupported("a block scalar", line)
    if text[0] in "&*!":
        raise _Unsupported("an anchor, alias or tag", line)
    if not _is_plain_start(text):
        raise _Unsupported(f"a value starting with {text[0]}", line)
    plain = re.split(r"[ \t]+#", text, maxsplit=1)[0].rstrip()
    if re.search(r":(?:[ \t]|\Z)", plain):
        raise _Unsupported("a value holding ': '", line)
    return "scalar", _resolve(plain)


def _key_of(text: str, line: int) -> tuple[Any, str] | None:
    if text.startswith('"'):
        key, end = _double_quoted(text, 0, line)
    elif text.startswith("'"):
        key, end = _single_quoted(text, 0, line)
    else:
        if text.startswith("? ") or text == "?":
            raise _Unsupported("a complex key", line)
        match = re.match(r"(.*?):(?:[ \t]|\Z)", text)
        if match is None or not _is_plain_start(text) or " #" in match.group(1):
            return None
        return _resolve(match.group(1).rstrip()), text[match.end() :]
    if not re.match(r"[ \t]*:(?:[ \t]|\Z)", text[end:]):
        return None
    colon = text.index(":", end)
    return key, text[colon + 1 :]


def _is_dash(text: str) -> bool:
    return text == "-" or text.startswith(("- ", "-\t"))


def _is_trivia(line: str) -> bool:
    stripped = line.strip()
    return not stripped or stripped.startswith("#")


def _indent_of(line: str, number: int) -> int:
    indent = len(line) - len(line.lstrip(" "))
    if line[indent : indent + 1] == "\t":
        raise _Unsupported("a tab in the indentation", number)
    return indent


class _Parser:
    def __init__(self, lines: list[str], start: int, end: int) -> None:
        self.lines = lines
        self.toks = [(i, _indent_of(lines[i], i)) for i in range(start, end) if not _is_trivia(lines[i])]
        self.pos = 0
        self.last = start

    def peek(self) -> tuple[int, int] | None:
        return self.toks[self.pos] if self.pos < len(self.toks) else None

    def text(self, tok: tuple[int, int]) -> str:
        return self.lines[tok[0]][tok[1] :]

    def take(self, tok: tuple[int, int], synthetic: bool) -> None:
        if not synthetic:
            self.pos += 1
        self.last = tok[0]

    def no_deeper(self, col: int) -> None:
        nxt = self.peek()
        if nxt is not None and nxt[1] > col:
            raise _Unsupported("a value that continues on the next line", nxt[0])

    def node(self, tok: tuple[int, int]) -> Node:
        text = self.text(tok)
        if _is_dash(text):
            return self.seq(tok[1])
        if _key_of(text, tok[0]) is not None:
            return self.map(tok[1])
        raise _Unsupported("a value that continues on the next line", tok[0])

    def child(self, col: int, in_map: bool) -> Node | None:
        nxt = self.peek()
        if nxt is None:
            return None
        if nxt[1] > col:
            return self.node(nxt)
        if in_map and nxt[1] == col and _is_dash(self.text(nxt)):
            return self.seq(col)
        return None

    def scalar(self, text: str, line: int, col: int) -> Node:
        kind, value = _inline(text, line)
        self.no_deeper(col)
        return Node(kind, value, [], line, line + 1, col)

    def map(self, col: int, first: tuple[int, int] | None = None) -> Node:
        items: list[Item] = []
        start = (first or self.toks[self.pos])[0]
        while True:
            tok = first or self.peek()
            if tok is None or tok[1] < col:
                break
            if tok[1] > col:
                raise _Unsupported("unexpected indentation", tok[0])
            text = self.text(tok)
            if _is_dash(text):
                break
            found = _key_of(text, tok[0])
            if found is None:
                raise _Unsupported("a line that is not a key: value pair", tok[0])
            self.take(tok, first is not None)
            first = None
            key, rest = found
            value = rest.strip()
            if value and not value.startswith("#"):
                node: Node | None = self.scalar(value, tok[0], col)
            else:
                node = self.child(col, True)
            if any(i.key == key for i in items):
                raise _Unsupported(f"the key {key} twice", tok[0])
            items.append(Item(key, node, tok[0], self.last + 1, col))
        return Node("map", {i.key: to_value(i.node) for i in items}, items, start, self.last + 1, col)

    def seq(self, col: int) -> Node:
        items: list[Item] = []
        start = self.toks[self.pos][0]
        while True:
            tok = self.peek()
            if tok is None or tok[1] < col:
                break
            if tok[1] > col:
                raise _Unsupported("unexpected indentation", tok[0])
            text = self.text(tok)
            if not _is_dash(text):
                break
            self.take(tok, False)
            rest = text[1:]
            inner = rest.lstrip(" \t")
            rest_col = tok[1] + 1 + len(rest) - len(inner)
            if not inner or inner.startswith("#"):
                node: Node | None = self.child(col, False)
            elif _is_dash(inner):
                raise _Unsupported("a list inside a list on one line", tok[0])
            elif _key_of(inner, tok[0]) is not None:
                node = self.map(rest_col, (tok[0], rest_col))
            else:
                node = self.scalar(inner, tok[0], col)
            items.append(Item(len(items), node, tok[0], self.last + 1, col))
        return Node("seq", [to_value(i.node) for i in items], items, start, self.last + 1, col)


def to_value(node: Node | None) -> Any:
    return None if node is None else node.value


def _plain_ok(text: str) -> bool:
    if not _is_plain_start(text) or text != text.strip(" \t") or text.endswith(":"):
        return False
    if ": " in text or " #" in text or ":\t" in text or "\t#" in text:
        return False
    return isinstance(_resolve(text), str)


def _double(text: str) -> str:
    out = []
    for c in text:
        if c in '\\"':
            out.append("\\" + c)
        elif _NEEDS_DOUBLE.match(c) or c == "\t":
            out.append(f"\\x{ord(c):02x}" if ord(c) < 0x100 else f"\\u{ord(c):04x}")
        else:
            out.append(c)
    return '"' + "".join(out) + '"'


def scalar_text(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value)
    if not isinstance(value, str):
        raise YamlError(f"can't write {type(value).__name__} as YAML")
    if "\n" in value or "\r" in value:
        raise YamlError("can't write a value with a line break")
    if value and not _NEEDS_DOUBLE.search(value) and _plain_ok(value):
        return value
    if '"' in value and "'" not in value and not _NEEDS_DOUBLE.search(value):
        return "'" + value.replace("'", "''") + "'"
    return _double(value)


def _inline_text(value: Any) -> str:
    if isinstance(value, dict) and not value:
        return "{}"
    if isinstance(value, list) and not value:
        return "[]"
    return scalar_text(value)


def render_pair(key: Any, value: Any, indent: int) -> list[str]:
    pad = " " * indent
    head = f"{pad}{scalar_text(key)}:"
    if isinstance(value, dict) and value:
        return [head, *render_map(value, indent + 2)]
    if isinstance(value, list) and value:
        return [head, *render_seq(value, indent + 2)]
    return [f"{head} {_inline_text(value)}"]


def render_map(value: dict[Any, Any], indent: int) -> list[str]:
    return [line for k, v in value.items() for line in render_pair(k, v, indent)]


def render_seq(value: list[Any], indent: int) -> list[str]:
    pad = " " * indent
    out: list[str] = []
    for item in value:
        if isinstance(item, dict) and item:
            lines = render_map(item, indent + 2)
            out.append(f"{pad}- {lines[0][indent + 2 :]}")
            out.extend(lines[1:])
        elif isinstance(item, list) and item:
            raise YamlError("can't write a list inside a list")
        else:
            out.append(f"{pad}- {_inline_text(item)}")
    return out


class _Section(NamedTuple):
    key: Any
    start: int
    end: int


class YamlDoc:
    def __init__(self, text: str | None, file: str) -> None:
        source = text or ""
        self.file = file
        self.eol = "\r\n" if "\r\n" in source else "\n"
        self.lines = _EOL.split(source)
        if self.lines and self.lines[-1] == "":
            self.lines.pop()
        self.touched: set[Any] = set()
        self.sections = self._top_level()

    def _unsupported(self, e: _Unsupported) -> YamlError:
        return YamlError(f"{self.file} uses YAML that Agent Tabs doesn't edit ({e.what} on line {e.line + 1}); edit it by hand")

    def _top_level(self) -> list[_Section]:
        sections: list[_Section] = []
        try:
            content = [i for i, line in enumerate(self.lines) if not _is_trivia(line)]
            if content and self.lines[content[0]].rstrip() == "---":
                content = content[1:]
            if content and _is_dash(self.lines[content[0]].lstrip(" ")):
                raise YamlError(f"{self.file} doesn't hold a YAML mapping; edit it by hand")
            tops = [i for i in content if _indent_of(self.lines[i], i) == 0 and not _is_dash(self.lines[i])]
            if content and content[0] not in tops:
                raise _Unsupported("unexpected indentation", content[0])
            for n, i in enumerate(tops):
                text = self.lines[i]
                if text.startswith(("---", "...", "%")):
                    raise _Unsupported("more than one document or a directive", i)
                found = _key_of(text, i)
                if found is None:
                    raise _Unsupported("a line that is not a key: value pair", i)
                if any(s.key == found[0] for s in sections):
                    raise _Unsupported(f"the key {found[0]} twice", i)
                nxt = tops[n + 1] if n + 1 < len(tops) else len(self.lines)
                last = max(j for j in content if i <= j < nxt)
                sections.append(_Section(found[0], i, last + 1))
        except _Unsupported as e:
            raise self._unsupported(e) from e
        return sections

    def _section(self, key: str) -> _Section | None:
        return next((s for s in self.sections if s.key == key), None)

    def _parse(self, section: _Section) -> Node | None:
        try:
            parser = _Parser(self.lines, section.start, section.end)
            parser.take(parser.toks[0], False)
            found = _key_of(self.lines[section.start], section.start)
            assert found is not None
            rest = found[1].strip()
            if rest and not rest.startswith("#"):
                node: Node | None = parser.scalar(rest, section.start, 0)
            else:
                node = parser.child(0, True)
            leftover = parser.peek()
            if leftover is not None:
                raise _Unsupported("unexpected indentation", leftover[0])
            return node
        except _Unsupported as e:
            raise self._unsupported(e) from e

    def get(self, key: str) -> Any:
        section = self._section(key)
        return None if section is None else to_value(self._parse(section))

    def _replace(self, start: int, end: int, lines: list[str]) -> None:
        self.lines[start:end] = lines
        self.sections = self._top_level()

    def _map(self, key: str, create: bool) -> tuple[_Section, Node | None] | None:
        self.touched.add(key)
        section = self._section(key)
        if section is None:
            if not create:
                return None
            self._replace(len(self.lines), len(self.lines), [f"{scalar_text(key)}: {{}}"])
            section = self._section(key)
            assert section is not None
        node = self._parse(section)
        if node is None or (node.kind == "scalar" and node.value is None) or (node.kind == "map" and node.value == {} and not node.items):
            return section, None
        if node.kind != "map":
            raise YamlError(f'{self.file}: "{key}" isn\'t a mapping; edit it by hand')
        if not node.items:
            raise self._unsupported(_Unsupported(f'"{key}" in flow style', section.start))
        return section, node

    def set_entry(self, section_key: str, name: str, entry: Any) -> None:
        found = self._map(section_key, entry is not None)
        if found is None:
            return
        section, node = found
        if node is None:
            if entry is not None:
                self._replace(section.start, section.end, render_pair(section_key, {name: entry}, 0))
            return
        item = next((i for i in node.items if i.key == name), None)
        if entry is None:
            if item is None:
                return
            if len(node.items) == 1:
                self._replace(section.start, section.end, [f"{scalar_text(section_key)}: {{}}"])
            else:
                self._replace(item.start, item.end, [])
            return
        if item is not None:
            if to_value(item.node) != entry:
                self._replace(item.start, item.end, render_pair(name, entry, item.indent))
            return
        self._replace(node.end, node.end, render_pair(name, entry, node.indent))

    def filter_lists(self, section_key: str, drop: Callable[[Any], bool]) -> None:
        found = self._map(section_key, False)
        if found is None:
            return
        section, node = found
        if node is None:
            return
        cuts: list[tuple[int, int]] = []
        emptied = 0
        for item in node.items:
            if item.node is None or item.node.kind != "seq" or not item.node.items:
                continue
            dropped = [i for i in item.node.items if drop(to_value(i.node))]
            if not dropped:
                continue
            if len(dropped) == len(item.node.items):
                cuts.append((item.start, item.end))
                emptied += 1
            else:
                cuts.extend((i.start, i.end) for i in dropped)
        if emptied == len(node.items):
            self._replace(section.start, section.end, [])
            return
        for start, end in sorted(cuts, reverse=True):
            self.lines[start:end] = []
        self.sections = self._top_level()

    def add_list_item(self, section_key: str, key: str, item: Any) -> None:
        found = self._map(section_key, True)
        assert found is not None
        section, node = found
        if node is None:
            self._replace(section.start, section.end, render_pair(section_key, {key: [item]}, 0))
            return
        pair = next((i for i in node.items if i.key == key), None)
        if pair is None:
            self._replace(node.end, node.end, render_pair(key, [item], node.indent))
        elif (
            pair.node is None
            or (pair.node.kind == "scalar" and pair.node.value is None)
            or (pair.node.kind == "seq" and not pair.node.items and pair.node.value == [])
        ):
            self._replace(pair.start, pair.end, render_pair(key, [item], pair.indent))
        elif pair.node.kind == "seq" and pair.node.items:
            self._replace(pair.node.end, pair.node.end, render_seq([item], pair.node.indent))
        elif pair.node.kind == "seq":
            raise self._unsupported(_Unsupported(f"{section_key}.{key} in flow style", pair.start))
        else:
            raise YamlError(f"{self.file}: {section_key}.{key} isn't a list; edit it by hand")

    def text(self) -> str:
        return "".join(line + self.eol for line in self.lines)


def edit_yaml(text: str | None, file: str, change: Callable[[YamlDoc], None]) -> str | None:
    doc = YamlDoc(text, file)
    before = YamlDoc(text, file)
    change(doc)
    if all(doc.get(k) == before.get(k) for k in doc.touched):
        return None
    out = doc.text()
    return None if out == (text or "") else out


def yaml_value(text: str | None, file: str, key: str) -> Any:
    if text is None or not text.strip():
        return None
    return YamlDoc(text, file).get(key)

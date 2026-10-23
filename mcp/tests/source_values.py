from __future__ import annotations

import os
import re
from typing import Any, NamedTuple

from support import ROOT

VSCODE_SRC = os.path.join(ROOT, "vscode", "src")
KOTLIN_SRC = os.path.join(ROOT, "jetbrains", "src", "main", "kotlin", "dev", "alexk", "ideagenttabs")
GRADLE_BUILD = os.path.join(ROOT, "jetbrains", "build.gradle.kts")


class Ref(NamedTuple):
    name: str


class Spread(NamedTuple):
    value: Any


class Infix(NamedTuple):
    op: str
    left: Any
    right: Any


class Call(NamedTuple):
    callee: str
    args: list[Any]
    kwargs: dict[str, Any]
    block: str | None


class Pattern(NamedTuple):
    source: str
    ignore_case: bool
    other_flags: tuple[str, ...]


_IDENT = re.compile(r"[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*")
_NUMBER = re.compile(r"-?[0-9][0-9_]*[Ll]?")
_NAMED_ARG = re.compile(r"([A-Za-z_]\w*)\s*=(?!=)")
_KOTLIN_ESCAPES = {"t": "\t", "b": "\b", "n": "\n", "r": "\r", "'": "'", '"': '"', "\\": "\\", "$": "$"}
_JS_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}


class _Parser:
    def __init__(self, text: str, pos: int, kotlin: bool) -> None:
        self.text = text
        self.pos = pos
        self.kotlin = kotlin

    def fail(self, what: str) -> ValueError:
        line = self.text.count("\n", 0, self.pos) + 1
        return ValueError(f"cannot read {what} at line {line}: {self.text[self.pos : self.pos + 40]!r}")

    def ws(self, newlines: bool = True) -> None:
        space = re.compile(r"(?:\s|//[^\n]*|/\*.*?\*/)*" if newlines else r"[ \t]*", re.DOTALL)
        match = space.match(self.text, self.pos)
        assert match is not None
        self.pos = match.end()

    def peek(self, s: str) -> bool:
        return self.text.startswith(s, self.pos)

    def take(self, s: str) -> None:
        self.ws()
        if not self.peek(s):
            raise self.fail(repr(s))
        self.pos += len(s)

    def expr(self) -> Any:
        left = self.unary()
        while True:
            self.ws()
            if self.peek("+") and not self.peek("++"):
                self.pos += 1
                left = Infix("+", left, self.unary())
            elif self.kotlin and re.match(r"to\b", self.text[self.pos : self.pos + 3]):
                self.pos += 2
                left = Infix("to", left, self.unary())
            else:
                return left

    def unary(self) -> Any:
        self.ws()
        if self.peek("..."):
            self.pos += 3
            return Spread(self.unary())
        if not self.kotlin and re.match(r"new\b", self.text[self.pos : self.pos + 4]):
            self.pos += 3
            return self.unary()
        return self.postfix(self.primary())

    def primary(self) -> Any:
        c = self.text[self.pos : self.pos + 1]
        if not c:
            raise self.fail("an expression")
        if c in "'\"`":
            return self.string()
        if c == "/" and not self.kotlin:
            return self.regex()
        if c == "[" and not self.kotlin:
            return self.sequence("[", "]")
        if c == "{" and not self.kotlin:
            return self.object()
        if c == "(":
            self.pos += 1
            value = self.expr()
            self.take(")")
            return value
        number = _NUMBER.match(self.text, self.pos)
        if number:
            self.pos = number.end()
            return int(number.group().rstrip("Ll").replace("_", ""))
        ident = _IDENT.match(self.text, self.pos)
        if ident:
            self.pos = ident.end()
            return Ref(ident.group())
        raise self.fail("an expression")

    def postfix(self, node: Any) -> Any:
        self.ws(newlines=False)
        if not isinstance(node, Ref) or not self.peek("("):
            return node
        self.pos += 1
        args: list[Any] = []
        kwargs: dict[str, Any] = {}
        while True:
            self.ws()
            if self.peek(")"):
                self.pos += 1
                break
            named = _NAMED_ARG.match(self.text, self.pos) if self.kotlin else None
            if named:
                self.pos = named.end()
                kwargs[named.group(1)] = self.expr()
            else:
                args.append(self.expr())
            self.ws()
            if self.peek(","):
                self.pos += 1
        block = None
        self.ws(newlines=False)
        if self.kotlin and self.peek("{"):
            block = self.braces()
        return Call(node.name, args, kwargs, block)

    def braces(self) -> str:
        start, depth = self.pos, 0
        while self.pos < len(self.text):
            c = self.text[self.pos]
            self.pos += 1
            depth += {"{": 1, "}": -1}.get(c, 0)
            if depth == 0:
                return self.text[start + 1 : self.pos - 1].strip()
        raise self.fail("a closing brace")

    def sequence(self, open_: str, close: str) -> list[Any]:
        self.take(open_)
        items: list[Any] = []
        while True:
            self.ws()
            if self.peek(close):
                self.pos += len(close)
                return items
            items.append(self.expr())
            self.ws()
            if self.peek(","):
                self.pos += 1

    def object(self) -> dict[str, Any]:
        self.take("{")
        out: dict[str, Any] = {}
        while True:
            self.ws()
            if self.peek("}"):
                self.pos += 1
                return out
            key = self.string() if self.text[self.pos] in "'\"" else self.word()
            self.ws()
            if self.peek(":"):
                self.pos += 1
                out[key] = self.expr()
            else:
                out[key] = Ref(key)
            self.ws()
            if self.peek(","):
                self.pos += 1

    def word(self) -> str:
        ident = re.compile(r"[A-Za-z_$][\w$]*").match(self.text, self.pos)
        if not ident:
            raise self.fail("a name")
        self.pos = ident.end()
        return ident.group()

    def string(self) -> str:
        quote = self.text[self.pos]
        if self.kotlin and self.peek('"""'):
            end = self.text.index('"""', self.pos + 3)
            raw = self.text[self.pos + 3 : end]
            self.pos = end + 3
            if re.search(r"\$[{A-Za-z_]", raw):
                raise self.fail("a raw string without templates")
            return raw
        self.pos += 1
        out: list[str] = []
        while True:
            c = self.text[self.pos]
            self.pos += 1
            if c == quote:
                return "".join(out)
            if c == "\\":
                out.append(self.escape())
            elif c == "$" and self._template(quote):
                raise self.fail("a string without templates")
            elif c == "\n":
                raise self.fail("a one-line string")
            else:
                out.append(c)

    def _template(self, quote: str) -> bool:
        nxt = self.text[self.pos]
        if self.kotlin:
            return nxt == "{" or nxt == "_" or nxt.isalpha()
        return quote == "`" and nxt == "{"

    def escape(self) -> str:
        c = self.text[self.pos]
        self.pos += 1
        if c == "u":
            if not self.kotlin and self.peek("{"):
                end = self.text.index("}", self.pos)
                code = int(self.text[self.pos + 1 : end], 16)
                self.pos = end + 1
                return chr(code)
            code = int(self.text[self.pos : self.pos + 4], 16)
            self.pos += 4
            return chr(code)
        if self.kotlin:
            if c not in _KOTLIN_ESCAPES:
                raise self.fail("a Kotlin escape")
            return _KOTLIN_ESCAPES[c]
        if c == "x":
            code = int(self.text[self.pos : self.pos + 2], 16)
            self.pos += 2
            return chr(code)
        return _JS_ESCAPES.get(c, c)

    def regex(self) -> Pattern:
        self.pos += 1
        start, in_class = self.pos, False
        while True:
            c = self.text[self.pos]
            if c == "\\":
                self.pos += 2
                continue
            if c == "\n":
                raise self.fail("a regex literal")
            self.pos += 1
            if c == "[":
                in_class = True
            elif c == "]":
                in_class = False
            elif c == "/" and not in_class:
                break
        source = self.text[start : self.pos - 1]
        flags = re.compile(r"[a-z]*").match(self.text, self.pos)
        assert flags is not None
        self.pos = flags.end()
        return Pattern(source, "i" in flags.group(), tuple(f for f in flags.group() if f != "i"))


class Sources:
    def __init__(self, folder: str, kotlin: bool) -> None:
        self.kotlin = kotlin
        ext = ".kt" if kotlin else ".ts"
        self.files: dict[str, str] = {}
        for parent, dirs, names in os.walk(folder):
            dirs[:] = sorted(d for d in dirs if d != "test")
            for name in sorted(names):
                if name.endswith(ext) and not name.endswith(f".test{ext}"):
                    path = os.path.join(parent, name)
                    with open(path, encoding="utf-8") as f:
                        self.files[path] = f.read()

    def _declaration(self, name: str) -> tuple[str, int]:
        if self.kotlin:
            pattern = rf"(?m)^[ \t]*(?:(?:private|internal|public|const)\s+)*val\s+{name}\b(?:\s*:[^=\n]*)?\s*=\s*"
        else:
            pattern = rf"(?m)^[ \t]*(?:export\s+)?(?:const|let)\s+{name}\b(?:\s*:[^=\n]*)?\s*=(?![=>])\s*"
        found = [(text, m.end()) for text in self.files.values() for m in re.finditer(pattern, text)]
        if len(found) != 1:
            where = "Kotlin" if self.kotlin else "TypeScript"
            raise AssertionError(f"expected one {where} declaration of {name}, found {len(found)}")
        return found[0]

    def raw(self, name: str) -> Any:
        text, pos = self._declaration(name)
        return _Parser(text, pos, self.kotlin).expr()

    def value(self, name: str) -> Any:
        return self.resolve(self.raw(name))

    def class_params(self, name: str) -> list[str]:
        found = [m for text in self.files.values() for m in re.finditer(rf"\bclass\s+{name}\s*\((.*?)\)\s*[{{\n]", text, re.DOTALL)]
        if len(found) != 1:
            raise AssertionError(f"expected one declaration of class {name}, found {len(found)}")
        return re.findall(r"\bva[lr]\s+(\w+)\s*:", found[0].group(1))

    def resolve(self, node: Any) -> Any:
        if isinstance(node, Ref):
            return node if "." in node.name else self.value(node.name)
        if isinstance(node, list):
            out: list[Any] = []
            for item in node:
                if isinstance(item, Spread):
                    out.extend(self.resolve(item.value))
                else:
                    out.append(self.resolve(item))
            return out
        if isinstance(node, dict):
            return {k: self.resolve(v) for k, v in node.items()}
        if isinstance(node, Infix):
            left, right = self.resolve(node.left), self.resolve(node.right)
            return left + right if node.op == "+" else (left, right)
        if isinstance(node, Call):
            return self._call(node)
        return node

    def _call(self, node: Call) -> Any:
        args = [self.resolve(a) for a in node.args]
        if node.callee == "Object.freeze":
            return args[0]
        if node.callee in ("listOf", "setOf", "arrayOf"):
            return args
        if node.callee == "mapOf":
            return dict(args)
        if node.callee == "Map":
            return dict(args[0]) if args else {}
        if node.callee == "Regex":
            options = tuple(a.name.rsplit(".", 1)[-1] for a in args[1:] if isinstance(a, Ref))
            return Pattern(args[0], "IGNORE_CASE" in options, tuple(o for o in options if o != "IGNORE_CASE"))
        return Call(node.callee, args, {k: self.resolve(v) for k, v in node.kwargs.items()}, node.block)


def typescript() -> Sources:
    return Sources(VSCODE_SRC, kotlin=False)


def kotlin() -> Sources:
    return Sources(KOTLIN_SRC, kotlin=True)


def gradle_text() -> str:
    with open(GRADLE_BUILD, encoding="utf-8") as f:
        return f.read()


def trim_indent(text: str) -> str:
    lines = text.split("\n")
    if lines and not lines[0].strip():
        lines = lines[1:]
    if lines and not lines[-1].strip():
        lines = lines[:-1]
    indent = min((len(line) - len(line.lstrip()) for line in lines if line.strip()), default=0)
    return "\n".join(line[indent:] for line in lines)

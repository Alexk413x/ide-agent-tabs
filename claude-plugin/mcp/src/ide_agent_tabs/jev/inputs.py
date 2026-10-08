from __future__ import annotations

import math
import re
from collections.abc import Sequence
from typing import Any, Union

from ..jsjson import MAX_SAFE_INTEGER, js_ordered, number, quote
from .client import MAX_CHOICE_OPTIONS

MAX_ID = 128

PathPart = Union[str, int]


class Issue:
    def __init__(self, path: Sequence[PathPart], message: str) -> None:
        self.path = list(path)
        self.message = message


class _Invalid:
    pass


INVALID: Any = _Invalid()


class _Absent:
    pass


ABSENT: Any = _Absent()


def received(value: Any) -> str:
    if value is ABSENT:
        return "undefined"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "NaN" if isinstance(value, float) and math.isnan(value) else "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def _type_issue(expected: str, value: Any, got: str | None = None) -> str:
    return f"Invalid input: expected {expected}, received {got or received(value)}"


class Schema:
    def parse(self, value: Any, path: list[PathPart], issues: list[Issue]) -> Any:
        raise NotImplementedError


class Unknown(Schema):
    def parse(self, value: Any, path: list[PathPart], issues: list[Issue]) -> Any:
        return value


class Str(Schema):
    def __init__(self, min_len: int | None = None, max_len: int | None = None) -> None:
        self.min_len = min_len
        self.max_len = max_len

    def parse(self, value: Any, path: list[PathPart], issues: list[Issue]) -> Any:
        if not isinstance(value, str):
            issues.append(Issue(path, _type_issue("string", value)))
            return INVALID
        length = len(value)
        if self.min_len is not None and length < self.min_len:
            issues.append(Issue(path, f"Too small: expected string to have >={self.min_len} characters"))
        if self.max_len is not None and length > self.max_len:
            issues.append(Issue(path, f"Too big: expected string to have <={self.max_len} characters"))
        return value


class Bool(Schema):
    def parse(self, value: Any, path: list[PathPart], issues: list[Issue]) -> Any:
        if not isinstance(value, bool):
            issues.append(Issue(path, _type_issue("boolean", value)))
            return INVALID
        return value


class Int(Schema):
    def __init__(self, min_value: int | None = None) -> None:
        self.min_value = min_value

    def parse(self, value: Any, path: list[PathPart], issues: list[Issue]) -> Any:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            issues.append(Issue(path, _type_issue("number", value)))
            return INVALID
        if isinstance(value, float) and not math.isfinite(value):
            issues.append(Issue(path, _type_issue("number", value, "NaN" if math.isnan(value) else "Infinity")))
            return INVALID
        if isinstance(value, float) and not value.is_integer():
            issues.append(Issue(path, _type_issue("int", value)))
            return INVALID
        whole = int(value)
        if whole > MAX_SAFE_INTEGER:
            issues.append(Issue(path, f"Too big: expected number to be <={MAX_SAFE_INTEGER}"))
        elif whole < -MAX_SAFE_INTEGER:
            issues.append(Issue(path, f"Too small: expected number to be >=-{MAX_SAFE_INTEGER}"))
        if self.min_value is not None and whole < self.min_value:
            issues.append(Issue(path, f"Too small: expected number to be >={number(self.min_value)}"))
        return whole


class Arr(Schema):
    def __init__(self, item: Schema, min_items: int | None = None, max_items: int | None = None) -> None:
        self.item = item
        self.min_items = min_items
        self.max_items = max_items

    def parse(self, value: Any, path: list[PathPart], issues: list[Issue]) -> Any:
        if not isinstance(value, list):
            issues.append(Issue(path, _type_issue("array", value)))
            return INVALID
        out = [self.item.parse(v, [*path, i], issues) for i, v in enumerate(value)]
        if self.min_items is not None and len(value) < self.min_items:
            issues.append(Issue(path, f"Too small: expected array to have >={self.min_items} items"))
        if self.max_items is not None and len(value) > self.max_items:
            issues.append(Issue(path, f"Too big: expected array to have <={self.max_items} items"))
        return out


class Rec(Schema):
    def __init__(self, item: Schema) -> None:
        self.item = item

    def parse(self, value: Any, path: list[PathPart], issues: list[Issue]) -> Any:
        if not isinstance(value, dict):
            issues.append(Issue(path, _type_issue("record", value)))
            return INVALID
        return {k: self.item.parse(v, [*path, k], issues) for k, v in js_ordered(value).items()}


class Obj(Schema):
    def __init__(self, shape: dict[str, tuple[Schema, bool]]) -> None:
        self.shape = shape

    def parse(self, value: Any, path: list[PathPart], issues: list[Issue]) -> Any:
        if not isinstance(value, dict):
            issues.append(Issue(path, _type_issue("object", value)))
            return INVALID
        out: dict[str, Any] = {}
        for key, (schema, optional) in self.shape.items():
            if key not in value and optional:
                continue
            if key not in value:
                schema.parse(ABSENT, [*path, key], issues)
                continue
            out[key] = schema.parse(value[key], [*path, key], issues)
        return out


class Nullable(Schema):
    def __init__(self, inner: Schema) -> None:
        self.inner = inner

    def parse(self, value: Any, path: list[PathPart], issues: list[Issue]) -> Any:
        return None if value is None else self.inner.parse(value, path, issues)


class AnyOf(Schema):
    def __init__(self, options: Sequence[Schema]) -> None:
        self.options = options

    def parse(self, value: Any, path: list[PathPart], issues: list[Issue]) -> Any:
        for option in self.options:
            found: list[Issue] = []
            out = option.parse(value, path, found)
            if not found:
                return out
        issues.append(Issue(path, "Invalid input"))
        return INVALID


class Tagged(Schema):
    def __init__(self, key: str, options: dict[str, Obj]) -> None:
        self.key = key
        self.options = options

    def parse(self, value: Any, path: list[PathPart], issues: list[Issue]) -> Any:
        if not isinstance(value, dict):
            issues.append(Issue(path, _type_issue("object", value)))
            return INVALID
        tag = value.get(self.key)
        option = self.options.get(tag) if isinstance(tag, str) else None
        if option is None:
            expected = " | ".join(f"'{t}'" for t in self.options)
            issues.append(Issue([*path, self.key], f"Invalid discriminator value. Expected {expected}"))
            return INVALID
        return option.parse(value, path, issues)


class Literal(Schema):
    def __init__(self, value: str) -> None:
        self.value = value

    def parse(self, value: Any, path: list[PathPart], issues: list[Issue]) -> Any:
        if value != self.value:
            issues.append(Issue(path, f"Invalid input: expected {quote(self.value)}"))
            return INVALID
        return value


ENTRY = AnyOf([Str(), Rec(Unknown()), Arr(Unknown())])
DESCRIBED = Nullable(ENTRY)
ID = Str(1, MAX_ID)

QUESTION = Tagged(
    "type",
    {
        "noul": Obj(
            {
                "type": (Literal("noul"), False),
                "instructions": (DESCRIBED, True),
                "criteria": (Nullable(Obj({"true": (DESCRIBED, True), "false": (DESCRIBED, True)})), True),
            }
        ),
        "choice": Obj({"type": (Literal("choice"), False), "instructions": (DESCRIBED, True), "criteria": (Rec(DESCRIBED), False)}),
        "score": Obj({"type": (Literal("score"), False), "instructions": (DESCRIBED, True), "criteria": (Arr(DESCRIBED), False)}),
    },
)

INPUTS: dict[str, Obj] = {
    "jev_status": Obj({}),
    "jev_ask": Obj({"state": (ENTRY, False), "questions": (Rec(QUESTION), False)}),
    "jev_choose": Obj(
        {
            "instruction": (Str(1), False),
            "options": (Arr(Obj({"id": (ID, False), "description": (Str(1), False)}), 1, MAX_CHOICE_OPTIONS - 1), False),
            "state": (ENTRY, True),
            "no_match": (Bool(), True),
        }
    ),
    "jev_check": Obj(
        {
            "state": (ENTRY, False),
            "conditions": (Arr(Obj({"id": (ID, False), "question": (Str(1), False)}), 1, MAX_CHOICE_OPTIONS), False),
        }
    ),
    "jev_rank": Obj(
        {
            "query": (Str(1), False),
            "items": (Arr(Obj({"id": (ID, False), "text": (Str(), False)}), 1), False),
            "top": (Int(1), True),
        }
    ),
    "jev_route": Obj({"task": (Str(1), False)}),
}

_SPECIAL = re.compile(r"[^A-Za-z0-9_$]")


def dot_path(path: Sequence[PathPart]) -> str:
    out: list[str] = []
    for part in path:
        if isinstance(part, int):
            out.append(f"[{part}]")
        elif _SPECIAL.search(part):
            out.append(f"[{quote(part)}]")
        else:
            if out:
                out.append(".")
            out.append(part)
    return "".join(out)


def prettify(issues: Sequence[Issue]) -> str:
    lines: list[str] = []
    for issue in sorted(issues, key=lambda i: len(i.path)):
        lines.append(f"✖ {issue.message}")
        if issue.path:
            lines.append(f"  → at {dot_path(issue.path)}")
    return "\n".join(lines)


class InputError(ValueError):
    def __init__(self, issues: Sequence[Issue]) -> None:
        super().__init__(prettify(issues))
        self.issues = list(issues)


def parse_input(tool: str, value: Any) -> dict[str, Any]:
    issues: list[Issue] = []
    out = INPUTS[tool].parse(value, [], issues)
    if issues:
        raise InputError(issues)
    return out

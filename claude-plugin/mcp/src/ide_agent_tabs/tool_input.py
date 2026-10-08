from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from typing import Any

from .jev.inputs import INVALID, Arr, Bool, Issue, Obj, PathPart, Rec, Schema, Str, received
from .jsjson import MAX_SAFE_INTEGER, number, quote


def _type_issue(expected: str, value: Any) -> str:
    return f"Invalid input: expected {expected}, received {received(value)}"


class PatternStr(Schema):
    def __init__(self, pattern: str) -> None:
        self.source = pattern
        # A JavaScript $ without the m flag matches only at the very end; Python's $ also matches before a final newline.
        self.regex = re.compile(pattern[:-1] + r"\Z" if pattern.endswith("$") and not pattern.endswith("\\$") else pattern)

    def parse(self, value: Any, path: list[PathPart], issues: list[Issue]) -> Any:
        if not isinstance(value, str):
            issues.append(Issue(path, _type_issue("string", value)))
            return INVALID
        if self.regex.search(value) is None:
            issues.append(Issue(path, f"Invalid string: must match pattern /{self.source}/"))
        return value


class Enum(Schema):
    def __init__(self, values: Sequence[str]) -> None:
        self.values = list(values)

    def parse(self, value: Any, path: list[PathPart], issues: list[Issue]) -> Any:
        if not isinstance(value, str) or value not in self.values:
            issues.append(Issue(path, f"Invalid option: expected one of {'|'.join(quote(v) for v in self.values)}"))
            return INVALID
        return value


class Num(Schema):
    def __init__(self, integer: bool, minimum: float | None, maximum: float | None) -> None:
        self.integer = integer
        self.minimum = minimum
        self.maximum = maximum

    def parse(self, value: Any, path: list[PathPart], issues: list[Issue]) -> Any:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            issues.append(Issue(path, _type_issue("number", value)))
            return INVALID
        if isinstance(value, float) and not math.isfinite(value):
            issues.append(Issue(path, f"Invalid input: expected number, received {'NaN' if math.isnan(value) else 'Infinity'}"))
            return INVALID
        if self.integer:
            if isinstance(value, float) and not value.is_integer():
                issues.append(Issue(path, "Invalid input: expected int, received number"))
                return INVALID
            if value > MAX_SAFE_INTEGER:
                issues.append(Issue(path, f"Too big: expected int to be <={MAX_SAFE_INTEGER}"))
            elif value < -MAX_SAFE_INTEGER:
                issues.append(Issue(path, f"Too small: expected int to be >=-{MAX_SAFE_INTEGER}"))
        if self.minimum is not None and value < self.minimum and not (self.integer and self.minimum <= -MAX_SAFE_INTEGER):
            issues.append(Issue(path, f"Too small: expected number to be >={number(self.minimum)}"))
        if self.maximum is not None and value > self.maximum and not (self.integer and self.maximum >= MAX_SAFE_INTEGER):
            issues.append(Issue(path, f"Too big: expected number to be <={number(self.maximum)}"))
        return value


def compile_schema(schema: Mapping[str, Any]) -> Schema:
    kind = schema.get("type")
    if "enum" in schema:
        return Enum(schema["enum"])
    if kind == "string":
        if "pattern" in schema:
            return PatternStr(schema["pattern"])
        return Str(schema.get("minLength"), schema.get("maxLength"))
    if kind in ("integer", "number"):
        return Num(kind == "integer", schema.get("minimum"), schema.get("maximum"))
    if kind == "boolean":
        return Bool()
    if kind == "array":
        return Arr(compile_schema(schema.get("items") or {}), schema.get("minItems"), schema.get("maxItems"))
    if kind == "object" and "additionalProperties" in schema and "properties" not in schema:
        return Rec(compile_schema(schema["additionalProperties"]))
    if kind == "object":
        required = set(schema.get("required") or [])
        return Obj({k: (compile_schema(v), k not in required) for k, v in (schema.get("properties") or {}).items()})
    raise ValueError(f"unsupported schema: {schema}")


def parse_args(schema: Schema, value: Any) -> tuple[Any, list[Issue]]:
    issues: list[Issue] = []
    out = schema.parse(value, [], issues)
    return out, issues


def dot_path(path: Sequence[PathPart]) -> str:
    if not path:
        return "object root"
    out = str(path[0])
    for part in path[1:]:
        out += f"[{part}]" if isinstance(part, int) else f".{part}"
    return out


def issues_text(issues: Sequence[Issue]) -> str:
    return "\n".join(i.message if not i.path else f"{i.message} at {dot_path(i.path)}" for i in issues)

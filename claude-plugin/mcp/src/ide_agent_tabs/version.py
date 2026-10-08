from __future__ import annotations

import json
import os
import re

_PLUGIN_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", ".claude-plugin", "plugin.json")
_LEADING_INT = re.compile(r"\s*[+-]?\d+")


def _read_version() -> str:
    try:
        with open(_PLUGIN_JSON, encoding="utf-8") as f:
            version = json.load(f).get("version")
    except (OSError, ValueError, AttributeError):
        return "0"
    return version if isinstance(version, str) else "0"


PACKAGE_VERSION = _read_version()


def _parse_int(text: str) -> int:
    match = _LEADING_INT.match(text)
    return int(match.group()) if match else 0


def compare_versions(a: str, b: str) -> int:
    pa = [_parse_int(p) for p in a.split(".")]
    pb = [_parse_int(p) for p in b.split(".")]
    for i in range(max(len(pa), len(pb))):
        diff = (pa[i] if i < len(pa) else 0) - (pb[i] if i < len(pb) else 0)
        if diff:
            return 1 if diff > 0 else -1
    return 0

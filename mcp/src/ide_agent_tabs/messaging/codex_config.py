from __future__ import annotations

import os
import re
from collections.abc import Mapping

from ..files import read_text_if_exists
from .sessions import is_effort, is_model

_KEY = re.compile(r"\s*([A-Za-z0-9_-]+)\s*=\s*(?:\"([^\"\\]*)\"|'([^']*)')\s*(?:#.*)?")
_TABLE = re.compile(r"\s*\[\s*([^\]]+?)\s*\]\s*(?:#.*)?")
_LINES = re.compile(r"\r?\n")


def codex_home(env: Mapping[str, str]) -> str | None:
    if env.get("CODEX_HOME"):
        return env["CODEX_HOME"]
    home = env.get("USERPROFILE") or env.get("HOME")
    return os.path.join(home, ".codex") if home else None


def parse_codex_config(text: str) -> dict[str, str]:
    tables: dict[str, dict[str, str]] = {"": {}}
    table = ""
    for line in _LINES.split(text):
        header = _TABLE.fullmatch(line)
        if header is not None:
            table = re.sub(r"[\"']", "", header.group(1))
            tables.setdefault(table, {})
            continue
        kv = _KEY.fullmatch(line)
        if kv is not None:
            tables[table][kv.group(1)] = kv.group(2) if kv.group(2) is not None else kv.group(3)
    top = tables[""]
    chosen = {**top, **tables.get(f"profiles.{top['profile']}", {})} if "profile" in top else top
    out: dict[str, str] = {}
    model = chosen.get("model")
    if model is not None and is_model(model):
        out["model"] = model
    effort = chosen.get("model_reasoning_effort")
    if effort is not None and is_effort(effort):
        out["effort"] = effort
    return out


def read_codex_config(env: Mapping[str, str]) -> dict[str, str]:
    folder = codex_home(env)
    if folder is None:
        return {}
    try:
        text = read_text_if_exists(os.path.join(folder, "config.toml"))
    except (OSError, ValueError):
        return {}
    return {} if text is None else parse_codex_config(text)

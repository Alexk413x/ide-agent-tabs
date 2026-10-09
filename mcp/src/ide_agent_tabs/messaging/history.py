from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any, NamedTuple

from ..clock import parse_iso
from ..jsjson import code_units, stringify, utf16_len, utf16_slice
from .store import new_message_id, stored_for

HISTORY_MAX = 500
# Claude Code replaces an MCP result over its output limit (about 25,000 tokens) with an error. JSON of ids,
# times and hex tokenizes at about 2.3 characters per token, so a history reply stays near 30,000 characters.
HISTORY_REPLY_CHARS = 30_000
HISTORY_BATCH = 50
PREVIEW_CHARS = 200
PIECE_CHARS = 30_000
_NATIVE_SAME_MS = 120_000
_STATUS = {"unread": "unread", "held": "delivering", "read": "read"}
_LOG_ID = re.compile(r"m-[0-9a-f]{16}")

HistoryItem = dict[str, Any]


class Who(NamedTuple):
    id: str | None
    names: list[str]


def _units(text: str) -> bytes:
    return code_units(text)


def text_piece(text: str, offset: int, budget: int = PIECE_CHARS) -> str:
    length = utf16_len(text)
    n = min(budget, max(0, length - offset))
    while n > 1 and utf16_len(stringify(utf16_slice(text, offset, offset + n))) > budget:
        n //= 2
    if n > 1 and offset + n < length:
        last = utf16_slice(text, offset + n - 1, offset + n)
        if last and 0xD800 <= ord(last[0]) <= 0xDBFF:
            n -= 1
    return utf16_slice(text, offset, offset + n)


def older_than(items: Sequence[HistoryItem], before: str | None) -> Sequence[HistoryItem]:
    if before is None:
        return items
    at = next((i for i, m in enumerate(items) if m["id"] == before), -1)
    if at != -1:
        return items[:at]
    limit = _units(before)
    return [m for m in items if _units(m["at"]) < limit]


def log_id(value: str | None) -> str:
    return value if value is not None and _LOG_ID.fullmatch(value) is not None else new_message_id()


def _matches(who: Who, p: dict[str, Any]) -> bool:
    return (who.id is not None and p.get("id") == who.id) or (p.get("name") is not None and p["name"] in who.names)


def _ms(text: str) -> float:
    found = parse_iso(text)
    return float("nan") if found is None else found


def _same_native(a: HistoryItem, b: HistoryItem) -> bool:
    return (
        a["route"] == "native"
        and b["route"] == "native"
        and a["direction"] == b["direction"]
        and a["text"] == b["text"]
        and abs(_ms(a["at"]) - _ms(b["at"])) < _NATIVE_SAME_MS
    )


def _record(m: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"id": m["id"], "at": m["sentAt"], "route": m["route"], "from": m["from"], "to": m["to"], "text": m["text"]}
    if m.get("replyTo") is not None:
        out["replyTo"] = m["replyTo"]
    if m.get("delivery") is not None:
        out["delivery"] = m["delivery"]
    if m.get("state") is not None:
        out["status"] = _STATUS[m["state"]]
    return out


def _select(stored: Sequence[dict[str, Any]], who: Who) -> list[HistoryItem]:
    items: list[HistoryItem] = []

    def add(r: dict[str, Any], direction: str) -> None:
        items.append({**r, "direction": direction, "peer": r["to"] if direction == "sent" else r["from"]})

    for m in stored:
        r = _record(m)
        owner = m.get("owner") if m["route"] == "native" else m["from"].get("id")
        own = owner is not None and owner == who.id
        if m.get("direction") == "received":
            if own or _matches(who, r["to"]):
                add(r, "received")
            elif _matches(who, r["from"]):
                add(r, "sent")
        elif own or _matches(who, r["from"]):
            add(r, "sent")
        elif _matches(who, r["to"]):
            add(r, "received")
    by_id: dict[str, HistoryItem] = {}
    for item in items:
        by_id.setdefault(item["id"], item)
    unique: list[HistoryItem] = []
    for item in sorted(by_id.values(), key=lambda i: (_units(i["at"]), _units(i["id"]))):
        if not any(_same_native(u, item) for u in unique):
            unique.append(item)
    return unique[-HISTORY_MAX:]


# Reads only: no message changes state, so a message stays unread for its session.
def history(home: str, who: Who) -> list[HistoryItem]:
    return _select(stored_for(home, who.id, who.names), who)


def previews(items: Sequence[HistoryItem], budget: int = HISTORY_REPLY_CHARS - 1_000) -> list[HistoryItem]:
    out: list[HistoryItem] = []
    used = 0
    for item in reversed(items):
        if len(out) >= HISTORY_BATCH:
            break
        preview = {**item, "text": utf16_slice(item["text"], 0, PREVIEW_CHARS), "textLength": utf16_len(item["text"])}
        used += utf16_len(stringify(preview)) + 1
        if used > budget:
            break
        out.insert(0, preview)
    return out


def history_counts(home: str, whos: Sequence[Who]) -> list[int]:
    return [0 if who.id is None and not who.names else len(history(home, who)) for who in whos]

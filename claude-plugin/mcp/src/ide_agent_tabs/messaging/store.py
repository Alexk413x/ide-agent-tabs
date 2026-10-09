from __future__ import annotations

import os
import re
import sqlite3
import threading
import time
from collections.abc import Collection, Sequence
from typing import Any, Callable

from ..clock import iso, now_ms, parse_iso
from ..jsjson import stringify, utf16_len, well_formed
from .db import (
    CLEAN_DEADLINE_MS,
    TOOL_DEADLINE_MS,
    Db,
    MailError,
    StoreBusyError,
    ensure_healthy,
    is_constraint,
    is_full,
    query,
    same_file,
    tx,
)
from .sessions import is_session_id
from .wake import on_wake, remove_stale_wakes, signal_wake

MAX_TEXT_CHARS = 32_000
MAX_SENT_PER_MINUTE = 20
MAX_UNREAD = 50
MAX_READ_CHARS = 40_000
MAX_SENDERS = 20
KEEP_MS = 7 * 24 * 60 * 60 * 1000
DEDUPE_MS = 60_000
CLAIM_TIMEOUT_MS = 2 * 60_000
RECHECK_MS = 10_000
_MINUTE_MS = 60_000
_CLOCK_SLACK_MS = 60_000
_CLEAN_BATCH = 500
_MESSAGE_ID = re.compile(r"m-[0-9a-f]{16}")
_CLAIM_ID = re.compile(r"c-[0-9a-f]{16}")

_COLUMNS = (
    "seq, id, route, owner, direction, from_id, from_name, from_agent, from_path, to_id, to_name, to_agent, to_path, "
    "text, reply_to, sent_at, delivery, state"
)
_PENDING = "(state = 'unread' OR (state = 'held' AND (state_ms <= ? OR state_ms > ?)))"

Message = dict[str, Any]


def new_message_id() -> str:
    return f"m-{os.urandom(8).hex()}"


def _new_claim_id() -> str:
    return f"c-{os.urandom(8).hex()}"


def check_message_id(value: str, field: str) -> None:
    if _MESSAGE_ID.fullmatch(value) is None:
        raise MailError(f"{field} must be a message id such as m-0123456789abcdef")


def send_digest(to: str, text: str, reply_to: str | None = None) -> str:
    import hashlib

    payload = stringify([to, "" if reply_to is None else reply_to, text])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _bind(value: object) -> object:
    return well_formed(value) if isinstance(value, str) else value


def _pending_args(now: float) -> list[float]:
    return [now - CLAIM_TIMEOUT_MS, now + _CLOCK_SLACK_MS]


def _now(now: float | None) -> int:
    return now_ms() if now is None else int(now)


def _str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _to_message(r: sqlite3.Row) -> Message:
    message: Message = {
        "id": str(r["id"]),
        "from": {"id": str(r["from_id"]), "agent": _str(r["from_agent"]) or "", "path": _str(r["from_path"]) or ""},
        "to": str(r["to_id"]),
        "text": str(r["text"]),
    }
    reply_to = _str(r["reply_to"])
    if reply_to is not None:
        message["replyTo"] = reply_to
    message["sentAt"] = str(r["sent_at"])
    return message


def _party(r: sqlite3.Row, side: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for key in ("id", "name", "agent", "path"):
        value = _str(r[f"{side}_{key}"])
        if value is not None:
            out[key] = value
    return out


def _to_stored(r: sqlite3.Row) -> Message:
    out: Message = {"id": str(r["id"]), "route": r["route"]}
    for key in ("owner", "direction"):
        if isinstance(r[key], str):
            out[key] = r[key]
    out["from"] = _party(r, "from")
    out["to"] = _party(r, "to")
    out["text"] = str(r["text"])
    if isinstance(r["reply_to"], str):
        out["replyTo"] = r["reply_to"]
    out["sentAt"] = str(r["sent_at"])
    for key in ("delivery", "state"):
        if isinstance(r[key], str):
            out[key] = r[key]
    return out


def _count(db: Db, text: str, *args: object) -> int:
    row = db.one(text, *[_bind(a) for a in args])
    return int(row["n"]) if row is not None else 0


def _insert_checked(db: Db, out: Message, message_id: str, digest: str, now: int, delivery: str | None) -> Message:
    sender = out["from"]
    dup = db.one(
        "SELECT id FROM messages WHERE route = 'agent-tabs' AND from_id = ? AND to_id = ? AND digest = ? AND sent_ms > ? "
        "ORDER BY seq DESC LIMIT 1",
        _bind(sender["id"]),
        _bind(out["to"]),
        digest,
        now - DEDUPE_MS,
    )
    if dup is not None:
        return {"id": str(dup["id"]), "duplicate": True}
    sent_last_minute = _count(
        db, "SELECT count(*) AS n FROM messages WHERE route = 'agent-tabs' AND from_id = ? AND sent_ms > ?", sender["id"], now - _MINUTE_MS
    )
    if sent_last_minute >= MAX_SENT_PER_MINUTE:
        raise MailError(f"this session sent {MAX_SENT_PER_MINUTE} messages in the last minute; wait before sending more")
    unread = _count(db, "SELECT count(*) AS n FROM messages WHERE route = 'agent-tabs' AND to_id = ? AND state = 'unread'", out["to"])
    if unread >= MAX_UNREAD:
        raise MailError(f"session {out['to']} already has {MAX_UNREAD} unread messages; wait until it reads them")
    db.run(
        "INSERT INTO messages (id, route, from_id, from_name, from_agent, from_path, to_id, to_name, text, reply_to, sent_at, sent_ms, "
        "digest, delivery, state, state_ms) VALUES (?, 'agent-tabs', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'unread', ?)",
        *[
            _bind(v)
            for v in (
                message_id,
                sender["id"],
                sender.get("name"),
                sender["agent"],
                sender["path"],
                out["to"],
                out.get("toName"),
                out["text"],
                out.get("replyTo"),
                iso(now),
                now,
                digest,
                delivery,
                now,
            )
        ],
    )
    return {"id": message_id}


def send_message(
    home: str,
    out: Message,
    now: float | None = None,
    deadline_ms: float = TOOL_DEADLINE_MS,
    label: str | None = "send",
    delivery: str | None = None,
) -> Message:
    if utf16_len(out["text"]) > MAX_TEXT_CHARS:
        raise MailError(f"text exceeds {MAX_TEXT_CHARS} characters")
    if not is_session_id(out["to"]):
        raise MailError(f"not a session id: {out['to']}")
    at = _now(now)
    digest = send_digest(out["to"], out["text"], out.get("replyTo"))
    attempt = 0
    while True:
        message_id = new_message_id()
        try:
            sent = tx(
                home, lambda db, message_id=message_id: _insert_checked(db, out, message_id, digest, at, delivery), deadline_ms, label
            )
        except sqlite3.Error as e:
            if is_constraint(e) and attempt == 0:
                attempt += 1
                continue
            if is_full(e):
                raise MailError("the disk is full; the message was not sent") from e
            raise
        if not sent.get("duplicate"):
            signal_wake(home, out["to"])
        return sent


def set_delivery(home: str, message_id: str, delivery: str) -> None:
    query(
        home,
        lambda db: db.run("UPDATE messages SET delivery = ? WHERE route = 'agent-tabs' AND id = ?", _bind(delivery), _bind(message_id)),
    )


def _return_stale(db: Db, session_id: str, now: int) -> None:
    db.run(
        "UPDATE messages SET state = 'unread', claim = NULL, state_ms = ? WHERE route = 'agent-tabs' AND to_id = ? AND state = 'held' "
        "AND (state_ms <= ? OR state_ms > ?)",
        now,
        _bind(session_id),
        *_pending_args(now),
    )


def _filtered(where: list[str], args: list[object], flt: dict[str, str] | None) -> None:
    flt = flt or {}
    if flt.get("from") is not None:
        where.append("from_id = ?")
        args.append(_bind(flt["from"]))
    if flt.get("replyTo") is not None:
        where.append("reply_to = ?")
        args.append(_bind(flt["replyTo"]))


def _unread_rows(db: Db, session_id: str, flt: dict[str, str] | None) -> list[sqlite3.Row]:
    where = ["route = 'agent-tabs'", "to_id = ?", "state = 'unread'"]
    args: list[object] = [_bind(session_id)]
    _filtered(where, args, flt)
    return db.all(f"SELECT {_COLUMNS} FROM messages WHERE {' AND '.join(where)} ORDER BY seq", *args)


def _pick(rows: Sequence[sqlite3.Row], count: float, chars: float) -> tuple[list[sqlite3.Row], int]:
    picked: list[sqlite3.Row] = []
    size = 0
    remaining = 0
    for row in rows:
        length = utf16_len(str(row["text"]))
        if len(picked) >= count or (picked and size + length > chars):
            remaining += 1
            continue
        size += length
        picked.append(row)
    return picked, remaining


def take_batch(
    home: str,
    session_id: str,
    flt: dict[str, str] | None = None,
    count: float = float("inf"),
    chars: float = float("inf"),
    now: float | None = None,
    deadline_ms: float = TOOL_DEADLINE_MS,
    label: str | None = "read",
) -> Message:
    at = _now(now)

    def work(db: Db) -> Message:
        _return_stale(db, session_id, at)
        picked, remaining = _pick(_unread_rows(db, session_id, flt), count, chars)
        for row in picked:
            db.run("UPDATE messages SET state = 'read', state_ms = ? WHERE seq = ? AND state = 'unread'", at, row["seq"])
        messages = [_to_message(r) for r in picked]
        return {"messages": messages, "ids": [m["id"] for m in messages], "remaining": remaining, "unreadable": 0}

    return tx(home, work, deadline_ms, label)


def put_back(home: str, session_id: str, ids: Sequence[str], now: float | None = None) -> None:
    if not ids:
        return
    at = _now(now)

    def work(db: Db) -> None:
        for message_id in ids:
            db.run(
                "UPDATE messages SET state = 'unread', state_ms = ? WHERE route = 'agent-tabs' AND to_id = ? AND id = ? AND state = 'read'",
                at,
                _bind(session_id),
                _bind(message_id),
            )

    tx(home, work, label="put back")


def claim_batch(home: str, session_id: str, count: float, chars: float = MAX_READ_CHARS, now: float | None = None) -> Message:
    at = _now(now)

    def work(db: Db) -> Message:
        _return_stale(db, session_id, at)
        picked, remaining = _pick(_unread_rows(db, session_id, None), count, chars)
        messages = [_to_message(r) for r in picked]
        claim = _new_claim_id() if picked else None
        for row in picked:
            db.run("UPDATE messages SET state = 'held', claim = ?, state_ms = ? WHERE seq = ? AND state = 'unread'", claim, at, row["seq"])
        return {"claim": claim, "messages": messages, "ids": [m["id"] for m in messages], "remaining": remaining, "unreadable": 0}

    return tx(home, work, label="claim")


def settle_claim(home: str, session_id: str, claim: str, op: str, now: float | None = None) -> int:
    if _CLAIM_ID.fullmatch(claim) is None:
        return 0
    at = _now(now)
    return tx(
        home,
        lambda db: db.run(
            "UPDATE messages SET state = ?, claim = NULL, state_ms = ? WHERE route = 'agent-tabs' AND to_id = ? AND claim = ? AND state = 'held'",
            "read" if op == "ack" else "unread",
            at,
            _bind(session_id),
            claim,
        ),
        label=op,
    )


def peek_unread(home: str, session_id: str, now: float | None = None, deadline_ms: float = TOOL_DEADLINE_MS) -> list[Message]:
    at = _now(now)
    return query(
        home,
        lambda db: [
            _to_message(r)
            for r in db.all(
                f"SELECT {_COLUMNS} FROM messages WHERE route = 'agent-tabs' AND to_id = ? AND {_PENDING} ORDER BY seq",
                _bind(session_id),
                *_pending_args(at),
            )
        ],
        deadline_ms,
    )


def has_unread(home: str, session_id: str, flt: dict[str, str] | None = None, now: float | None = None) -> bool:
    at = _now(now)
    where = ["route = 'agent-tabs'", "to_id = ?", _PENDING]
    args: list[object] = [_bind(session_id), *_pending_args(at)]
    _filtered(where, args, flt)

    def work(db: Db) -> bool:
        row = db.one(f"SELECT EXISTS (SELECT 1 FROM messages WHERE {' AND '.join(where)}) AS hit", *args)
        return row is not None and row["hit"] == 1

    return query(home, work)


def unread_summary(home: str, session_id: str, now: float | None = None) -> dict[str, Any]:
    at = _now(now)

    def work(db: Db) -> dict[str, Any]:
        n = _count(
            db,
            f"SELECT count(*) AS n FROM messages WHERE route = 'agent-tabs' AND to_id = ? AND {_PENDING}",
            session_id,
            *_pending_args(at),
        )
        senders = (
            [
                str(r["from_id"])
                for r in db.all(
                    f"SELECT from_id FROM messages WHERE route = 'agent-tabs' AND to_id = ? AND {_PENDING} "
                    "GROUP BY from_id ORDER BY max(seq) DESC LIMIT ?",
                    _bind(session_id),
                    *_pending_args(at),
                    MAX_SENDERS,
                )
            ]
            if n
            else []
        )
        return {"count": n, "senders": senders}

    return query(home, work)


def mail_to(home: str, session_id: str) -> list[Message]:
    return query(
        home,
        lambda db: [
            _to_message(r)
            for r in db.all(
                f"SELECT {_COLUMNS} FROM messages WHERE route = 'agent-tabs' AND to_id = ? AND state IN ('unread', 'read') ORDER BY seq",
                _bind(session_id),
            )
        ],
    )


def log_native(home: str, record: Message) -> None:
    if utf16_len(record["text"]) > MAX_TEXT_CHARS:
        raise MailError(f"text exceeds {MAX_TEXT_CHARS} characters")
    at = parse_iso(record["sentAt"])
    sender = record.get("from") or {}
    receiver = record.get("to") or {}
    values = (
        record["id"],
        record["owner"],
        record["direction"],
        sender.get("id"),
        sender.get("name"),
        sender.get("agent"),
        sender.get("path"),
        receiver.get("id"),
        receiver.get("name"),
        receiver.get("agent"),
        receiver.get("path"),
        record["text"],
        record["sentAt"],
        at if at is not None else now_ms(),
        record.get("delivery"),
    )
    query(
        home,
        lambda db: db.run(
            "INSERT OR IGNORE INTO messages (id, route, owner, direction, from_id, from_name, from_agent, from_path, to_id, to_name, "
            "to_agent, to_path, text, sent_at, sent_ms, delivery) VALUES (?, 'native', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            *[_bind(v) for v in values],
        ),
    )


def stored_for(home: str, session_id: str | None, names: Sequence[str]) -> list[Message]:
    marks = ", ".join("?" for _ in names)
    by_name = f" OR from_name IN ({marks}) OR to_name IN ({marks})" if names else ""
    bound = [_bind(n) for n in names]
    sid = _bind(session_id)
    return query(
        home,
        lambda db: [
            _to_stored(r)
            for r in db.all(
                f"SELECT {_COLUMNS} FROM messages WHERE from_id = ? OR to_id = ? OR owner = ?{by_name} ORDER BY seq",
                sid,
                sid,
                sid,
                *bound,
                *bound,
            )
        ],
    )


def wait_for_message(
    home: str,
    session_id: str,
    flt: dict[str, str] | None,
    timeout_ms: float,
    cancel: threading.Event | None = None,
    now: Callable[[], float] | None = None,
) -> Message | None:
    clock = now or now_ms
    deadline = time.monotonic() + timeout_ms / 1000
    woken = threading.Event()
    stop = on_wake(home, session_id, woken.set)
    dirty = True
    checked = time.monotonic()
    try:
        while True:
            if dirty:
                dirty = False
                checked = time.monotonic()
                if has_unread(home, session_id, flt, clock()):
                    batch = take_batch(home, session_id, flt, count=1, now=clock())
                    if batch["messages"] and cancel is not None and cancel.is_set():
                        put_back(home, session_id, batch["ids"], clock())
                        return None
                    if batch["messages"]:
                        return batch["messages"][0]
            left = deadline - time.monotonic()
            if left <= 0 or (cancel is not None and cancel.is_set()):
                return None
            if woken.wait(min(left, 0.1)):
                woken.clear()
                dirty = True
            elif time.monotonic() - checked >= RECHECK_MS / 1000:
                dirty = True
    finally:
        stop()


def _delete_in_batches(home: str, text: str, args: Sequence[object]) -> None:
    while True:
        changes = tx(home, lambda db: db.run(text, *args), CLEAN_DEADLINE_MS)
        if changes < _CLEAN_BATCH:
            return


def clean_store(home: str, live: Collection[str], now: float | None = None) -> None:
    at = _now(now)
    try:
        same_file(home)
        if not ensure_healthy(home, at):
            return
        cutoff = at - KEEP_MS
        _delete_in_batches(
            home,
            "DELETE FROM messages WHERE seq IN (SELECT seq FROM messages WHERE sent_ms < ? AND (route = 'native' OR state = 'read') "
            f"LIMIT {_CLEAN_BATCH})",
            [cutoff],
        )
        boxes = query(
            home,
            lambda db: db.all(
                "SELECT to_id, max(max(sent_ms, coalesce(state_ms, 0))) AS newest FROM messages "
                "WHERE route = 'agent-tabs' AND to_id IN (SELECT DISTINCT to_id FROM messages WHERE route = 'agent-tabs' "
                "AND state IN ('unread', 'held')) GROUP BY to_id"
            ),
            CLEAN_DEADLINE_MS,
        )
        for box in boxes:
            to = str(box["to_id"])
            if to in live or int(box["newest"]) >= cutoff:
                continue
            _delete_in_batches(
                home,
                "DELETE FROM messages WHERE seq IN (SELECT seq FROM messages WHERE route = 'agent-tabs' AND to_id = ? "
                f"AND state IN ('unread', 'held') LIMIT {_CLEAN_BATCH})",
                [to],
            )
        query(home, lambda db: db.all("PRAGMA wal_checkpoint(TRUNCATE)"), CLEAN_DEADLINE_MS)
        remove_stale_wakes(home, live, cutoff)
    except StoreBusyError:
        return

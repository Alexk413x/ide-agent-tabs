from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import time
from typing import Any, Callable

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "claude-plugin", "mcp", "src")
)

from ide_agent_tabs.clock import now_ms
from ide_agent_tabs.files import file_lock
from ide_agent_tabs.messaging import db, store
from ide_agent_tabs.processes import utf8_stdio

MODES: dict[str, Callable[[dict[str, Any]], None]] = {}


def mode(fn: Callable[[dict[str, Any]], None]) -> Callable[[dict[str, Any]], None]:
    head, *rest = fn.__name__.split("_")
    MODES[head + "".join(part.title() for part in rest)] = fn
    return fn


def say(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def go() -> None:
    say("ready")
    sys.stdin.readline()


def done(result: Any) -> None:
    say(json.dumps(result, ensure_ascii=False))


def outgoing(sender: str, to: str, text: str) -> dict[str, Any]:
    return {"from": {"id": sender, "agent": "codex", "path": f"/w/{sender}"}, "to": to, "text": text}


@mode
def lock_count(args: dict[str, Any]) -> None:
    file = args["file"]
    rounds = int(args["rounds"])
    go()
    for _ in range(rounds):
        with file_lock(file):
            with open(file, encoding="utf-8") as f:
                n = int(f.read() or "0")
            time.sleep(0.001)
            with open(file, "w", encoding="utf-8") as f:
                f.write(str(n + 1))
    done({"rounds": rounds})


@mode
def lock_once(args: dict[str, Any]) -> None:
    file = args["file"]
    go()
    started = now_ms()
    try:
        with file_lock(file, timeout_ms=float(args.get("timeoutMs", 15_000))):
            pass
        done({"ok": True, "ms": now_ms() - started})
    except TimeoutError as e:
        done({"ok": False, "error": str(e), "ms": now_ms() - started})


@mode
def lock_hold(args: dict[str, Any]) -> None:
    with file_lock(args["file"]):
        say("locked")
        sys.stdin.readline()
    done({"released": True})


@mode
def mesh(args: dict[str, Any]) -> None:
    home = args["home"]
    me = args["me"]
    peers = args["peers"]
    count = int(args["count"])
    expected = int(args["expected"])
    sent: list[dict[str, str]] = []
    read: list[dict[str, str]] = []
    pairs: list[float] = []

    def take() -> None:
        batch = store.take_batch(home, me)
        store.settle_read_claims(home, me, [batch["claim"]] if batch["claim"] else [])
        for m in batch["messages"]:
            read.append({"id": m["id"], "from": m["from"]["id"], "to": m["to"]})

    db.open_db(home)
    go()
    for k in range(count):
        started = time.perf_counter()
        to = peers[k % len(peers)]
        sent.append({"id": store.send_message(home, outgoing(me, to, f"{me} to {to} #{k}"))["id"], "to": to})
        take()
        pairs.append((time.perf_counter() - started) * 1000)
    deadline = time.monotonic() + 60
    while len(read) < expected and time.monotonic() < deadline:
        take()
        if len(read) < expected:
            time.sleep(0.02)
    done({"sent": sent, "read": read, "pairs": pairs})


@mode
def read_loop(args: dict[str, Any]) -> None:
    home = args["home"]
    ids: list[str] = []
    db.open_db(home)
    go()
    while True:
        stopping = os.path.exists(args["stop"])
        batch = store.take_batch(home, args["target"], count=2)
        store.settle_read_claims(home, args["target"], [batch["claim"]] if batch["claim"] else [])
        ids.extend(batch["ids"])
        if stopping and not batch["ids"]:
            break
        if not batch["ids"]:
            time.sleep(0.005)
    done({"ids": ids})


@mode
def claim_loop(args: dict[str, Any]) -> None:
    home = args["home"]
    target = args["target"]
    ids: list[str] = []
    db.open_db(home)
    go()
    while True:
        stopping = os.path.exists(args["stop"])
        claimed = store.claim_batch(home, target, 3)
        if claimed["claim"] is not None:
            store.settle_claim(home, target, claimed["claim"], "ack")
            ids.extend(claimed["ids"])
        if stopping and claimed["claim"] is None:
            break
        if claimed["claim"] is None:
            time.sleep(0.005)
    done({"ids": ids})


@mode
def send(args: dict[str, Any]) -> None:
    home = args["home"]
    targets = args["targets"]
    texts = args["texts"]
    at = args.get("now")
    results: list[dict[str, Any]] = []
    db.open_db(home)
    go()
    for i, text in enumerate(texts):
        started = time.perf_counter()
        try:
            sent = store.send_message(home, outgoing(args["from"], targets[i % len(targets)], text), at if at is not None else now_ms())
            results.append({**sent, "ms": (time.perf_counter() - started) * 1000})
        except db.MailError as e:
            results.append({"error": str(e), "ms": (time.perf_counter() - started) * 1000})
    done({"results": results})


@mode
def hold_lock(args: dict[str, Any]) -> None:
    home = args["home"]
    d = db.open_db(home)
    d.sql.execute("BEGIN IMMEDIATE")
    now = now_ms()
    d.sql.execute(
        "INSERT INTO messages (id, route, from_id, from_agent, from_path, to_id, text, sent_at, sent_ms, state, state_ms) "
        "VALUES (?, 'agent-tabs', 'tab-dead', 'codex', '/', 'tab-b', 'never committed', ?, ?, 'unread', ?)",
        (store.new_message_id(), "2026-01-01T00:00:00.000Z", now, now),
    )
    say("locked")
    while True:
        time.sleep(1)


@mode
def open_store(args: dict[str, Any]) -> None:
    go()
    d = db.open_db(args["home"])
    done({"version": d.one("PRAGMA user_version")[0]})  # type: ignore[index]


MODES["open"] = open_store


@mode
def shared(args: dict[str, Any]) -> None:
    home = args["home"]
    db.set_busy_timeout(db.SHARED_BUSY_TIMEOUT_MS)
    ids: list[str] = args["sessions"]
    internal = int(args["internal"])
    expected = int(args["expected"])
    sends: list[float] = []
    lateness: list[float] = []
    received: dict[str, list[dict[str, str]]] = {sid: [] for sid in ids}
    lock = threading.Lock()
    db.open_db(home)
    go()
    stop = threading.Event()

    def tick() -> None:
        while not stop.is_set():
            started = time.perf_counter()
            time.sleep(0.01)
            lateness.append((time.perf_counter() - started) * 1000 - 10)

    ticker = threading.Thread(target=tick, daemon=True)
    ticker.start()
    deadline = time.monotonic() + 90

    def waiter(me: str) -> None:
        while len(received[me]) < expected and time.monotonic() < deadline:
            m = store.wait_for_message(home, me, None, max(1.0, (deadline - time.monotonic()) * 1000))
            if m is not None:
                received[me].append({"id": m["id"], "from": m["from"]["id"]})

    def session(me: str, index: int) -> None:
        w = threading.Thread(target=waiter, args=(me,))
        w.start()
        for k in range(1, internal + 1):
            to = ids[(index + k) % len(ids)]
            started = time.perf_counter()
            store.send_message(home, outgoing(me, to, f"{me} to {to} #{k}"))
            with lock:
                sends.append((time.perf_counter() - started) * 1000)
        w.join()

    threads = [threading.Thread(target=session, args=(sid, i)) for i, sid in enumerate(ids)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stop.set()
    ordered = sorted(lateness) or [0.0]
    done(
        {
            "received": received,
            "sends": sends,
            "loopP99Ms": ordered[min(len(ordered) - 1, int(0.99 * len(ordered)))],
            "loopMaxMs": ordered[-1],
        }
    )


def main() -> None:
    utf8_stdio()
    name = sys.argv[1]
    args = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    try:
        MODES[name](args)
    except sqlite3.Error as e:
        sys.stderr.write(f"py_worker {name}: {e}\n")
        raise


if __name__ == "__main__":
    main()

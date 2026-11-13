from __future__ import annotations

import contextlib
import os
import re

from .clock import now_ms, parse_iso
from .files import discard_file, mtime_ms, read_text_if_exists
from .jev.ledger import trim_ledger
from .jsjson import parse
from .profiles import CLAUDE_TAB_SETTINGS_FILE

RETENTION_MS = 30 * 24 * 60 * 60 * 1000
HANDOFFS_DIR = "handoffs"
_HANDOFF_FILE = re.compile(r"(h-[0-9a-f]{12})\.(json|md)")
_MERGED_SETTINGS = re.compile(re.escape(os.path.splitext(CLAUDE_TAB_SETTINGS_FILE)[0]) + r"-[0-9a-f]{16}\.json")


# A record is finished once nothing waits on it: the old tab was confirmed stopped, or it was never to be closed, or
# check_close can't read it anyway. Any other record is kept at any age, because close_tab still refuses to close
# its old tab until it is confirmed.
def _handoff_finished(text: str | None) -> bool:
    try:
        record = parse(text or "")
    except ValueError:
        return True
    return not isinstance(record, dict) or isinstance(record.get("confirmedAt"), str) or record.get("closeAfter") is False


# The 30 days run from when the record stopped being needed: its confirmation, else its creation.
def _settled_ms(text: str | None, file: str) -> float | None:
    try:
        record = parse(text or "")
    except ValueError:
        record = None
    times = [record.get(k) for k in ("confirmedAt", "createdAt")] if isinstance(record, dict) else []
    at = next((ms for ms in (parse_iso(t) for t in times if isinstance(t, str)) if ms is not None), None)
    return mtime_ms(file) if at is None else at


def prune_handoffs(home: str, now: float) -> None:
    folder = os.path.join(home, HANDOFFS_DIR)
    try:
        names = os.listdir(folder)
    except OSError:
        return
    found = [m for n in names if (m := _HANDOFF_FILE.fullmatch(n)) is not None]
    records = {m.group(1) for m in found if m.group(2) == "json"}
    for match in found:
        handoff_id, ext = match.groups()
        file = os.path.join(folder, match.group(0))
        if ext == "json":
            text = read_text_if_exists(file)
            settled = _settled_ms(text, file)
            if settled is not None and now - settled > RETENTION_MS and _handoff_finished(text):
                discard_file(file)
                discard_file(os.path.join(folder, f"{handoff_id}.md"))
        elif handoff_id not in records:
            # A brief with no record is left over from a tab that never opened.
            written = mtime_ms(file)
            if written is not None and now - written > RETENTION_MS:
                discard_file(file)


# Nothing records which tab started with which merged file, and both IDE plugins write these files too. So while
# any Claude session is live every merged file counts as in use: it is kept and its age starts again, and the 30
# days run from the last time a Claude session was live.
def prune_tab_settings(home: str, now: float, claude_live: bool = False) -> None:
    folder = os.path.join(home, "mcp")
    try:
        names = os.listdir(folder)
    except OSError:
        return
    for name in names:
        if _MERGED_SETTINGS.fullmatch(name) is None:
            continue
        file = os.path.join(folder, name)
        if claude_live:
            with contextlib.suppress(OSError):
                os.utime(file, (now / 1000, now / 1000))
            continue
        mtime = mtime_ms(file)
        if mtime is not None and now - mtime > RETENTION_MS:
            discard_file(file)


def prune_old_files(home: str, now: float | None = None, claude_live: bool = False) -> None:
    at = now_ms() if now is None else now
    for prune in (prune_handoffs, lambda h, n: prune_tab_settings(h, n, claude_live), lambda h, n: trim_ledger(h, n - RETENTION_MS)):
        with contextlib.suppress(OSError, TimeoutError):
            prune(home, at)

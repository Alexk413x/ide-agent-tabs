from __future__ import annotations

import json
from dataclasses import dataclass

from ..clock import now_ms
from ..files import file_lock, read_text_if_exists, write_atomically
from . import ingress
from .private_files import ensure_private_dir

PRESENCE_FILE = "presence.json"
MAX_RECORDS = 64


@dataclass(frozen=True)
class Presence:
    online: bool
    address: str
    at_ms: int


def presence_path(home: str) -> str:
    return str(ingress.net_dir(home) / PRESENCE_FILE)


def read_presence(home: str) -> dict[str, Presence]:
    try:
        value = json.loads(read_text_if_exists(presence_path(home)) or "{}")
    except (OSError, ValueError):
        return {}
    if not isinstance(value, dict):
        return {}
    found: dict[str, Presence] = {}
    for device, entry in value.items():  # pyright: ignore[reportUnknownVariableType]
        if not isinstance(device, str) or not isinstance(entry, dict):
            continue
        online, address, at_ms = entry.get("online"), entry.get("address"), entry.get("atMs")  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
        if isinstance(online, bool) and isinstance(address, str) and isinstance(at_ms, int) and not isinstance(at_ms, bool):
            found[device] = Presence(online, address, at_ms)
    return found


def record_presence(home: str, device_id: str, online: bool, address: str, at_ms: int | None = None) -> None:
    path = presence_path(home)
    ensure_private_dir(ingress.net_dir(home))
    with file_lock(path):
        records = read_presence(home)
        records[device_id] = Presence(online, address, now_ms() if at_ms is None else at_ms)
        newest = sorted(records.items(), key=lambda item: item[1].at_ms, reverse=True)[:MAX_RECORDS]
        body = {device: {"online": p.online, "address": p.address, "atMs": p.at_ms} for device, p in newest}
        write_atomically(path, json.dumps(body, indent=2) + "\n")

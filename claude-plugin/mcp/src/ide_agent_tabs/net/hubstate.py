from __future__ import annotations

import json
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from ..files import read_text_if_exists, remove_file, write_atomically
from . import ingress
from .pairing import MemberRef, check_hub_id, check_hub_key, check_member_fields
from .policy import PolicyError
from .private_files import check_private, ensure_private_dir, make_private_file

HUB_STATE_FILE = "hub.json"
_KEYS = frozenset({"hub", "key", "members"})


class HubStateError(PolicyError):
    pass


@dataclass(frozen=True)
class HubState:
    hub_id: str
    key: str
    members: tuple[MemberRef, ...] = ()


def hub_state_path(home: str) -> Path:
    return ingress.net_dir(home) / HUB_STATE_FILE


def read_hub_state(home: str) -> HubState | None:
    path = hub_state_path(home)
    try:
        text = read_text_if_exists(str(path))
    except OSError as error:
        raise HubStateError(f"{path} cannot be read: {error}") from error
    if text is None:
        return None
    problems = check_private(path)
    if problems:
        raise HubStateError(f"{path} is not private: {'; '.join(problems)}")
    try:
        value = json.loads(text)
        if not isinstance(value, dict) or set(value) != _KEYS:  # pyright: ignore[reportUnknownArgumentType]
            raise ValueError("unknown or missing fields")
        hub_id, key, members = value["hub"], value["key"], value["members"]
        if not isinstance(members, list):
            raise TypeError("members must be a list")
        refs = tuple(check_member_fields(m) for m in members)  # pyright: ignore[reportUnknownVariableType]
        return HubState(check_hub_id(hub_id), check_hub_key(key), refs)
    except (ValueError, TypeError) as error:
        raise HubStateError(f"{path} is not valid ({error}); delete it only after leaving the hub") from error


def write_hub_state(home: str, state: HubState) -> None:
    path = hub_state_path(home)
    body = {
        "hub": state.hub_id,
        "key": state.key,
        "members": [
            {"deviceId": m.device_id, "label": m.label, "address": m.address, "port": m.port, "certificatePem": m.pem}
            for m in state.members
        ],
    }
    ensure_private_dir(path.parent)
    write_atomically(str(path), json.dumps(body, indent=2) + "\n")
    make_private_file(path)


def remove_hub_state(home: str) -> None:
    remove_file(str(hub_state_path(home)))


def new_hub_key() -> str:
    return secrets.token_hex(32)


def ensure_hub_key(home: str, hub_id: str) -> str:
    state = read_hub_state(home)
    if state is not None and state.hub_id == hub_id:
        return state.key
    key = new_hub_key()
    write_hub_state(home, HubState(hub_id, key))
    return key


def adopt_hub(home: str, hub_id: str, key: str, members: Sequence[MemberRef]) -> None:
    write_hub_state(home, HubState(hub_id, key, tuple(members)))

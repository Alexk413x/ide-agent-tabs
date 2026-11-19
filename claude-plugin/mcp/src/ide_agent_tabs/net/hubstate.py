from __future__ import annotations

import json
import secrets
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from ..clock import now_ms
from ..files import file_lock, read_text_if_exists, remove_file, write_atomically
from . import ingress
from .pairing import (
    MemberRef,
    PairingError,
    RemovedRef,
    check_hub_id,
    check_hub_key,
    check_member_fields,
    full_device_id,
)
from .policy import PolicyError
from .private_files import check_private, ensure_private_dir, make_private_file

HUB_STATE_FILE = "hub.json"
MAX_EPOCH = 2**31 - 1
MAX_EPOCH_JUMP = 16
MAX_PENDING = 256
MAX_REMOVED = 256
PENDING_OPS = ("introduce", "remove", "rekey")
_REQUIRED = frozenset({"hub", "key", "members"})
_OPTIONAL = frozenset({"epoch", "pending", "removed"})
_PENDING_REQUIRED = frozenset({"to", "op", "device", "atMs"})
_PENDING_KEYS = _PENDING_REQUIRED | {"epoch"}
_REMOVED_KEYS = frozenset({"deviceId", "atMs"})


class HubStateError(PolicyError):
    pass


@dataclass(frozen=True)
class Pending:
    to: str
    op: str
    device: str = ""
    at_ms: int = 0
    epoch: int = 0

    def same_task(self, other: Pending) -> bool:
        if self.op == "rekey":
            return (self.to, self.op) == (other.to, other.op)
        return (self.to, self.op, self.device) == (other.to, other.op, other.device)

    def covered_by(self, done: Pending) -> bool:
        if (self.to, self.op) != (done.to, done.op):
            return False
        return self.epoch <= done.epoch if self.op == "rekey" else self.device == done.device


@dataclass(frozen=True)
class HubState:
    hub_id: str
    key: str
    members: tuple[MemberRef, ...] = ()
    epoch: int = 0
    pending: tuple[Pending, ...] = ()
    removed: tuple[RemovedRef, ...] = ()

    def is_removed(self, device_id: str) -> bool:
        return any(r.device_id == device_id for r in self.removed)

    def version(self) -> tuple[int, str]:
        return self.epoch, self.key


def hub_state_path(home: str) -> Path:
    return ingress.net_dir(home) / HUB_STATE_FILE


def _whole(value: object, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= high:
        raise ValueError("not a whole number in range")
    return value


def _pending(value: object) -> Pending:
    if not isinstance(value, dict) or not _PENDING_REQUIRED <= set(value) <= _PENDING_KEYS:  # pyright: ignore[reportUnknownArgumentType]
        raise ValueError("a pending entry has missing or unknown fields")
    op = value["op"]
    if op not in PENDING_OPS:
        raise ValueError("a pending entry has an unknown operation")
    device = value["device"]
    return Pending(
        full_device_id(value["to"]),
        op,
        "" if device == "" else full_device_id(device),
        _whole(value["atMs"], 2**53),
        _whole(value.get("epoch", 0), MAX_EPOCH),
    )


def _removed(value: object) -> RemovedRef:
    if not isinstance(value, dict) or set(value) != _REMOVED_KEYS:  # pyright: ignore[reportUnknownArgumentType]
        raise ValueError("a removed entry has missing or unknown fields")
    return RemovedRef(full_device_id(value["deviceId"]), _whole(value["atMs"], 2**53))


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
        if not isinstance(value, dict) or not _REQUIRED <= set(value) <= _REQUIRED | _OPTIONAL:  # pyright: ignore[reportUnknownArgumentType]
            raise ValueError("unknown or missing fields")
        hub_id, key, members = value["hub"], value["key"], value["members"]
        if not isinstance(members, list):
            raise TypeError("members must be a list")
        pending = value.get("pending", [])
        if not isinstance(pending, list) or len(pending) > MAX_PENDING:  # pyright: ignore[reportUnknownArgumentType]
            raise TypeError("pending must be a short list")
        removed = value.get("removed", [])
        if not isinstance(removed, list) or len(removed) > MAX_REMOVED:  # pyright: ignore[reportUnknownArgumentType]
            raise TypeError("removed must be a short list")
        refs = tuple(check_member_fields(m) for m in members)  # pyright: ignore[reportUnknownVariableType]
        return HubState(
            check_hub_id(hub_id),
            check_hub_key(key),
            refs,
            _whole(value.get("epoch", 0), MAX_EPOCH),
            tuple(_pending(entry) for entry in pending),  # pyright: ignore[reportUnknownVariableType]
            tuple(_removed(entry) for entry in removed),  # pyright: ignore[reportUnknownVariableType]
        )
    except (ValueError, TypeError, PairingError) as error:
        raise HubStateError(f"{path} is not valid ({error}); delete it only after leaving the hub") from error


def write_hub_state(home: str, state: HubState) -> None:
    path = hub_state_path(home)
    body = {
        "hub": state.hub_id,
        "key": state.key,
        "epoch": state.epoch,
        "members": [
            {"deviceId": m.device_id, "label": m.label, "address": m.address, "port": m.port, "certificatePem": m.pem}
            for m in state.members
        ],
        "pending": [{"to": p.to, "op": p.op, "device": p.device, "atMs": p.at_ms, "epoch": p.epoch} for p in state.pending],
        "removed": [{"deviceId": r.device_id, "atMs": r.at_ms} for r in state.removed],
    }
    ensure_private_dir(path.parent)
    write_atomically(str(path), json.dumps(body, indent=2) + "\n")
    make_private_file(path)


def remove_hub_state(home: str) -> None:
    remove_file(str(hub_state_path(home)))


def new_hub_key() -> str:
    return secrets.token_hex(32)


def update_hub_state(home: str, change: Callable[[HubState], HubState]) -> HubState | None:
    ensure_private_dir(hub_state_path(home).parent)
    with file_lock(str(hub_state_path(home))):
        state = read_hub_state(home)
        if state is None:
            return None
        updated = change(state)
        if updated != state:
            write_hub_state(home, updated)
        return updated


def ensure_hub_state(home: str, hub_id: str) -> HubState:
    ensure_private_dir(hub_state_path(home).parent)
    with file_lock(str(hub_state_path(home))):
        state = read_hub_state(home)
        if state is not None and state.hub_id == hub_id:
            return state
        created = HubState(hub_id, new_hub_key())
        write_hub_state(home, created)
        return created


def ensure_hub_key(home: str, hub_id: str) -> str:
    return ensure_hub_state(home, hub_id).key


def adopt_hub(home: str, hub_id: str, key: str, members: Sequence[MemberRef], epoch: int = 0, removed: Sequence[RemovedRef] = ()) -> None:
    write_hub_state(home, HubState(hub_id, key, tuple(members), epoch, (), tuple(removed)[-MAX_REMOVED:]))


def rotate_key(home: str) -> HubState | None:
    return update_hub_state(home, lambda state: replace(state, key=new_hub_key(), epoch=min(state.epoch + 1, MAX_EPOCH)))


def adopt_key(home: str, hub_id: str, epoch: int, key: str) -> HubState | None:
    def change(state: HubState) -> HubState:
        if state.hub_id != hub_id or (epoch, key) <= state.version() or epoch > state.epoch + MAX_EPOCH_JUMP:
            return state
        return replace(state, key=key, epoch=epoch)

    return update_hub_state(home, change)


def add_pending(home: str, tasks: Sequence[Pending]) -> None:
    def change(state: HubState) -> HubState:
        kept = list(state.pending)
        for task in tasks:
            if task.op == "rekey":
                older = [e for e in kept if e.op == "rekey" and e.to == task.to]
                if any(e.epoch >= task.epoch for e in older):
                    continue
                kept = [e for e in kept if e not in older]
            elif any(task.same_task(existing) for existing in kept):
                continue
            if len(kept) >= MAX_PENDING:
                victim = next((i for i, e in enumerate(kept) if e.op != "remove"), None) if task.op == "remove" else None
                if victim is None:
                    continue
                del kept[victim]
            kept.append(task)
        return replace(state, pending=tuple(kept))

    update_hub_state(home, change)


def drop_pending(home: str, tasks: Sequence[Pending]) -> None:
    def change(state: HubState) -> HubState:
        return replace(state, pending=tuple(p for p in state.pending if not any(p.covered_by(done) for done in tasks)))

    update_hub_state(home, change)


def _with_removed(removed: tuple[RemovedRef, ...], device_id: str) -> tuple[RemovedRef, ...]:
    others = tuple(r for r in removed if r.device_id != device_id)
    return (*others, RemovedRef(device_id, now_ms()))[-MAX_REMOVED:]


def forget_member(home: str, device_id: str, *, record: bool = False) -> None:
    def keep(task: Pending) -> bool:
        if task.to == device_id:
            return False
        return task.op == "remove" if task.device == device_id else True

    def change(state: HubState) -> HubState:
        removed = _with_removed(state.removed, device_id) if record else state.removed
        return replace(
            state,
            members=tuple(m for m in state.members if m.device_id != device_id),
            pending=tuple(p for p in state.pending if keep(p)),
            removed=removed,
        )

    update_hub_state(home, change)


def remember_member(home: str, member: MemberRef) -> None:
    def change(state: HubState) -> HubState:
        others = tuple(m for m in state.members if m.device_id != member.device_id)
        return replace(state, members=(*others, member))

    update_hub_state(home, change)


def record_removed(home: str, device_id: str) -> None:
    def change(state: HubState) -> HubState:
        return replace(state, removed=_with_removed(state.removed, device_id))

    update_hub_state(home, change)


def clear_removed(home: str, device_id: str) -> None:
    def change(state: HubState) -> HubState:
        return replace(state, removed=tuple(r for r in state.removed if r.device_id != device_id))

    update_hub_state(home, change)

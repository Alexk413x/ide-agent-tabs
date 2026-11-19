from __future__ import annotations

import contextlib
import ssl
import threading
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from typing import Any, TypeVar

from ..clock import now_ms
from . import wire
from .client import NetClientError, local_device_id, read_peer_pem, send_request
from .hubstate import (
    MAX_EPOCH_JUMP,
    HubState,
    HubStateError,
    Pending,
    add_pending,
    adopt_key,
    drop_pending,
    read_hub_state,
    record_removed,
    remember_member,
    rotate_key,
)
from .names import default_label
from .pairing import MAX_JOIN_MEMBERS, MemberRef, PairingError, canonical_pem, check_member_fields, private_source
from .peers import LabelTaken, remove_peer, save_peer
from .policy import DEFAULT_PORT, PeerGrant, PolicyError, grant_for, load_policy

SEND_TIMEOUT_S = 5.0
WORKERS = 4
KEY_ROUNDS = 3
PENDING_MAX_AGE_MS = 14 * 24 * 3_600_000

QUEUED_TEXT = "Not reachable now: {names}. The listener retries while it runs, and the member learns the change then."

Request = Callable[..., dict[str, Any]]
T = TypeVar("T")
_ERRORS = (NetClientError, PolicyError, HubStateError, OSError, ValueError)


class HubRefusal(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Delivery:
    outcome: str
    body: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class Report:
    told: list[str] = field(default_factory=list)
    queued: list[str] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)
    rotator: str | None = None
    state_problem: str | None = None

    def lines(self, queued: str = QUEUED_TEXT) -> list[str]:
        found: list[str] = []
        if self.state_problem:
            found.append(
                f"The hub state file is not usable ({self.state_problem}), so the key was not replaced and offline members "
                "learn nothing later. Fix or delete hub.json after leaving the hub."
            )
        if self.told:
            found.append(f"Told: {', '.join(sorted(self.told))}.")
        if self.queued:
            found.append(queued.format(names=", ".join(sorted(self.queued))))
        if self.refused:
            found.append(f"Refused the notice: {', '.join(sorted(self.refused))}.")
        return found


def member_ref(home: str, peer: PeerGrant) -> MemberRef | None:
    text = read_peer_pem(home, peer.device_id)
    if text is None:
        return None
    try:
        pem, _der = canonical_pem(ssl.DER_cert_to_PEM_cert(ssl.PEM_cert_to_DER_cert(text)))
    except (ValueError, PairingError):
        return None
    return MemberRef(peer.device_id, peer.label, peer.address, DEFAULT_PORT, pem)


def introduce_body(hub_id: str, ref: MemberRef) -> dict[str, Any]:
    member = {"deviceId": ref.device_id, "label": ref.label, "address": ref.address, "port": ref.port, "certificatePem": ref.pem}
    return {"hub": hub_id, "member": member}


def _deliver(home: str, request: Request, to: str, op: str, body: Mapping[str, Any]) -> Delivery:
    try:
        reply = request(home, to, op, body, timeout=SEND_TIMEOUT_S)
    except _ERRORS:
        return Delivery("unreachable")
    if reply.get("ok") is not True:
        return Delivery("refused")
    found = reply.get("body")
    return Delivery("ok", found if isinstance(found, dict) else {})  # pyright: ignore[reportUnknownVariableType]


def _queue(home: str, tasks: Sequence[Pending]) -> None:
    with contextlib.suppress(HubStateError):
        add_pending(home, tasks)


def _unqueue(home: str, tasks: Sequence[Pending]) -> None:
    with contextlib.suppress(HubStateError):
        drop_pending(home, tasks)


def _each(items: Sequence[T], work: Callable[[T], None]) -> None:
    if not items:
        return
    with ThreadPoolExecutor(max_workers=min(WORKERS, len(items))) as pool:
        list(pool.map(work, items))


def _members(home: str, *excluded: str) -> list[PeerGrant]:
    return [p for p in load_policy(home).peers if p.member and p.device_id not in excluded]


def _classify(report: Report, peer: PeerGrant, delivery: Delivery) -> None:
    {"ok": report.told, "refused": report.refused, "unreachable": report.queued}[delivery.outcome].append(peer.label)


def _version(body: Mapping[str, Any]) -> tuple[int, str] | None:
    epoch, key = body.get("epoch"), body.get("key")
    if isinstance(epoch, bool) or not isinstance(epoch, int) or not isinstance(key, str):
        return None
    return (epoch, key) if 0 <= epoch <= wire.MAX_EPOCH and len(key) == 64 else None


def _push_once(home: str, request: Request, state: HubState) -> tuple[Report, list[tuple[int, str]]]:
    report = Report()
    versions: list[tuple[int, str]] = []
    body = {"hub": state.hub_id, "epoch": state.epoch, "key": state.key}

    def one(peer: PeerGrant) -> None:
        delivery = _deliver(home, request, peer.device_id, "rekey", body)
        _classify(report, peer, delivery)
        if delivery.outcome == "unreachable":
            _queue(home, [Pending(peer.device_id, "rekey", "", now_ms(), state.epoch)])
        elif delivery.outcome == "ok":
            _unqueue(home, [Pending(peer.device_id, "rekey", "", 0, state.epoch)])
            version = _version(delivery.body)
            if version is not None:
                versions.append(version)

    _each(_members(home), one)
    return report, versions


def push_key(home: str, request: Request = send_request) -> Report:
    report = Report()
    for _round in range(KEY_ROUNDS):
        state = read_hub_state(home)
        if state is None:
            break
        report, versions = _push_once(home, request, state)
        newer = [v for v in versions if v > state.version()]
        if not newer:
            break
        adopt_key(home, state.hub_id, *max(newer))
    return report


def announce_removal(home: str, removed: str, request: Request = send_request) -> Report:
    hub = load_policy(home).hub
    if hub is None:
        return Report()
    report = Report()
    try:
        rotate_key(home)
    except HubStateError as error:
        report.state_problem = str(error)
    body = {"hub": hub.id, "device": removed, "rotate": False}

    def one(peer: PeerGrant) -> None:
        delivery = _deliver(home, request, peer.device_id, "remove", body)
        _classify(report, peer, delivery)
        if delivery.outcome == "unreachable":
            _queue(home, [Pending(peer.device_id, "remove", removed, now_ms())])

    _each(_members(home, removed), one)
    try:
        keyed = push_key(home, request)
    except HubStateError as error:
        report.state_problem = report.state_problem or str(error)
        return report
    report.queued.extend(label for label in keyed.queued if label not in report.queued)
    report.refused.extend(label for label in keyed.refused if label not in report.refused)
    return report


def introduce_member(home: str, device_id: str, request: Request = send_request) -> Report:
    policy = load_policy(home)
    newcomer = grant_for(policy, device_id)
    ref = member_ref(home, newcomer) if newcomer is not None and newcomer.member else None
    if policy.hub is None or ref is None:
        return Report()
    report = Report()
    body = introduce_body(policy.hub.id, ref)

    def one(peer: PeerGrant) -> None:
        delivery = _deliver(home, request, peer.device_id, "introduce", body)
        _classify(report, peer, delivery)
        if delivery.outcome == "unreachable":
            _queue(home, [Pending(peer.device_id, "introduce", device_id, now_ms())])

    _each(_members(home, device_id), one)
    return report


def leave_hub(home: str, request: Request = send_request) -> Report:
    own = local_device_id(home)
    hub = load_policy(home).hub
    report = Report()
    if own is None or hub is None:
        return report
    members = sorted(_members(home), key=lambda p: p.device_id)
    rotator: PeerGrant | None = None
    for peer in members:
        delivery = _deliver(home, request, peer.device_id, "remove", {"hub": hub.id, "device": own, "rotate": True})
        if delivery.outcome == "ok":
            rotator = peer
            _classify(report, peer, delivery)
            break
        if delivery.outcome == "refused":
            report.refused.append(peer.label)
    report.rotator = rotator.label if rotator is not None else None
    rest = [p for p in members if rotator is None or p.device_id != rotator.device_id]
    already = set(report.refused)

    def one(peer: PeerGrant) -> None:
        if peer.label in already:
            return
        _classify(report, peer, _deliver(home, request, peer.device_id, "remove", {"hub": hub.id, "device": own, "rotate": False}))

    _each(rest, one)
    return report


def flush_pending(home: str, request: Request = send_request) -> int:
    state = read_hub_state(home)
    policy = load_policy(home)
    if state is None or policy.hub is None or not state.pending:
        return 0
    now = now_ms()
    stale = [t for t in state.pending if now - t.at_ms > PENDING_MAX_AGE_MS]
    if stale:
        _unqueue(home, stale)
    groups: dict[str, list[Pending]] = {}
    for task in state.pending:
        if task not in stale:
            groups.setdefault(task.to, []).append(task)
    delivered: list[Pending] = []
    dropped: list[Pending] = []
    versions: list[tuple[int, str]] = []
    hub_id = policy.hub.id

    def body_for(task: Pending) -> tuple[str, dict[str, Any]] | None:
        if task.op == "remove":
            return "remove", {"hub": hub_id, "device": task.device, "rotate": False}
        if task.op == "rekey":
            current = read_hub_state(home)
            return None if current is None else ("rekey", {"hub": hub_id, "epoch": current.epoch, "key": current.key})
        subject = grant_for(policy, task.device)
        ref = member_ref(home, subject) if subject is not None and subject.member else None
        return None if ref is None else ("introduce", introduce_body(hub_id, ref))

    def one(device: str) -> None:
        target = grant_for(policy, device)
        for task in groups[device]:
            prepared = body_for(task) if target is not None and target.member else None
            if prepared is None:
                dropped.append(task)
                continue
            sent = replace(task, epoch=prepared[1]["epoch"]) if task.op == "rekey" else task
            delivery = _deliver(home, request, device, *prepared)
            if delivery.outcome == "unreachable":
                return
            (delivered if delivery.outcome == "ok" else dropped).append(sent)
            if delivery.outcome == "ok" and task.op == "rekey":
                version = _version(delivery.body)
                if version is not None:
                    versions.append(version)

    _each(list(groups), one)
    done = [*delivered, *dropped]
    if done:
        _unqueue(home, done)
    current = read_hub_state(home)
    if current is not None and any(v > current.version() for v in versions):
        adopt_key(home, hub_id, *max(versions))
        push_key(home, request)
    return len(delivered)


def _start(task: Callable[[], None]) -> None:
    threading.Thread(target=task, daemon=True).start()


class HubSync:
    def __init__(
        self,
        home: str,
        log: Callable[[str], None],
        notice: Callable[[str], None],
        bump_generation: Callable[[], object],
        request: Request = send_request,
        spawn: Callable[[Callable[[], None]], None] = _start,
        allow_address: Callable[[str], bool] = private_source,
    ) -> None:
        self._home = home
        self._log = log
        self._notice = notice
        self._bump = bump_generation
        self._request = request
        self._spawn = spawn
        self._allow_address = allow_address
        self._rotation_lock = threading.Lock()
        self._rotation_queue: list[str] = []
        self._rotation_running = False

    def _state(self) -> HubState:
        try:
            state = read_hub_state(self._home)
        except HubStateError:
            raise HubRefusal("hub-state-unusable") from None
        if state is None:
            raise HubRefusal("no-hub-state")
        return state

    def handle(self, sender: PeerGrant, body: wire.HubBody) -> dict[str, Any]:
        hub = load_policy(self._home).hub
        if not sender.member:
            raise HubRefusal("not-a-member")
        if hub is None or body.hub != hub.id:
            raise HubRefusal("wrong-hub")
        if isinstance(body, wire.IntroduceBody):
            return self._introduce(sender, body)
        if isinstance(body, wire.RemoveBody):
            return self._remove(sender, body)
        return self._rekey(body)

    def _introduce(self, sender: PeerGrant, body: wire.IntroduceBody) -> dict[str, Any]:
        try:
            ref = check_member_fields(
                {
                    "deviceId": body.device_id,
                    "label": body.label,
                    "address": body.address,
                    "port": body.port,
                    "certificatePem": body.pem,
                }
            )
        except PairingError:
            raise HubRefusal("invalid-member") from None
        if ref.device_id == local_device_id(self._home):
            return {"added": False}
        if not self._allow_address(ref.address):
            raise HubRefusal("address-not-private")
        if self._state().is_removed(ref.device_id):
            raise HubRefusal("removed-member")
        policy = load_policy(self._home)
        known = grant_for(policy, ref.device_id)
        if known is not None:
            if not known.member:
                raise HubRefusal("not-a-member-peer")
            return {"added": False}
        if sum(1 for p in policy.peers if p.member) >= MAX_JOIN_MEMBERS:
            raise HubRefusal("too-many-members")
        label = ref.label
        for _attempt in range(3):
            try:
                save_peer(self._home, ref.device_id, ref.pem, ref.address, label, member=True)
                break
            except LabelTaken:
                label = default_label(ref.label, load_policy(self._home))
        else:
            raise HubRefusal("label-taken")
        remember_member(self._home, replace(ref, label=label))
        self._notice(f"The hub member {sender.label} introduced {label}. {label} joined from {ref.address}.")
        return {"added": True}

    def _remove(self, sender: PeerGrant, body: wire.RemoveBody) -> dict[str, Any]:
        if body.device == local_device_id(self._home):
            raise HubRefusal("self-removal")
        target = grant_for(load_policy(self._home), body.device)
        if target is None:
            if body.rotate:
                raise HubRefusal("unknown-member")
            with contextlib.suppress(HubStateError):
                record_removed(self._home, body.device)
            return {"removed": False}
        if not target.member:
            raise HubRefusal("not-a-member-peer")
        remove_peer(self._home, target.device_id)
        self._bump()
        if target.device_id == sender.device_id:
            self._notice(f"The hub member {target.label} left the hub.")
        else:
            self._notice(f"The hub member {sender.label} removed {target.label} from the hub.")
        if body.rotate:
            self._request_rotation(body.device)
        return {"removed": True}

    def _request_rotation(self, device: str) -> None:
        with self._rotation_lock:
            if device not in self._rotation_queue:
                self._rotation_queue.append(device)
            if self._rotation_running:
                return
            self._rotation_running = True
        self._spawn(self._rotation_worker)

    def _rotation_worker(self) -> None:
        while True:
            with self._rotation_lock:
                if not self._rotation_queue:
                    self._rotation_running = False
                    return
                device = self._rotation_queue.pop(0)
            try:
                self._rotate(device)
            except BaseException:
                with self._rotation_lock:
                    self._rotation_running = False
                raise

    def _rotate(self, removed: str) -> None:
        try:
            report = announce_removal(self._home, removed, self._request)
        except _ERRORS as error:
            self._log(f"net: rotating the hub key failed: {type(error).__name__}: {error}")
            return
        self._log(f"net: rotated the hub key after {removed[:15]} left; {'; '.join(report.lines()) or 'no other member to tell'}")

    def _rekey(self, body: wire.RekeyBody) -> dict[str, Any]:
        if body.epoch > self._state().epoch + MAX_EPOCH_JUMP:
            raise HubRefusal("epoch-too-far")
        state = adopt_key(self._home, body.hub, body.epoch, body.key)
        if state is None:
            raise HubRefusal("no-hub-state")
        return {"epoch": state.epoch, "key": state.key}

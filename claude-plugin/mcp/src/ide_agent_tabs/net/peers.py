from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from ..files import file_lock, write_atomically
from . import ingress
from .client import peer_cert_path, peers_dir
from .policy import (
    MEMBER_MAX_TABS,
    MEMBER_MESSAGES_PER_MINUTE,
    MEMBER_OPENS_PER_HOUR,
    FolderGrant,
    PeerGrant,
    PolicyError,
    grant_for,
    load_policy,
    policy_path,
    save_policy,
)
from .private_files import ensure_private_dir, make_private_file


class LabelTaken(PolicyError):
    pass


def _as_member(peer: PeerGrant) -> PeerGrant:
    return replace(
        peer,
        member=True,
        max_tabs=MEMBER_MAX_TABS,
        opens_per_hour=MEMBER_OPENS_PER_HOUR,
        messages_per_minute=MEMBER_MESSAGES_PER_MINUTE,
    )


def save_peer(
    home: str,
    device_id: str,
    pem: str,
    address: str,
    label: str,
    *,
    folders: Sequence[FolderGrant] = (),
    agents: Sequence[str] = (),
    member: bool = False,
) -> tuple[PeerGrant, bool]:
    ensure_private_dir(ingress.net_dir(home))
    with file_lock(policy_path(home)):
        policy = load_policy(home)
        current = grant_for(policy, device_id)
        if current is None and any(p.label == label for p in policy.peers):
            raise LabelTaken(f"the label {label} belongs to another peer")
        ensure_private_dir(peers_dir(home))
        target = peer_cert_path(home, device_id)
        write_atomically(str(target), pem)
        make_private_file(target)
        if current is None:
            result = PeerGrant(device_id=device_id, label=label, address=address, folders=tuple(folders), agents=tuple(agents))
            result = _as_member(result) if member else result
            peers = (*policy.peers, result)
        else:
            result = _as_member(replace(current, address=address)) if member and not current.member else replace(current, address=address)
            peers = tuple(result if p.device_id == device_id else p for p in policy.peers)
        save_policy(home, replace(policy, peers=peers))
    return result, current is None

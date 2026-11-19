from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from ..files import file_lock
from . import ingress
from .client import NetClientError, load_identity, local_device_id
from .hubstate import adopt_hub, ensure_hub_state
from .hubsync import member_ref
from .identity import der_from_pem
from .names import default_label, device_name_of
from .pairing import (
    ALREADY_IN_ANOTHER_HUB,
    HubMessage,
    HubRef,
    MemberRef,
    PairIdentity,
    PairingError,
    PeerInfo,
)
from .peers import save_peer
from .policy import Hub, grant_for, load_policy, policy_path, save_policy
from .private_files import ensure_private_dir


def local_pair_identity(home: str) -> PairIdentity:
    try:
        identity = load_identity(home)
        der = der_from_pem(identity.cert.read_text(encoding="ascii"))
    except (NetClientError, OSError, ValueError) as error:
        raise PairingError(f"this machine's network identity cannot be read: {error}") from error
    return PairIdentity(device_name_of(home), der, identity.cert, identity.key, lambda: identity.passphrase)


class HubJoiner:
    def __init__(
        self,
        home: str,
        on_saved: Callable[[str, str], None] | None = None,
        introduce: Callable[[str], None] | None = None,
    ) -> None:
        self.home = home
        self.on_saved = on_saved
        self.introduce = introduce

    def hub(self) -> HubRef | None:
        hub = load_policy(self.home).hub
        return None if hub is None else HubRef(hub.id, hub.name)

    def message(self, peer: PeerInfo) -> HubMessage:
        policy = load_policy(self.home)
        if policy.hub is None:
            raise PairingError("this machine is not in a hub")
        state = ensure_hub_state(self.home, policy.hub.id)
        members: list[MemberRef] = []
        for member in policy.peers:
            if not member.member or member.device_id == peer.device_id:
                continue
            ref = member_ref(self.home, member)
            if ref is not None:
                members.append(ref)
        return HubMessage(HubRef(policy.hub.id, policy.hub.name), state.key, tuple(members), state.epoch)

    def save(self, peer: PeerInfo, received: HubMessage | None) -> None:
        ensure_private_dir(ingress.net_dir(self.home))
        if received is not None:
            self._adopt(received)
        policy = load_policy(self.home)
        known = grant_for(policy, peer.device_id)
        label = known.label if known is not None else default_label(peer.name, policy)
        save_peer(self.home, peer.device_id, peer.pem, peer.address, label, member=True)
        if self.on_saved is not None:
            self.on_saved(label, peer.address)
        if received is None and self.introduce is not None:
            self.introduce(peer.device_id)

    def _adopt(self, received: HubMessage) -> None:
        with file_lock(policy_path(self.home)):
            policy = load_policy(self.home)
            if policy.hub is not None and policy.hub.id != received.hub.id:
                raise PairingError(f"{ALREADY_IN_ANOTHER_HUB}: this machine joined another hub while the join was running")
            if policy.hub is None:
                save_policy(self.home, replace(policy, hub=Hub(received.hub.id, received.hub.name)))
        own = local_device_id(self.home)
        others = tuple(m for m in received.members if m.device_id != own)
        adopt_hub(self.home, received.hub.id, received.key, others, received.epoch)
        for member in others:
            current = load_policy(self.home)
            if grant_for(current, member.device_id) is None:
                label = member.label
                if any(p.label == label for p in current.peers):
                    label = default_label(label, current)
                save_peer(self.home, member.device_id, member.pem, member.address, label, member=True)

from __future__ import annotations

import ssl
from collections.abc import Callable
from dataclasses import replace

from ..files import file_lock
from . import ingress
from .client import NetClientError, load_identity, local_device_id, read_peer_pem
from .hubstate import adopt_hub, ensure_hub_key
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
    canonical_pem,
)
from .peers import save_peer
from .policy import DEFAULT_PORT, Hub, grant_for, load_policy, policy_path, save_policy
from .private_files import ensure_private_dir


def local_pair_identity(home: str) -> PairIdentity:
    try:
        identity = load_identity(home)
        der = der_from_pem(identity.cert.read_text(encoding="ascii"))
    except (NetClientError, OSError, ValueError) as error:
        raise PairingError(f"this machine's network identity cannot be read: {error}") from error
    return PairIdentity(device_name_of(home), der, identity.cert, identity.key, lambda: identity.passphrase)


class HubJoiner:
    def __init__(self, home: str, on_saved: Callable[[str, str], None] | None = None) -> None:
        self.home = home
        self.on_saved = on_saved

    def hub(self) -> HubRef | None:
        hub = load_policy(self.home).hub
        return None if hub is None else HubRef(hub.id, hub.name)

    def message(self, peer: PeerInfo) -> HubMessage:
        policy = load_policy(self.home)
        if policy.hub is None:
            raise PairingError("this machine is not in a hub")
        key = ensure_hub_key(self.home, policy.hub.id)
        members: list[MemberRef] = []
        for member in policy.peers:
            if not member.member or member.device_id == peer.device_id:
                continue
            text = read_peer_pem(self.home, member.device_id)
            if text is None:
                continue
            try:
                pem, _der = canonical_pem(ssl.DER_cert_to_PEM_cert(ssl.PEM_cert_to_DER_cert(text)))
            except (ValueError, PairingError):
                continue
            members.append(MemberRef(member.device_id, member.label, member.address, DEFAULT_PORT, pem))
        return HubMessage(HubRef(policy.hub.id, policy.hub.name), key, tuple(members))

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

    def _adopt(self, received: HubMessage) -> None:
        with file_lock(policy_path(self.home)):
            policy = load_policy(self.home)
            if policy.hub is not None and policy.hub.id != received.hub.id:
                raise PairingError(f"{ALREADY_IN_ANOTHER_HUB}: this machine joined another hub while the join was running")
            if policy.hub is None:
                save_policy(self.home, replace(policy, hub=Hub(received.hub.id, received.hub.name)))
        own = local_device_id(self.home)
        adopt_hub(self.home, received.hub.id, received.key, tuple(m for m in received.members if m.device_id != own))

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any

from ..clock import now_ms
from ..files import file_lock, read_text_if_exists, write_atomically
from ..liveness import pid_alive
from ..messaging.db import MailError
from ..messaging.sessions import live_sessions
from ..messaging.store import send_message
from . import ingress
from .client import NetClientError, local_device_id, read_peer_pem
from .hub import default_member_folder
from .hubstate import HubStateError, Pending, add_pending, ensure_hub_state, remember_member
from .hubsync import member_ref
from .netid import current_network_id
from .pairing import PairingError
from .peers import save_peer
from .policy import Hub, PeerGrant, PolicyError, load_network_config, load_policy, policy_path, save_policy, update_network_section
from .private_files import ensure_private_dir
from .store import NetStore, NetStoreError, db_path
from .transport import network_enabled

MIGRATION_FILE = "hub-migration.json"
HUB_NAME = "home"
HUB_ID_DOMAIN = b"agent-tabs hub migration 1\0"
NOTICE_SENDER = "net-notice"
ERRORS = (PolicyError, HubStateError, NetClientError, PairingError, NetStoreError, MailError, OSError, ValueError)


@dataclass(frozen=True)
class Marker:
    moved: tuple[str, ...] = ()
    noticed: bool = True
    network_pending: bool = False


def marker_path(home: str) -> str:
    return str(ingress.net_dir(home) / MIGRATION_FILE)


def read_marker(home: str) -> Marker | None:
    try:
        text = read_text_if_exists(marker_path(home))
    except OSError:
        return Marker()
    if text is None:
        return None
    try:
        value: Any = json.loads(text)
        moved = value["moved"]
        if not isinstance(value, dict) or not isinstance(moved, list) or not all(isinstance(label, str) for label in moved):  # pyright: ignore[reportUnknownVariableType]
            raise ValueError("unexpected shape")
        return Marker(tuple(moved), value.get("noticed") is not False, value.get("networkPending") is True)  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    except (ValueError, KeyError, TypeError):
        return Marker()


def write_marker(home: str, marker: Marker) -> None:
    ensure_private_dir(ingress.net_dir(home))
    body = {"moved": list(marker.moved), "noticed": marker.noticed, "networkPending": marker.network_pending}
    write_atomically(marker_path(home), json.dumps(body, indent=2) + "\n")


def migrated_hub_id(device_ids: Sequence[str]) -> str:
    return hashlib.sha256(HUB_ID_DOMAIN + min(device_ids).encode("ascii")).digest()[:16].hex()


def _local_folder(path: str) -> str | None:
    return path if os.path.isabs(path) and not path.startswith(("\\\\", "//")) and os.path.isdir(path) else None


def default_folders(peers: Sequence[PeerGrant], user_home: str, session_folder: str | None) -> list[str]:
    sources = [folder.path for peer in peers for folder in peer.folders]
    if not sources and session_folder is not None:
        sources = [session_folder]
    found: list[str] = []
    for source in sources:
        local = _local_folder(os.path.abspath(source))
        if local is None:
            continue
        chosen = _local_folder(default_member_folder(local, user_home))
        if chosen is not None and not any(os.path.normcase(chosen) == os.path.normcase(seen) for seen in found):
            found.append(chosen)
    return found


def _read_network_id(read: Callable[[str], str | None], bind: str) -> str | None:
    try:
        found = read(bind)
    except (OSError, ValueError):
        return None
    return None if found is None else str(found)


def migrate(
    home: str,
    *,
    network_id: Callable[[str], str | None] = current_network_id,
    user_home: str | None = None,
    session_folder: str | None = None,
) -> tuple[str, ...]:
    ensure_private_dir(ingress.net_dir(home))
    with file_lock(marker_path(home)):
        if read_marker(home) is not None:
            return ()
        policy = load_policy(home)
        movable = [peer for peer in policy.peers if not peer.member and read_peer_pem(home, peer.device_id) is not None]
        if policy.hub is not None or not movable:
            write_marker(home, Marker())
            return ()
        own = local_device_id(home)
        if own is None:
            return ()
        hub = Hub(migrated_hub_id([own, *(peer.device_id for peer in movable)]), HUB_NAME)
        with file_lock(policy_path(home)):
            current = load_policy(home)
            if current.hub is None:
                save_policy(home, replace(current, hub=hub))
        ensure_hub_state(home, hub.id)
        moved: list[PeerGrant] = []
        for peer in movable:
            pem = read_peer_pem(home, peer.device_id)
            if pem is None:
                continue
            saved, _created = save_peer(home, peer.device_id, pem, peer.address, peer.label, member=True)
            ref = member_ref(home, saved)
            if ref is not None:
                remember_member(home, ref)
            moved.append(saved)
        add_pending(home, [Pending(peer.device_id, "rekey", "", now_ms()) for peer in moved])
        config, _warnings = load_network_config(home)
        updates: dict[str, Any] = {}
        network_pending = False
        if not config.allowed_networks and network_enabled(config) and config.bind is not None:
            found = _read_network_id(network_id, config.bind)
            if found is None:
                network_pending = True
            else:
                updates["allowedNetworks"] = [found]
        folders = default_folders(movable, user_home if user_home is not None else os.path.expanduser("~"), session_folder)
        if folders and not config.member_folders:
            updates["memberFolders"] = folders
        if folders and not config.outbound_folders:
            updates["outboundFolders"] = folders
        if updates:
            update_network_section(home, updates)
        labels = tuple(peer.label for peer in moved)
        write_marker(home, Marker(labels, noticed=not labels, network_pending=network_pending))
        return labels


def complete_network(home: str, network_id: str | None) -> bool:
    with file_lock(marker_path(home)):
        marker = read_marker(home)
        if marker is None or not marker.network_pending:
            return False
        config, _warnings = load_network_config(home)
        if config.allowed_networks:
            write_marker(home, replace(marker, network_pending=False))
            return False
        if network_id is None:
            return False
        update_network_section(home, {"allowedNetworks": [str(network_id)]})
        write_marker(home, replace(marker, network_pending=False))
        return True


def notice_text(labels: Sequence[str], member_folders: Sequence[str]) -> str:
    where = (
        f"in the member folders ({', '.join(member_folders)})"
        if member_folders
        else "in the member folders, which are empty, so run agent-tabs net folders add <absolute path> to allow some"
    )
    return (
        f"Agent Tabs moved these paired machines into a hub: {', '.join(labels)}. "
        f"They now have the full member role on this machine: each can list and message every session and open tabs {where}. "
        "Run agent-tabs net revoke <label> to remove one."
    )


def post_to_sessions(home: str, text: str) -> int:
    sessions = live_sessions(home, pid_alive, now_ms())
    owned: set[str] = set()
    if sessions and os.path.exists(db_path(home)):
        store = NetStore(home)
        try:
            owned = {s["id"] for s in sessions if store.remote_tab_peer(s["id"]) or store.opened_by(s["id"])}
        finally:
            store.close()
    sent = 0
    for session in sessions:
        if session["id"] in owned:
            continue
        try:
            send_message(home, {"from": {"id": NOTICE_SENDER, "agent": "remote", "path": ""}, "to": session["id"], "text": text})
            sent += 1
        except (MailError, OSError):
            continue
    return sent


def deliver_notice(home: str, post: Callable[[str, str], int] = post_to_sessions) -> bool:
    with file_lock(marker_path(home)):
        marker = read_marker(home)
        if marker is None or marker.noticed or not marker.moved:
            return True
        config, _warnings = load_network_config(home)
        if post(home, notice_text(marker.moved, config.member_folders)) == 0:
            return False
        write_marker(home, replace(marker, noticed=True))
        return True


def pending(home: str) -> bool:
    marker = read_marker(home)
    return marker is not None and (marker.network_pending or (bool(marker.moved) and not marker.noticed))


@dataclass
class Migrator:
    network_id: Callable[[str], str | None] = current_network_id
    user_home: str | None = None
    post: Callable[[str, str], int] = post_to_sessions

    def start(self, home: str, session_folder: str | None = None) -> tuple[str, ...]:
        moved = migrate(home, network_id=self.network_id, user_home=self.user_home, session_folder=session_folder)
        deliver_notice(home, self.post)
        return moved

    def tick(self, home: str, network_id: str | None) -> None:
        if not pending(home):
            return
        complete_network(home, network_id)
        deliver_notice(home, self.post)

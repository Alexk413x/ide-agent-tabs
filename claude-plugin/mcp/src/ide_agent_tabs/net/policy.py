from __future__ import annotations

import ipaddress
import json
import os
import re
from dataclasses import dataclass
from typing import Any, Callable

from ..files import file_lock, read_text_if_exists, write_atomically
from ..jev.settings import CONFIG_FILE
from ..jsjson import parse, stringify
from ..messaging.sessions import SESSION_ID

NET_DIR = "net"
POLICY_FILE = "policy.json"
DEFAULT_PORT = 47829
DEFAULT_MAX_REMOTE_TABS = 4
MAX_REMOTE_TABS = 64
MEMBER_MAX_TABS = 4
MEMBER_OPENS_PER_HOUR = 30
MEMBER_MESSAGES_PER_MINUTE = 60
SUPPORTED_AGENTS = frozenset({"claude", "codex"})
MAX_LIMIT = 1000
MAX_UINT64 = 2**64 - 1

_LABEL = re.compile(r"[a-z0-9-]{1,32}")
ALIAS = re.compile(r"[a-z0-9-]{1,32}")
_ALIAS = ALIAS
_DEVICE_ID = re.compile(r"[A-Za-z0-9-]{1,96}")
_SESSION_ID = SESSION_ID
NETWORK_ID = re.compile(r"[A-Za-z0-9{}:._-]{1,128}")
_NETWORK_ID = NETWORK_ID
_MAX_MODEL_CHARS = 128
_CONFIG_KEYS = frozenset(
    {
        "network",
        "bind",
        "port",
        "openssl",
        "outboundFolders",
        "maxRemoteTabs",
        "deviceName",
        "memberFolders",
        "allowedNetworks",
        "discoverable",
        "autoUpdate",
    }
)
_PEER_KEYS = frozenset(
    {"label", "address", "folders", "agents", "models", "send", "maxTabs", "opensPerHour", "messagesPerMinute", "approveEachOpen", "member"}
)
_LATER_STAGE_PEER_KEYS = frozenset({"person", "expires", "paused"})
_FOLDER_KEYS = frozenset({"alias", "path", "dev", "ino"})
_POLICY_KEYS = frozenset({"generation", "peers", "hub"})
_HUB_KEYS = frozenset({"id", "name"})
_HUB_ID = re.compile(r"[0-9a-f]{32}")
_PRIVATE_V4 = tuple(ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
_UNIQUE_LOCAL_V6 = ipaddress.ip_network("fc00::/7")


class PolicyError(ValueError):
    pass


@dataclass(frozen=True)
class NetworkConfig:
    network: str = "off"
    bind: str | None = None
    port: int = DEFAULT_PORT
    openssl: str | None = None
    outbound_folders: tuple[str, ...] = ()
    max_remote_tabs: int = DEFAULT_MAX_REMOTE_TABS
    member_folders: tuple[str, ...] = ()
    allowed_networks: tuple[str, ...] = ()
    discoverable: bool = False


@dataclass(frozen=True)
class FolderGrant:
    alias: str
    path: str
    dev: int
    ino: int


@dataclass(frozen=True)
class PeerGrant:
    device_id: str
    label: str
    address: str
    folders: tuple[FolderGrant, ...] = ()
    agents: tuple[str, ...] = ()
    models: tuple[str, ...] = ()
    send: tuple[str, ...] = ()
    max_tabs: int = 1
    opens_per_hour: int = 6
    messages_per_minute: int = 10
    approve_each_open: bool = False
    member: bool = False


@dataclass(frozen=True)
class Hub:
    id: str
    name: str


@dataclass(frozen=True)
class Policy:
    peers: tuple[PeerGrant, ...] = ()
    generation: int = 0
    hub: Hub | None = None


def is_allowed_bind(address: str) -> bool:
    if not isinstance(address, str) or "%" in address:  # pyright: ignore[reportUnnecessaryIsInstance]
        return False
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv4Address):
        return any(ip in net for net in _PRIVATE_V4)
    return ip in _UNIQUE_LOCAL_V6


def _whole(value: Any, low: int, high: int) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and low <= value <= high else None


def _config_section(config: Any, warnings: list[str]) -> dict[str, Any] | None:
    if not isinstance(config, dict):
        warnings.append(f"Ignoring {CONFIG_FILE}: it must hold a JSON object, so the network is off")
        return None
    section = config.get("network")  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
    if section is None:
        return None
    if not isinstance(section, dict):
        warnings.append(f"Ignoring network in {CONFIG_FILE}, so the network is off: it must be an object")
        return None
    return section  # pyright: ignore[reportUnknownVariableType]


def _network_mode(value: Any, invalid: list[str]) -> str:
    if value is None or value == "off":
        return "off"
    if value == "lan":
        return "lan"
    if value == "tailscale":
        invalid.append("network.network tailscale is a later stage that version 1 refuses")
    else:
        invalid.append('network.network must be "off" or "lan"')
    return "off"


def _bind(value: Any, invalid: list[str]) -> str | None:
    if value is None:
        return None
    if isinstance(value, str) and is_allowed_bind(value):
        return value
    invalid.append("network.bind must be a private IPv4 or IPv6 unique local address")
    return None


def _port(value: Any, invalid: list[str]) -> int:
    if value is None:
        return DEFAULT_PORT
    port = _whole(value, 1, 65535)
    if port is not None:
        return port
    invalid.append("network.port must be a whole number from 1 to 65535")
    return DEFAULT_PORT


def _openssl(value: Any, invalid: list[str]) -> str | None:
    if value is None:
        return None
    if isinstance(value, str) and value and "\0" not in value and os.path.isabs(value):
        return value
    invalid.append("network.openssl must be an absolute path")
    return None


def _is_local_path(value: Any) -> bool:
    return isinstance(value, str) and bool(value) and "\0" not in value and os.path.isabs(value)


def _outbound_folders(value: Any, invalid: list[str]) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, list) and all(_is_local_path(f) for f in value):  # pyright: ignore[reportUnknownVariableType]
        return tuple(value)  # pyright: ignore[reportUnknownArgumentType]
    invalid.append("network.outboundFolders must be a list of absolute paths")
    return ()


def _member_folders(value: Any, invalid: list[str]) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, list) and all(_is_local_path(f) for f in value):  # pyright: ignore[reportUnknownVariableType]
        return tuple(value)  # pyright: ignore[reportUnknownArgumentType]
    invalid.append("network.memberFolders must be a list of absolute paths")
    return ()


def _allowed_networks(value: Any, invalid: list[str]) -> tuple[str, ...]:
    if value is None:
        return ()
    if (
        isinstance(value, list)
        and all(isinstance(n, str) and _NETWORK_ID.fullmatch(n) for n in value)  # pyright: ignore[reportUnknownVariableType]
        and len(set(value)) == len(value)  # pyright: ignore[reportUnknownArgumentType]
    ):
        return tuple(value)  # pyright: ignore[reportUnknownArgumentType]
    invalid.append("network.allowedNetworks must be a list of unique network ids of 1 to 128 characters from A-Z a-z 0-9 { } : . _ -")
    return ()


def _discoverable(value: Any, invalid: list[str]) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    invalid.append("network.discoverable must be true or false")
    return False


def _max_remote_tabs(value: Any, invalid: list[str]) -> int:
    if value is None:
        return DEFAULT_MAX_REMOTE_TABS
    tabs = _whole(value, 0, MAX_REMOTE_TABS)
    if tabs is not None:
        return tabs
    invalid.append(f"network.maxRemoteTabs must be a whole number from 0 to {MAX_REMOTE_TABS}")
    return DEFAULT_MAX_REMOTE_TABS


def load_network_config(home: str) -> tuple[NetworkConfig, list[str]]:
    warnings: list[str] = []
    path = os.path.join(home, CONFIG_FILE)
    try:
        text = read_text_if_exists(path)
    except OSError as e:
        return NetworkConfig(), [f"Ignoring {path}, so the network is off: {e}"]
    if text is None:
        return NetworkConfig(), warnings
    try:
        config = parse(text)
    except ValueError as e:
        return NetworkConfig(), [f"Ignoring {path}, so the network is off: it is not JSON: {e}"]
    section = _config_section(config, warnings)
    if section is None:
        return NetworkConfig(), warnings
    for key in section:
        if key not in _CONFIG_KEYS:
            warnings.append(f"Ignoring unknown key network.{key} in {CONFIG_FILE}")
    invalid: list[str] = []
    result = NetworkConfig(
        network=_network_mode(section.get("network"), invalid),
        bind=_bind(section.get("bind"), invalid),
        port=_port(section.get("port"), invalid),
        openssl=_openssl(section.get("openssl"), invalid),
        outbound_folders=_outbound_folders(section.get("outboundFolders"), invalid),
        max_remote_tabs=_max_remote_tabs(section.get("maxRemoteTabs"), invalid),
        member_folders=_member_folders(section.get("memberFolders"), invalid),
        allowed_networks=_allowed_networks(section.get("allowedNetworks"), invalid),
        discoverable=_discoverable(section.get("discoverable"), invalid),
    )
    if invalid:
        warnings.extend(f"Ignoring network in {CONFIG_FILE}, so the network is off: {reason}" for reason in invalid)
        return NetworkConfig(), warnings
    return result, warnings


def update_network_section(home: str, updates: dict[str, Any]) -> None:
    path = os.path.join(home, CONFIG_FILE)
    with file_lock(path):
        text = read_text_if_exists(path)
        config: Any = parse(text) if text and text.strip() else {}
        if not isinstance(config, dict):
            raise PolicyError(f"{path} must hold a JSON object")
        section: Any = config.get("network")  # pyright: ignore[reportUnknownMemberType]
        if section is None:
            section = {}
        if not isinstance(section, dict):
            raise PolicyError(f'"network" in {path} must be an object')
        section.update(updates)  # pyright: ignore[reportUnknownMemberType]
        config["network"] = section
        write_atomically(path, stringify(config, 2) + "\n")


def policy_path(home: str) -> str:
    return os.path.join(home, NET_DIR, POLICY_FILE)


def _no_constant(name: str) -> Any:
    raise ValueError(f"{name} is not valid JSON")


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate key {key!r}")
        out[key] = value
    return out


def _object(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PolicyError(f"{where} must be an object")
    return value  # pyright: ignore[reportUnknownVariableType]


def _check_keys(obj: dict[str, Any], allowed: frozenset[str], where: str, later: frozenset[str] = frozenset()) -> None:
    for key in obj:
        if key in later:
            raise PolicyError(f"{where}: {key} belongs to a later stage that version 1 refuses")
        if key not in allowed:
            raise PolicyError(f"{where}: unknown key {key}")


def _limit(obj: dict[str, Any], key: str, default: int, where: str) -> int:
    value = _whole(obj.get(key, default), 0, MAX_LIMIT)
    if value is None:
        raise PolicyError(f"{where}: {key} must be a whole number from 0 to {MAX_LIMIT}")
    return value


def _string_list(obj: dict[str, Any], key: str, where: str, check: Callable[[str], Any]) -> tuple[str, ...]:
    value = obj.get(key, [])
    if not isinstance(value, list):
        raise PolicyError(f"{where}: {key} must be a list")
    items: list[str] = []
    for item in value:  # pyright: ignore[reportUnknownVariableType]
        if not isinstance(item, str) or not check(item):
            raise PolicyError(f"{where}: {key} holds an invalid entry")
        if item in items:
            raise PolicyError(f"{where}: {key} holds a duplicate entry")
        items.append(item)
    return tuple(items)


def model_ok(text: str) -> bool:
    return 0 < len(text) <= _MAX_MODEL_CHARS and text.isprintable()


def _folder(value: Any, where: str) -> FolderGrant:
    obj = _object(value, f"{where}: a folder")
    _check_keys(obj, _FOLDER_KEYS, f"{where}: a folder")
    alias, path, dev, ino = obj.get("alias"), obj.get("path"), obj.get("dev"), obj.get("ino")
    if not isinstance(alias, str) or not _ALIAS.fullmatch(alias):
        raise PolicyError(f"{where}: a folder alias must match [a-z0-9-]{{1,32}}")
    if not isinstance(path, str) or not path or "\0" in path or not os.path.isabs(path) or path.startswith(("\\\\", "//")):
        raise PolicyError(f"{where}: folder {alias} needs a local absolute path")
    dev, ino = _whole(dev, 0, MAX_UINT64), _whole(ino, 0, MAX_UINT64)
    if dev is None or ino is None:
        raise PolicyError(f"{where}: folder {alias} needs whole-number dev and ino")
    return FolderGrant(alias, path, dev, ino)


def _folders(obj: dict[str, Any], where: str) -> tuple[FolderGrant, ...]:
    value = obj.get("folders", [])
    if not isinstance(value, list):
        raise PolicyError(f"{where}: folders must be a list")
    folders = tuple(_folder(f, where) for f in value)  # pyright: ignore[reportUnknownVariableType]
    if len({f.alias for f in folders}) != len(folders):
        raise PolicyError(f"{where}: folders holds a duplicate alias")
    return folders


def _address(value: Any, where: str) -> str:
    try:
        ip = ipaddress.ip_address(value) if isinstance(value, str) and "%" not in value else None
    except ValueError:
        ip = None
    if ip is None:
        raise PolicyError(f"{where}: address must be an IPv4 or IPv6 literal")
    if ip.is_unspecified or ip.is_multicast:
        raise PolicyError(f"{where}: address must name one machine")
    return value


def _peer(device_id: str, value: Any) -> PeerGrant:
    where = f"peer {device_id[:12]}"
    if not _DEVICE_ID.fullmatch(device_id):
        raise PolicyError(f"{where}: the device id is malformed")
    obj = _object(value, where)
    _check_keys(obj, _PEER_KEYS, where, _LATER_STAGE_PEER_KEYS)
    label = obj.get("label")
    if not isinstance(label, str) or not _LABEL.fullmatch(label):
        raise PolicyError(f"{where}: label must match [a-z0-9-]{{1,32}}")
    approve = obj.get("approveEachOpen", False)
    if not isinstance(approve, bool):
        raise PolicyError(f"{where}: approveEachOpen must be true or false")
    member = obj.get("member", False)
    if not isinstance(member, bool):
        raise PolicyError(f"{where}: member must be true or false")
    agents = _string_list(obj, "agents", where, lambda a: a in SUPPORTED_AGENTS)
    return PeerGrant(
        device_id=device_id,
        label=label,
        address=_address(obj.get("address"), where),
        folders=_folders(obj, where),
        agents=agents,
        models=_string_list(obj, "models", where, model_ok),
        send=_string_list(obj, "send", where, _SESSION_ID.fullmatch),
        max_tabs=_limit(obj, "maxTabs", 1, where),
        opens_per_hour=_limit(obj, "opensPerHour", 6, where),
        messages_per_minute=_limit(obj, "messagesPerMinute", 10, where),
        approve_each_open=approve,
        member=member,
    )


def _hub(value: Any) -> Hub:
    obj = _object(value, "policy: hub")
    _check_keys(obj, _HUB_KEYS, "policy: hub")
    hub_id, name = obj.get("id"), obj.get("name")
    if not isinstance(hub_id, str) or not _HUB_ID.fullmatch(hub_id):
        raise PolicyError("policy: hub id must be 32 lowercase hexadecimal characters")
    if not isinstance(name, str) or not _LABEL.fullmatch(name):
        raise PolicyError("policy: hub name must match [a-z0-9-]{1,32}")
    return Hub(hub_id, name)


def _policy(value: Any) -> Policy:
    obj = _object(value, "policy")
    _check_keys(obj, _POLICY_KEYS, "policy")
    generation = _whole(obj.get("generation", 0), 0, MAX_UINT64)
    if generation is None:
        raise PolicyError("policy: generation must be a whole number")
    peers_obj = _object(obj.get("peers", {}), "policy: peers")
    peers = tuple(_peer(device_id, entry) for device_id, entry in peers_obj.items())
    if len({p.device_id.casefold() for p in peers}) != len(peers):
        raise PolicyError("policy: duplicate device id")
    if len({p.label for p in peers}) != len(peers):
        raise PolicyError("policy: duplicate label")
    hub = _hub(obj["hub"]) if "hub" in obj else None
    if hub is None and any(p.member for p in peers):
        raise PolicyError("policy: a member peer needs a hub")
    return Policy(peers, generation, hub)


def load_policy(home: str) -> Policy:
    path = policy_path(home)
    try:
        text = read_text_if_exists(path)
    except OSError as e:
        raise PolicyError(f"{path} can't be read: {e}") from e
    if text is None:
        return Policy()
    try:
        value = json.loads(text, parse_constant=_no_constant, object_pairs_hook=_no_duplicates)
    except ValueError as e:
        raise PolicyError(f"{path} is not valid: {e}") from e
    return _policy(value)


def _peer_object(peer: PeerGrant) -> dict[str, Any]:
    obj: dict[str, Any] = {
        "label": peer.label,
        "address": peer.address,
        "folders": [{"alias": f.alias, "path": f.path, "dev": f.dev, "ino": f.ino} for f in peer.folders],
        "agents": list(peer.agents),
        "models": list(peer.models),
        "send": list(peer.send),
        "maxTabs": peer.max_tabs,
        "opensPerHour": peer.opens_per_hour,
        "messagesPerMinute": peer.messages_per_minute,
        "approveEachOpen": peer.approve_each_open,
    }
    if peer.member:
        obj["member"] = True
    return obj


def save_policy(home: str, policy: Policy) -> None:
    peers: dict[str, Any] = {}
    for peer in policy.peers:
        if peer.device_id in peers:
            raise PolicyError("policy: duplicate device id")
        peers[peer.device_id] = _peer_object(peer)
    obj: dict[str, Any] = {"generation": policy.generation, "peers": peers}
    if policy.hub is not None:
        obj["hub"] = {"id": policy.hub.id, "name": policy.hub.name}
    _policy(obj)
    write_atomically(policy_path(home), json.dumps(obj, indent=2) + "\n")


def grant_for(policy: Policy, device_id: str) -> PeerGrant | None:
    return next((p for p in policy.peers if p.device_id == device_id), None)


def folder_grant(peer: PeerGrant, alias: str) -> FolderGrant | None:
    return next((f for f in peer.folders if f.alias == alias), None)

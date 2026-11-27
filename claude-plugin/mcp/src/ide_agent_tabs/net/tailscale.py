from __future__ import annotations

import ipaddress
import json
import ntpath
import os
import re
import sys
from collections.abc import Callable, Collection, Generator, Mapping
from dataclasses import dataclass
from typing import Any

from .. import processes

TIMEOUT_S = 10.0
TAILSCALE_NET = ipaddress.IPv4Network("100.64.0.0/10")
TAILSCALE_SCOPE = str(TAILSCALE_NET)

STATUS_SELF = "Self"
STATUS_PEERS = "Peer"
NODE_ID = "ID"
NODE_USER_ID = "UserID"
NODE_HOST_NAME = "HostName"
NODE_DNS_NAME = "DNSName"
NODE_ADDRESSES = "TailscaleIPs"
NODE_TAGS = "Tags"
NODE_SHAREE = "ShareeNode"
NODE_ONLINE = "Online"

WHOIS_NODE = "Node"
WHOIS_PROFILE = "UserProfile"
WHOIS_NODE_ID = "ID"
WHOIS_NODE_NAME = "Name"
WHOIS_NODE_USER = "User"
WHOIS_NODE_ADDRESSES = "Addresses"
WHOIS_NODE_TAGS = "Tags"
WHOIS_NODE_SHARER = "Sharer"
WHOIS_PROFILE_ID = "ID"
WHOIS_PROFILE_LOGIN = "LoginName"

_OCTET = r"(?:0|[1-9][0-9]{0,2})"
_IPV4 = re.compile(rf"{_OCTET}(?:\.{_OCTET}){{3}}")

Runner = Callable[[list[str], float], str]


class TailscaleError(Exception):
    pass


class TailscaleNotInstalled(TailscaleError):
    pass


@dataclass(frozen=True)
class Node:
    id: str
    user_id: int
    host_name: str
    dns_name: str
    addresses: tuple[str, ...]
    tags: tuple[str, ...]
    shared_in: bool
    online: bool


@dataclass(frozen=True)
class Status:
    self_node: Node
    peers: tuple[Node, ...]


@dataclass(frozen=True)
class Whois:
    node_id: str
    name: str
    user_id: int
    login_name: str
    addresses: tuple[str, ...]
    tags: tuple[str, ...]
    shared_in: bool


@dataclass(frozen=True)
class OwnDevice:
    host_name: str
    address: str
    online: bool


def _parse_ipv4(address: object) -> ipaddress.IPv4Address | None:
    if not isinstance(address, str) or _IPV4.fullmatch(address) is None:
        return None
    return ipaddress.IPv4Address(address)


def is_tailscale_address(address: object) -> bool:
    ip = _parse_ipv4(address)
    return ip is not None and ip in TAILSCALE_NET


def binary_paths(platform: str, environ: Mapping[str, str]) -> list[str]:
    if platform == "win32":
        program_files = environ.get("ProgramFiles", "")
        return [ntpath.join(program_files, "Tailscale", "tailscale.exe")] if ntpath.isabs(program_files) else []
    if platform == "darwin":
        return [
            "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
            "/usr/local/bin/tailscale",
            "/opt/homebrew/bin/tailscale",
        ]
    if platform.startswith("linux"):
        return ["/usr/bin/tailscale", "/usr/sbin/tailscale"]
    return []


def find_binary(
    platform: str = sys.platform,
    environ: Mapping[str, str] = os.environ,
    is_file: Callable[[str], bool] = os.path.isfile,
) -> str:
    paths = binary_paths(platform, environ)
    found = next((path for path in paths if is_file(path)), None)
    if found is None:
        searched = ", ".join(paths) or f"a known location on {platform}"
        raise TailscaleNotInstalled(f"Tailscale isn't installed: no tailscale program exists at {searched}")
    return found


def run_command(argv: list[str], timeout_s: float) -> str:
    try:
        result = processes.run(argv[0], argv[1:], timeout=timeout_s)
    except (OSError, TimeoutError) as error:
        raise TailscaleError(f"tailscale did not answer: {error}") from error
    if result.code != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no output"
        raise TailscaleError(f"tailscale exited with code {result.code}: {detail[:200]}")
    return result.stdout


def _json(text: str, what: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except (ValueError, TypeError) as error:
        raise TailscaleError(f"tailscale returned {what} that is not JSON") from error
    if not isinstance(value, dict):
        raise TailscaleError(f"tailscale returned {what} that is not a JSON object")
    return value  # pyright: ignore[reportUnknownVariableType]


def _field(data: Mapping[str, Any], name: str, what: str) -> Any:
    if name not in data:
        raise TailscaleError(f"{what} has no {name} field")
    return data[name]


def _text(data: Mapping[str, Any], name: str, what: str) -> str:
    value = _field(data, name, what)
    if not isinstance(value, str) or not value:
        raise TailscaleError(f"{what} has a {name} field that is not a non-empty string")
    return value


def _whole(data: Mapping[str, Any], name: str, what: str) -> int:
    value = _field(data, name, what)
    if isinstance(value, bool) or not isinstance(value, int):
        raise TailscaleError(f"{what} has a {name} field that is not a whole number")
    return value


def _identifier(data: Mapping[str, Any], name: str, what: str) -> str:
    value = _field(data, name, what)
    if isinstance(value, bool) or not isinstance(value, (str, int)) or value == "":
        raise TailscaleError(f"{what} has a {name} field that is not an identifier")
    return str(value)


def _flag(data: Mapping[str, Any], name: str, what: str) -> bool:
    value = _field(data, name, what)
    if not isinstance(value, bool):
        raise TailscaleError(f"{what} has a {name} field that is not true or false")
    return value


def _optional_flag(data: Mapping[str, Any], name: str, what: str) -> bool:
    return _flag(data, name, what) if name in data else False


def _optional_tags(data: Mapping[str, Any], name: str, what: str) -> tuple[str, ...]:
    if name not in data:
        return ()
    value = data[name]
    if not isinstance(value, list) or not all(isinstance(tag, str) for tag in value):  # pyright: ignore[reportUnknownVariableType]
        raise TailscaleError(f"{what} has a {name} field that is not a list of strings")
    return tuple(value)  # pyright: ignore[reportUnknownArgumentType]


def _addresses(data: Mapping[str, Any], name: str, what: str, *, prefixed: bool) -> tuple[str, ...]:
    value = _field(data, name, what)
    if not isinstance(value, list):
        raise TailscaleError(f"{what} has a {name} field that is not a list")
    found: list[str] = []
    for entry in value:  # pyright: ignore[reportUnknownVariableType]
        if not isinstance(entry, str):
            raise TailscaleError(f"{what} has an address in {name} that is not a string")
        host, separator, length = entry.partition("/")
        if separator and not (prefixed and length.isdigit()):
            raise TailscaleError(f"{what} has the address {entry!r} in {name}, which is not an IP address")
        try:
            ipaddress.ip_address(host)
        except ValueError as error:
            raise TailscaleError(f"{what} has the address {entry!r} in {name}, which is not an IP address") from error
        found.append(host)
    return tuple(found)


def _node(value: object, what: str) -> Node:
    if not isinstance(value, dict):
        raise TailscaleError(f"{what} is not a JSON object")
    data: dict[str, Any] = value  # pyright: ignore[reportUnknownVariableType]
    return Node(
        id=_identifier(data, NODE_ID, what),
        user_id=_whole(data, NODE_USER_ID, what),
        host_name=_text(data, NODE_HOST_NAME, what),
        dns_name=_text(data, NODE_DNS_NAME, what),
        addresses=_addresses(data, NODE_ADDRESSES, what, prefixed=False),
        tags=_optional_tags(data, NODE_TAGS, what),
        shared_in=_optional_flag(data, NODE_SHAREE, what),
        online=_flag(data, NODE_ONLINE, what),
    )


def parse_ip4(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) != 1:
        raise TailscaleError(f"tailscale ip -4 returned {len(lines)} addresses, not one")
    if not is_tailscale_address(lines[0]):
        raise TailscaleError(f"tailscale ip -4 returned {lines[0]!r}, which is not an address in {TAILSCALE_SCOPE}")
    return lines[0]


def parse_status(text: str) -> Status:
    data = _json(text, "the status")
    self_node = _node(_field(data, STATUS_SELF, "the status"), f"the status {STATUS_SELF} entry")
    peers: list[Node] = []
    entries = data.get(STATUS_PEERS)
    if entries is not None:
        if not isinstance(entries, dict):
            raise TailscaleError(f"the status has a {STATUS_PEERS} field that is not a JSON object")
        peers = [_node(entry, f"the status {STATUS_PEERS} entry {key!r}") for key, entry in entries.items()]  # pyright: ignore[reportUnknownVariableType]
    return Status(self_node, tuple(peers))


def parse_whois(text: str, address: str | None = None) -> Whois:
    data = _json(text, "the whois answer")
    node = _field(data, WHOIS_NODE, "the whois answer")
    if not isinstance(node, dict) or not node:
        raise TailscaleError(f"the whois answer has a {WHOIS_NODE} field that is not exactly one node")
    profile = _field(data, WHOIS_PROFILE, "the whois answer")
    if not isinstance(profile, dict) or not profile:
        raise TailscaleError(f"the whois answer has a {WHOIS_PROFILE} field that is not exactly one user")
    node_data: dict[str, Any] = node  # pyright: ignore[reportUnknownVariableType]
    profile_data: dict[str, Any] = profile  # pyright: ignore[reportUnknownVariableType]
    what = "the whois node"
    user_id = _whole(node_data, WHOIS_NODE_USER, what)
    if _whole(profile_data, WHOIS_PROFILE_ID, "the whois user profile") != user_id:
        raise TailscaleError("the whois node and user profile name different users")
    addresses = _addresses(node_data, WHOIS_NODE_ADDRESSES, what, prefixed=True)
    if address is not None and address not in addresses:
        raise TailscaleError(f"the whois node does not carry the address {address}")
    return Whois(
        node_id=_identifier(node_data, WHOIS_NODE_ID, what),
        name=_text(node_data, WHOIS_NODE_NAME, what),
        user_id=user_id,
        login_name=_text(profile_data, WHOIS_PROFILE_LOGIN, "the whois user profile"),
        addresses=addresses,
        tags=_optional_tags(node_data, WHOIS_NODE_TAGS, what),
        shared_in=WHOIS_NODE_SHARER in node_data,
    )


def _owner_refusal(tags: tuple[str, ...], shared_in: bool, user_id: int, allowed_owner_ids: Collection[int]) -> str | None:
    if not allowed_owner_ids:
        return "no owner is allowed"
    if tags:
        return "the device is tagged, and tagged devices have no owner"
    if shared_in:
        return "the device is shared in from another tailnet"
    if user_id not in allowed_owner_ids:
        return "the device belongs to another user"
    return None


def check_owner(whois: Whois, allowed_owner_ids: Collection[int]) -> None:
    reason = _owner_refusal(whois.tags, whois.shared_in, whois.user_id, allowed_owner_ids)
    if reason is not None:
        raise TailscaleError(f"Tailscale refuses {whois.name}: {reason}")


def own_user_id(node: Node) -> int:
    if node.tags:
        raise TailscaleError("this machine is tagged, so it has no owner")
    if node.shared_in:
        raise TailscaleError("this machine is shared in from another tailnet, so it has no owner here")
    return node.user_id


def own_devices(status: Status, allowed_owner_ids: Collection[int]) -> list[OwnDevice]:
    found: list[OwnDevice] = []
    for peer in status.peers:
        if _owner_refusal(peer.tags, peer.shared_in, peer.user_id, allowed_owner_ids) is not None:
            continue
        address = next((candidate for candidate in peer.addresses if is_tailscale_address(candidate)), None)
        if address is not None:
            found.append(OwnDevice(peer.host_name, address, peer.online))
    return found


class TailscaleClient:
    def __init__(self, runner: Runner = run_command, locate: Callable[[], str] = find_binary, timeout_s: float = TIMEOUT_S) -> None:
        self._runner = runner
        self._locate = locate
        self._timeout_s = timeout_s

    def _run(self, *args: str) -> str:
        binary = self._locate()
        try:
            output = self._runner([binary, *args], self._timeout_s)
        except OSError as error:
            raise TailscaleError(f"tailscale did not answer: {error}") from error
        if not isinstance(output, str):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise TailscaleError("tailscale returned output that is not text")
        return output

    def ip4(self) -> str:
        return parse_ip4(self._run("ip", "-4"))

    def self_status(self) -> Node:
        return parse_status(self._run("status", "--self=true", "--peers=false", "--json")).self_node

    def status(self) -> Status:
        return parse_status(self._run("status", "--json"))

    def whois(self, address: str) -> Whois:
        if not is_tailscale_address(address):
            raise TailscaleError(f"{address!r} is not an address in {TAILSCALE_SCOPE}")
        return parse_whois(self._run("whois", "--json", address), address)


class TailscaleDiscovery:
    def __init__(self, client: TailscaleClient, allowed_owner_ids: Collection[int]) -> None:
        self.client = client
        self.allowed_owner_ids = frozenset(allowed_owner_ids)
        self.error: str | None = None

    def scan(self) -> Generator[OwnDevice, None, None]:
        self.error = None
        try:
            devices = own_devices(self.client.status(), self.allowed_owner_ids)
        except TailscaleError as error:
            self.error = str(error)
            return
        yield from (device for device in devices if device.online)

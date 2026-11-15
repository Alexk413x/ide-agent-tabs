from __future__ import annotations

import ipaddress
import json
import os
import re
from dataclasses import dataclass
from typing import Any

from .login_task import Environment, LoginTaskError

PROFILE_PRIVATE = "Private"
PROFILE_PUBLIC = "Public"
PROFILE_DOMAIN = "Domain"
PROFILE_UNKNOWN = "unknown"
QUERY_TIMEOUT_S = 15.0
MIN_PREFIX = 8
MAX_PREFIX = 30

_ADDRESS_ENV = "AGENT_TABS_ADDRESS"
_ALIAS = re.compile(r"[A-Za-z0-9._()#-](?:[A-Za-z0-9 ._()#-]{0,62}[A-Za-z0-9._()#-])?")
_PRIVATE_V4 = tuple(ipaddress.IPv4Network(text) for text in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
_VIRTUAL_WORDS = (
    "hyper-v",
    "vethernet",
    "virtual",
    "vpn",
    "tap-",
    "tap ",
    "wireguard",
    "wintun",
    "vmware",
    "virtualbox",
    "docker",
    "wsl",
    "tailscale",
    "loopback",
    "zerotier",
)
_VIRTUAL_PREFIXES = ("lo", "veth", "docker", "br-", "virbr", "tun", "tap", "wg", "tailscale", "utun", "vmnet", "vboxnet", "zt")
_IP_TOOLS = ("/usr/sbin/ip", "/sbin/ip", "/usr/bin/ip", "/bin/ip")

# NetworkCategory goes through [string] because ConvertTo-Json writes an enum as a number.
_QUERY_SCRIPT = (
    "$ErrorActionPreference = 'Stop'; "
    f"$a = @(Get-NetIPAddress -AddressFamily IPv4 -IPAddress $env:{_ADDRESS_ENV})[0]; "
    "$n = @(Get-NetAdapter -InterfaceIndex $a.InterfaceIndex -ErrorAction SilentlyContinue)[0]; "
    "$c = @(Get-NetConnectionProfile -InterfaceIndex $a.InterfaceIndex -ErrorAction SilentlyContinue)[0]; "
    "[pscustomobject]@{ "
    "alias = [string]$a.InterfaceAlias; "
    "address = [string]$a.IPAddress; "
    "prefix = [int]$a.PrefixLength; "
    "profile = $(if ($c) { [string]$c.NetworkCategory } else { '' }); "
    "hardware = $(if ($n) { [bool]$n.HardwareInterface } else { $null }); "
    "description = $(if ($n) { [string]$n.InterfaceDescription } else { '' }) "
    "} | ConvertTo-Json -Compress"
)


class InterfaceError(Exception):
    pass


@dataclass(frozen=True)
class InterfaceInfo:
    alias: str
    address: str
    prefix_len: int
    subnet: str
    profile: str
    physical: bool


def is_safe_alias(alias: str) -> bool:
    return _ALIAS.fullmatch(alias) is not None


def _ipv4(address: str) -> ipaddress.IPv4Address:
    try:
        return ipaddress.IPv4Address(address)
    except (ValueError, TypeError) as error:
        raise InterfaceError(f"{address!r} is not an IPv4 address") from error


def _category(value: object) -> str:
    text = str(value).strip().lower() if isinstance(value, (str, int)) and not isinstance(value, bool) else ""
    names = {
        "private": PROFILE_PRIVATE,
        "1": PROFILE_PRIVATE,
        "public": PROFILE_PUBLIC,
        "0": PROFILE_PUBLIC,
        "domainauthenticated": PROFILE_DOMAIN,
        "domain": PROFILE_DOMAIN,
        "2": PROFILE_DOMAIN,
    }
    return names.get(text, PROFILE_UNKNOWN)


def _looks_virtual(text: str) -> bool:
    lowered = text.lower()
    return any(word in lowered for word in _VIRTUAL_WORDS)


def _object(text: str) -> dict[str, Any] | None:
    for candidate in (text.strip(), *reversed([line.strip() for line in text.splitlines() if line.strip()])):
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        return value if isinstance(value, dict) else None  # pyright: ignore[reportUnknownVariableType]
    return None


def _info(alias: str, address: str, prefix: int, profile: str, physical: bool) -> InterfaceInfo:
    network = ipaddress.IPv4Network(f"{address}/{prefix}", strict=False)
    return InterfaceInfo(alias, address, prefix, str(network), profile, physical)


def parse_windows_interface(text: str, address: str) -> InterfaceInfo:
    data = _object(text)
    if data is None:
        raise InterfaceError("Windows returned no readable data about the network interface")
    alias, found, prefix, description = data.get("alias"), data.get("address"), data.get("prefix"), data.get("description")
    if not isinstance(alias, str) or not is_safe_alias(alias):
        raise InterfaceError(f"the network interface name {alias!r} is not one Agent Tabs accepts")
    if found != address:
        raise InterfaceError(f"Windows answered for {found!r}, not for {address}")
    if not isinstance(prefix, int) or isinstance(prefix, bool) or not 0 < prefix <= 32:
        raise InterfaceError(f"Windows returned the prefix length {prefix!r}, which is not 1 to 32")
    hardware = data.get("hardware")
    physical = hardware is True and not _looks_virtual(alias) and not (isinstance(description, str) and _looks_virtual(description))
    return _info(alias, address, prefix, _category(data.get("profile")), physical)


def _windows_interface(address: str, env: Environment) -> InterfaceInfo:
    shell = os.path.join(env.windows_directory(), "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    try:
        result = env.run(
            shell,
            ["-NoProfile", "-NonInteractive", "-Command", _QUERY_SCRIPT],
            env={**os.environ, _ADDRESS_ENV: address},
            timeout=QUERY_TIMEOUT_S,
        )
    except (OSError, TimeoutError) as error:
        raise InterfaceError(f"Windows could not describe the interface of {address}: {error}") from error
    if result.code != 0:
        raise InterfaceError(f"Windows has no network interface with the address {address}")
    return parse_windows_interface(result.stdout, address)


def parse_ip_json(text: str, address: str) -> InterfaceInfo | None:
    try:
        rows = json.loads(text)
    except ValueError:
        return None
    if not isinstance(rows, list):
        return None
    for row in rows:  # pyright: ignore[reportUnknownVariableType]
        if not isinstance(row, dict):
            continue
        name = row.get("ifname")  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
        entries = row.get("addr_info")  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
        if not isinstance(name, str) or not is_safe_alias(name) or not isinstance(entries, list):
            continue
        for entry in entries:  # pyright: ignore[reportUnknownVariableType]
            if not isinstance(entry, dict) or entry.get("family") != "inet" or entry.get("local") != address:  # pyright: ignore[reportUnknownMemberType]
                continue
            prefix = entry.get("prefixlen")  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
            if isinstance(prefix, int) and not isinstance(prefix, bool) and 0 < prefix <= 32:
                return _info(name, address, prefix, PROFILE_UNKNOWN, not name.lower().startswith(_VIRTUAL_PREFIXES))
    return None


def _posix_interface(address: str, env: Environment) -> InterfaceInfo:
    tool = next((path for path in _IP_TOOLS if os.path.isfile(path)), None)
    if tool is not None:
        try:
            result = env.run(tool, ["-j", "-4", "addr"], timeout=QUERY_TIMEOUT_S)
        except (OSError, TimeoutError):
            result = None
        if result is not None and result.code == 0:
            found = parse_ip_json(result.stdout, address)
            if found is not None:
                return found
    return _info("", address, 32, PROFILE_UNKNOWN, True)


def interface_for(address: str, env: Environment | None = None) -> InterfaceInfo:
    env = env or Environment()
    _ipv4(address)
    try:
        if env.platform == "win32":
            return _windows_interface(address, env)
        return _posix_interface(address, env)
    except LoginTaskError as error:
        raise InterfaceError(str(error)) from error


def require_lan_ready(address: str, env: Environment | None = None, *, allow_virtual: bool = False) -> InterfaceInfo:
    env = env or Environment()
    ip = _ipv4(address)
    if not any(ip in network for network in _PRIVATE_V4):
        raise InterfaceError(f"{address} is not an RFC 1918 address (10.x.x.x, 172.16-31.x.x or 192.168.x.x), so LAN mode refuses it")
    info = interface_for(address, env)
    name = info.alias or "the network"
    if info.profile == PROFILE_PUBLIC:
        raise InterfaceError(
            f"{name} is a Public network. Open Settings > Network & internet > {name} > Private network, then run the command again."
        )
    if info.profile == PROFILE_DOMAIN:
        raise InterfaceError(
            f"{name} is a Domain network, and LAN mode accepts Private networks only. Away from a Private network, use Tailscale mode."
        )
    if not info.physical and not allow_virtual:
        raise InterfaceError(
            f"{name} is a virtual or VPN adapter (Hyper-V, WSL, Docker or a VPN). LAN mode binds a physical interface "
            f"unless you name this one with --bind {address}."
        )
    if env.platform == "win32" and info.profile != PROFILE_PRIVATE:
        raise InterfaceError(
            f"Windows did not report whether {name} is a Private network. "
            f"Open Settings > Network & internet > {name} and set Private network."
        )
    if env.platform == "win32" and not MIN_PREFIX <= info.prefix_len <= MAX_PREFIX:
        raise InterfaceError(f"{name} has the prefix length /{info.prefix_len}, which leaves no subnet to scope the firewall rule to")
    return info

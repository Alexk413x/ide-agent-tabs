from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from ..processes import RunResult
from ..processes import run as run_process
from .interfaces import InterfaceError, InterfaceInfo, interface_for
from .notify import powershell_path
from .policy import NETWORK_ID

QUERY_TIMEOUT_S = 15.0
NOT_ALLOWED = "this network is not in network.allowedNetworks"
UNREADABLE = "the id of this network can't be read, so it counts as not allowed"
LEGACY_ONLY = (
    "this network is listed only by its older id (profile name and subnet), which another network can share; "
    "run `agent-tabs net networks add` here to allow it by its id with the gateway MAC"
)

_ADDRESS_ENV = "AGENT_TABS_ADDRESS"
_MAC = re.compile(r"(?:[0-9a-f]{1,2}:){5}[0-9a-f]{1,2}", re.IGNORECASE)
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
_IP_TOOLS = ("/usr/sbin/ip", "/sbin/ip", "/usr/bin/ip", "/bin/ip")
_ROUTE_TOOL = "/sbin/route"
_IFCONFIG_TOOL = "/sbin/ifconfig"
_ARP_TOOLS = ("/usr/sbin/arp", "/sbin/arp")
_GATEWAY_LINE = re.compile(r"^\s*gateway:\s*(\S+)\s*$", re.MULTILINE)
_INTERFACE_LINE = re.compile(r"^\s*interface:\s*(\S+)\s*$", re.MULTILINE)
_INET_LINE = re.compile(r"\binet\s+(\d+\.\d+\.\d+\.\d+)\s+netmask\s+(0x[0-9a-f]{8})\b", re.IGNORECASE)
_ARP_LINE = re.compile(r"\bat\s+((?:[0-9a-f]{1,2}:){5}[0-9a-f]{1,2})\b", re.IGNORECASE)
_MAC_ANY = re.compile(r"(?:[0-9a-f]{1,2}[:-]){5}[0-9a-f]{1,2}", re.IGNORECASE)
_GATEWAY_V4 = re.compile(r"(?:0|[1-9][0-9]{0,2})(?:\.(?:0|[1-9][0-9]{0,2})){3}")
NO_GATEWAY_MAC = (
    "the MAC address of the default gateway can't be read, so the network id is built from the profile name and the subnet only"
)

# Get-NetConnectionProfile's InstanceID is the adapter's interface GUID, so it stays the same on every network the
# adapter joins. The profile Name is what Windows keeps unique per network (it adds " 2" to a second network that
# reuses a name), but two home networks can still share a Name and a subnet, so the id also holds the MAC address of
# the default gateway.
_WINDOWS_SCRIPT = (
    "$ErrorActionPreference = 'Stop'; "
    f"$a = @(Get-NetIPAddress -AddressFamily IPv4 -IPAddress $env:{_ADDRESS_ENV})[0]; "
    "$c = @(Get-NetConnectionProfile -InterfaceIndex $a.InterfaceIndex)[0]; "
    "$r = @(Get-NetRoute -DestinationPrefix 0.0.0.0/0 -InterfaceIndex $a.InterfaceIndex -ErrorAction SilentlyContinue "
    "| Sort-Object RouteMetric)[0]; "
    "$g = if ($r) { [string]$r.NextHop } else { '' }; "
    "$n = if ($g) { @(Get-NetNeighbor -IPAddress $g -InterfaceIndex $a.InterfaceIndex -ErrorAction SilentlyContinue "
    "| Where-Object { $_.LinkLayerAddress })[0] } else { $null }; "
    "$m = if ($n) { [string]$n.LinkLayerAddress } else { '' }; "
    "[pscustomobject]@{ name = [string]$c.Name; address = [string]$a.IPAddress; prefix = [int]$a.PrefixLength; "
    "gateway = $g; mac = $m } | ConvertTo-Json -Compress"
)

Run = Callable[..., RunResult]


class NetworkId(str):
    legacy: str | None = None
    fallback: str | None = None


def tagged_network_id(value: str, legacy: str | None = None, fallback: str | None = None) -> NetworkId:
    found = NetworkId(value)
    found.legacy = legacy
    found.fallback = fallback
    return found


def legacy_of(network_id: str | None) -> str | None:
    return network_id.legacy if isinstance(network_id, NetworkId) else None


def fallback_of(network_id: str | None) -> str | None:
    return network_id.fallback if isinstance(network_id, NetworkId) else None


@dataclass(frozen=True)
class GateState:
    allowed: bool
    network_id: str | None = None
    reason: str | None = None


def gate_state(allowed_networks: Sequence[str], network_id: str | None) -> GateState:
    if network_id is None:
        return GateState(False, None, UNREADABLE)
    if network_id in allowed_networks:
        return GateState(True, network_id)
    legacy = legacy_of(network_id)
    return GateState(False, network_id, LEGACY_ONLY if legacy is not None and legacy in allowed_networks else NOT_ALLOWED)


def upgraded_allowed(allowed_networks: Sequence[str], network_id: str | None) -> tuple[str, ...] | None:
    legacy = legacy_of(network_id)
    if network_id is None or legacy is None or network_id in allowed_networks or legacy not in allowed_networks:
        return None
    return tuple(network_id if item == legacy else item for item in allowed_networks)


def check_network(allowed_networks: Sequence[str], bind: str, read: Callable[[str], str | None]) -> GateState:
    try:
        found = read(bind)
    except (OSError, ValueError, InterfaceError):
        found = None
    return gate_state(allowed_networks, found)


def _checked(identifier: str) -> str | None:
    return identifier if NETWORK_ID.fullmatch(identifier) is not None else None


def normalize_mac(text: object) -> str | None:
    if not isinstance(text, str) or _MAC_ANY.fullmatch(text.strip()) is None:
        return None
    mac = ":".join(part.zfill(2) for part in text.strip().lower().replace("-", ":").split(":"))
    return None if mac in ("00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff") else mac


def windows_network_id(name: str, subnet: str, gateway_mac: str | None = None) -> str | None:
    if not name.strip():
        return None
    material = f"{name}\0{subnet}" if gateway_mac is None else f"{name}\0{subnet}\0{gateway_mac}"
    digest = hashlib.sha256(material.encode()).hexdigest()[:12]
    readable = _UNSAFE.sub("-", name).strip("-")[:48] or "network"
    return _checked(f"win:{readable}:{subnet.replace('/', '_')}:{digest}")


def posix_network_id(gateway_mac: str, subnet: str) -> str | None:
    if _MAC.fullmatch(gateway_mac) is None:
        return None
    mac = ":".join(part.zfill(2) for part in gateway_mac.lower().split(":"))
    if mac == "00:00:00:00:00:00":
        return None
    return _checked(f"{mac}_{subnet.replace('/', '_')}")


def windows_arp_path() -> str | None:
    root = os.environ.get("SYSTEMROOT") or os.environ.get("WINDIR")
    path = os.path.join(root, "System32", "arp.exe") if root else None
    return path if path is not None and os.path.isfile(path) else None


def _arp_mac(bind: str, gateway: str, run: Run, arp: str | None) -> str | None:
    if arp is None or _GATEWAY_V4.fullmatch(gateway) is None:
        return None
    try:
        result = run(arp, ["-a", gateway, "-N", bind], timeout=QUERY_TIMEOUT_S)
    except (OSError, TimeoutError):
        return None
    if result.code != 0:
        return None
    row = re.search(rf"^\s*{re.escape(gateway)}\s+((?:[0-9a-f]{{2}}-){{5}}[0-9a-f]{{2}})\b", result.stdout, re.IGNORECASE | re.MULTILINE)
    return normalize_mac(row.group(1)) if row is not None else None


def _windows(bind: str, run: Run, shell: str | None, arp: Callable[[], str | None]) -> str | None:
    if shell is None:
        return None
    try:
        result = run(
            shell,
            ["-NoProfile", "-NonInteractive", "-Command", _WINDOWS_SCRIPT],
            env={**os.environ, _ADDRESS_ENV: bind},
            timeout=QUERY_TIMEOUT_S,
        )
    except (OSError, TimeoutError):
        return None
    if result.code != 0:
        return None
    for candidate in reversed([line.strip() for line in result.stdout.splitlines() if line.strip()]):
        try:
            data: Any = json.loads(candidate)
        except ValueError:
            continue
        if not isinstance(data, dict) or data.get("address") != bind:  # pyright: ignore[reportUnknownMemberType]
            return None
        name, prefix = data.get("name"), data.get("prefix")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        if not isinstance(name, str) or isinstance(prefix, bool) or not isinstance(prefix, int) or not 0 < prefix <= 32:
            return None
        subnet = _subnet(bind, prefix)
        legacy = windows_network_id(name, subnet)
        gateway = data.get("gateway")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        mac = normalize_mac(data.get("mac"))  # pyright: ignore[reportUnknownMemberType]
        if mac is None and isinstance(gateway, str):
            mac = _arp_mac(bind, gateway, run, arp())
        if legacy is None:
            return None
        if mac is None:
            return tagged_network_id(legacy, fallback=NO_GATEWAY_MAC)
        found = windows_network_id(name, subnet, mac)
        return None if found is None else tagged_network_id(found, legacy=legacy)
    return None


def _subnet(address: str, prefix: int) -> str:
    return str(ipaddress.IPv4Network(f"{address}/{prefix}", strict=False))


def _json(text: str) -> list[dict[str, Any]]:
    try:
        rows = json.loads(text)
    except ValueError:
        return []
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []  # pyright: ignore[reportUnknownVariableType]


def _linux(info: InterfaceInfo, run: Run) -> str | None:
    tool = next((path for path in _IP_TOOLS if os.path.isfile(path)), None)
    if tool is None:
        return None
    try:
        routes = run(tool, ["-j", "-4", "route", "show", "default"], timeout=QUERY_TIMEOUT_S)
        if routes.code != 0:
            return None
        gateway = next(
            (str(row["gateway"]) for row in _json(routes.stdout) if "gateway" in row and (not info.alias or row.get("dev") == info.alias)),
            None,
        )
        if gateway is None:
            return None
        neighbours = run(tool, ["-j", "-4", "neigh", "show", gateway], timeout=QUERY_TIMEOUT_S)
        if neighbours.code != 0:
            return None
    except (OSError, TimeoutError):
        return None
    mac = next((str(row["lladdr"]) for row in _json(neighbours.stdout) if row.get("dst") == gateway and "lladdr" in row), None)
    return None if mac is None else posix_network_id(mac, info.subnet)


def _mask_subnet(address: str, hex_mask: str) -> str | None:
    try:
        return str(ipaddress.IPv4Network(f"{address}/{ipaddress.IPv4Address(int(hex_mask, 16))}", strict=False))
    except ValueError:
        return None


def _macos(bind: str, run: Run) -> str | None:
    arp = next((path for path in _ARP_TOOLS if os.path.isfile(path)), None)
    if arp is None or not os.path.isfile(_ROUTE_TOOL) or not os.path.isfile(_IFCONFIG_TOOL):
        return None
    try:
        route = run(_ROUTE_TOOL, ["-n", "get", "default"], timeout=QUERY_TIMEOUT_S)
        gateway = _GATEWAY_LINE.search(route.stdout) if route.code == 0 else None
        device = _INTERFACE_LINE.search(route.stdout) if route.code == 0 else None
        if gateway is None or device is None:
            return None
        configured = run(_IFCONFIG_TOOL, [device.group(1)], timeout=QUERY_TIMEOUT_S)
        table = run(arp, ["-n", gateway.group(1)], timeout=QUERY_TIMEOUT_S)
    except (OSError, TimeoutError):
        return None
    subnet = next(
        (_mask_subnet(found.group(1), found.group(2)) for found in _INET_LINE.finditer(configured.stdout) if found.group(1) == bind), None
    )
    mac = _ARP_LINE.search(table.stdout) if table.code == 0 else None
    return None if mac is None or subnet is None else posix_network_id(mac.group(1), subnet)


def current_network_id(
    bind_address: str,
    *,
    platform: str = sys.platform,
    run: Run = run_process,
    shell: Callable[[], str | None] = powershell_path,
    interface: Callable[[str], InterfaceInfo] = interface_for,
    arp: Callable[[], str | None] = windows_arp_path,
) -> str | None:
    if platform == "win32":
        return _windows(bind_address, run, shell(), arp)
    if platform == "darwin":
        return _macos(bind_address, run)
    try:
        info = interface(bind_address)
    except (InterfaceError, ValueError):
        return None
    return None if info.prefix_len >= 32 else _linux(info, run)

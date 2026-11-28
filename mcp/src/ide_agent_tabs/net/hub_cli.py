from __future__ import annotations

import json
import os
import shutil
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace

from ..version import compare_versions
from . import elevation, hubsync, ingress, pairing, selfupdate, tls
from .beacon import wire_for
from .cli import (
    CliError,
    Services,
    UsageError,
    create_identity,
    device_name,
    enable_lan,
    folder_defaults,
    listener_lines,
    make_parser,
    refuse_remote_origin,
    renewed_services,
    report_folders,
    set_network,
    start_listener,
    tailscale_gate,
    tailscale_gate_lines,
)
from .client import NetClientError, load_identity, local_device_id, send_request
from .find import FindRequest, FindResult, device_lines, find_devices, not_found_text
from .hubjoin import HubJoiner
from .identity import der_from_pem
from .interfaces import InterfaceInfo, local_addresses
from .listener import live_listener
from .netid import fallback_of, gate_state, upgraded_allowed
from .pairing import INCOMPLETE_TEXT, PAIRING_PORT, PairIdentity, PairResult
from .policy import NETWORK_ID, NetworkConfig, grant_for, load_network_config, load_policy
from .tailscale import TAILSCALE_SCOPE, TailscaleDiscovery
from .transport import LanTransport, TailscaleTransport, Transport, command_transport, network_enabled


def find_openssl(
    configured: str | None,
    env: Mapping[str, str],
    platform: str = sys.platform,
    which: Callable[[str], str | None] = shutil.which,
) -> str | None:
    candidates: list[str] = [configured] if configured else []
    if platform == "win32":
        for key in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
            root = env.get(key)
            if root:
                candidates.append(os.path.join(root, "Git", "usr", "bin", "openssl.exe"))
        local = env.get("LOCALAPPDATA")
        if local:
            candidates.append(os.path.join(local, "Programs", "Git", "usr", "bin", "openssl.exe"))
    else:
        found = which("openssl")
        if found:
            candidates.append(found)
    return next((path for path in candidates if os.path.isabs(path) and os.path.isfile(path)), None)


def _require_lan_ready(address: str, allow_virtual: bool) -> InterfaceInfo:
    return LanTransport().require_ready(address, allow_virtual)


@dataclass
class PairDeps:
    refuse_if_elevated: Callable[[], None] = elevation.refuse_if_elevated
    local_addresses: Callable[[], list[str]] = local_addresses
    require_lan_ready: Callable[[str, bool], InterfaceInfo] = _require_lan_ready
    find_openssl: Callable[[str | None, Mapping[str, str]], str | None] = find_openssl
    check_destination: Callable[[str, int, str, int], str] = LanTransport().check_destination
    dial_join: Callable[..., PairResult] = pairing.dial_join
    timeouts: pairing.Timeouts = pairing.DEFAULT_TIMEOUTS


def _deps(svc: Services) -> PairDeps:
    return svc.pair or PairDeps()


@dataclass(frozen=True)
class Prepared:
    identity: PairIdentity
    device_id: str
    name: str
    iface: InterfaceInfo
    bind: str
    mode: str = "lan"
    transport: Transport | None = None


def _choose_bind(svc: Services, deps: PairDeps, transport: Transport | None = None) -> str:
    found = (transport or LanTransport()).candidates(deps.local_addresses(), svc.interface_for)
    if not found and isinstance(transport, TailscaleTransport):
        raise CliError(
            f"No Tailscale address ({TAILSCALE_SCOPE}) of this machine was found. "
            "Check that Tailscale runs and is logged in, then run the command again."
        )
    if not found:
        raise CliError(
            "No private address (10.x.x.x, 172.16-31.x.x or 192.168.x.x) of a physical network interface was found. "
            "Pass --bind <this machine's address>, then run the command again."
        )
    if len(found) > 1:
        listed = ", ".join(f"{info.address} ({info.alias})" for info in found)
        raise CliError(f"This machine has more than one private address: {listed}. Pass --bind <address> to choose one.")
    svc.out(f"Using {found[0].address} on {found[0].alias or 'the network'}.")
    return found[0].address


def current_bind(svc: Services, deps: PairDeps, saved: str | None, transport: Transport | None = None) -> tuple[str | None, bool]:
    if saved is None or svc.bind_available(saved):
        return saved, saved is not None
    svc.out(f"The saved address {saved} is no longer assigned to this machine.")
    return _choose_bind(svc, deps, transport), False


def prepare(svc: Services, deps: PairDeps, requested: str | None) -> Prepared:
    problem = tls.runtime_problem()
    if problem is not None:
        raise CliError(problem)
    config, warnings = load_network_config(svc.home)
    for warning in warnings:
        svc.err(f"warning: {warning}")
    name = device_name(svc)
    mode = "tailscale" if config.network == "tailscale" else "lan"
    transport = command_transport(config, svc.tailscale, svc.interface_for) if mode == "tailscale" else None
    if requested is not None:
        bind, explicit = requested, True
    else:
        found, explicit = current_bind(svc, deps, config.bind, transport)
        bind = found if found is not None else _choose_bind(svc, deps, transport)
    iface = transport.require_ready(bind, explicit) if transport is not None else deps.require_lan_ready(bind, explicit)
    if local_device_id(svc.home) is None:
        openssl = deps.find_openssl(config.openssl, svc.env)
        if openssl is None:
            raise CliError(
                "No openssl program was found. Install Git for Windows (or openssl elsewhere), "
                "or run agent-tabs net lan setup --openssl <absolute path of openssl>, then run the command again."
            )
        svc.out(f"Creating this machine's network identity with openssl at {openssl}.")
        create_identity(svc, openssl)
    try:
        local = load_identity(svc.home)
        der = der_from_pem(local.cert.read_text(encoding="ascii"))
    except (NetClientError, OSError, ValueError) as error:
        raise CliError(f"this machine's network identity cannot be read: {error}") from error
    identity = PairIdentity(name, der, local.cert, local.key, lambda: local.passphrase)
    device = local_device_id(svc.home)
    if device is None:
        raise CliError("this machine's network identity cannot be read")
    return Prepared(identity, device, name, iface, bind, mode, transport)


def _read_network_id(svc: Services, bind: str) -> str | None:
    try:
        return svc.network_id(bind)
    except (OSError, ValueError):
        return None


def _require_network_id(svc: Services, bind: str) -> str:
    found = _read_network_id(svc, bind)
    if found is None:
        raise CliError(
            f"The id of the network at {bind} can't be read, so it can't be added to network.allowedNetworks "
            "and the listener would drop every connection. Connect to the network and run the command again."
        )
    note = fallback_of(found)
    if note is not None:
        svc.err(f"warning: {note}")
    return found


def _setup_tailscale(svc: Services, prepared: Prepared, folder: str | None, extra: dict[str, object]) -> bool:
    config, _ = load_network_config(svc.home)
    folders = folder_defaults(svc, config, folder)
    set_network(svc.home, {"network": "tailscale", "bind": prepared.bind, **extra, **folders})
    ingress.ensure_ingress_token(svc.home)
    report_folders(svc, config, folders)
    return enable_lan(svc, prepared.bind, config.port, True, "tailscale")


def _setup_lan(svc: Services, prepared: Prepared, network_id: str | None, folder: str | None, extra: dict[str, object]) -> bool:
    if network_id is None:
        return _setup_tailscale(svc, prepared, folder, extra)
    config, _ = load_network_config(svc.home)
    allowed = list(config.allowed_networks)
    upgraded = upgraded_allowed(allowed, network_id)
    if upgraded is not None:
        allowed = list(upgraded)
    elif network_id not in allowed:
        allowed.append(network_id)
    folders = folder_defaults(svc, config, folder)
    set_network(svc.home, {"network": "lan", "bind": prepared.bind, "allowedNetworks": allowed, **extra, **folders})
    ingress.ensure_ingress_token(svc.home)
    svc.out(f"allowed network: {network_id}")
    report_folders(svc, config, folders)
    return enable_lan(svc, prepared.bind, config.port, True)


def _find_request(svc: Services, prepared: Prepared) -> FindRequest:
    transport = prepared.transport
    if isinstance(transport, TailscaleTransport):
        return FindRequest(
            svc.home,
            prepared.identity,
            prepared.device_id,
            wire_for(prepared.bind, prepared.iface.prefix_len),
            lambda text: svc.record("find", note=text),
            TailscaleDiscovery(transport.client, transport.allowed_owner_ids),
            lambda address: _check_tailscale_destination(transport, address, prepared),
        )
    return FindRequest(
        svc.home,
        prepared.identity,
        prepared.device_id,
        wire_for(prepared.bind, prepared.iface.prefix_len),
        lambda text: svc.record("find", note=text),
    )


def _check_tailscale_destination(transport: Transport, address: str, prepared: Prepared) -> None:
    transport.check_destination(address, PAIRING_PORT, prepared.iface.address, prepared.iface.prefix_len)


def run_find(svc: Services, prepared: Prepared) -> FindResult:
    request = _find_request(svc, prepared)
    result = (svc.find or find_devices)(request)
    svc.record("find", found=len(result.devices), probed=int(result.probed), heard=result.heard, datagrams=result.datagrams)
    return result


def _resolve(svc: Services, prepared: Prepared, devices: Sequence[str]) -> list[tuple[str, str | None]]:
    result = run_find(svc, prepared)
    for line in result.warnings:
        svc.err(line)
    found = {d.device_id: d.address for d in result.devices}
    if not result.devices:
        request = _find_request(svc, prepared)
        svc.err(not_found_text(result, request.wire, request.tailscale is not None))
    return [(device, found.get(device)) for device in devices]


def _dial_one(svc: Services, deps: PairDeps, prepared: Prepared, device: str, address: str, introduced: list[str]) -> PairResult:
    peer_id = pairing.full_device_id(device)
    if peer_id == prepared.device_id:
        return PairResult("failed", "that is this machine's own device id")
    check = prepared.transport.check_destination if prepared.transport is not None else deps.check_destination
    target = check(address, PAIRING_PORT, prepared.iface.address, prepared.iface.prefix_len)

    def saved(label: str, found: str) -> None:
        svc.record("hub-join", device=peer_id, label=label, address=found)
        svc.notify(f"joined the hub with {label} at {found}")

    handler = HubJoiner(svc.home, saved, introduced.append)
    extra: dict[str, str] = {} if prepared.mode == "lan" else {"transport": prepared.mode}
    return deps.dial_join(
        target, PAIRING_PORT, peer_id, prepared.identity, handler, pairing.Limits(svc.home), timeouts=deps.timeouts, **extra
    )


@dataclass
class _Versions:
    own: str
    attempted: bool = False


def _update_here(svc: Services, versions: _Versions, theirs: str) -> Services | None:
    command = svc.update_command()
    if versions.attempted:
        svc.out(selfupdate.manual_text(command))
        return None
    versions.attempted = True
    if command is None:
        svc.out(selfupdate.manual_text(None))
        return None
    svc.out(f"Updating this machine with: {command.display}")
    svc.record("self-update", attempt=1, theirs=theirs, mine=versions.own)
    outcome = selfupdate.run_update(command, svc.update_runner)
    svc.record("self-update", ok=int(outcome.ok))
    if not outcome.ok:
        svc.err(f"warning: the update failed: {outcome.summary}")
        svc.out(selfupdate.manual_text(command))
        return None
    svc.out(selfupdate.RESTART_NOTE)
    fresh = svc.fresh_install()
    if fresh is None or compare_versions(fresh.version, versions.own) <= 0:
        svc.err(
            "warning: no newer install of ide-agent-tabs was found, so the listener keeps its current build. Run agent-tabs net lan start in a new session."
        )
        return None
    try:
        renewed = renewed_services(svc, fresh)
    except OSError as error:
        svc.err(f"warning: the new install cannot be read ({error}). Run agent-tabs net lan start in a new session.")
        return None
    versions.own = fresh.version
    if live_listener(renewed.home) is not None:
        start_listener(renewed)
    return renewed


def _compare_versions(svc: Services, versions: _Versions, where: str, peer: pairing.PeerInfo) -> Services | None:
    theirs = peer.plugin_version
    if theirs is None:
        svc.out(
            f"{where}: the other machine reports no ide-agent-tabs version, so it runs a release older than this one "
            "and cannot update itself. Update ide-agent-tabs on that machine."
        )
        return None
    order = compare_versions(theirs, versions.own)
    if order == 0:
        return None
    svc.out(f"{where}: the other machine runs ide-agent-tabs {theirs} and this machine runs {versions.own}.")
    if order < 0:
        svc.out(
            "The other machine must update. It updates itself when its unattended update is on; otherwise run the update on that machine."
        )
        return None
    return _update_here(svc, versions, theirs)


def _dial_attempt(svc: Services, deps: PairDeps, prepared: Prepared, device: str, address: str) -> tuple[PairResult, list[str]]:
    introduced: list[str] = []
    try:
        return _dial_one(svc, deps, prepared, device, address, introduced), introduced
    except pairing.PairingError as error:
        return PairResult("failed", str(error)), introduced


def _join_all(svc: Services, deps: PairDeps, devices: Sequence[str], bind: str | None, folder: str | None) -> int:
    prepared = prepare(svc, deps, bind)
    network_id = _require_network_id(svc, prepared.bind) if prepared.mode == "lan" else None
    joined, complete = 0, True
    versions = _Versions(svc.plugin_version)
    for device, address in _resolve(svc, prepared, devices):
        if address is None:
            svc.err(f"{device[:15]}: did not join: no verified device with that id was found. Nothing was saved on this machine for it.")
            continue
        where = f"{device[:15]}@{address}"
        result, introduced = _dial_attempt(svc, deps, prepared, device, address)
        if result.peer is not None:
            renewed = _compare_versions(svc, versions, where, result.peer)
            if renewed is not None:
                svc = renewed
                if result.outcome == "failed":
                    result, introduced = _dial_attempt(svc, deps, prepared, device, address)
        for newcomer in introduced:
            report = hubsync.introduce_member(svc.home, newcomer, svc.member_request or send_request)
            svc.record("hub-introduce", device=newcomer, told=len(report.told), queued=len(report.queued))
        if result.outcome in ("paired", "incomplete") and result.peer is not None:
            peer = grant_for(load_policy(svc.home), result.peer.device_id)
            hub = load_policy(svc.home).hub
            svc.out(f"{where}: joined the hub {hub.name if hub else 'none'} as {peer.label if peer else result.peer.name}.")
            joined += 1
            if result.outcome == "incomplete":
                complete = False
                svc.err(INCOMPLETE_TEXT)
        else:
            svc.err(f"{where}: did not join: {result.reason}. Nothing was saved on this machine for it.")
    if joined == 0:
        return 1
    applied = _setup_lan(svc, prepared, network_id, folder, {})
    return 0 if applied and complete and joined == len(devices) else 1


def join(svc: Services, args: Sequence[str]) -> int:
    parser = make_parser("hub join")
    parser.add_argument("device_id")
    parser.add_argument("--bind")
    parser.add_argument("--folder")
    options = parser.parse_args(args)
    refuse_remote_origin(svc)
    deps = _deps(svc)
    deps.refuse_if_elevated()
    return _join_all(svc, deps, [options.device_id], options.bind, options.folder)


def join_listed(svc: Services, devices: Sequence[str], bind: str | None, folder: str | None) -> int:
    deps = _deps(svc)
    deps.refuse_if_elevated()
    return _join_all(svc, deps, devices, bind, folder)


def _discoverable_status(svc: Services, config: NetworkConfig) -> int:
    svc.out(f"discoverable: {'on' if config.discoverable else 'off'}")
    svc.out(f"network: {config.network}")
    svc.out(f"bind: {config.bind or 'none'}")
    expects_joins = False
    if config.network == "tailscale":
        gate, address = tailscale_gate(svc, config)
        for line in tailscale_gate_lines(gate, address):
            svc.out(line)
        expects_joins = gate.allowed and config.discoverable
    else:
        svc.out(f"allowed networks: {', '.join(config.allowed_networks) or 'none'}")
    if config.network != "tailscale" and network_enabled(config) and config.bind is not None:
        gate = gate_state(config.allowed_networks, _read_network_id(svc, config.bind))
        svc.out(f"current network: {gate.network_id or 'unreadable'}")
        svc.out(f"network gate: {'open' if gate.allowed else gate.reason}")
        expects_joins = gate.allowed and config.discoverable
    for line in listener_lines(svc, live_listener(svc.home), expects_joins):
        svc.out(line)
    return 0


def discoverable(svc: Services, args: Sequence[str]) -> int:
    parser = make_parser("discoverable")
    parser.add_argument("action", nargs="?", choices=("on", "off", "status"), default="status")
    parser.add_argument("--bind")
    parser.add_argument("--folder")
    options = parser.parse_args(args)
    if options.action == "status":
        config, warnings = load_network_config(svc.home)
        for warning in warnings:
            svc.err(f"warning: {warning}")
        return _discoverable_status(svc, config)
    refuse_remote_origin(svc)
    if options.action == "off":
        set_network(svc.home, {"discoverable": False})
        svc.record("discoverable", state="off")
        svc.out("Discoverable is off. The listener stops serving joins within a few seconds. Current members stay connected.")
        return 0
    deps = _deps(svc)
    deps.refuse_if_elevated()
    prepared = prepare(svc, deps, options.bind)
    network_id = _require_network_id(svc, prepared.bind) if prepared.mode == "lan" else None
    applied = _setup_lan(svc, prepared, network_id, options.folder, {"discoverable": True})
    svc.record("discoverable", state="on", bind=prepared.bind)
    svc.out(f"Discoverable as {prepared.name}.")
    svc.out(f"device id: {prepared.device_id}")
    svc.out(f"address: {prepared.bind} port {PAIRING_PORT}")
    return 0 if applied else 1


def _networks_view(svc: Services, config: NetworkConfig) -> None:
    if config.network == "tailscale":
        gate, address = tailscale_gate(svc, config)
        svc.out("allowed networks: not used in Tailscale mode")
        for line in tailscale_gate_lines(gate, address):
            svc.out(line)
        return
    svc.out("allowed networks: " + (", ".join(config.allowed_networks) if config.allowed_networks else "none"))
    if config.bind is None:
        svc.out("current network: unknown (no bind address is set)")
        return
    gate = gate_state(config.allowed_networks, _read_network_id(svc, config.bind))
    svc.out(f"current network: {gate.network_id or 'unreadable'} ({'allowed' if gate.allowed else gate.reason})")


def networks(svc: Services, args: Sequence[str]) -> int:
    parser = make_parser("networks")
    parser.add_argument("action", nargs="?", choices=("add", "remove"))
    parser.add_argument("id", nargs="?")
    options = parser.parse_args(args)
    config, warnings = load_network_config(svc.home)
    for warning in warnings:
        svc.err(f"warning: {warning}")
    if options.action is None:
        _networks_view(svc, config)
        return 0
    refuse_remote_origin(svc)
    if config.network == "tailscale":
        raise CliError("Tailscale mode doesn't use allowed networks; run agent-tabs net lan on to return to LAN mode first")
    allowed = list(config.allowed_networks)
    if options.action == "add":
        if options.id is not None:
            raise UsageError("agent-tabs net networks add: takes no id; it adds the current network")
        if config.bind is None:
            raise CliError("no bind address is set; run agent-tabs net discoverable on first")
        bind, _explicit = current_bind(svc, _deps(svc), config.bind)
        current = _require_network_id(svc, bind or config.bind)
        upgraded = upgraded_allowed(allowed, current)
        if upgraded is None and current in allowed:
            svc.out(f"{current} is already allowed.")
            return 0
        allowed = list(upgraded) if upgraded is not None else [*allowed, current]
        subject = current
    else:
        subject = options.id
        if subject is None or NETWORK_ID.fullmatch(subject) is None:
            raise UsageError("agent-tabs net networks remove: expected a network id from agent-tabs net networks")
        if subject not in allowed:
            raise CliError(f"{subject} is not an allowed network; agent-tabs net networks lists them")
        allowed.remove(subject)
    set_network(svc.home, {"allowedNetworks": allowed})
    svc.record("networks", action=options.action, count=len(allowed))
    svc.out(("Added " if options.action == "add" else "Removed ") + f"{subject}.")
    svc.out("allowed networks: " + (", ".join(allowed) if allowed else "none"))
    return 0


def find(svc: Services, args: Sequence[str]) -> int:
    parser = make_parser("find")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--bind")
    options = parser.parse_args(args)
    deps = _deps(svc)
    deps.refuse_if_elevated()
    prepared = prepare(replace(svc, out=svc.err) if options.json else svc, deps, options.bind)
    result = run_find(svc, prepared)
    request = _find_request(svc, prepared)
    message = None if result.devices else not_found_text(result, request.wire, request.tailscale is not None)
    if options.json:
        body: dict[str, object] = {
            "devices": [d.as_json() for d in result.devices],
            "warnings": list(result.warnings),
            "probed": result.probed,
        }
        if message is not None:
            body["message"] = message
        svc.out(json.dumps(body))
        return 0 if result.devices else 1
    if message is not None:
        svc.err(message)
        return 1
    svc.out(f"found {len(result.devices)} device(s):")
    for line in device_lines(result):
        svc.out(f"  {line}")
    for warning in result.warnings:
        svc.out(warning)
    return 0

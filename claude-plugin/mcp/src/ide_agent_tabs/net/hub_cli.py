from __future__ import annotations

from collections.abc import Sequence

from . import ingress, pairing
from .cli import (
    CliError,
    Services,
    UsageError,
    enable_lan,
    folder_defaults,
    make_parser,
    refuse_remote_origin,
    report_folders,
    set_network,
)
from .hubjoin import HubJoiner
from .listener import live_listener
from .netid import gate_state
from .pair_cli import PairDeps, Prepared, default_deps, prepare
from .pairing import INCOMPLETE_TEXT, PAIRING_PORT, PairResult
from .policy import NETWORK_ID, NetworkConfig, grant_for, load_network_config, load_policy
from .transport import network_enabled


def _deps(svc: Services) -> PairDeps:
    return svc.pair or default_deps()


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
    return found


def _setup_lan(svc: Services, prepared: Prepared, network_id: str, folder: str | None, extra: dict[str, object]) -> bool:
    config, _ = load_network_config(svc.home)
    allowed = list(config.allowed_networks)
    if network_id not in allowed:
        allowed.append(network_id)
    folders = folder_defaults(svc, config, folder)
    set_network(svc.home, {"network": "lan", "bind": prepared.bind, "allowedNetworks": allowed, **extra, **folders})
    ingress.ensure_ingress_token(svc.home)
    svc.out(f"allowed network: {network_id}")
    report_folders(svc, config, folders)
    return enable_lan(svc, prepared.bind, config.port, True)


def _split_target(spec: str) -> tuple[str, str]:
    device, separator, address = spec.partition("@")
    if not separator or not device or not address:
        raise UsageError(f"agent-tabs net hub create --with: expected <device id>@<address>, got {spec!r}")
    return device, address


def _dial_one(svc: Services, deps: PairDeps, prepared: Prepared, device: str, address: str) -> PairResult:
    peer_id = pairing.full_device_id(device)
    if peer_id == prepared.device_id:
        return PairResult("failed", "that is this machine's own device id")
    target = deps.check_destination(address, PAIRING_PORT, prepared.iface.address, prepared.iface.prefix_len)

    def saved(label: str, found: str) -> None:
        svc.record("hub-join", device=peer_id, label=label, address=found)
        svc.notify(f"joined the hub with {label} at {found}")

    handler = HubJoiner(svc.home, saved)
    return deps.dial_join(target, PAIRING_PORT, peer_id, prepared.identity, handler, pairing.Limits(svc.home), timeouts=deps.timeouts)


def _join_all(svc: Services, deps: PairDeps, targets: Sequence[tuple[str, str]], bind: str | None, folder: str | None) -> int:
    prepared = prepare(svc, deps, bind)
    network_id = _require_network_id(svc, prepared.bind)
    pairing.Limits(svc.home).check_locked()
    joined, complete = 0, True
    for device, address in targets:
        try:
            result = _dial_one(svc, deps, prepared, device, address)
        except pairing.PairingError as error:
            result = PairResult("failed", str(error))
        where = f"{device[:15]}@{address}"
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
    return 0 if applied and complete and joined == len(targets) else 1


def join(svc: Services, args: Sequence[str]) -> int:
    parser = make_parser("hub join")
    parser.add_argument("device_id")
    parser.add_argument("--address", required=True)
    parser.add_argument("--bind")
    parser.add_argument("--folder")
    options = parser.parse_args(args)
    refuse_remote_origin(svc)
    deps = _deps(svc)
    deps.refuse_if_elevated()
    return _join_all(svc, deps, [(options.device_id, options.address)], options.bind, options.folder)


def join_listed(svc: Services, specs: Sequence[str], bind: str | None, folder: str | None) -> int:
    targets = [_split_target(spec) for spec in specs]
    deps = _deps(svc)
    deps.refuse_if_elevated()
    return _join_all(svc, deps, targets, bind, folder)


def _discoverable_status(svc: Services, config: NetworkConfig) -> int:
    svc.out(f"discoverable: {'on' if config.discoverable else 'off'}")
    svc.out(f"network: {config.network}")
    svc.out(f"bind: {config.bind or 'none'}")
    svc.out(f"allowed networks: {', '.join(config.allowed_networks) or 'none'}")
    serving = False
    if network_enabled(config) and config.bind is not None:
        gate = gate_state(config.allowed_networks, _read_network_id(svc, config.bind))
        svc.out(f"current network: {gate.network_id or 'unreadable'}")
        svc.out(f"network gate: {'open' if gate.allowed else gate.reason}")
        serving = gate.allowed and config.discoverable
    info = live_listener(svc.home)
    svc.out("listener: " + (f"running (pid {info.pid}, {info.bind} port {info.port})" if info is not None else "not running"))
    svc.out(f"serving joins: {'yes' if serving and info is not None else 'no'}")
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
    network_id = _require_network_id(svc, prepared.bind)
    applied = _setup_lan(svc, prepared, network_id, options.folder, {"discoverable": True})
    svc.record("discoverable", state="on", bind=prepared.bind)
    svc.out(f"Discoverable as {prepared.name}.")
    svc.out(f"device id: {prepared.device_id}")
    svc.out(f"address: {prepared.bind} port {PAIRING_PORT}")
    return 0 if applied else 1


def _networks_view(svc: Services, config: NetworkConfig) -> None:
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
    allowed = list(config.allowed_networks)
    if options.action == "add":
        if options.id is not None:
            raise UsageError("agent-tabs net networks add: takes no id; it adds the current network")
        if config.bind is None:
            raise CliError("no bind address is set; run agent-tabs net discoverable on first")
        current = _require_network_id(svc, config.bind)
        if current in allowed:
            svc.out(f"{current} is already allowed.")
            return 0
        allowed.append(current)
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

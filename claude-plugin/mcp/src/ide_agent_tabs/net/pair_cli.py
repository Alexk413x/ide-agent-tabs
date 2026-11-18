from __future__ import annotations

import contextlib
import ipaddress
import json
import os
import shutil
import socket
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..files import read_text_if_exists, remove_file, write_atomically
from ..processes import spawn_detached, terminal_environment
from . import elevation, firewall, ingress, login_task, pairing, tls
from .cli import (
    CliError,
    Services,
    UsageError,
    create_identity,
    default_label,
    device_name,
    enable_lan,
    grants_text,
    make_parser,
    set_network,
    store_peer,
)
from .client import NetClientError, load_identity, local_device_id
from .identity import der_from_pem
from .interfaces import InterfaceError, InterfaceInfo
from .pairing import INCOMPLETE_TEXT, PAIRING_PORT, PairIdentity, PairResult, PeerInfo
from .policy import grant_for, load_network_config, load_policy
from .private_files import check_private, ensure_private_dir, make_private_file
from .transport import LanTransport

INVITATION_FILE = "invitation.txt"
RESULT_FILE = "pair-result.json"
INVITATION_WAIT_S = 30.0
INVITATION_POLL_S = 0.2
INVITATION_LIFETIME_S = 300
INVITATION_CLOCK_SLACK_S = 2.0
LOCK_NOTICE = "Pairing is locked for an hour after repeated failed attempts. `net trust` still works."


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


def local_addresses() -> list[str]:
    found: set[str] = set()
    host = socket.gethostname()
    with contextlib.suppress(OSError):
        found.update(socket.gethostbyname_ex(host)[2])
    with contextlib.suppress(OSError):
        found.update(str(info[4][0]) for info in socket.getaddrinfo(host, None, socket.AF_INET))
    with contextlib.suppress(OSError), socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect(("10.255.255.255", 9))
        found.add(str(probe.getsockname()[0]))
    return sorted(found)


def bind_pairing_socket(address: str, port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if sys.platform == "win32":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((address, port))
        sock.listen(4)
    except OSError:
        sock.close()
        raise
    return sock


def _spawn_server(home: str, bind: str, interpreter: str, launch_script: str) -> int | None:
    env = terminal_environment(os.environ)
    env["IDE_AGENT_TABS_HOME"] = home
    command = [interpreter, "-I", "-S", launch_script, "net", "pair", "--serve", "--bind", bind]
    try:
        return spawn_detached(command, env, os.path.expanduser("~")).pid
    except OSError:
        return None


def _require_lan_ready(address: str, allow_virtual: bool) -> InterfaceInfo:
    return LanTransport().require_ready(address, allow_virtual)


@dataclass
class PairDeps:
    refuse_if_elevated: Callable[[], None] = elevation.refuse_if_elevated
    local_addresses: Callable[[], list[str]] = local_addresses
    require_lan_ready: Callable[[str, bool], InterfaceInfo] = _require_lan_ready
    find_openssl: Callable[[str | None, Mapping[str, str]], str | None] = find_openssl
    ensure_rule: Callable[[str, str, str], str] = firewall.apply_listener_rule
    spawn_server: Callable[[str, str, str, str], int | None] = _spawn_server
    bind_socket: Callable[[str, int], socket.socket] = bind_pairing_socket
    check_destination: Callable[[str, int, str, int], str] = LanTransport().check_destination
    listen: Callable[..., PairResult] = pairing.listen_once
    dial: Callable[..., PairResult] = pairing.dial
    timeouts: pairing.Timeouts = pairing.DEFAULT_TIMEOUTS
    interpreter: str = field(default_factory=lambda: sys.executable)
    launch_script: str = field(default_factory=login_task.default_launch_script)
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    invitation_wait_s: float = INVITATION_WAIT_S


def default_deps() -> PairDeps:
    return PairDeps()


def invitation_path(home: str) -> Path:
    return ingress.net_dir(home) / INVITATION_FILE


def result_path(home: str) -> Path:
    return ingress.net_dir(home) / RESULT_FILE


def _write_private(path: Path, text: str) -> None:
    ensure_private_dir(path.parent)
    write_atomically(str(path), text)
    make_private_file(path)


def write_invitation(home: str, line: str) -> None:
    _write_private(invitation_path(home), line + "\n")


def read_invitation(home: str, not_before: float, now: float) -> str | None:
    path = invitation_path(home)
    try:
        if path.stat().st_mtime < not_before or check_private(path):
            return None
        lines = path.read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    if len(lines) != 1:
        return None
    try:
        pairing.parse_invitation(lines[0], now)
    except pairing.PairingError:
        return None
    return lines[0].strip()


def _remove_invitation(home: str) -> None:
    with contextlib.suppress(OSError):
        remove_file(str(invitation_path(home)))


def write_result(home: str, result: PairResult, label: str | None, now: float) -> None:
    peer = result.peer
    body = {
        "outcome": result.outcome,
        "reason": result.reason,
        "label": label,
        "device": peer.device_id if peer else None,
        "address": peer.address if peer else None,
        "at": int(now),
    }
    _write_private(result_path(home), json.dumps(body, indent=2) + "\n")


def read_result(home: str) -> dict[str, Any] | None:
    path = result_path(home)
    try:
        text = read_text_if_exists(str(path))
        if text is None or check_private(path):
            return None
        value = json.loads(text)
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None  # pyright: ignore[reportUnknownVariableType]


def result_text(home: str) -> str | None:
    value = read_result(home)
    if value is None:
        return None
    at = value.get("at")
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(at)) if isinstance(at, int) else "unknown time"
    if value.get("outcome") in ("paired", "incomplete"):
        return f"{value.get('outcome')} with {value.get('label')} at {value.get('address')} ({when})"
    return f"{value.get('outcome')}: {value.get('reason')} ({when})"


@dataclass(frozen=True)
class Mode:
    kind: str
    invitation: str = ""
    bind: str | None = None


def _mode(args: Sequence[str]) -> Mode:
    parser = make_parser("pair")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--invite", action="store_true")
    group.add_argument("--join")
    group.add_argument("--serve", action="store_true")
    parser.add_argument("--bind")
    options = parser.parse_args(args)
    bind: str | None = options.bind
    if options.join is not None:
        return Mode("join", invitation=options.join.strip(), bind=bind)
    if options.serve:
        if bind is None:
            raise UsageError("agent-tabs net pair --serve needs --bind")
        return Mode("serve", bind=bind)
    return Mode("invite", bind=bind)


@dataclass(frozen=True)
class Prepared:
    identity: PairIdentity
    device_id: str
    name: str
    iface: InterfaceInfo
    bind: str


def _choose_bind(svc: Services, deps: PairDeps) -> str:
    found: list[InterfaceInfo] = []
    for address in deps.local_addresses():
        try:
            ip = ipaddress.IPv4Address(address)
            info = svc.interface_for(address)
        except (ValueError, InterfaceError):
            continue
        if ip.is_private and not ip.is_loopback and not ip.is_link_local and info.physical:
            found.append(info)
    if not found:
        raise CliError(
            "No private address (10.x.x.x, 172.16-31.x.x or 192.168.x.x) of a physical network interface was found. "
            "Pass --bind <this machine's address>, then pair again."
        )
    if len(found) > 1:
        listed = ", ".join(f"{info.address} ({info.alias})" for info in found)
        raise CliError(f"This machine has more than one private address: {listed}. Pass --bind <address> to choose one.")
    svc.out(f"Using {found[0].address} on {found[0].alias or 'the network'}.")
    return found[0].address


def _prepare(svc: Services, deps: PairDeps, requested: str | None) -> Prepared:
    problem = tls.runtime_problem()
    if problem is not None:
        raise CliError(problem)
    config, warnings = load_network_config(svc.home)
    for warning in warnings:
        svc.err(f"warning: {warning}")
    name = device_name(svc)
    chosen = requested or config.bind
    bind = chosen if chosen is not None else _choose_bind(svc, deps)
    iface = deps.require_lan_ready(bind, chosen is not None)
    if local_device_id(svc.home) is None:
        openssl = deps.find_openssl(config.openssl, svc.env)
        if openssl is None:
            raise CliError(
                "No openssl program was found. Install Git for Windows (or openssl elsewhere), "
                "or run agent-tabs net lan setup --openssl <absolute path of openssl>, then pair again."
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
    return Prepared(identity, device, name, iface, bind)


class Saver:
    def __init__(self, svc: Services) -> None:
        self.svc = svc
        self.label: str | None = None

    def __call__(self, peer: PeerInfo) -> None:
        policy = load_policy(self.svc.home)
        known = grant_for(policy, peer.device_id)
        label = known.label if known is not None else default_label(peer.name, policy)
        result, is_new = store_peer(self.svc, peer.device_id, peer.pem, peer.address, label)
        self.label = result.label
        self.svc.record("pair", device=peer.device_id, label=result.label, address=peer.address, new=int(is_new))
        self.svc.notify(f"paired with {result.label} at {peer.address}")


def _ensure_rule(svc: Services, deps: PairDeps, prepared: Prepared) -> None:
    try:
        text = deps.ensure_rule(prepared.bind, LanTransport().firewall_scope(prepared.iface), prepared.iface.alias)
    except firewall.FirewallError as error:
        raise CliError(f"{error}. The other machine can reach this one only through the firewall rule {firewall.LISTENER_RULE}") from error
    if text:
        svc.out(text)


def _finish(svc: Services, prepared: Prepared, result: PairResult, label: str | None, deps: PairDeps) -> int:
    write_result(svc.home, result, label, deps.clock())
    if result.locked:
        svc.notify(LOCK_NOTICE)
        svc.err(LOCK_NOTICE)
    if result.outcome not in ("paired", "incomplete") or label is None:
        svc.err(f"Pairing did not finish: {result.reason}. Nothing was saved on this machine.")
        return 1
    peer = grant_for(load_policy(svc.home), result.peer.device_id) if result.peer else None
    grants = grants_text(peer) if peer is not None else "none"
    svc.out(f"Paired with {label} at {result.peer.address if result.peer else 'unknown'}. Grants: {grants}.")
    set_network(svc.home, {"bind": prepared.bind})
    ingress.ensure_ingress_token(svc.home)
    config, _ = load_network_config(svc.home)
    applied = enable_lan(svc, prepared.bind, config.port, True)
    if result.outcome == "incomplete":
        svc.err(INCOMPLETE_TEXT.replace("<label>", label))
        return 1
    return 0 if applied else 1


def _invite(svc: Services, deps: PairDeps, mode: Mode) -> int:
    prepared = _prepare(svc, deps, mode.bind)
    pairing.Limits(svc.home).check()
    _ensure_rule(svc, deps, prepared)
    started = deps.clock()
    _remove_invitation(svc.home)
    if deps.spawn_server(svc.home, prepared.bind, deps.interpreter, deps.launch_script) is None:
        raise CliError("the pairing listener could not be started")
    deadline = started + deps.invitation_wait_s
    while True:
        now = deps.clock()
        line = read_invitation(svc.home, started - INVITATION_CLOCK_SLACK_S, now)
        if line is not None:
            break
        if now >= deadline:
            detail = result_text(svc.home)
            raise CliError("the pairing listener showed no invitation" + (f": {detail}" if detail else ""))
        deps.sleep(INVITATION_POLL_S)
    svc.out("Invitation, valid for 5 minutes:")
    svc.out(line)
    svc.out(f"Send this line to the agent session on the other machine. It runs: agent-tabs net pair --join {line}")
    svc.out("This machine waits in the background. Run agent-tabs net status here to see the result.")
    return 0


def _serve(svc: Services, deps: PairDeps, mode: Mode) -> int:
    prepared = _prepare(svc, deps, mode.bind)
    limits = pairing.Limits(svc.home)
    try:
        limits.open_window()
    except pairing.LimitError as error:
        write_result(svc.home, PairResult("failed", str(error)), None, deps.clock())
        raise
    saver = Saver(svc)
    sock: socket.socket | None = None
    try:
        sock = deps.bind_socket(prepared.bind, PAIRING_PORT)
        port = int(sock.getsockname()[1])
        token = pairing.new_invitation_token()
        expires = int(deps.clock()) + INVITATION_LIFETIME_S
        write_invitation(svc.home, pairing.make_invitation(prepared.name, prepared.bind, port, prepared.device_id, token, expires))
        result = deps.listen(sock, token, prepared.identity, saver, limits, timeouts=deps.timeouts)
    except (OSError, pairing.PairingError) as error:
        result = PairResult("failed", str(error))
    finally:
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.close()
        limits.close_window()
        _remove_invitation(svc.home)
    return _finish(svc, prepared, result, saver.label, deps)


def _join(svc: Services, deps: PairDeps, mode: Mode) -> int:
    prepared = _prepare(svc, deps, mode.bind)
    limits = pairing.Limits(svc.home)
    limits.check_locked()
    invitation = pairing.parse_invitation(mode.invitation, deps.clock())
    address = deps.check_destination(invitation.address, invitation.port, prepared.iface.address, prepared.iface.prefix_len)
    svc.out(f"Pairing {prepared.name} {prepared.bind} with {invitation.name} at {address}.")
    saver = Saver(svc)
    result = deps.dial(address, invitation.port, invitation.token, invitation.id, prepared.identity, saver, limits, timeouts=deps.timeouts)
    return _finish(svc, prepared, result, saver.label, deps)


def run(svc: Services, deps: PairDeps, args: Sequence[str]) -> int:
    deps.refuse_if_elevated()
    mode = _mode(args)
    if mode.kind == "join":
        return _join(svc, deps, mode)
    if mode.kind == "serve":
        return _serve(svc, deps, mode)
    return _invite(svc, deps, mode)

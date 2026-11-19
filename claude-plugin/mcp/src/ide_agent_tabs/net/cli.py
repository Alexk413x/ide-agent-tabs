from __future__ import annotations

import argparse
import contextlib
import hashlib
import ipaddress
import json
import os
import re
import socket
import sqlite3
import ssl
import stat
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

from ..clock import now_ms
from ..files import file_lock, read_text_if_exists, remove_file, write_atomically
from ..home import agent_tabs_home
from ..jev.settings import CONFIG_FILE
from ..jsjson import parse, stringify
from ..liveness import pid_alive
from ..messaging.sessions import SESSION_ID
from ..processes import spawn_detached, terminal_environment
from ..profiles import TAB_ID_ENV
from ..shared.state import DEFAULT_PORT as SERVER_DEFAULT_PORT
from ..shared.state import PORT_OPTION_ENV, parse_port
from . import elevation, firewall, ingress, login_task, tls
from .certgen import CertgenError, Identity, generate_identity
from .client import identity_dir, local_device_id, peer_cert_path
from .confirm import ConfirmError, confirm
from .firewall import FirewallError
from .hub import default_member_folder
from .hubstate import ensure_hub_key, remove_hub_state
from .identity import der_from_pem, format_digest, parse_device_id
from .interfaces import InterfaceError, InterfaceInfo, interface_for
from .listener import append_log, live_listener, log_file, read_listener, request_stop
from .login_task import LoginTaskError
from .names import BAD_DEVICE_NAME, DeviceNameError, default_label, device_name_of
from .netid import current_network_id, gate_state
from .notify import notify
from .origin import Caller, OriginChecker
from .outbound import OFFLINE_AFTER_MS
from .pairing import PAIRING_FILE, Busy, Limits, Locked, PairingError
from .peers import LabelTaken, save_peer
from .policy import (
    ALIAS,
    SUPPORTED_AGENTS,
    FolderGrant,
    Hub,
    NetworkConfig,
    PeerGrant,
    Policy,
    PolicyError,
    grant_for,
    load_network_config,
    load_policy,
    policy_path,
    save_policy,
)
from .presence import read_presence
from .private_files import ensure_private_dir
from .secrets import SecretsError, new_passphrase, store_passphrase
from .store import NetStore, NetStoreError, db_path
from .transport import LanTransport, network_enabled

if TYPE_CHECKING:
    from .find import FindRequest, FindResult
    from .pair_cli import PairDeps

USAGE = "net <id|status|off|trust|grant|revoke|rename|pair|lan|hub|folders|discoverable|networks|find>"
_GRAMMAR = (
    "agent-tabs net id | status | off | revoke <label> | rename <label> <new label>"
    " | pair --invite [--bind <address>] | pair --join <invitation> [--bind <address>]"
    " | trust <cert file> --id <device id> --address <IP> --label <name>"
    " | grant <label> [--folder <alias>=<absolute path>] [--agent claude|codex] [--model <model>] [--send <session id>]"
    " | lan setup --openssl <absolute path> | lan on --bind <address> [--at-login] | lan start | lan stop"
    " | hub create [--name <name>] [--folder <session folder>] [--with <device id>[@<address>] ...] [--bind <address>]"
    " | hub join <device id> [--address <IP>] [--folder <session folder>] [--bind <address>] | hub status | hub leave"
    " | folders | folders add <absolute path> | folders remove <absolute path>"
    " | discoverable [on [--bind <address>] [--folder <session folder>] | off | status]"
    " | networks | networks add | networks remove <network id>"
    " | find [--json] [--bind <address>]"
)
LABEL = re.compile(r"[a-z0-9-]{1,32}")
_MAX_CERT_BYTES = 65536
_MAX_MODEL_CHARS = 128
_DRIVE_REMOTE = 4
NETWORK_SETTINGS_REFUSAL = "refused: a session opened by another machine can't change this machine's network settings"
_PROJECT_TABLE = re.compile(r"""\s*\[\s*projects\s*\.\s*("(?:[^"\\]|\\.)*"|'[^']*')\s*\]\s*(?:#.*)?""")
_TRUST_LINE = re.compile(r"""\s*trust_level\s*=\s*("trusted"|'trusted')\s*(?:#.*)?""")


class UsageError(Exception):
    pass


class CliError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise UsageError(f"{self.prog}: {message}")


def _stdout(text: str) -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def _stderr(text: str) -> None:
    sys.stderr.write(text + "\n")
    sys.stderr.flush()


def _launcher() -> str:
    return str(Path(__file__).resolve().parents[3] / "launch" / "net_listener.py")


def _spawn_listener(home: str, server_port: int) -> int | None:
    if not os.path.isabs(sys.executable) or not os.path.isfile(_launcher()):
        return None
    env = terminal_environment(os.environ)
    env["IDE_AGENT_TABS_HOME"] = home
    try:
        child = spawn_detached([sys.executable, "-I", "-S", _launcher(), "--server-port", str(server_port)], env, os.path.expanduser("~"))
    except OSError:
        return None
    return child.pid


def _bind_available(address: str) -> bool:
    sock = socket.socket(socket.AF_INET6 if ":" in address else socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((address, 0))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _caller_is_remote(home: str) -> bool:
    checker = OriginChecker(home)
    try:
        return checker.is_remote(Caller("cli", os.getcwd(), os.environ.get(TAB_ID_ENV) or None, os.getpid()))
    finally:
        checker.close()


@dataclass
class Services:
    home: str
    out: Callable[[str], None] = _stdout
    err: Callable[[str], None] = _stderr
    confirm: Callable[[Sequence[str]], bool] = confirm
    notify: Callable[[str], object] = notify
    spawn: Callable[[str, int], int | None] = _spawn_listener
    bind_available: Callable[[str], bool] = _bind_available
    register_login: Callable[[str], str] = login_task.register
    remove_login: Callable[[str], str] = login_task.remove
    login_status: Callable[[str], str] = login_task.status
    apply_firewall: Callable[[str, str, str], str] = firewall.apply_listener_rule
    remove_firewall: Callable[[], str] = firewall.remove_listener_rule
    interface_for: Callable[[str], InterfaceInfo] = interface_for
    unsupported_reason: Callable[[], str | None] = elevation.unsupported_reason
    pair: PairDeps | None = None
    find: Callable[[FindRequest], FindResult] | None = None
    env: Mapping[str, str] = field(default_factory=lambda: os.environ)
    user_home: str = field(default_factory=lambda: os.path.expanduser("~"))
    server_port: int = SERVER_DEFAULT_PORT
    start_wait_s: float = 3.0
    stop_wait_s: float = 5.0
    cwd: Callable[[], str] = os.getcwd
    new_hub_id: Callable[[], str] = lambda: os.urandom(16).hex()
    network_id: Callable[[str], str | None] = current_network_id
    is_remote_origin: Callable[[str], bool] = _caller_is_remote

    def record(self, event: str, **fields: object) -> None:
        pairs = " ".join(f"{key}={value}" for key, value in fields.items())
        append_log(log_file(self.home, "cli.log"), f"{event} {pairs}".strip())


def default_services() -> Services:
    env = os.environ
    return Services(home=agent_tabs_home(env), env=env, server_port=parse_port(env.get(PORT_OPTION_ENV)) or SERVER_DEFAULT_PORT)


def make_parser(name: str) -> _Parser:
    return _Parser(prog=f"agent-tabs net {name}", add_help=False)


def net_dir_for(home: str) -> Path:
    path = ingress.net_dir(home)
    ensure_private_dir(path)
    return path


def read_config(home: str) -> dict[str, Any]:
    path = os.path.join(home, CONFIG_FILE)
    text = read_text_if_exists(path)
    if text is None or not text.strip():
        return {}
    try:
        config = parse(text)
    except ValueError as error:
        raise CliError(f"{path} is not valid JSON: {error}") from error
    if not isinstance(config, dict):
        raise CliError(f"{path} must hold a JSON object")
    return config  # pyright: ignore[reportUnknownVariableType]


def set_network(home: str, updates: Mapping[str, Any]) -> None:
    config = read_config(home)
    section = config.get("network")
    if section is None:
        section = {}
    if not isinstance(section, dict):
        raise CliError(f'"network" in {os.path.join(home, CONFIG_FILE)} must be an object')
    section.update(updates)  # pyright: ignore[reportUnknownMemberType]
    config["network"] = section
    write_atomically(os.path.join(home, CONFIG_FILE), stringify(config, 2) + "\n")


def device_name(svc: Services) -> str:
    try:
        return device_name_of(svc.home)
    except DeviceNameError as error:
        raise CliError(BAD_DEVICE_NAME) from error


def refuse_remote_origin(svc: Services) -> None:
    if svc.is_remote_origin(svc.home):
        raise CliError(NETWORK_SETTINGS_REFUSAL)


def _require_confirmed(svc: Services, lines: Sequence[str]) -> None:
    if not svc.confirm(lines):
        raise CliError("not confirmed; nothing changed")


def _peer_by_label(policy: Policy, label: str) -> PeerGrant:
    peer = next((p for p in policy.peers if p.label == label), None)
    if peer is None:
        raise CliError(f"no peer is labelled {label}; agent-tabs net status lists the peers")
    return peer


def grants_text(peer: PeerGrant) -> str:
    if peer.member:
        return "hub member"
    parts: list[str] = []
    if peer.folders:
        parts.append("folders " + ", ".join(f"{f.alias}={f.path}" for f in peer.folders))
    if peer.agents:
        parts.append("agents " + ", ".join(peer.agents))
    if peer.models:
        parts.append("models " + ", ".join(peer.models))
    if peer.send:
        parts.append("send " + ", ".join(peer.send))
    return "; ".join(parts) if parts else "none"


def _wait_for(condition: Callable[[], bool], seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while True:
        if condition():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def start_listener(svc: Services) -> bool:
    info = live_listener(svc.home)
    if info is not None:
        svc.out(f"The listener already runs (pid {info.pid}).")
        return True
    pid = svc.spawn(svc.home, svc.server_port)
    if pid is None:
        svc.err("error: the listener could not be started")
        return False
    started = _wait_for(lambda: live_listener(svc.home) is not None or not pid_alive(pid), svc.start_wait_s)
    info = live_listener(svc.home) if started else None
    if info is None:
        svc.err(f"error: the listener did not start; see {log_file(svc.home)}")
        return False
    svc.out(f"The listener runs (pid {info.pid}) on {info.bind} port {info.port}.")
    return True


def _stop_listener(svc: Services) -> bool:
    info = live_listener(svc.home)
    if info is None:
        with contextlib.suppress(OSError):
            remove_file(str(ingress.net_dir(svc.home) / "listener.json"))
        svc.out("No listener runs.")
        return True
    request_stop(svc.home)
    pid = info.pid
    stopped = _wait_for(lambda: not pid_alive(pid) or read_listener(svc.home) is None, svc.stop_wait_s)
    if not stopped:
        svc.err(f"error: the listener (pid {pid}) did not stop within {svc.stop_wait_s:g} s; end that process yourself")
        return False
    svc.out(f"The listener stopped (pid {pid}).")
    return True


def apply_firewall(svc: Services, bind: str, port: int) -> None:
    try:
        if port != firewall.LISTENER_PORT:
            raise FirewallError(f"{port!r} is not the listener port {firewall.LISTENER_PORT}, so no firewall rule is created")
        info = svc.interface_for(bind)
        text = svc.apply_firewall(bind, LanTransport().firewall_scope(info), info.alias)
    except (FirewallError, InterfaceError) as error:
        svc.err(f"warning: {error}. Peers can connect only when the firewall allows them.")
        return
    if text:
        svc.out(text)


def _remove_firewall(svc: Services) -> bool:
    try:
        text = svc.remove_firewall()
    except FirewallError as error:
        svc.err(
            f"error: {error}. Delete the rules named {firewall.LISTENER_RULE} and {firewall.DISCOVERY_RULE} in Windows Defender Firewall, or run agent-tabs net off again."
        )
        return False
    if text:
        svc.out(text)
    return True


def _remove_login(svc: Services) -> bool:
    try:
        svc.out(svc.remove_login(svc.home))
    except LoginTaskError as error:
        svc.err(f"error: the login task was not removed: {error}. A leftover task runs lan start, which refuses while the network is off.")
        return False
    return True


def _cmd_id(svc: Services, args: Sequence[str]) -> int:
    make_parser("id").parse_args(args)
    identity = local_device_id(svc.home)
    if identity is None:
        svc.err("error: this machine has no identity yet; run agent-tabs net lan setup --openssl <absolute path of openssl>")
        return 1
    svc.out(f"device id: {identity}")
    svc.out(f"certificate: {identity_dir(svc.home) / 'cert.pem'}")
    return 0


def _pairing_state(svc: Services) -> str:
    state = "available"
    if (ingress.net_dir(svc.home) / PAIRING_FILE).exists():
        try:
            Limits(svc.home).check()
        except Busy as error:
            return str(error)
        except Locked as error:
            state = str(error)
        except PairingError as error:
            return f"unreadable ({error})"
    return state


def _read_network_id(svc: Services, bind: str) -> str | None:
    try:
        return svc.network_id(bind)
    except (OSError, ValueError):
        return None


def _cmd_status(svc: Services, args: Sequence[str]) -> int:
    make_parser("status").parse_args(args)
    unsupported = svc.unsupported_reason()
    if unsupported is not None:
        svc.out(f"unsupported: {unsupported}")
    config, warnings = load_network_config(svc.home)
    for warning in warnings:
        svc.out(f"warning: {warning}")
    svc.out(f"network: {config.network}")
    svc.out(f"bind: {config.bind or 'none'}")
    svc.out(f"port: {config.port}")
    svc.out(f"openssl: {config.openssl or 'none'}")
    info = live_listener(svc.home)
    svc.out("listener: " + (f"running (pid {info.pid}, {info.bind} port {info.port})" if info is not None else "not running"))
    svc.out(f"login task: {svc.login_status(svc.home)}")
    svc.out(f"identity: {local_device_id(svc.home) or 'none'}")
    svc.out(f"pairing: {_pairing_state(svc)}")
    from .pair_cli import result_text

    last = result_text(svc.home)
    if last is not None:
        svc.out(f"last pairing: {last}")
    svc.out(f"member folders: {', '.join(config.member_folders) or 'none'}")
    svc.out(f"discoverable: {'on' if config.discoverable else 'off'}")
    svc.out(f"allowed networks: {', '.join(config.allowed_networks) or 'none'}")
    if network_enabled(config) and config.bind is not None:
        gate = gate_state(config.allowed_networks, _read_network_id(svc, config.bind))
        svc.out(f"current network: {gate.network_id or 'unreadable'}")
        svc.out(f"network gate: {'open' if gate.allowed else gate.reason}")
    try:
        policy = load_policy(svc.home)
    except PolicyError as error:
        svc.out(f"hub: unreadable ({error})")
        svc.out(f"peers: unreadable ({error})")
        return 0
    svc.out(f"hub: {policy.hub.name} ({policy.hub.id})" if policy.hub is not None else "hub: none")
    if not policy.peers:
        svc.out("peers: none")
        return 0
    svc.out("peers:")
    for peer in policy.peers:
        svc.out(f"  {peer.label}  {peer.device_id[:15]}  {peer.address}  grants: {grants_text(peer)}")
    return 0


def create_identity(svc: Services, openssl: str) -> Identity:
    folder = identity_dir(svc.home)
    net_dir_for(svc.home)
    ingress.ensure_ingress_token(svc.home)
    ensure_private_dir(folder)
    passphrase = new_passphrase()
    store_passphrase(folder, passphrase)
    identity = generate_identity(Path(openssl), folder, passphrase, now=datetime.now(timezone.utc))
    set_network(svc.home, {"openssl": openssl})
    svc.record("lan-setup", device=identity.device_id)
    svc.notify("network identity created")
    return identity


def _cmd_setup(svc: Services, args: Sequence[str]) -> int:
    parser = make_parser("lan setup")
    parser.add_argument("--openssl", required=True)
    options = parser.parse_args(args)
    openssl: str = options.openssl
    if not os.path.isabs(openssl) or "\0" in openssl or not os.path.isfile(openssl):
        raise CliError("--openssl must be the absolute path of an openssl program")
    problem = tls.runtime_problem()
    if problem is not None:
        raise CliError(problem)
    existing = local_device_id(svc.home)
    if existing is not None:
        ingress.ensure_ingress_token(svc.home)
        svc.out(f"This machine already has an identity: {existing}")
        return 0
    folder = identity_dir(svc.home)
    _require_confirmed(
        svc,
        [
            "Create this machine's network identity:",
            f"  openssl: {openssl}",
            f"  key and certificate: {folder}",
            f"  ingress credential: {ingress.ingress_token_path(svc.home)}",
            "  The identity changes nothing else; no peer is trusted and nothing listens.",
        ],
    )
    identity = create_identity(svc, openssl)
    svc.out(f"device id: {identity.device_id}")
    svc.out(f"certificate: {identity.cert_path}")
    return 0


def _on_windows() -> bool:
    return sys.platform == "win32"


def enable_lan(svc: Services, bind: str, port: int, at_login: bool) -> bool:
    set_network(svc.home, {"network": "lan", "bind": bind})
    svc.record("lan-on", bind=bind, port=port)
    svc.notify(f"LAN network turned on at {bind}")
    apply_firewall(svc, bind, port)
    login_ok = True
    if at_login:
        try:
            svc.out(svc.register_login(svc.home))
        except LoginTaskError as error:
            svc.err(f"error: the login task was not registered: {error}")
            login_ok = False
    return start_listener(svc) and login_ok


def _cmd_on(svc: Services, args: Sequence[str]) -> int:
    parser = make_parser("lan on")
    parser.add_argument("--bind", required=True)
    parser.add_argument("--at-login", action="store_true")
    options = parser.parse_args(args)
    bind: str = options.bind
    if not LanTransport().allows_bind(bind):
        raise CliError("--bind must be a private IPv4 address (10/8, 172.16/12, 192.168/16) or an IPv6 unique local address (fc00::/7)")
    if not svc.bind_available(bind):
        raise CliError(f"{bind} is not assigned to a local interface")
    problem = tls.runtime_problem()
    if problem is not None:
        raise CliError(problem)
    if local_device_id(svc.home) is None or ingress.read_ingress_token(svc.home) is None:
        raise CliError("run agent-tabs net lan setup --openssl <absolute path of openssl> first")
    config, _ = load_network_config(svc.home)
    _require_confirmed(
        svc,
        [
            "Turn the LAN network on:",
            f"  listener address: {bind} port {config.port}",
            f"  config.json network: lan (it is {config.network} now)",
            "  The listener accepts connections only from peers you trust with agent-tabs net trust.",
            *(
                [
                    "  Windows firewall: one rule for ports 47829 and 47830 from this network's subnet on Private networks.",
                    "  When the rule isn't there yet, the Windows prompt asks for your approval once.",
                ]
                if _on_windows()
                else []
            ),
            *(
                ["  At login: a task starts the listener when you sign in, as you and without administrator rights."]
                if options.at_login
                else []
            ),
        ],
    )
    return 0 if enable_lan(svc, bind, config.port, options.at_login) else 1


def _cmd_start(svc: Services, args: Sequence[str]) -> int:
    make_parser("lan start").parse_args(args)
    config, warnings = load_network_config(svc.home)
    for warning in warnings:
        svc.err(f"warning: {warning}")
    if not network_enabled(config) or config.bind is None:
        raise CliError("the network is off or has no bind address; run agent-tabs net lan on --bind <address> in a terminal")
    problem = tls.runtime_problem()
    if problem is not None:
        raise CliError(problem)
    if local_device_id(svc.home) is None:
        raise CliError("this machine has no identity; run agent-tabs net lan setup")
    started = start_listener(svc)
    svc.record("lan-start", bind=config.bind, ok=int(started))
    svc.notify(f"LAN listener started at {config.bind}" if started else "LAN listener start failed")
    return 0 if started else 1


def _cmd_stop(svc: Services, args: Sequence[str]) -> int:
    make_parser("lan stop").parse_args(args)
    stopped = _stop_listener(svc)
    svc.record("lan-stop", ok=int(stopped))
    return 0 if stopped else 1


def _cmd_off(svc: Services, args: Sequence[str]) -> int:
    make_parser("off").parse_args(args)
    net_dir_for(svc.home)
    try:
        set_network(svc.home, {"network": "off", **({"discoverable": False} if load_network_config(svc.home)[0].discoverable else {})})
    except CliError as error:
        svc.err(f"warning: {error}; the network counts as off until config.json is fixed")
    store = NetStore(svc.home)
    try:
        generation = store.bump_generation()
    finally:
        store.close()
    svc.out(f"The network is off; the policy generation is {generation}. Requests already handed off may still finish.")
    svc.record("off", generation=generation)
    stopped = _stop_listener(svc)
    login_removed = _remove_login(svc)
    firewall_removed = _remove_firewall(svc)
    return 0 if stopped and login_removed and firewall_removed else 1


def _read_certificate(path: str) -> tuple[bytes, str]:
    try:
        size = os.path.getsize(path)
        if size > _MAX_CERT_BYTES:
            raise CliError(f"{path} is too large to be a certificate")
        with open(path, encoding="ascii") as handle:
            text = handle.read()
        der = der_from_pem(text)
    except (OSError, UnicodeDecodeError, ValueError) as error:
        raise CliError(f"{path} is not a PEM certificate: {error}") from error
    pem = ssl.DER_cert_to_PEM_cert(der)
    try:
        ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT).load_verify_locations(cadata=pem)
    except ssl.SSLError as error:
        raise CliError(f"{path} does not hold a usable certificate: {error}") from error
    return der, pem


def _address(text: str) -> str:
    try:
        ip = ipaddress.ip_address(text) if "%" not in text else None
    except ValueError:
        ip = None
    if ip is None or ip.is_unspecified or ip.is_multicast:
        raise CliError("--address must be the IPv4 or IPv6 address of one machine, without a zone")
    return str(ip)


def store_peer(
    svc: Services,
    identity: str,
    pem: str,
    address: str,
    label: str,
    folders: Sequence[FolderGrant] = (),
    agents: Sequence[str] = (),
) -> tuple[PeerGrant, bool]:
    try:
        return save_peer(svc.home, identity, pem, address, label, folders=folders, agents=agents)
    except LabelTaken as error:
        raise CliError(str(error)) from error


def _cmd_trust(svc: Services, args: Sequence[str]) -> int:
    parser = make_parser("trust")
    parser.add_argument("cert")
    parser.add_argument("--id", required=True)
    parser.add_argument("--address", required=True)
    parser.add_argument("--label", required=True)
    options = parser.parse_args(args)
    der, pem = _read_certificate(options.cert)
    try:
        wanted = parse_device_id(options.id)
    except ValueError as error:
        raise CliError(f"--id is not a full device id: {error}") from error
    if hashlib.sha256(der).digest() != wanted:
        raise CliError("the certificate does not match the device id; read the id again from the other machine's screen")
    identity = format_digest(wanted)
    if identity == local_device_id(svc.home):
        raise CliError("that is this machine's own device id")
    address = _address(options.address)
    label: str = options.label
    if LABEL.fullmatch(label) is None:
        raise CliError("--label is 1 to 32 characters of a-z, 0-9 and -")
    policy = load_policy(svc.home)
    known = grant_for(policy, identity)
    if known is None and any(p.label == label for p in policy.peers):
        raise CliError(f"the label {label} belongs to another peer")
    if known is not None and known.address == address and peer_cert_path(svc.home, identity).is_file():
        svc.out(f"{known.label} is already trusted at {address}. Grants: {grants_text(known)}.")
        return 0
    if known is None:
        lines = [
            "Trust a new peer:",
            f"  label: {label}",
            f"  device id: {identity}",
            f"  address: {address}",
            "  grants: none; the peer can do nothing until you run agent-tabs net grant.",
        ]
    else:
        lines = [
            "Change the address of a trusted peer; its label and grants stay as they are:",
            f"  label: {known.label}",
            f"  device id: {identity}",
            f"  address: {known.address} -> {address}",
            f"  grants: {grants_text(known)}",
        ]
    _require_confirmed(svc, lines)
    result, is_new = store_peer(svc, identity, pem, address, label)
    svc.record("trust", device=identity, label=result.label, address=address, new=int(is_new))
    svc.notify(f"trusted peer {result.label} at {address}" if is_new else f"changed the address of peer {result.label}")
    svc.out(("Trusted " if is_new else "Updated the address of ") + f"{result.label} ({identity}) at {address}.")
    svc.out(f"Effective grants: {grants_text(result)}.")
    return 0


def _strip_extended(path: str) -> str:
    if path.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path[8:]
    if path.startswith("\\\\?\\") and path[5:6] == ":":
        return path[4:]
    return path


def _is_remote_drive(path: str) -> bool:
    if sys.platform != "win32":
        return False
    import ctypes

    root = os.path.splitdrive(path)[0] + "\\"
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetDriveTypeW.argtypes = (ctypes.c_wchar_p,)
    kernel32.GetDriveTypeW.restype = ctypes.c_uint
    return kernel32.GetDriveTypeW(root) == _DRIVE_REMOTE


def _has_link_component(path: str) -> bool:
    parts = Path(path).parts
    current = Path(parts[0])
    for part in parts[1:]:
        current = current / part
        try:
            status = os.lstat(current)
        except OSError:
            return True
        if stat.S_ISLNK(status.st_mode) or getattr(status, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            return True
    return False


def _local_folder(raw: str, subject: str) -> str:
    if not os.path.isabs(raw) or "\0" in raw or raw.startswith(("\\\\", "//")):
        raise CliError(f"{subject}: the path must be a local absolute path, not a network path")
    real = _strip_extended(os.path.realpath(raw))
    if real.startswith(("\\\\", "//")) or _is_remote_drive(real):
        raise CliError(f"{subject}: network folders are refused")
    if not os.path.isdir(real):
        raise CliError(f"{subject}: {real} is not a folder")
    if _has_link_component(real):
        raise CliError(f"{subject}: {real} passes through a symbolic link or reparse point")
    return real


def folder_grant(spec: str) -> FolderGrant:
    alias, separator, raw = spec.partition("=")
    if not separator or ALIAS.fullmatch(alias) is None:
        raise CliError("--folder needs <alias>=<absolute path>; an alias is 1 to 32 characters of a-z, 0-9 and -")
    real = _local_folder(raw, f"folder {alias}")
    status = os.stat(real)
    if status.st_ino == 0:
        raise CliError(f"folder {alias}: this file system reports no file identity for {real}")
    return FolderGrant(alias, real, status.st_dev, status.st_ino)


def _same_path(left: str, right: str) -> bool:
    def normal(path: str) -> str:
        return os.path.normcase(os.path.normpath(path))

    return normal(left) == normal(right) or normal(os.path.realpath(left)) == normal(right)


def claude_trusts(svc: Services, folder: str) -> bool:
    configured = svc.env.get("CLAUDE_CONFIG_DIR")
    path = Path(configured) / ".claude.json" if configured else Path(svc.user_home) / ".claude.json"
    try:
        data = parse(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    projects = data.get("projects") if isinstance(data, dict) else None  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
    if not isinstance(projects, dict):
        return False
    return any(
        isinstance(entry, dict) and entry.get("hasTrustDialogAccepted") is True and _same_path(str(key), folder)  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
        for key, entry in projects.items()  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
    )


def codex_trusts(svc: Services, folder: str) -> bool:
    configured = svc.env.get("CODEX_HOME")
    path = Path(configured) / "config.toml" if configured else Path(svc.user_home) / ".codex" / "config.toml"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return False
    inside = False
    for line in lines:
        if line.lstrip().startswith("["):
            table = _PROJECT_TABLE.fullmatch(line)
            inside = False
            if table is not None:
                quoted = table.group(1)
                try:
                    name = json.loads(quoted) if quoted.startswith('"') else quoted[1:-1]
                except ValueError:
                    continue
                inside = isinstance(name, str) and _same_path(name, folder)
        elif inside and _TRUST_LINE.fullmatch(line):
            return True
    return False


_TRUST_CHECKS: dict[str, tuple[Callable[[Services, str], bool], str]] = {
    "claude": (claude_trusts, "open Claude Code in that folder once and accept the trust prompt"),
    "codex": (
        codex_trusts,
        'open Codex in that folder once and trust it, or add [projects."<path>"] trust_level = "trusted" to ~/.codex/config.toml',
    ),
}


def _check_trusted(svc: Services, folders: Sequence[FolderGrant], agents: Sequence[str]) -> None:
    for folder in folders:
        for agent in agents:
            check, how = _TRUST_CHECKS[agent]
            if not check(svc, folder.path):
                raise CliError(f"{agent} has not trusted {folder.path}; {how}, then run grant again")


def _checked_models(models: Sequence[str]) -> list[str]:
    for model in models:
        if not 0 < len(model) <= _MAX_MODEL_CHARS or not model.isprintable():
            raise CliError("a --model value must be 1 to 128 printable characters")
    return list(models)


def _checked_sessions(sessions: Sequence[str]) -> list[str]:
    for session in sessions:
        if SESSION_ID.fullmatch(session) is None:
            raise CliError(f"{session!r} is not a valid session id")
    return list(sessions)


def _merged(old: Sequence[str], new: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys((*old, *new)))


def _cmd_grant(svc: Services, args: Sequence[str]) -> int:
    parser = make_parser("grant")
    parser.add_argument("label")
    parser.add_argument("--folder", action="append", default=[])
    parser.add_argument("--agent", action="append", default=[], choices=sorted(SUPPORTED_AGENTS))
    parser.add_argument("--model", action="append", default=[])
    parser.add_argument("--send", action="append", default=[])
    options = parser.parse_args(args)
    refuse_remote_origin(svc)
    if not (options.folder or options.agent or options.model or options.send):
        raise UsageError("agent-tabs net grant: name at least one of --folder, --agent, --model or --send")
    new_folders = [folder_grant(spec) for spec in options.folder]
    if len({f.alias for f in new_folders}) != len(new_folders):
        raise CliError("--folder repeats an alias")
    models, sessions = _checked_models(options.model), _checked_sessions(options.send)
    peer = _peer_by_label(load_policy(svc.home), options.label)
    agents = _merged(peer.agents, options.agent)
    kept = tuple(f for f in peer.folders if f.alias not in {n.alias for n in new_folders})
    folders = (*kept, *new_folders)
    _check_trusted(svc, new_folders, agents)
    _check_trusted(svc, [f for f in folders if f not in new_folders], [a for a in options.agent if a not in peer.agents])
    lines = [f"Add grants to {peer.label} ({peer.device_id}):"]
    lines += [f"  folder {f.alias} = {f.path} (st_dev {f.dev}, st_ino {f.ino})" for f in new_folders]
    lines += [f"  agent {a}" for a in options.agent if a not in peer.agents]
    lines += [f"  model {m}" for m in models if m not in peer.models]
    lines += [f"  send to session {s}" for s in sessions if s not in peer.send]
    lines.append("  A granted peer can start agents in the granted folders, within each agent's unattended permissions.")
    _require_confirmed(svc, lines)
    with file_lock(policy_path(svc.home)):
        policy = load_policy(svc.home)
        current = _peer_by_label(policy, options.label)
        kept = tuple(f for f in current.folders if f.alias not in {n.alias for n in new_folders})
        updated = replace(
            current,
            folders=(*kept, *new_folders),
            agents=_merged(current.agents, options.agent),
            models=_merged(current.models, models),
            send=_merged(current.send, sessions),
        )
        save_policy(svc.home, replace(policy, peers=tuple(updated if p.device_id == current.device_id else p for p in policy.peers)))
    svc.record(
        "grant",
        device=peer.device_id,
        folders=len(new_folders),
        agents=len(options.agent),
        models=len(models),
        send=len(sessions),
    )
    svc.notify(f"changed the grants of peer {peer.label}")
    svc.out(f"Effective grants of {peer.label}: {grants_text(updated)}.")
    return 0


def _cmd_revoke(svc: Services, args: Sequence[str]) -> int:
    parser = make_parser("revoke")
    parser.add_argument("label")
    options = parser.parse_args(args)
    refuse_remote_origin(svc)
    net_dir_for(svc.home)
    with file_lock(policy_path(svc.home)):
        policy = load_policy(svc.home)
        peer = _peer_by_label(policy, options.label)
        save_policy(svc.home, replace(policy, peers=tuple(p for p in policy.peers if p.device_id != peer.device_id)))
    remove_file(str(peer_cert_path(svc.home, peer.device_id)))
    store = NetStore(svc.home)
    try:
        generation = store.bump_generation()
    finally:
        store.close()
    svc.record("revoke", device=peer.device_id, generation=generation)
    svc.out(f"Revoked {peer.label} ({peer.device_id}); the policy generation is {generation}.")
    svc.out("Requests already handed off to a local tab may still finish. Revoking here removes no trust on the other machine.")
    return 0


def _cmd_rename(svc: Services, args: Sequence[str]) -> int:
    parser = make_parser("rename")
    parser.add_argument("label")
    parser.add_argument("new_label")
    options = parser.parse_args(args)
    new_label: str = options.new_label
    if LABEL.fullmatch(new_label) is None:
        raise CliError("a label is 1 to 32 characters of a-z, 0-9 and -")
    net_dir_for(svc.home)
    with file_lock(policy_path(svc.home)):
        policy = load_policy(svc.home)
        peer = _peer_by_label(policy, options.label)
        if new_label != peer.label and any(p.label == new_label for p in policy.peers):
            raise CliError(f"the label {new_label} belongs to another peer")
        renamed = replace(peer, label=new_label)
        save_policy(svc.home, replace(policy, peers=tuple(renamed if p.device_id == peer.device_id else p for p in policy.peers)))
    svc.record("rename", device=peer.device_id, label=new_label)
    svc.out(f"Renamed {peer.label} to {new_label} ({peer.device_id}). Its sessions now show as <session>@{new_label}.")
    return 0


def _folder_key(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def folder_defaults(svc: Services, config: NetworkConfig, folder: str | None) -> dict[str, list[str]]:
    if config.member_folders and config.outbound_folders:
        return {}
    session = os.path.abspath(folder if folder is not None else svc.cwd())
    _local_folder(session, "the session folder")
    default = _local_folder(default_member_folder(session, svc.user_home), "the default member folder")
    updates: dict[str, list[str]] = {}
    if not config.member_folders:
        updates["memberFolders"] = [default]
    if not config.outbound_folders:
        updates["outboundFolders"] = [default]
    return updates


def report_folders(svc: Services, config: NetworkConfig, updates: Mapping[str, Sequence[str]]) -> None:
    svc.out(f"member folders: {', '.join(updates.get('memberFolders', config.member_folders))}")
    svc.out(f"outbound folders: {', '.join(updates.get('outboundFolders', config.outbound_folders))}")


def _cmd_hub_create(svc: Services, args: Sequence[str]) -> int:
    parser = make_parser("hub create")
    parser.add_argument("--name")
    parser.add_argument("--folder")
    parser.add_argument("--with", dest="members", action="append", default=[])
    parser.add_argument("--bind")
    options = parser.parse_args(args)
    refuse_remote_origin(svc)
    config, warnings = load_network_config(svc.home)
    for warning in warnings:
        svc.err(f"warning: {warning}")
    name: str = options.name if options.name is not None else default_label(device_name(svc), Policy())
    if LABEL.fullmatch(name) is None:
        raise CliError("--name is 1 to 32 characters of a-z, 0-9 and -")
    _local_folder(os.path.abspath(options.folder if options.folder is not None else svc.cwd()), "the session folder")
    updates = folder_defaults(svc, config, options.folder)
    net_dir_for(svc.home)
    with file_lock(policy_path(svc.home)):
        policy = load_policy(svc.home)
        if policy.hub is not None and not options.members:
            raise CliError(f"this machine is already in the hub {policy.hub.name} ({policy.hub.id}); run agent-tabs net hub leave first")
        if policy.hub is not None and options.name is not None and options.name != policy.hub.name:
            raise CliError(f"this machine is already in the hub {policy.hub.name} ({policy.hub.id}); --name {options.name} does not match")
        created = policy.hub is None
        hub = Hub(svc.new_hub_id(), name) if policy.hub is None else policy.hub
        if created:
            save_policy(svc.home, replace(policy, hub=hub))
    ensure_hub_key(svc.home, hub.id)
    if updates:
        set_network(svc.home, updates)
    svc.record("hub-create" if created else "hub-reuse", hub=hub.id)
    svc.out(f"{'Created' if created else 'Using'} the hub {hub.name} ({hub.id}).")
    report_folders(svc, config, updates)
    if options.members:
        from . import hub_cli

        return hub_cli.join_listed(svc, options.members, options.bind, options.folder)
    return 0


def _member_state(svc: Services, peer: PeerGrant) -> str:
    seen = read_presence(svc.home).get(peer.device_id)
    cached_at: int | None = None
    if os.path.exists(db_path(svc.home)):
        try:
            store = NetStore(svc.home)
            try:
                cached = store.get_inventory(peer.device_id)
            finally:
                store.close()
        except (NetStoreError, sqlite3.Error, OSError):
            cached = None
        cached_at = None if cached is None else cached[1]
    if seen is not None and (cached_at is None or seen.at_ms >= cached_at):
        if not seen.online:
            return "offline"
        if now_ms() - seen.at_ms <= OFFLINE_AFTER_MS:
            return "online"
        if cached_at is None:
            return "unknown"
    if cached_at is None:
        return "unknown"
    return "online" if now_ms() - cached_at <= OFFLINE_AFTER_MS else "offline"


def _cmd_hub_status(svc: Services, args: Sequence[str]) -> int:
    make_parser("hub status").parse_args(args)
    config, warnings = load_network_config(svc.home)
    for warning in warnings:
        svc.err(f"warning: {warning}")
    policy = load_policy(svc.home)
    if policy.hub is None:
        svc.out("hub: none")
        return 0
    svc.out(f"hub: {policy.hub.name} ({policy.hub.id})")
    members = [p for p in policy.peers if p.member]
    if members:
        svc.out("members:")
        for peer in members:
            svc.out(f"  {peer.label}  {peer.device_id[:15]}  {peer.address}  {_member_state(svc, peer)}")
    else:
        svc.out("members: none")
    svc.out(f"member folders: {', '.join(config.member_folders) or 'none'}")
    svc.out(f"allowed networks: {', '.join(config.allowed_networks) or 'none'}")
    return 0


def _cmd_hub_leave(svc: Services, args: Sequence[str]) -> int:
    make_parser("hub leave").parse_args(args)
    refuse_remote_origin(svc)
    net_dir_for(svc.home)
    with file_lock(policy_path(svc.home)):
        policy = load_policy(svc.home)
        hub = policy.hub
        if hub is None:
            raise CliError("this machine is not in a hub")
        members = [p for p in policy.peers if p.member]
        save_policy(svc.home, replace(policy, peers=tuple(p for p in policy.peers if not p.member), hub=None))
    for peer in members:
        remove_file(str(peer_cert_path(svc.home, peer.device_id)))
    remove_hub_state(svc.home)
    store = NetStore(svc.home)
    try:
        generation = store.bump_generation()
    finally:
        store.close()
    svc.record("hub-leave", hub=hub.id, members=len(members), generation=generation)
    svc.out(f"Left the hub {hub.name} ({hub.id}); removed {len(members)} member(s); the policy generation is {generation}.")
    svc.out("The other members are not told yet, and keep their trust of this machine until they revoke it.")
    svc.out("Requests already handed off to a local tab may still finish.")
    return 0


def _cmd_hub_join(svc: Services, args: Sequence[str]) -> int:
    from . import hub_cli

    return hub_cli.join(svc, args)


def _cmd_hub(svc: Services, args: Sequence[str]) -> int:
    handlers: dict[str, Callable[[Services, Sequence[str]], int]] = {
        "create": _cmd_hub_create,
        "join": _cmd_hub_join,
        "status": _cmd_hub_status,
        "leave": _cmd_hub_leave,
    }
    if not args or args[0] not in handlers:
        raise UsageError("agent-tabs net hub: expected create, join, status or leave")
    return handlers[args[0]](svc, args[1:])


def _cmd_folders(svc: Services, args: Sequence[str]) -> int:
    parser = make_parser("folders")
    parser.add_argument("action", nargs="?", choices=("add", "remove"))
    parser.add_argument("path", nargs="?")
    options = parser.parse_args(args)
    if options.action is not None:
        refuse_remote_origin(svc)
    config, warnings = load_network_config(svc.home)
    for warning in warnings:
        svc.err(f"warning: {warning}")
    folders = list(config.member_folders)
    if options.action is None:
        svc.out("member folders: " + (", ".join(folders) if folders else "none"))
        return 0
    path: str | None = options.path
    if path is None:
        raise UsageError(f"agent-tabs net folders {options.action}: expected an absolute path")
    if options.action == "add":
        real = _local_folder(path, "member folder")
        if any(_folder_key(f) == _folder_key(real) for f in folders):
            svc.out(f"{real} is already a member folder.")
            return 0
        folders.append(real)
    else:
        if not os.path.isabs(path):
            raise CliError("member folder: the path must be an absolute path")
        wanted = {_folder_key(path), _folder_key(os.path.realpath(path))}
        kept = [f for f in folders if _folder_key(f) not in wanted]
        if len(kept) == len(folders):
            raise CliError(f"{path} is not a member folder; agent-tabs net folders lists them")
        folders = kept
    set_network(svc.home, {"memberFolders": folders})
    svc.record("folders", action=options.action, count=len(folders))
    svc.out("member folders: " + (", ".join(folders) if folders else "none"))
    return 0


def _cmd_lan(svc: Services, args: Sequence[str]) -> int:
    handlers: dict[str, Callable[[Services, Sequence[str]], int]] = {
        "setup": _cmd_setup,
        "on": _cmd_on,
        "start": _cmd_start,
        "stop": _cmd_stop,
    }
    if not args or args[0] not in handlers:
        raise UsageError("agent-tabs net lan: expected setup, on, start or stop")
    return handlers[args[0]](svc, args[1:])


def _cmd_discoverable(svc: Services, args: Sequence[str]) -> int:
    from . import hub_cli

    return hub_cli.discoverable(svc, args)


def _cmd_networks(svc: Services, args: Sequence[str]) -> int:
    from . import hub_cli

    return hub_cli.networks(svc, args)


def _cmd_find(svc: Services, args: Sequence[str]) -> int:
    from . import hub_cli

    return hub_cli.find(svc, args)


def _cmd_pair(svc: Services, args: Sequence[str]) -> int:
    from . import pair_cli

    return pair_cli.run(svc, svc.pair or pair_cli.default_deps(), args)


def main(argv: Sequence[str], services: Services | None = None) -> int:
    svc = services or default_services()
    handlers: dict[str, Callable[[Services, Sequence[str]], int]] = {
        "id": _cmd_id,
        "status": _cmd_status,
        "off": _cmd_off,
        "trust": _cmd_trust,
        "grant": _cmd_grant,
        "revoke": _cmd_revoke,
        "rename": _cmd_rename,
        "pair": _cmd_pair,
        "lan": _cmd_lan,
        "hub": _cmd_hub,
        "folders": _cmd_folders,
        "discoverable": _cmd_discoverable,
        "networks": _cmd_networks,
        "find": _cmd_find,
    }
    try:
        if not argv or argv[0] not in handlers:
            raise UsageError("expected a command")
        return handlers[argv[0]](svc, list(argv[1:]))
    except UsageError as error:
        svc.err(f"{error}\nUsage: {_GRAMMAR}")
        return 2
    except (
        CliError,
        ConfirmError,
        PolicyError,
        NetStoreError,
        CertgenError,
        SecretsError,
        ingress.IngressTokenError,
        elevation.ElevationError,
        PairingError,
        InterfaceError,
        FirewallError,
        OSError,
    ) as error:
        svc.err(f"error: {error}")
        return 1

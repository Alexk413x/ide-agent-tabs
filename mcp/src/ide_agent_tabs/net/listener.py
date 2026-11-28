from __future__ import annotations

import contextlib
import functools
import ipaddress
import os
import select
import socket
import ssl
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from ..clock import now_iso, now_ms
from ..files import read_text_if_exists, remove_file, replace_retrying, write_atomically
from ..home import agent_tabs_home
from ..jsjson import parse, stringify
from ..liveness import pid_alive
from ..processes import child_flags
from ..shared.client import ensure_server, probe, server_command, verified_state
from ..shared.client import request as http_request
from ..shared.state import DEFAULT_PORT as SERVER_DEFAULT_PORT
from ..shared.state import PORT_OPTION_ENV, SERVER_LAUNCHER, parse_port
from ..version import PACKAGE_VERSION, compare_versions
from . import ingress, pairing, selfupdate, tls, wire
from .beaconsvc import BeaconService, BeaconSettings, BeaconState
from .client import (
    IDLE_S,
    DeadlineSocket,
    NetClientError,
    as_socket,
    load_identity,
    peers_dir,
    read_peer_pem,
    send_request,
)
from .hubjoin import HubJoiner, local_pair_identity
from .hubsync import introduce_member
from .identity import format_digest
from .interfaces import InterfaceError, InterfaceInfo, interface_for, local_addresses
from .migration import ERRORS as MIGRATION_ERRORS
from .migration import Migrator, post_to_sessions
from .netid import GateState, current_network_id, fallback_of, gate_state
from .notify import notify, powershell_path
from .policy import NetworkConfig, PolicyError, load_network_config, load_policy, policy_path, update_network_section
from .tailscale import TailscaleClient, TailscaleDiscovery, TailscaleError, is_tailscale_address
from .transport import LanTransport, TailscaleTransport, Transport, network_enabled, transport_for
from .tsmembers import TailscaleMembers

MAX_CONNECTIONS = 8
PER_SOURCE = 2
HANDSHAKE_S = 5.0
REQUEST_TOTAL_S = 60.0
INGRESS_TIMEOUT_MS = 25_000
SERVER_START_WAIT_MS = 3_000
SERVER_START_INTERVAL_S = 10.0
PROFILE_REFRESH_S = 30.0
PROFILE_TIMEOUT_S = 15
NETWORK_ID_GRACE_S = 120.0
MONITOR_S = 1.0
CONFIG_RECHECK_S = 5.0
FAILURE_LOG_INTERVAL_S = 60.0
FAILURE_LOG_ENTRIES = 256
LOG_LIMIT = 1_000_000
LISTEN_BACKLOG = 16
JOIN_RETRY_S = 30.0
REBIND_WAIT_S = 120.0
REBIND_RETRY_S = 5.0
TAILSCALE_WAIT_S = 120.0
MIGRATION_TICK_S = 30.0
_PROFILE_SCRIPT = (
    "$ErrorActionPreference = 'Stop';"
    "$i = (Get-NetIPAddress -IPAddress $env:AGENT_TABS_BIND).InterfaceIndex;"
    "(Get-NetConnectionProfile -InterfaceIndex $i).NetworkCategory"
)
_PROFILE_ENV = "AGENT_TABS_BIND"


def listener_file(home: str) -> Path:
    return ingress.net_dir(home) / "listener.json"


def stop_file(home: str) -> Path:
    return ingress.net_dir(home) / "listener.stop"


def log_file(home: str, name: str = "listener.log") -> str:
    return str(ingress.net_dir(home) / name)


def append_log(path: str, line: str, limit: int = LOG_LIMIT) -> None:
    text = "".join(char if char.isprintable() else "?" for char in line)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path) and os.path.getsize(path) > limit:
            replace_retrying(path, f"{path}.1")
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"{now_iso()} {text}\n")
    except OSError:
        pass


@dataclass(frozen=True)
class ListenerInfo:
    pid: int
    started_ms: int
    bind: str
    port: int
    version: str | None = None
    build: str | None = None
    serving_joins: bool = False


@dataclass(frozen=True)
class Build:
    version: str
    id: str


@functools.lru_cache(maxsize=1)
def installed_build() -> Build:
    from ..server_copy import bundled_build, plugin_source

    return Build(PACKAGE_VERSION, bundled_build(plugin_source()))


def stale_reason(info: ListenerInfo, installed: Build) -> str | None:
    if info.build is None or info.version is None:
        return "records no build, so an older plugin version started it"
    if info.build == installed.id or compare_versions(info.version, installed.version) > 0:
        return None
    return f"runs build {info.build}, not the installed build {installed.id}"


def _whole(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def read_listener(home: str) -> ListenerInfo | None:
    try:
        data = parse(read_text_if_exists(str(listener_file(home))) or "")
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    pid, started, bind, port = _whole(data.get("pid")), _whole(data.get("startedMs")), data.get("bind"), _whole(data.get("port"))
    if pid is None or started is None or port is None or not isinstance(bind, str):
        return None
    version, build = data.get("version"), data.get("build")
    return ListenerInfo(
        pid,
        started,
        bind,
        port,
        version if isinstance(version, str) else None,
        build if isinstance(build, str) else None,
        data.get("joins") is True,
    )


def live_listener(home: str) -> ListenerInfo | None:
    info = read_listener(home)
    return info if info is not None and pid_alive(info.pid) else None


def request_stop(home: str) -> None:
    write_atomically(str(stop_file(home)), "stop\n")


def _close_listening(sock: socket.socket) -> None:
    # On Linux, close() leaves the socket listening while another thread waits in accept(); shutdown() wakes it.
    with contextlib.suppress(OSError):
        sock.shutdown(socket.SHUT_RDWR)
    with contextlib.suppress(OSError):
        sock.close()


def _normalize(text: str) -> str | None:
    try:
        ip = ipaddress.ip_address(text.split("%", 1)[0])
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return str(ip)


def windows_network_is_public(bind: str) -> bool | None:
    if sys.platform != "win32":
        return False
    shell = powershell_path()
    if shell is None:
        return None
    try:
        done = subprocess.run(
            [shell, "-NoProfile", "-NonInteractive", "-Command", _PROFILE_SCRIPT],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            env={**os.environ, _PROFILE_ENV: bind},
            timeout=PROFILE_TIMEOUT_S,
            creationflags=child_flags(),
            shell=False,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    lines = [line.strip() for line in done.stdout.decode("utf-8", "replace").splitlines() if line.strip()]
    if done.returncode != 0 or not lines:
        return None
    return "Public" in lines


def _address_present(bind: str) -> bool:
    probe_socket = socket.socket(socket.AF_INET6 if ":" in bind else socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe_socket.bind((bind, 0))
        return True
    except OSError:
        return False
    finally:
        probe_socket.close()


def _local_addresses() -> frozenset[str]:
    try:
        return frozenset(str(info[4][0]) for info in socket.getaddrinfo(socket.gethostname(), None))
    except OSError:
        return frozenset()


class IngressForwarder:
    def __init__(
        self,
        home: str,
        server_port: int,
        log: Callable[[str], None],
        *,
        launcher: str | None = None,
        python: str | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._home = home
        self._port = server_port
        self._log = log
        self._launcher = launcher or str(Path(__file__).resolve().parents[3] / "launch" / SERVER_LAUNCHER)
        self._python = python or sys.executable
        self._clock = clock
        self._lock = threading.Lock()
        self._last_start: float | None = None

    def _start(self) -> dict[str, Any] | None:
        if not os.path.isabs(self._python) or not os.path.isfile(self._launcher):
            self._log("the local server cannot be started: the interpreter or launcher path is not usable")
            return None
        with self._lock:
            now = self._clock()
            if self._last_start is not None and now - self._last_start < SERVER_START_INTERVAL_S:
                self._log("the local server is down and a start was tried less than 10 s ago")
                return None
            self._last_start = now
        ensured = ensure_server(
            server_command(self._launcher, self._port, self._python), self._port, self._home, PACKAGE_VERSION, SERVER_START_WAIT_MS
        )
        if ensured.problem is not None or ensured.health is None:
            self._log(f"the local server did not start: {ensured.problem}")
            return None
        return ensured.health

    def forward(self, peer: str, request: object) -> dict[str, Any] | None:
        found = probe(self._port)
        if found.kind == "other":
            self._log(f"port {self._port} belongs to another program; the request is refused")
            return None
        health = found.health if found.kind == "ours" else self._start()
        if health is None:
            return None
        state = verified_state(self._home, self._port, health)
        if state is None or not pid_alive(int(state.pid)):
            self._log(f"the server on port {self._port} matches no live state file; the request is refused")
            return None
        try:
            token = ingress.read_ingress_token(self._home)
        except ingress.IngressTokenError as error:
            self._log(f"the ingress token is not usable: {error}")
            return None
        if token is None:
            self._log("there is no ingress token; run agent-tabs net lan setup")
            return None
        payload = wire.encode({"peer": peer, "request": request}).decode("utf-8")
        reply = http_request(self._port, "POST", ingress.INGRESS_PATH, token, INGRESS_TIMEOUT_MS, payload)
        if reply is None or reply.status != 200:
            self._log(f"the server answered the ingress request with status {'none' if reply is None else reply.status}")
            return None
        try:
            parsed = wire.parse_json(reply.body.encode("utf-8"))
        except wire.WireError:
            self._log("the server sent an ingress reply that is not valid JSON")
            return None
        return parsed if isinstance(parsed, dict) else None  # pyright: ignore[reportUnknownVariableType]


@dataclass(frozen=True)
class Trust:
    context: ssl.SSLContext | None = None
    peers: dict[str, str] = field(default_factory=dict)
    addresses: frozenset[str] = frozenset()


class TrustCache:
    def __init__(self, home: str, cert: Path, key: Path, passphrase: bytes, log: Callable[[str], None]) -> None:
        self._home = home
        self._cert = cert
        self._key = key
        self._passphrase = passphrase
        self._log = log
        self._lock = threading.Lock()
        self._signature: object = object()
        self._trust = Trust()

    def _stamp(self) -> object:
        def stat_of(path: str) -> tuple[int, int] | None:
            try:
                status = os.stat(path)
            except OSError:
                return None
            return status.st_mtime_ns, status.st_size

        pems: list[tuple[str, int, int]] = []
        with contextlib.suppress(OSError), os.scandir(peers_dir(self._home)) as entries:
            for entry in entries:
                if entry.name.endswith(".pem"):
                    status = entry.stat()
                    pems.append((entry.name, status.st_mtime_ns, status.st_size))
        return stat_of(policy_path(self._home)), tuple(sorted(pems))

    def _build(self) -> Trust:
        try:
            policy = load_policy(self._home)
        except PolicyError as error:
            self._log(f"policy.json is not usable, so every peer is refused: {error}")
            return Trust()
        pems: list[str] = []
        peers: dict[str, str] = {}
        for peer in policy.peers:
            pem = read_peer_pem(self._home, peer.device_id)
            address = _normalize(peer.address)
            if pem is None or address is None:
                self._log(f"peer {peer.label} has no usable certificate or address, so it is refused")
                continue
            pems.append(pem)
            peers[peer.device_id] = address
        if not pems:
            return Trust()
        try:
            context = tls.server_context(self._cert, self._key, lambda: self._passphrase, pems)
        except (ssl.SSLError, ValueError, OSError) as error:
            self._log(f"the TLS context cannot be built, so every peer is refused: {error}")
            return Trust()
        return Trust(context, peers, frozenset(peers.values()))

    def current(self) -> Trust:
        with self._lock:
            stamp = self._stamp()
            if stamp != self._signature:
                self._trust = self._build()
                self._signature = stamp
            return self._trust


class NetworkWatch:
    def __init__(
        self,
        bind: str,
        check: Callable[[str], bool | None],
        clock: Callable[[], float] = time.monotonic,
        addresses: Callable[[], frozenset[str]] = _local_addresses,
        present: Callable[[str], bool] | None = None,
    ) -> None:
        self._bind = bind
        self._present = present
        self._check = check
        self._clock = clock
        self._addresses = addresses
        self._known = addresses()
        self._checked_at = clock()
        self._ok = True
        self.changed = False
        self.lost = False
        self._lock = threading.Lock()

    def allowed(self) -> bool:
        with self._lock:
            return self._ok

    def initial(self) -> str | None:
        public = self._check(self._bind)
        self._checked_at = self._clock()
        if public is None:
            self._set(False)
            return "the network category of the bind address cannot be determined"
        if public:
            self._set(False)
            return "the network of the bind address is marked Public"
        return None

    def _set(self, ok: bool) -> None:
        with self._lock:
            self._ok = ok

    def retarget(self, bind: str) -> None:
        self._bind = bind
        self._known = self._addresses()
        self._checked_at = self._clock()
        self.lost = False
        self._set(True)

    def tick(self) -> str | None:
        if not (self._present or _address_present)(self._bind):
            self._set(False)
            self.lost = True
            return "the bind address is no longer assigned to a local interface"
        addresses = self._addresses()
        changed = addresses != self._known
        self.changed = changed
        self._known = addresses
        if not changed and self._clock() - self._checked_at < PROFILE_REFRESH_S:
            return None
        public = self._check(self._bind)
        self._checked_at = self._clock()
        if public:
            self._set(False)
            return "the network of the bind address is marked Public"
        return None


class _Refused(Exception):
    pass


class _OwnerRefused(_Refused):
    pass


def _not_public(_bind: str) -> bool | None:
    return False


@dataclass
class Options:
    config: NetworkConfig | None = None
    bind_check: Callable[[str], bool] | None = None
    network_check: Callable[[str], bool | None] = windows_network_is_public
    server_port: int = SERVER_DEFAULT_PORT
    stop: threading.Event = field(default_factory=threading.Event)
    ready: Callable[[int], None] | None = None
    forwarder: IngressForwarder | None = None
    network_id: Callable[[str], str | None] = current_network_id
    join_port: int = pairing.PAIRING_PORT
    join_timeouts: pairing.Timeouts = pairing.DEFAULT_TIMEOUTS
    join_source_ok: Callable[[str], bool] | None = None
    notice: Callable[[str], object] = notify
    beacons: BeaconSettings | None = None
    member_request: Callable[..., dict[str, Any]] | None = None
    address_present: Callable[[str], bool] | None = None
    candidates: Callable[[], list[InterfaceInfo]] | None = None
    rebind_wait_s: float = REBIND_WAIT_S
    migration: Migrator | None = None
    build: Build | None = None
    plugin_version: str = PACKAGE_VERSION
    updater: selfupdate.JoinUpdater | None = None
    tailscale: TailscaleClient | None = None
    transport: Transport | None = None
    ready_wait_s: float = TAILSCALE_WAIT_S
    members: TailscaleMembers | None = None


class Listener:
    def __init__(self, home: str, options: Options) -> None:
        self._home = home
        self._options = options
        self._stop = options.stop
        self._lock = threading.Lock()
        self._active = 0
        self._per_source: dict[str, int] = {}
        self._failures: dict[str, float] = {}
        self._sock: socket.socket | None = None
        self._bind = ""
        self._trust: TrustCache | None = None
        self._watch: NetworkWatch | None = None
        self._forwarder: IngressForwarder | None = None
        self._live = NetworkConfig()
        self._gate = GateState(False, None, "the network has not been checked yet")
        self._network_id: str | None = None
        self._fallback_noted: str | None = None
        self._network_read_at = float("-inf")
        self._unread_since: float | None = None
        self._join_sock: socket.socket | None = None
        self._join_thread: threading.Thread | None = None
        self._join_stop = threading.Event()
        self._join_retry_at = 0.0
        self._address_epoch = 0
        self._beacon_thread: threading.Thread | None = None
        self._beacons: BeaconService | None = None
        self._started = NetworkConfig()
        self._started_ms = now_ms()
        self._port = 0
        self._info_lock = threading.Lock()
        self._rebind_at = 0.0
        self._rebind_note = ""
        self._migration_at = 0.0
        self._transport: Transport = LanTransport()
        self._transport_lock = threading.Lock()
        self._mode = "lan"
        self._members: TailscaleMembers | None = None

    def log(self, text: str) -> None:
        append_log(log_file(self._home), text)

    def _refuse(self, reason: str) -> int:
        self.log(f"not listening: {reason}")
        return 1

    def _config(self) -> NetworkConfig:
        return self._options.config if self._options.config is not None else load_network_config(self._home)[0]

    def _build_transport(self, config: NetworkConfig) -> Transport:
        if self._options.transport is not None:
            return self._options.transport
        return transport_for(config, self._options.tailscale) or LanTransport()

    def _active_transport(self) -> Transport:
        with self._transport_lock:
            held = self._transport
            if isinstance(held, TailscaleTransport) and not held.allowed_owner_ids and self._options.transport is None:
                held = self._transport = self._build_transport(self._live)
            return held

    def _bind_check(self) -> Callable[[str], bool]:
        return self._options.bind_check or self._transport.allows_bind

    def _network_check(self) -> Callable[[str], bool | None]:
        return _not_public if self._mode == "tailscale" else self._options.network_check

    def _own_devices(self) -> list[Any]:
        held = self._active_transport()
        if not isinstance(held, TailscaleTransport):
            return []
        return list(TailscaleDiscovery(held.client, held.allowed_owner_ids).scan())

    def _open_socket(self, bind: str, port: int) -> socket.socket:
        sock = socket.socket(socket.AF_INET6 if ":" in bind else socket.AF_INET, socket.SOCK_STREAM)
        try:
            if sys.platform == "win32":
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            else:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((bind, port))
            sock.listen(LISTEN_BACKLOG)
            sock.settimeout(0.5)
        except OSError:
            sock.close()
            raise
        return sock

    def _is_present(self, bind: str) -> bool:
        return (self._options.address_present or _address_present)(bind)

    def _pick_address(self, allowed: Sequence[str], old: str) -> str | None:
        try:
            if self._options.candidates is not None:
                found = self._options.candidates()
            else:
                found = self._transport.candidates(local_addresses(), interface_for)
        except (OSError, ValueError) as error:
            self._note_rebind(f"the addresses of this machine can't be listed: {error}")
            return None
        usable: list[str] = []
        for info in found:
            if self._mode == "tailscale":
                usable.append(info.address)
                continue
            try:
                network = self._options.network_id(info.address)
            except (OSError, ValueError):
                continue
            if gate_state(allowed, network).allowed:
                usable.append(info.address)
        if len(usable) > 1:
            try:
                near = ipaddress.ip_network(f"{old}/24", strict=False)
                usable = [a for a in usable if ipaddress.ip_address(a) in near]
            except ValueError:
                usable = []
        if len(usable) == 1:
            return usable[0]
        self._note_rebind(
            "no address on an allowed network replaced it"
            if not usable
            else f"more than one address on an allowed network could replace it: {', '.join(usable)}"
        )
        return None

    def _note_rebind(self, text: str) -> None:
        if text != self._rebind_note:
            self._rebind_note = text
            self.log(f"the bind address is gone and {text}")

    def run(self) -> int:
        if self._options.migration is not None:
            try:
                self._options.migration.start(self._home)
            except MIGRATION_ERRORS as error:
                self.log(f"moving the paired machines into a hub failed: {type(error).__name__}: {error}")
        config = self._config()
        if not network_enabled(config) or config.bind is None:
            return self._refuse("network is not lan or no bind address is set")
        bind = config.bind
        self._mode = config.network
        self._transport = self._build_transport(config)
        if self._mode == "tailscale":
            try:
                self._transport.wait_ready(bind, self._options.ready_wait_s, True)
            except InterfaceError as error:
                return self._refuse(f"the Tailscale address {bind} is not ready after {self._options.ready_wait_s:g} s: {error}")
        elif not self._is_present(bind):
            moved = self._pick_address(config.allowed_networks, bind)
            if moved is not None:
                try:
                    update_network_section(self._home, {"bind": moved})
                except (OSError, ValueError) as error:
                    return self._refuse(f"the bind address {bind} is gone, and the new address {moved} could not be saved: {error}")
                self.log(f"the bind address {bind} is gone; using {moved} and saving it as network.bind")
                bind = moved
                config = replace(config, bind=moved)
        if not self._bind_check()(bind):
            return self._refuse(
                "the bind address is not in 100.64.0.0/10"
                if self._mode == "tailscale"
                else "the bind address is not a private IPv4 or IPv6 unique local address"
            )
        problem = tls.runtime_problem()
        if problem is not None:
            return self._refuse(problem)
        try:
            identity = load_identity(self._home)
        except NetClientError as error:
            return self._refuse(str(error))
        existing = read_listener(self._home)
        if existing is not None and existing.pid != os.getpid() and pid_alive(existing.pid):
            return self._refuse(f"a listener already runs (pid {existing.pid})")
        watch = NetworkWatch(bind, self._network_check(), present=self._options.address_present)
        reason = watch.initial()
        if reason is not None:
            return self._refuse(reason)
        try:
            sock = self._open_socket(bind, config.port)
        except OSError as error:
            return self._refuse(f"cannot bind {bind} port {config.port}: {error}")
        self._sock, self._bind, self._watch = sock, bind, watch
        self._live = self._started = config
        self._started_ms = now_ms()
        self._refresh_gate(force=True)
        self._trust = TrustCache(self._home, identity.cert, identity.key, identity.passphrase, self.log)
        self._forwarder = self._options.forwarder or IngressForwarder(self._home, self._options.server_port, self.log)
        port = self._port = sock.getsockname()[1]
        with contextlib.suppress(OSError):
            remove_file(str(stop_file(self._home)))
        self._write_info()
        self.log(f"listening on {bind} port {port} (pid {os.getpid()})")
        if self._options.ready is not None:
            self._options.ready(port)
        monitor = threading.Thread(target=self._monitor, daemon=True)
        monitor.start()
        if self._mode == "tailscale":
            self._members = self._options.members or TailscaleMembers(self._home, self._own_devices, self.log)
        elif self._options.beacons is not None:
            self._beacons = BeaconService(self._home, bind, self._beacon_state, self._stop, self.log, self._options.beacons)
            self._beacon_thread = self._beacons.start()
        try:
            self._serve()
        finally:
            self._stop.set()
            if self._beacon_thread is not None:
                self._beacon_thread.join(2.0)
            self._stop_joins("the listener stopped")
            if self._sock is not None:
                _close_listening(self._sock)
            info = read_listener(self._home)
            if info is not None and info.pid == os.getpid():
                with contextlib.suppress(OSError):
                    remove_file(str(listener_file(self._home)))
            self.log("stopped")
        return 0

    def _write_info(self) -> None:
        build = self._options.build or installed_build()
        body = {
            "pid": os.getpid(),
            "startedMs": self._started_ms,
            "bind": self._bind,
            "port": self._port,
            "version": build.version,
            "build": build.id,
            "joins": self._join_sock is not None,
        }
        with self._info_lock:
            write_atomically(str(listener_file(self._home)), stringify(body, 2) + "\n")

    def _write_info_quietly(self) -> None:
        with contextlib.suppress(OSError):
            self._write_info()

    def _halt(self, reason: str) -> None:
        self.log(f"stopping: {reason}")
        self._stop.set()

    def _monitor(self) -> None:
        assert self._watch is not None
        config_checked = time.monotonic()
        lost_since: float | None = None
        while not self._stop.wait(MONITOR_S):
            if os.path.exists(stop_file(self._home)):
                with contextlib.suppress(OSError):
                    remove_file(str(stop_file(self._home)))
                self._halt("stop requested")
                return
            reason = self._watch.tick()
            lost = self._watch.lost
            if reason is not None and not lost:
                self._halt(reason)
                return
            if self._options.config is None and time.monotonic() - config_checked >= CONFIG_RECHECK_S:
                config_checked = time.monotonic()
                current = self._config()
                if not network_enabled(current) or current.port != self._started.port or current.network != self._started.network:
                    self._halt("the network setting changed")
                    return
                if current.bind != self._started.bind and not (current.bind is not None and self._follow_config(current.bind)):
                    self._halt("the network setting changed")
                    return
                self._live = current
            if lost and self._watch.lost:
                now = time.monotonic()
                lost_since = now if lost_since is None else lost_since
                if now >= self._rebind_at:
                    self._rebind_at = now + REBIND_RETRY_S
                    moved = self._pick_address(self._live.allowed_networks, self._bind)
                    if moved is not None and self._move_to(moved, persist=True):
                        lost_since = None
                if lost_since is not None:
                    if now - lost_since >= self._options.rebind_wait_s:
                        self._halt(f"{reason}, and no address on an allowed network replaced it within {self._options.rebind_wait_s:g} s")
                        return
                    continue
            lost_since = None
            if self._watch.changed:
                self._address_epoch += 1
            self._refresh_gate(force=self._watch.changed)
            self._sync_joins()
            self._tick_migration()
            if self._members is not None:
                self._members.tick()

    def _tick_migration(self) -> None:
        migration = self._options.migration
        if migration is None or time.monotonic() < self._migration_at:
            return
        self._migration_at = time.monotonic() + MIGRATION_TICK_S
        try:
            migration.tick(self._home, self._network_id)
        except MIGRATION_ERRORS as error:
            self.log(f"completing the move into a hub failed: {type(error).__name__}: {error}")

    def _follow_config(self, bind: str) -> bool:
        if self._is_present(self._bind):
            return False
        return self._move_to(bind, persist=False)

    def _move_to(self, bind: str, *, persist: bool) -> bool:
        old = self._bind
        if not self._bind_check()(bind):
            self.log(
                f"not moving to {bind}: it is not in 100.64.0.0/10"
                if self._mode == "tailscale"
                else f"not moving to {bind}: it is not a private IPv4 or IPv6 unique local address"
            )
            return False
        public = self._network_check()(bind)
        if public is None or public:
            self.log(f"not moving to {bind}: its network is marked Public or its category can't be read")
            return False
        try:
            sock = self._open_socket(bind, self._live.port)
        except OSError as error:
            self.log(f"not moving to {bind}: cannot bind it: {error}")
            return False
        if persist:
            try:
                update_network_section(self._home, {"bind": bind})
            except (OSError, ValueError) as error:
                sock.close()
                self.log(f"not moving to {bind}: network.bind could not be saved: {error}")
                return False
        self._started = replace(self._started, bind=bind)
        previous, self._sock, self._bind = self._sock, sock, bind
        self._live = replace(self._live, bind=bind)
        if previous is not None:
            _close_listening(previous)
        assert self._watch is not None
        self._watch.retarget(bind)
        self._stop_joins("the bind address changed")
        self._join_retry_at = 0.0
        self._network_read_at = float("-inf")
        self._address_epoch += 1
        port = self._port = sock.getsockname()[1]
        self._write_info()
        self._refresh_gate(force=True)
        if self._beacons is not None:
            self._beacons.rebind(bind)
        self._rebind_note = ""
        self.log(
            f"the bind address {old} is gone; now listening on {bind} port {port}. "
            "If the subnet changed, run agent-tabs net discoverable on to update the firewall rule"
        )
        return True

    def _refresh_tailscale_gate(self, force: bool) -> None:
        now = time.monotonic()
        if force or now - self._network_read_at >= PROFILE_REFRESH_S:
            self._network_read_at = now
            try:
                self._active_transport().require_ready(self._bind, True)
                state = GateState(True)
            except (InterfaceError, OSError, ValueError, TailscaleError) as error:
                state = GateState(False, None, str(error))
            if (state.allowed, state.reason) != (self._gate.allowed, self._gate.reason):
                self.log("Tailscale is ready" if state.allowed else f"network gate closed: {state.reason}")
            self._gate = state

    def _refresh_gate(self, force: bool = False) -> None:
        if self._mode == "tailscale":
            self._refresh_tailscale_gate(force)
            return
        now = time.monotonic()
        if force or now - self._network_read_at >= PROFILE_REFRESH_S:
            self._network_read_at = now
            try:
                found = self._options.network_id(self._bind)
            except (OSError, ValueError):
                found = None
            self._network_id = self._kept_network_id(found, now, force)
        self._note_fallback()
        state = gate_state(self._live.allowed_networks, self._network_id)
        if (state.allowed, state.reason) != (self._gate.allowed, self._gate.reason):
            self.log("this network is allowed" if state.allowed else f"network gate closed: {state.reason}")
        self._gate = state

    def _kept_network_id(self, found: str | None, now: float, force: bool) -> str | None:
        # A forced read follows a start or an address change, which can mean another network, so it never keeps the last id.
        if found is not None or force or self._network_id is None:
            if found is not None and self._unread_since is not None:
                self.log("the id of this network can be read again")
            self._unread_since = None
            return found
        if self._unread_since is None:
            self._unread_since = now
            self.log(f"the id of this network can't be read; the gate keeps the last id for up to {NETWORK_ID_GRACE_S:g} s")
        if now - self._unread_since < NETWORK_ID_GRACE_S:
            return self._network_id
        self._unread_since = None
        return None

    def _note_fallback(self) -> None:
        note = fallback_of(self._network_id)
        if note != self._fallback_noted:
            self._fallback_noted = note
            if note is not None:
                self.log(f"network id: {note}")

    def _beacon_state(self) -> BeaconState:
        watch = self._watch
        open_ = network_enabled(self._live) and self._gate.allowed and watch is not None and watch.allowed()
        return BeaconState(open_, self._live.discoverable, self._address_epoch)

    def join_port(self) -> int | None:
        sock = self._join_sock
        if sock is None:
            return None
        try:
            return int(sock.getsockname()[1])
        except OSError:
            return None

    def _wants_joins(self) -> bool:
        return network_enabled(self._live) and self._live.discoverable and self._gate.allowed and ":" not in self._bind

    def _sync_joins(self) -> None:
        wanted = self._wants_joins()
        if wanted and self._join_sock is None:
            self._start_joins()
        elif not wanted and self._join_sock is not None:
            self._stop_joins("discoverable is off, the network is not allowed, or LAN mode is off")

    def _start_joins(self) -> None:
        if time.monotonic() < self._join_retry_at:
            return
        self._join_retry_at = time.monotonic() + JOIN_RETRY_S
        try:
            identity = local_pair_identity(self._home)
            sock = self._open_socket(self._bind, self._options.join_port)
        except (pairing.PairingError, OSError) as error:
            self.log(f"not serving joins: {error}")
            return
        stop = threading.Event()
        handler = HubJoiner(self._home, self._saved, self._introduce)
        tailscale = self._mode == "tailscale"
        kwargs: dict[str, Any] = {
            "timeouts": self._options.join_timeouts,
            "on_result": self._joined,
            "allow_source": self._options.join_source_ok or (is_tailscale_address if tailscale else pairing.private_source),
            "log": self.log,
            "plugin_version": self._options.plugin_version,
        }
        if tailscale:
            kwargs["transport"] = "tailscale"
            kwargs["check_connection"] = self._check_owner
        thread = threading.Thread(
            target=pairing.serve_joins,
            args=(sock, identity, handler, pairing.Limits(self._home), stop),
            kwargs=kwargs,
            daemon=True,
        )
        self._join_sock, self._join_stop, self._join_thread = sock, stop, thread
        thread.start()
        self._write_info_quietly()
        self.log(f"serving joins on {self._bind} port {sock.getsockname()[1]}")

    def _check_owner(self, source: str) -> None:
        self._active_transport().check_connection(source)

    def _stop_joins(self, reason: str) -> None:
        sock, thread = self._join_sock, self._join_thread
        if sock is None:
            return
        self._join_stop.set()
        if thread is not None:
            thread.join(2.0)
        sock.close()
        self._join_sock, self._join_thread = None, None
        self._write_info_quietly()
        self.log(f"stopped serving joins: {reason}")

    def _saved(self, label: str, address: str) -> None:
        self.log(f"a device joined the hub: {label} at {address}")
        with contextlib.suppress(Exception):
            self._options.notice(f"{label} at {address} joined this machine's hub")

    def _introduce(self, device_id: str) -> None:
        def run() -> None:
            try:
                report = introduce_member(self._home, device_id, self._options.member_request or send_request)
            except (OSError, ValueError) as error:
                self.log(f"introducing {device_id[:15]} to the other members failed: {type(error).__name__}: {error}")
                return
            self.log(f"introduced {device_id[:15]} to the other members; {'; '.join(report.lines()) or 'there was no other member'}")

        threading.Thread(target=run, daemon=True).start()

    def _joined(self, result: pairing.PairResult) -> None:
        if result.outcome not in ("paired", "incomplete", "probed"):
            self.log(f"join refused: {result.reason}")
        if result.outcome in ("paired", "incomplete", "failed") and result.peer is not None:
            if self._options.updater is None:
                self._options.updater = selfupdate.JoinUpdater(
                    self._home, self._options.plugin_version, self.log, post_to_sessions, self._options.server_port
                )
            self._options.updater.on_join(result.peer.device_id, result.peer.plugin_version)

    def _serve(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                conn, address = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    return
                time.sleep(0.05)
                continue
            self._admit(conn, str(address[0]))

    def _admit(self, conn: socket.socket, host: str) -> None:
        assert self._trust is not None and self._watch is not None
        source = _normalize(host)
        trust = self._trust.current()
        if source is None or trust.context is None or not self._watch.allowed() or not self._gate.allowed:
            conn.close()
            return
        if source not in trust.addresses and not (self._mode == "tailscale" and is_tailscale_address(source)):
            conn.close()
            return
        with self._lock:
            if self._active >= MAX_CONNECTIONS or self._per_source.get(source, 0) >= PER_SOURCE:
                conn.close()
                return
            self._active += 1
            self._per_source[source] = self._per_source.get(source, 0) + 1
        threading.Thread(target=self._connection, args=(conn, source, trust.context), daemon=True).start()

    def _handshake(self, conn: socket.socket, context: ssl.SSLContext) -> ssl.SSLSocket:
        tls_sock = context.wrap_socket(conn, server_side=True, do_handshake_on_connect=False)
        try:
            tls_sock.setblocking(False)
            deadline = time.monotonic() + HANDSHAKE_S
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise socket.timeout("the TLS handshake took too long")
                try:
                    tls_sock.do_handshake()
                    return tls_sock
                except ssl.SSLWantReadError:
                    select.select([tls_sock], [], [], remaining)
                except ssl.SSLWantWriteError:
                    select.select([], [tls_sock], [], remaining)
        except BaseException:
            tls_sock.close()
            raise

    def _authenticate(self, tls_sock: ssl.SSLSocket) -> str:
        assert self._trust is not None
        tls.check_not_reused(tls_sock)
        peer = format_digest(tls.peer_digest(tls_sock))
        if peer not in self._trust.current().peers:
            raise _Refused("the peer is not trusted")
        return peer

    def _answer(self, peer: str, raw: object) -> dict[str, Any] | None:
        assert self._forwarder is not None
        try:
            request = wire.parse_request(raw)
        except wire.WireError:
            return None
        reply = self._forwarder.forward(peer, raw)
        if reply is not None:
            try:
                parsed = wire.parse_reply(reply)
            except wire.WireError:
                parsed = None
            if parsed is not None and parsed.re == request.id:
                return reply
        return wire.refused_reply(request.id)

    def _connection(self, conn: socket.socket, source: str, context: ssl.SSLContext) -> None:
        tls_sock: ssl.SSLSocket | None = None
        try:
            if self._mode == "tailscale":
                try:
                    self._check_owner(source)
                except TailscaleError as error:
                    raise _OwnerRefused(str(error)) from error
            tls_sock = self._handshake(conn, context)
            peer = self._authenticate(tls_sock)
            sock = as_socket(DeadlineSocket(tls_sock, time.monotonic() + REQUEST_TOTAL_S, IDLE_S))
            wire.check_hello(wire.parse_json(wire.read_frame(sock)))
            wire.write_frame(sock, wire.hello_frame())
            try:
                frame = wire.read_frame(sock)
            except wire.ConnectionClosed:
                return
            reply = self._answer(peer, wire.parse_json(frame))
            if reply is not None:
                wire.write_frame(sock, wire.encode(reply))
        except (OSError, wire.WireError, _Refused) as error:
            self._note_failure(source, error)
        finally:
            for closing in (tls_sock, conn):
                if closing is not None:
                    with contextlib.suppress(OSError):
                        closing.close()
            with self._lock:
                self._active -= 1
                left = self._per_source.get(source, 1) - 1
                if left > 0:
                    self._per_source[source] = left
                else:
                    self._per_source.pop(source, None)

    def _note_failure(self, source: str, error: BaseException) -> None:
        now = time.monotonic()
        with self._lock:
            last = self._failures.get(source)
            if last is not None and now - last < FAILURE_LOG_INTERVAL_S:
                return
            if len(self._failures) >= FAILURE_LOG_ENTRIES:
                self._failures.clear()
            self._failures[source] = now
        detail = f" by the owner check: {error}" if isinstance(error, _OwnerRefused) else f": {type(error).__name__}"
        self.log(f"connection from {source} refused{detail}")


def run_listener(home: str, options: Options | None = None) -> int:
    return Listener(home, options or Options()).run()


def main(argv: Sequence[str]) -> int:
    import signal

    server_port = parse_port(os.environ.get(PORT_OPTION_ENV)) or SERVER_DEFAULT_PORT
    if len(argv) == 2 and argv[0] == "--server-port":
        server_port = parse_port(argv[1]) or server_port
    options = Options(server_port=server_port, beacons=BeaconSettings(), migration=Migrator())
    if sys.platform != "win32":
        signal.signal(signal.SIGTERM, lambda _signal, _frame: options.stop.set())
    return run_listener(agent_tabs_home(), options)

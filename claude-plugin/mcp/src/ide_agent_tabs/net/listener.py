from __future__ import annotations

import contextlib
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
from dataclasses import dataclass, field
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
from ..version import PACKAGE_VERSION
from . import ingress, pairing, tls, wire
from .client import (
    IDLE_S,
    DeadlineSocket,
    NetClientError,
    as_socket,
    load_identity,
    peers_dir,
    read_peer_pem,
)
from .hubjoin import HubJoiner, local_pair_identity
from .identity import format_digest
from .netid import GateState, current_network_id, gate_state
from .notify import notify, powershell_path
from .policy import NetworkConfig, PolicyError, load_network_config, load_policy, policy_path
from .transport import LanTransport, network_enabled

MAX_CONNECTIONS = 8
PER_SOURCE = 2
HANDSHAKE_S = 5.0
REQUEST_TOTAL_S = 60.0
INGRESS_TIMEOUT_MS = 25_000
SERVER_START_WAIT_MS = 3_000
SERVER_START_INTERVAL_S = 10.0
PROFILE_REFRESH_S = 30.0
PROFILE_TIMEOUT_S = 15
MONITOR_S = 1.0
CONFIG_RECHECK_S = 5.0
FAILURE_LOG_INTERVAL_S = 60.0
FAILURE_LOG_ENTRIES = 256
LOG_LIMIT = 1_000_000
LISTEN_BACKLOG = 16
JOIN_RETRY_S = 30.0
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
    return ListenerInfo(pid, started, bind, port)


def live_listener(home: str) -> ListenerInfo | None:
    info = read_listener(home)
    return info if info is not None and pid_alive(info.pid) else None


def request_stop(home: str) -> None:
    write_atomically(str(stop_file(home)), "stop\n")


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
    ) -> None:
        self._bind = bind
        self._check = check
        self._clock = clock
        self._addresses = addresses
        self._known = addresses()
        self._checked_at = clock()
        self._ok = True
        self.changed = False
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

    def tick(self) -> str | None:
        if not _address_present(self._bind):
            self._set(False)
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


@dataclass
class Options:
    config: NetworkConfig | None = None
    bind_check: Callable[[str], bool] = LanTransport().allows_bind
    network_check: Callable[[str], bool | None] = windows_network_is_public
    server_port: int = SERVER_DEFAULT_PORT
    stop: threading.Event = field(default_factory=threading.Event)
    ready: Callable[[int], None] | None = None
    forwarder: IngressForwarder | None = None
    network_id: Callable[[str], str | None] = current_network_id
    join_port: int = pairing.PAIRING_PORT
    join_timeouts: pairing.Timeouts = pairing.DEFAULT_TIMEOUTS
    join_source_ok: Callable[[str], bool] = pairing.private_source
    notice: Callable[[str], object] = notify


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
        self._network_read_at = float("-inf")
        self._join_sock: socket.socket | None = None
        self._join_thread: threading.Thread | None = None
        self._join_stop = threading.Event()
        self._join_retry_at = 0.0

    def log(self, text: str) -> None:
        append_log(log_file(self._home), text)

    def _refuse(self, reason: str) -> int:
        self.log(f"not listening: {reason}")
        return 1

    def _config(self) -> NetworkConfig:
        return self._options.config if self._options.config is not None else load_network_config(self._home)[0]

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

    def run(self) -> int:
        config = self._config()
        if not network_enabled(config) or config.bind is None:
            return self._refuse("network is not lan or no bind address is set")
        bind = config.bind
        if not self._options.bind_check(bind):
            return self._refuse("the bind address is not a private IPv4 or IPv6 unique local address")
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
        watch = NetworkWatch(bind, self._options.network_check)
        reason = watch.initial()
        if reason is not None:
            return self._refuse(reason)
        try:
            sock = self._open_socket(bind, config.port)
        except OSError as error:
            return self._refuse(f"cannot bind {bind} port {config.port}: {error}")
        self._sock, self._bind, self._watch = sock, bind, watch
        self._live = config
        self._refresh_gate(force=True)
        self._trust = TrustCache(self._home, identity.cert, identity.key, identity.passphrase, self.log)
        self._forwarder = self._options.forwarder or IngressForwarder(self._home, self._options.server_port, self.log)
        port = sock.getsockname()[1]
        with contextlib.suppress(OSError):
            remove_file(str(stop_file(self._home)))
        write_atomically(
            str(listener_file(self._home)), stringify({"pid": os.getpid(), "startedMs": now_ms(), "bind": bind, "port": port}, 2) + "\n"
        )
        self.log(f"listening on {bind} port {port} (pid {os.getpid()})")
        if self._options.ready is not None:
            self._options.ready(port)
        monitor = threading.Thread(target=self._monitor, args=(config,), daemon=True)
        monitor.start()
        try:
            self._serve()
        finally:
            self._stop.set()
            self._stop_joins("the listener stopped")
            sock.close()
            info = read_listener(self._home)
            if info is not None and info.pid == os.getpid():
                with contextlib.suppress(OSError):
                    remove_file(str(listener_file(self._home)))
            self.log("stopped")
        return 0

    def _halt(self, reason: str) -> None:
        self.log(f"stopping: {reason}")
        self._stop.set()

    def _monitor(self, started: NetworkConfig) -> None:
        assert self._watch is not None
        config_checked = time.monotonic()
        while not self._stop.wait(MONITOR_S):
            if os.path.exists(stop_file(self._home)):
                with contextlib.suppress(OSError):
                    remove_file(str(stop_file(self._home)))
                self._halt("stop requested")
                return
            reason = self._watch.tick()
            if reason is not None:
                self._halt(reason)
                return
            if self._options.config is None and time.monotonic() - config_checked >= CONFIG_RECHECK_S:
                config_checked = time.monotonic()
                current = self._config()
                if not network_enabled(current) or current.bind != started.bind or current.port != started.port:
                    self._halt("the network setting changed")
                    return
                self._live = current
            self._refresh_gate(force=self._watch.changed)
            self._sync_joins()

    def _refresh_gate(self, force: bool = False) -> None:
        now = time.monotonic()
        if force or now - self._network_read_at >= PROFILE_REFRESH_S:
            self._network_read_at = now
            try:
                self._network_id = self._options.network_id(self._bind)
            except (OSError, ValueError):
                self._network_id = None
        state = gate_state(self._live.allowed_networks, self._network_id)
        if (state.allowed, state.reason) != (self._gate.allowed, self._gate.reason):
            self.log("this network is allowed" if state.allowed else f"network gate closed: {state.reason}")
        self._gate = state

    def join_port(self) -> int | None:
        sock = self._join_sock
        return None if sock is None else int(sock.getsockname()[1])

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
        handler = HubJoiner(self._home, self._saved)
        thread = threading.Thread(
            target=pairing.serve_joins,
            args=(sock, identity, handler, pairing.Limits(self._home), stop),
            kwargs={
                "timeouts": self._options.join_timeouts,
                "on_result": self._joined,
                "allow_source": self._options.join_source_ok,
            },
            daemon=True,
        )
        self._join_sock, self._join_stop, self._join_thread = sock, stop, thread
        thread.start()
        self.log(f"serving joins on {self._bind} port {sock.getsockname()[1]}")

    def _stop_joins(self, reason: str) -> None:
        sock, thread = self._join_sock, self._join_thread
        if sock is None:
            return
        self._join_stop.set()
        if thread is not None:
            thread.join(2.0)
        sock.close()
        self._join_sock, self._join_thread = None, None
        self.log(f"stopped serving joins: {reason}")

    def _saved(self, label: str, address: str) -> None:
        self.log(f"a device joined the hub: {label} at {address}")
        with contextlib.suppress(Exception):
            self._options.notice(f"{label} at {address} joined this machine's hub")

    def _joined(self, result: pairing.PairResult) -> None:
        if result.outcome not in ("paired", "incomplete"):
            self.log(f"join refused: {result.reason}")

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
        if source is None or trust.context is None or source not in trust.addresses or not self._watch.allowed() or not self._gate.allowed:
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
            tls_sock = self._handshake(conn, context)
            peer = self._authenticate(tls_sock)
            sock = as_socket(DeadlineSocket(tls_sock, time.monotonic() + REQUEST_TOTAL_S, IDLE_S))
            wire.check_hello(wire.parse_json(wire.read_frame(sock)))
            wire.write_frame(sock, wire.hello_frame())
            reply = self._answer(peer, wire.parse_json(wire.read_frame(sock)))
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
        self.log(f"connection from {source} refused: {type(error).__name__}")


def run_listener(home: str, options: Options | None = None) -> int:
    return Listener(home, options or Options()).run()


def main(argv: Sequence[str]) -> int:
    import signal

    server_port = parse_port(os.environ.get(PORT_OPTION_ENV)) or SERVER_DEFAULT_PORT
    if len(argv) == 2 and argv[0] == "--server-port":
        server_port = parse_port(argv[1]) or server_port
    options = Options(server_port=server_port)
    if sys.platform != "win32":
        signal.signal(signal.SIGTERM, lambda _signal, _frame: options.stop.set())
    return run_listener(agent_tabs_home(), options)

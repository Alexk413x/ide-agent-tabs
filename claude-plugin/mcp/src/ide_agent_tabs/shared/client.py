from __future__ import annotations

import functools
import os
import socket
import sys
import time
from collections.abc import Mapping, Sequence
from typing import Any, NamedTuple

from ..jsjson import js_ordered, parse, stringify
from ..version import compare_versions
from .state import SERVICE, ServerState, read_state, read_token

HEALTH_TIMEOUT_MS = 1_000
POLL_S = 0.05
STOP_WAIT_MS = 5_000
START_WAIT_MS = 3_000
_SIO_TCP_INITIAL_RTO = 0x98000011
_RTO_UNSPECIFIED_RTT = 0xFFFF
_RTO_NO_SYN_RETRANSMISSIONS = 0xFE


class Reply(NamedTuple):
    status: int
    body: str


class Probe(NamedTuple):
    kind: str
    health: dict[str, Any] | None = None


@functools.cache
def _wsa_ioctl() -> Any:
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    ws2 = ctypes.WinDLL("ws2_32", use_last_error=True)
    ws2.WSAIoctl.argtypes = (
        ctypes.c_size_t,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
        ctypes.c_void_p,
    )
    ws2.WSAIoctl.restype = ctypes.c_int
    return ws2.WSAIoctl


# Windows retries a refused SYN for about two seconds, so a probe of a free port would time out and read as
# "another program" instead of "free"; libuv turns the retries off for loopback the same way.
def _no_syn_retries(sock: socket.socket) -> None:
    if sys.platform != "win32":
        return
    import ctypes
    from ctypes import wintypes

    class Rto(ctypes.Structure):
        _fields_ = (("Rtt", ctypes.c_ushort), ("MaxSynRetransmissions", ctypes.c_ubyte))

    params = Rto(_RTO_UNSPECIFIED_RTT, _RTO_NO_SYN_RETRANSMISSIONS)
    returned = wintypes.DWORD()
    _wsa_ioctl()(
        sock.fileno(), _SIO_TCP_INITIAL_RTO, ctypes.byref(params), ctypes.sizeof(params), None, 0, ctypes.byref(returned), None, None
    )


def connect_loopback(port: int, timeout_s: float) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        _no_syn_retries(sock)
        sock.settimeout(timeout_s)
        sock.connect(("127.0.0.1", port))
    except BaseException:
        sock.close()
        raise
    return sock


def _read_reply(sock: socket.socket) -> Reply:
    data = b""
    head_end = -1
    length: int | None = None
    while True:
        if head_end == -1:
            head_end = data.find(b"\r\n\r\n")
            if head_end != -1:
                for line in data[:head_end].split(b"\r\n")[1:]:
                    name, _, value = line.partition(b":")
                    if name.strip().lower() == b"content-length" and value.strip().isdigit():
                        length = int(value.strip())
        if head_end != -1 and length is not None and len(data) >= head_end + 4 + length:
            break
        chunk = sock.recv(65536)
        if not chunk:
            break
        data += chunk
    status_line = data.split(b"\r\n", 1)[0].split(b" ")
    if head_end == -1 or len(status_line) < 2 or not status_line[0].startswith(b"HTTP/") or not status_line[1].isdigit():
        raise OSError("not an HTTP reply")
    body = data[head_end + 4 :]
    return Reply(int(status_line[1]), (body if length is None else body[:length]).decode("utf-8", "replace"))


# http.client pulls in the email package and ssl, which doubles the headers helper's start-up; these requests
# go only to loopback servers that answer with a Content-Length.
def request(
    port: int, method: str, route: str, token: str | None = None, timeout_ms: float = HEALTH_TIMEOUT_MS, payload: str = ""
) -> Reply | None:
    timeout_s = timeout_ms / 1000
    body = payload.encode("utf-8")
    lines = [f"{method} {route} HTTP/1.1", f"Host: 127.0.0.1:{port}", f"Content-Length: {len(body)}", "Connection: close"]
    if token:
        lines.append(f"Authorization: Bearer {token}")
    if payload != "":
        lines.append("Content-Type: application/json")
    try:
        sock = connect_loopback(port, timeout_s)
    except ConnectionRefusedError:
        return None
    except OSError:
        return Reply(0, "")
    try:
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body)
        return _read_reply(sock)
    except OSError:
        return Reply(0, "")
    finally:
        sock.close()


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def probe(port: int) -> Probe:
    reply = request(port, "GET", "/health")
    if reply is None:
        return Probe("free")
    try:
        health = parse(reply.body)
    except ValueError:
        return Probe("other")
    if (
        reply.status == 200
        and isinstance(health, dict)
        and health.get("service") == SERVICE
        and _is_number(health.get("pid"))
        and isinstance(health.get("version"), str)
    ):
        return Probe("ours", js_ordered(health))
    return Probe("other")


def verified_state(home: str, port: int, health: dict[str, Any]) -> ServerState | None:
    state = read_state(home, port)
    if state is None or state.pid != health.get("pid") or health.get("port") != port or isinstance(health.get("port"), bool):
        return None
    return state


def stop_server(home: str, port: int, wait_ms: float = STOP_WAIT_MS) -> dict[str, Any]:
    found = probe(port)
    if found.kind == "free":
        return {"stopped": False, "problem": f"nothing listens on port {port}"}
    if found.kind == "other" or found.health is None:
        return {"stopped": False, "problem": f"port {port} belongs to another program"}
    state = verified_state(home, port, found.health)
    if state is None:
        return {"stopped": False, "pid": found.health.get("pid"), "problem": f"the server on port {port} matches no state file in {home}"}
    reply = request(port, "POST", "/shutdown", state.shutdown_token)
    if reply is None or reply.status != 200:
        status = "none" if reply is None else str(reply.status)
        return {"stopped": False, "pid": state.pid, "problem": f"the server refused to stop (status {status})"}
    deadline = time.monotonic() + wait_ms / 1000
    while time.monotonic() < deadline:
        if probe(port).kind == "free":
            return {"stopped": True, "pid": state.pid}
        time.sleep(POLL_S)
    return {"stopped": False, "pid": state.pid, "problem": f"the server still listens on port {port} after {wait_ms / 1000:g} s"}


def verified_token(home: str, port: int, health: dict[str, Any]) -> str | None:
    return None if verified_state(home, port, health) is None else read_token(home)


def server_command(launcher: str, port: int, python: str | None = None) -> list[str]:
    return [python or sys.executable, "-I", "-S", launcher, "--port", str(port)]


def start_server(command: Sequence[str], home: str, env: Mapping[str, str] | None = None) -> int | None:
    import subprocess

    from ..processes import CREATE_BREAKAWAY_FROM_JOB, detached_flags
    from ..terminals.processes import terminal_environment

    child_env = terminal_environment(os.environ if env is None else env)
    child_env["IDE_AGENT_TABS_HOME"] = home
    # The helper runs in the plugin's versioned folder; a server left in it would keep Windows from removing
    # that folder when the plugin updates.
    cwd = os.path.expanduser("~")
    for flags in (detached_flags(), detached_flags() & ~CREATE_BREAKAWAY_FROM_JOB):
        try:
            return subprocess.Popen(
                list(command),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=child_env,
                cwd=cwd,
                creationflags=flags,
                start_new_session=sys.platform != "win32",
            ).pid
        except PermissionError:
            # A job object that forbids breakaway refuses CREATE_BREAKAWAY_FROM_JOB with access denied.
            if sys.platform != "win32":
                return None
        except OSError:
            return None
    return None


class Ensured(NamedTuple):
    started: bool
    health: dict[str, Any] | None = None
    token: str | None = None
    problem: str | None = None


# Claude Code caches a 403 as "needs auth" and stops connecting, so no caller sends a request without the
# token: each one waits for a server it can verify, starting one when nothing listens.
def ensure_server(
    command: Sequence[str],
    port: int,
    home: str,
    version: str,
    wait_ms: float = START_WAIT_MS,
    env: Mapping[str, str] | None = None,
) -> Ensured:
    deadline = time.monotonic() + wait_ms / 1000
    started = False
    while True:
        found = probe(port)
        other = found.kind == "other" or found.health is None and found.kind == "ours"
        # A server handing the port to the one this caller started can reset a probe; only a holder seen before
        # any start counts as another program.
        if other and not started:
            return Ensured(started, problem=f"port {port} belongs to another program; set the Agent Tabs server_port option to a free port")
        health = None if other else found.health
        stale = health is not None and compare_versions(str(health.get("version")), version) < 0
        if health is not None and not stale:
            token = verified_token(home, port, health)
            if token is not None:
                return Ensured(started, health, token)
        if not started and (found.kind == "free" or stale):
            start_server(command, home, env)
            started = True
        if time.monotonic() >= deadline:
            if health is not None:
                token = verified_token(home, port, health)
                if token is not None:
                    return Ensured(started, health, token)
                return Ensured(
                    started,
                    health,
                    problem=f"the Agent Tabs server on port {port} (pid {health.get('pid')}) matches no state file in {home}",
                )
            return Ensured(started, problem=f"the Agent Tabs server did not start on port {port} within {wait_ms / 1000:g} s")
        time.sleep(POLL_S)


def ask_to_stop(port: int, shutdown_token: str) -> bool:
    reply = request(port, "POST", "/shutdown", shutdown_token)
    return reply is not None and reply.status == 200


def notify_end(home: str, port: int, pid: int) -> bool:
    found = probe(port)
    if found.kind != "ours" or found.health is None:
        return False
    token = verified_token(home, port, found.health)
    if token is None:
        return False
    reply = request(port, "POST", "/end", token, HEALTH_TIMEOUT_MS, stringify({"pid": pid}))
    return reply is not None and reply.status == 200

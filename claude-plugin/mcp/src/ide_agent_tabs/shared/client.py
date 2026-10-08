from __future__ import annotations

import functools
import http.client
import socket
import sys
import time
from typing import Any, NamedTuple

from ..jsjson import js_ordered, parse
from .state import SERVICE, ServerState, read_state

HEALTH_TIMEOUT_MS = 1_000
POLL_S = 0.05
STOP_WAIT_MS = 5_000
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
    assert sys.platform == "win32"
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


def request(
    port: int, method: str, route: str, token: str | None = None, timeout_ms: float = HEALTH_TIMEOUT_MS, payload: str = ""
) -> Reply | None:
    timeout_s = timeout_ms / 1000
    body = payload.encode("utf-8")
    headers = {"Host": f"127.0.0.1:{port}", "Content-Length": str(len(body))}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if payload != "":
        headers["Content-Type"] = "application/json"
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout_s)
    try:
        conn.sock = connect_loopback(port, timeout_s)
        conn.request(method, route, body=body, headers=headers)
        response = conn.getresponse()
        return Reply(response.status, response.read().decode("utf-8", "replace"))
    except ConnectionRefusedError:
        return None
    except (OSError, http.client.HTTPException):
        return Reply(0, "")
    finally:
        conn.close()


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

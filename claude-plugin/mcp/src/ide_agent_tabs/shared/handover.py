from __future__ import annotations

import socket
import time
from typing import Callable

from ..processes import pid_alive
from ..version import compare_versions
from .client import ask_to_stop, probe, verified_state
from .front import bind
from .state import SERVICE, read_state

HANDOVER_MS = 5_000
RETRY_S = 0.1


# A newer build takes the port from an older one; the stop request goes only to a holder whose pid and port
# match this user's state file, with that file's own token, so a program squatting on the port gets no credential.
def claim_port(
    port: int,
    home: str,
    version: str,
    log: Callable[[str], None],
    wait_ms: float = HANDOVER_MS,
    listen: Callable[[int], socket.socket] = bind,
) -> socket.socket | None:
    try:
        return listen(port)
    except OSError:
        pass
    found = probe(port)
    if found.kind != "ours" or found.health is None:
        log(f"port {port} belongs to another program" if found.kind == "other" else f"port {port} is in use")
        return None
    theirs = str(found.health.get("version"))
    if compare_versions(version, theirs) <= 0:
        return None
    state = verified_state(home, port, found.health)
    if state is None:
        log(f"port {port} answers as {SERVICE} {theirs} but matches no state file; leaving it")
        return None
    log(f"asking {SERVICE} {theirs} (pid {state.pid:g}) to hand over port {port}")
    ask_to_stop(port, state.shutdown_token)
    deadline = time.monotonic() + wait_ms / 1000
    while time.monotonic() < deadline:
        try:
            sock = listen(port)
        except OSError:
            time.sleep(RETRY_S)
            continue
        _await_state_release(home, port, int(state.pid), deadline)
        return sock
    log(f"{SERVICE} {theirs} did not hand over port {port}")
    return None


# The old server closes its port first and removes its state file after; it removes the file only while the
# file names it, but reads and removes in two steps, so a state written in between would be deleted.
def _await_state_release(home: str, port: int, old_pid: int, deadline: float) -> None:
    while time.monotonic() < deadline:
        current = read_state(home, port)
        if current is None or current.pid != old_pid or not pid_alive(old_pid):
            return
        time.sleep(RETRY_S / 4)

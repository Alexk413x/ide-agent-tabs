from __future__ import annotations

import contextlib
import os
import signal
import threading
import time
from collections.abc import Mapping, Sequence
from typing import Any, Callable

from ..clock import now_iso
from ..home import agent_tabs_home
from ..processes import pid_alive
from ..version import PACKAGE_VERSION
from .front import Front, FrontDeps
from .handover import HANDOVER_MS, claim_port
from .host import ToolHost
from .hub import Hub
from .state import (
    DEFAULT_PORT,
    PORT_OPTION_ENV,
    SERVICE,
    ServerState,
    ensure_token,
    new_token,
    parse_port,
    read_state,
    remove_state,
    server_dir,
    write_state,
)

IDLE_EXIT_MS = 8 * 60 * 60 * 1000
IDLE_CHECK_S = 60.0
STATE_CHECK_S = 2.0
LOG_LIMIT_BYTES = 1024 * 1024


def port_of(args: Sequence[str], env: Mapping[str, str]) -> int:
    at = list(args).index("--port") if "--port" in args else -1
    given = parse_port(args[at + 1]) if 0 <= at < len(args) - 1 else None
    return given or parse_port(env.get(PORT_OPTION_ENV)) or DEFAULT_PORT


class Logger:
    def __init__(self, file: str) -> None:
        self.file = file
        self._lock = threading.Lock()

    def __call__(self, message: str) -> None:
        line = f"{now_iso()} {os.getpid()} {message}\n"
        with self._lock, contextlib.suppress(OSError):
            if os.path.exists(self.file) and os.path.getsize(self.file) > LOG_LIMIT_BYTES:
                with open(self.file, "w", encoding="utf-8"):
                    pass
            fd = os.open(self.file, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0), 0o600)
            with os.fdopen(fd, "ab") as f:
                f.write(line.encode("utf-8", "replace"))


class SharedServer:
    def __init__(
        self,
        home: str,
        port: int,
        host: ToolHost,
        version: str = PACKAGE_VERSION,
        log: Callable[[str], None] | None = None,
        now: Callable[[], float] = time.monotonic,
        alive: Callable[[int], bool] | None = None,
    ) -> None:
        self.home = home
        self.port = port
        self.host = host
        self.version = version
        self.log = log or Logger(os.path.join(server_dir(home), f"server-{port}.log"))
        self.now = now
        self.pid = os.getpid()
        self.started_at = now_iso()
        self.last_request = now()
        self.token = ensure_token(home)
        self.shutdown_token = new_token()
        self.hub = Hub(host, alive=alive or pid_alive, log=self.log, prepare=shared_store_mode)
        self.front: Front | None = None
        self.stopped = threading.Event()
        self._stopping = threading.Lock()
        self._stop_started = False
        self._serving = False
        self._timers = threading.Event()

    def health(self) -> dict[str, Any]:
        return {
            "service": SERVICE,
            "version": self.version,
            "pid": self.pid,
            "port": self.port,
            "sessions": self.hub.size,
            "startedAt": self.started_at,
        }

    def touch(self) -> None:
        self.last_request = self.now()

    def deps(self) -> FrontDeps:
        return FrontDeps(
            port=self.port,
            token=self.token,
            shutdown_token=self.shutdown_token,
            server_info={"name": SERVICE, "version": self.version},
            health=self.health,
            tools=self.host.tools_for,
            instructions=self.host.instructions_for,
            call=self.hub.call,
            end=self.hub.end_pid,
            on_request=self.touch,
            on_shutdown=lambda: self.stop_soon("asked to stop"),
        )

    def claim(self, wait_ms: float = HANDOVER_MS) -> bool:
        sock = claim_port(self.port, self.home, self.version, self.log, wait_ms)
        if sock is None:
            return False
        self.front = Front(sock, self.deps())
        write_state(self.home, ServerState(self.pid, self.port, self.version, self.started_at, self.shutdown_token))
        self.log(f"{SERVICE} {self.version} serving http://127.0.0.1:{self.port}/mcp for {self.home}")
        return True

    def idle_expired(self) -> bool:
        return (self.now() - self.last_request) * 1000 >= IDLE_EXIT_MS and self.hub.live_sessions() == 0

    def check_idle(self) -> bool:
        if self.idle_expired():
            self.stop_soon("idle for 8 hours")
            return True
        return False

    def check_state(self) -> bool:
        state = read_state(self.home, self.port)
        if state is None or state.pid != self.pid:
            self.stop_soon("its state file is gone" if state is None else f"its state file names pid {state.pid:g}")
            return True
        return False

    def start_timers(self, idle_s: float = IDLE_CHECK_S, state_s: float = STATE_CHECK_S) -> None:
        self.hub.start_liveness()

        def every(seconds: float, check: Callable[[], bool], name: str) -> None:
            def loop() -> None:
                while not self._timers.wait(seconds):
                    with contextlib.suppress(Exception):
                        if check():
                            return

            threading.Thread(target=loop, name=name, daemon=True).start()

        every(idle_s, self.check_idle, "idle-exit")
        every(state_s, self.check_state, "state-check")

    def stop_soon(self, why: str) -> None:
        threading.Thread(target=self.stop, args=(why,), name="stop", daemon=True).start()

    def stop(self, why: str) -> None:
        with self._stopping:
            if self._stop_started:
                return
            self._stop_started = True
            serving = self._serving
        self.log(f"stopping: {why}")
        self._timers.set()
        if self.front is not None:
            # socketserver's shutdown() waits for serve_forever() to return, so it would hang before serve() runs.
            if serving:
                with contextlib.suppress(Exception):
                    self.front.shutdown()
            with contextlib.suppress(Exception):
                self.front.server_close()
        with contextlib.suppress(Exception):
            self.hub.close()
        with contextlib.suppress(Exception):
            self.host.close()
        with contextlib.suppress(Exception):
            remove_state(self.home, self.port, self.pid)
        self.stopped.set()

    def serve(self) -> None:
        with self._stopping:
            if self.front is None or self._stop_started:
                return
            self._serving = True
        try:
            self.front.serve_forever(poll_interval=0.5)
        finally:
            self.stopped.wait(HANDOVER_MS / 1000)


# One process writes for every Claude Code session, so a busy store returns at once and retries off the lock,
# and a send to a session waiting in this process wakes it directly instead of through a wake file.
def shared_store_mode() -> None:
    from ..messaging.db import SHARED_BUSY_TIMEOUT_MS, set_busy_timeout
    from ..messaging.wake import skip_wake_files_for_local_waiters

    set_busy_timeout(SHARED_BUSY_TIMEOUT_MS)
    skip_wake_files_for_local_waiters(True)


def load_host(home: str, log: Callable[[str], None]) -> ToolHost:
    from .engine import load_host as load

    return load(home, log)


def main(
    args: Sequence[str],
    env: Mapping[str, str] | None = None,
    version: str = PACKAGE_VERSION,
    load: Callable[[str, Callable[[str], None]], ToolHost] = load_host,
) -> int:
    env = os.environ if env is None else env
    home = agent_tabs_home(env)
    port = port_of(args, env)
    log = Logger(os.path.join(server_dir(home), f"server-{port}.log"))
    try:
        server = SharedServer(home, port, load(home, log), version, log)
        if not server.claim():
            return 0
    except Exception:  # noqa: BLE001 - a server that can't start logs why and exits; the helper reports the rest
        import traceback

        log(f"start failed: {traceback.format_exc()}")
        return 1
    server.start_timers()
    for name in ("SIGINT", "SIGTERM", "SIGHUP", "SIGBREAK"):
        number = getattr(signal, name, None)
        if number is not None:
            with contextlib.suppress(OSError, ValueError):
                signal.signal(number, lambda _n, _f, name=name: server.stop_soon(name))
    server.serve()
    return 0

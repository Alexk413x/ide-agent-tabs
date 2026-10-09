from __future__ import annotations

import contextlib
import os
import sys
import threading
from collections.abc import Mapping
from typing import Any, Callable, Protocol

from .jsjson import parse, stringify
from .mcp_tools import SERVER_NAME, Call, ToolDeps, Tools
from .version import PACKAGE_VERSION

SUPPORTED_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05", "2024-10-07")
LATEST_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0]
METHOD_NOT_FOUND = -32601
INTERNAL_ERROR = -32603
END_RECORD_S = 2.0
_READ_CHUNK = 65536


_MISSING: Any = object()


def _received(value: Any) -> str:
    if value is _MISSING:
        return "undefined"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    return "array" if isinstance(value, list) else "object"


def _issue(expected: str, path: list[str], value: Any) -> dict[str, Any]:
    return {
        "expected": expected,
        "code": "invalid_type",
        "path": path,
        "message": f"Invalid input: expected {expected}, received {_received(value)}",
    }


def _call_problems(params: Any) -> list[dict[str, Any]]:
    if not isinstance(params, dict):
        return [_issue("object", ["params"], params)]
    problems: list[dict[str, Any]] = []
    if not isinstance(params.get("name"), str):
        problems.append(_issue("string", ["params", "name"], params.get("name", _MISSING)))
    if "arguments" in params and not isinstance(params["arguments"], dict):
        problems.append(_issue("record", ["params", "arguments"], params["arguments"]))
    return problems


class Stream(Protocol):
    def write(self, data: bytes, /) -> object: ...

    def flush(self) -> object: ...


class Output:
    def __init__(self, stream: Stream) -> None:
        self._stream = stream
        self._lock = threading.Lock()

    def send(self, message: Mapping[str, Any]) -> None:
        data = (stringify(message) + "\n").encode("utf-8")
        with self._lock, contextlib.suppress(OSError, ValueError):
            self._stream.write(data)
            self._stream.flush()


class StdioServer:
    def __init__(self, tools: Tools, output: Output, on_initialized: Callable[[str | None], None] | None = None) -> None:
        self.tools = tools
        self.output = output
        self.on_initialized = on_initialized
        self.client_name: str | None = None
        self._calls: dict[str, threading.Event] = {}
        self._calls_lock = threading.Lock()

    def _reply(self, request_id: Any, result: Any) -> None:
        self.output.send({"result": result, "jsonrpc": "2.0", "id": request_id})

    def _error(self, request_id: Any, code: int, message: str) -> None:
        self.output.send({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})

    def receive_line(self, line: bytes) -> None:
        text = line.decode("utf-8", "replace")
        text = text.removesuffix("\r")
        try:
            message = parse(text)
        except ValueError:
            return
        self.receive(message)

    def receive(self, message: Any) -> None:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return
        method = message.get("method")
        if not isinstance(method, str):
            return
        params = message.get("params", _MISSING)
        if "id" in message:
            request_id = message["id"]
            if isinstance(request_id, bool) or not isinstance(request_id, (str, int, float)):
                return
            self._request(request_id, method, params)
        else:
            self._notification(method, params)

    def _request(self, request_id: Any, method: str, params: Any) -> None:
        if method == "initialize":
            if not isinstance(params, dict) or not isinstance(params.get("clientInfo"), dict):
                self._error(request_id, INTERNAL_ERROR, "Invalid initialize request")
                return
            name = params["clientInfo"].get("name")
            self.client_name = name if isinstance(name, str) else None
            requested = params.get("protocolVersion")
            version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else LATEST_PROTOCOL_VERSION
            self._reply(
                request_id,
                {
                    "protocolVersion": version,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": SERVER_NAME, "version": PACKAGE_VERSION},
                    "instructions": self.tools.instructions(),
                },
            )
        elif method == "ping":
            self._reply(request_id, {})
        elif method == "tools/list":
            self._reply(request_id, {"tools": self.tools.list(self.client_name)})
        elif method == "tools/call":
            problems = _call_problems(params)
            if problems:
                self._error(request_id, INTERNAL_ERROR, stringify(problems, 2))
                return
            assert isinstance(params, dict)
            arguments = params.get("arguments")
            meta = params.get("_meta")
            self._start_call(request_id, params["name"], arguments, meta if isinstance(meta, dict) else {})
        else:
            self._error(request_id, METHOD_NOT_FOUND, "Method not found")

    def _start_call(self, request_id: Any, name: str, arguments: dict[str, Any] | None, meta: dict[str, Any]) -> None:
        key = stringify(request_id)
        cancel = threading.Event()
        with self._calls_lock:
            self._calls[key] = cancel

        def notify(method: str, params: dict[str, Any]) -> None:
            if not cancel.is_set():
                self.output.send({"method": method, "params": params, "jsonrpc": "2.0"})

        def run() -> None:
            try:
                result = self.tools.call(self.client_name, name, arguments, Call(meta, cancel, notify))
                if not cancel.is_set():
                    self._reply(request_id, result)
            finally:
                with self._calls_lock:
                    if self._calls.get(key) is cancel:
                        del self._calls[key]

        threading.Thread(target=run, name=f"tool-{name}", daemon=True).start()

    def _notification(self, method: str, params: Any) -> None:
        if method == "notifications/initialized":
            if self.on_initialized is not None:
                self.on_initialized(self.client_name)
        elif method == "notifications/cancelled" and isinstance(params, dict):
            request_id = params.get("requestId")
            if request_id is None:
                return
            with self._calls_lock:
                cancel = self._calls.get(stringify(request_id))
            if cancel is not None:
                cancel.set()

    def serve(self, fd: int = 0, after_first: Callable[[], None] | None = None) -> None:
        buffer = b""
        while True:
            try:
                chunk = os.read(fd, _READ_CHUNK)
            except OSError:
                return
            if not chunk:
                return
            buffer += chunk
            while True:
                end = buffer.find(b"\n")
                if end == -1:
                    break
                line, buffer = buffer[:end], buffer[end + 1 :]
                self.receive_line(line)
                if after_first is not None:
                    after_first()
                    after_first = None


def _log(message: str) -> None:
    with contextlib.suppress(OSError, ValueError):
        sys.stderr.write(f"ide-agent-tabs: {message}\n")
        sys.stderr.flush()


class Boot:
    def __init__(self, home: str) -> None:
        self.home = home
        self._done = threading.Event()
        self._client_done = threading.Event()
        self._client_done.set()
        self._deps: ToolDeps | None = None
        self._error: BaseException | None = None
        self._started = False
        self._start_lock = threading.Lock()
        self.messaging: Any = None

    # The first reply goes out before this starts: the service's imports would otherwise hold the GIL while
    # the client waits for initialize.
    def start(self) -> None:
        with self._start_lock:
            if self._started:
                return
            self._started = True
        threading.Thread(target=self._run, name="agent-tabs-boot", daemon=True).start()

    def _run(self) -> None:
        try:
            self._deps = self._build()
        except BaseException as e:  # noqa: BLE001
            self._error = e
        finally:
            self._done.set()
        if self._deps is not None:
            service = self._deps.service
            threading.Thread(target=lambda: _quietly(service.refresh_detection), name="agent-tabs-detect", daemon=True).start()

    def _build(self) -> ToolDeps:
        from .handoff import HandoffDeps, Handoffs
        from .jev.service import start_jev
        from .list_ides_cli import system_service
        from .messaging.messaging import Messaging, MessagingDeps
        from .resume import ResumeDeps, Resumes

        env = os.environ
        service = system_service(self.home, _log)
        jev = start_jev(self.home, env, sys.platform, lambda: service.list_agents()["agents"]).jev
        messaging = Messaging(MessagingDeps(home=self.home, env=env, pid=os.getpid(), cwd=os.getcwd(), hosts=service))
        self.messaging = messaging
        messaging.start_registered(log=_log)
        handoffs = Handoffs(
            HandoffDeps(
                home=self.home,
                env=env,
                session_id=lambda: messaging.id,
                open_tab=lambda given: service.open_tab(given),
                find_host=service.find_host,
            )
        )
        resumes = Resumes(
            ResumeDeps(
                home=self.home,
                settings=service.settings,
                open_tab=lambda given: service.open_tab(given),
                live_host=service.live_host,
                live=messaging.live,
            )
        )
        return ToolDeps(service, jev, messaging, handoffs, resumes)

    def deps(self) -> ToolDeps:
        self.start()
        self._done.wait()
        if self._deps is None:
            raise RuntimeError(f"the server did not start: {self._error}")
        self._client_done.wait()
        return self._deps

    def initialized(self, client_name: str | None) -> None:
        self._client_done.clear()

        def run() -> None:
            try:
                self._done.wait()
                if self._deps is not None:
                    _quietly(lambda: self._deps.messaging.set_client(client_name) if self._deps is not None else None)
            finally:
                self._client_done.set()

        threading.Thread(target=run, name="agent-tabs-client", daemon=True).start()

    def wait(self, timeout: float) -> None:
        if self._started:
            self._done.wait(timeout)


def _quietly(work: Callable[[], object]) -> None:
    with contextlib.suppress(Exception):
        work()


class Stopper:
    def __init__(self, boot: Boot) -> None:
        self.boot = boot
        self._lock = threading.Lock()
        self._stopping = False

    def stop(self) -> None:
        with self._lock:
            if self._stopping:
                return
            self._stopping = True
        self.boot.wait(END_RECORD_S)
        messaging = self.boot.messaging
        if messaging is not None:
            ended = threading.Thread(target=lambda: _quietly(messaging.record_end), name="agent-tabs-end", daemon=True)
            ended.start()
            ended.join(END_RECORD_S)
            _quietly(messaging.stop_sync)
        with contextlib.suppress(Exception):
            sys.stdout.flush()
        os._exit(0)


def main() -> None:
    from .home import agent_tabs_home
    from .jev.settings import read_jev_config
    from .processes import utf8_stdio

    utf8_stdio()
    home = agent_tabs_home()
    settings, _ = read_jev_config(home)
    boot = Boot(home)
    stopper = Stopper(boot)
    tools = Tools(boot.deps, settings.enabled)
    server = StdioServer(tools, Output(sys.stdout.buffer), boot.initialized)

    def after_first() -> None:
        import signal

        for name in ("SIGINT", "SIGTERM", "SIGHUP", "SIGBREAK"):
            number = getattr(signal, name, None)
            if number is not None:
                with contextlib.suppress(OSError, ValueError):
                    signal.signal(number, lambda *_: stopper.stop())
        boot.start()

    # Windows sends no SIGTERM to a child; a client ends the server by closing its stdin.
    server.serve(after_first=after_first)
    stopper.stop()

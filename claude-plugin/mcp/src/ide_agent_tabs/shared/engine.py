from __future__ import annotations

import contextlib
import os
import sys
import threading
from collections.abc import Mapping
from typing import Any, Callable

from .host import Binding, BoundSession, Progress, ToolHost

CLIENT_NAME = "claude-code"
HTTP_MAX_WAIT_S = 240
_SESSION_ENV = ("IDE_AGENT_TABS_ID", "IDE_AGENT_TABS_AGENT", "IDE_AGENT_TABS_MOD")


def _no_notify(_method: str, _params: dict[str, Any]) -> None:
    return None


class _Session:
    def __init__(self, host: Engine, binding: Binding) -> None:
        from ..handoff import HandoffDeps, Handoffs
        from ..jev.service import start_jev
        from ..mcp_tools import ToolDeps, Tools
        from ..messaging.messaging import Messaging, MessagingDeps
        from ..profiles import AGENT_ENV, TAB_ID_ENV
        from ..resume import ResumeDeps, Resumes

        env = dict(host.env)
        env[AGENT_ENV] = binding.agent
        if binding.tab is not None:
            env[TAB_ID_ENV] = binding.tab
        home = host.home
        service = host.service(env)
        jev = start_jev(home, env, sys.platform, lambda: service.list_agents()["agents"]).jev
        messaging = Messaging(
            MessagingDeps(
                home=home,
                env=env,
                pid=binding.pid,
                cwd=binding.cwd,
                hosts=service,
                pid_start=binding.pid_start,
                max_wait_s=HTTP_MAX_WAIT_S,
                random_id=lambda: binding.id,
                scheduler=host.scheduler,
            )
        )
        messaging.start_registered(log=host.log)
        handoffs = Handoffs(
            HandoffDeps(
                home=home,
                env=env,
                session_id=lambda: messaging.id,
                open_tab=lambda given: service.open_tab(given),
                find_host=service.find_host,
            )
        )
        resumes = Resumes(
            ResumeDeps(
                home=home,
                settings=service.settings,
                open_tab=lambda given: service.open_tab(given),
                live_host=service.live_host,
                live=messaging.live,
            )
        )
        deps = ToolDeps(service, jev, messaging, handoffs, resumes)
        self.messaging = messaging
        self.host = host
        self.tools = Tools(lambda: deps, host.jev_enabled())
        self._lock = threading.Lock()
        self._done = False

    @property
    def id(self) -> str:
        return self.messaging.id

    def call(self, name: str, arguments: dict[str, Any], progress: Progress | None, cancel: threading.Event) -> dict[str, Any]:
        from ..mcp_tools import Call

        self.tools.jev_enabled = self.host.jev_enabled()
        return self.tools.call(CLIENT_NAME, name, arguments, Call({}, cancel, _no_notify))

    def _finish(self) -> bool:
        with self._lock:
            if self._done:
                return False
            self._done = True
            return True

    def end(self) -> None:
        if not self._finish():
            return
        with contextlib.suppress(Exception):
            self.messaging.record_end()
        self.messaging.stop_sync()

    def release(self) -> None:
        if not self._finish():
            return
        self.messaging.stop_heartbeat()
        self.messaging.stop_follow_ups()


# One tool layer for every Claude Code session on the machine: each bound session gets its own Messaging, service
# and Jev, as a stdio server has, and they share one scheduler thread.
class Engine:
    def __init__(self, home: str, log: Callable[[str], None], env: Mapping[str, str] | None = None, detect: bool = True) -> None:
        from ..scheduler import Scheduler

        self.home = home
        self.log = log
        self.env = {k: v for k, v in (os.environ if env is None else env).items() if k.upper() not in _SESSION_ENV}
        self.scheduler = Scheduler("agent-tabs-shared-scheduler")
        if detect:
            threading.Thread(target=self._detect, name="agent-tabs-detect", daemon=True).start()

    def service(self, env: Mapping[str, str]) -> Any:
        from ..ide_client import ide_caller
        from ..ide_installs import node_platform
        from ..list_ides_cli import SessionPresence, scripts_dir
        from ..service import Service, ServiceDeps
        from ..terminals import TERMINAL_DRIVERS

        deps = ServiceDeps(
            home=self.home,
            scripts_dir=scripts_dir(),
            platform=node_platform(),
            env=env,
            call_ide=ide_caller(),
            drivers=TERMINAL_DRIVERS,
            log=self.log,
            presence=SessionPresence(),
        )
        return Service(deps)

    def _detect(self) -> None:
        with contextlib.suppress(Exception):
            self.service(self.env).refresh_detection()

    def jev_enabled(self) -> bool:
        from ..jev.settings import read_jev_config

        try:
            return bool(read_jev_config(self.home)[0].enabled)
        except Exception:  # noqa: BLE001 - an unreadable config leaves Jev off, as the stdio server reads it
            return False

    def tools_for(self, agent: str) -> list[dict[str, Any]]:
        from ..mcp_tools import tools_for

        return tools_for(CLIENT_NAME, self.jev_enabled())

    def instructions_for(self, agent: str) -> str:
        from ..mcp_tools import server_instructions

        return server_instructions(self.jev_enabled())

    def bind(self, binding: Binding) -> BoundSession:
        return _Session(self, binding)

    def close(self) -> None:
        self.scheduler.stop()


def load_host(home: str, log: Callable[[str], None]) -> ToolHost:
    return Engine(home, log)

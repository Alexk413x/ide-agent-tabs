from __future__ import annotations

import contextlib
import os
import threading
import time
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Callable, NamedTuple, Protocol, TypeVar

from .clock import now_ms, parse_iso
from .detection import DRIVER_ERRORS, Detection, available_drivers, detect, read_detection, write_detection
from .editor_clis import env_value, path_var_of
from .files import read_text_if_exists, remove_stale_files
from .ide_catalog import IdeEntry, find_ide_entry, matches_product, product_matches_name
from .ide_client import IdeCall, IdeError
from .ide_installs import IdeInstall, discover_ides, ide_launch_command, launch_environment, spawn_ide, system_discovery
from .installed import is_cmd_shim, is_installed
from .jsjson import js_round, js_string
from .launch_plan import ORI_AGENTS, LaunchRequest, plan_launch
from .parallel import Task, run_all
from .profiles import (
    AGENTS_FILE,
    CONFIG_FILE,
    TAB_ID_ENV,
    AgentSettings,
    claude_tab_settings,
    codex_python,
    resolve_focus,
    resolve_settings,
)
from .registry import Endpoint, Registry, read_registry
from .request import OpenRequest, validate_open
from .reveal import RevealDeps, check_reveal_target, file_manager_command, system_reveal
from .routing import ChoiceError, IdeCandidate, Project, choose_ide, choose_terminal, project_depth
from .spec import launch_spec
from .tab_store import TabStore
from .terminals import default_terminal_name
from .terminals.driver import OpenOptions, TerminalContext, TerminalDriver, TerminalTab
from .terminals.powershell import ShellProbe, list_power_shells, pick_power_shell, system_probe

if TYPE_CHECKING:
    from .closed import TranscriptDirs

SPEC_MAX_AGE_MS = 60 * 60 * 1000
FRESH_TAB_START_MS = 10_000
IDE_POLL_MS = 500
IDE_SYNC_WAIT_MS = 40_000
IDE_PROGRESS_MS = 5_000
SELF_CLOSE_DELAY_MS = 500


def random_uuid() -> str:
    h = bytearray(os.urandom(16))
    h[6] = (h[6] & 0x0F) | 0x40
    h[8] = (h[8] & 0x3F) | 0x80
    x = h.hex()
    return f"{x[:8]}-{x[8:12]}-{x[12:16]}-{x[16:20]}-{x[20:]}"


T = TypeVar("T")
Progress = Callable[[float, float, str], None]


class ToolError(Exception):
    pass


class IdeLauncher(Protocol):
    def discover(self) -> list[IdeInstall]: ...

    def launch(self, install: IdeInstall, folder: str) -> None: ...


class PresenceOps(Protocol):
    def read(self, home: str, session_id: str) -> dict[str, Any] | None: ...

    def update(self, home: str, session_id: str, change: Callable[[dict[str, Any] | None], dict[str, Any] | None]) -> Any: ...

    def with_state(self, p: dict[str, Any], state: str, now: int) -> dict[str, Any]: ...


class IdeWait(NamedTuple):
    poll_ms: float = IDE_POLL_MS
    sync_ms: float = IDE_SYNC_WAIT_MS
    progress_ms: float = IDE_PROGRESS_MS


class ServiceDeps(NamedTuple):
    home: str
    scripts_dir: str
    platform: str
    env: Mapping[str, str]
    call_ide: IdeCall
    drivers: Sequence[TerminalDriver]
    is_alive: Callable[[int], bool] | None = None
    new_id: Callable[[], str] | None = None
    self_close_delay_ms: float = SELF_CLOSE_DELAY_MS
    shell_probe: Callable[[bool], ShellProbe] | None = None
    transcripts: TranscriptDirs | None = None
    reveal: RevealDeps | None = None
    ides: IdeLauncher | None = None
    ide_wait: IdeWait = IdeWait()
    log: Callable[[str], None] | None = None
    presence: PresenceOps | None = None


class SystemIdes:
    def __init__(self, platform: str, env: Mapping[str, str]) -> None:
        self.platform = platform
        self.env = env

    def discover(self) -> list[IdeInstall]:
        return discover_ides(system_discovery(self.platform, self.env))

    def launch(self, install: IdeInstall, folder: str) -> None:
        comspec = env_value(self.env, "ComSpec", "COMSPEC")
        spawn_ide(ide_launch_command(install, folder, self.platform, comspec), launch_environment(self.env))


class IdeInfo(NamedTuple):
    endpoint: Endpoint
    product: str
    version: str
    projects: list[Project]


class Started(NamedTuple):
    endpoint: Endpoint | None = None
    why: str | None = None


def error_text(e: BaseException) -> str:
    return str(e) if e.args else type(e).__name__


def projects_of(reply: Mapping[str, Any]) -> list[Project]:
    listed = reply.get("projects")
    if not isinstance(listed, list):
        listed = []
    projects: list[Project] = []
    for p in listed:
        if not isinstance(p, dict):
            continue
        name = p.get("name")
        path = p.get("path")
        projects.append(
            Project(js_string(name) if name is not None else "", js_string(path) if path is not None else "", p.get("focused") is True)
        )
    return projects


def _copy_present(source: Mapping[str, Any], keys: Sequence[str], into: dict[str, Any]) -> None:
    for key in keys:
        if key in source:
            into[key] = source[key]


class Service:
    def __init__(self, deps: ServiceDeps) -> None:
        self.deps = deps
        self.store = TabStore(deps.home)
        self.ctx = TerminalContext(deps.home, deps.scripts_dir, path_var_of(deps.env), deps.env)
        self._starting: dict[str, Task[Started]] = {}
        self._background: set[threading.Thread] = set()
        self._lock = threading.Lock()

    @property
    def is_windows(self) -> bool:
        return self.deps.platform == "win32"

    def settings(self) -> AgentSettings:
        agents_path = os.path.join(self.deps.home, AGENTS_FILE)
        config_path = os.path.join(self.deps.home, CONFIG_FILE)

        def read(file: str) -> str | None:
            try:
                return read_text_if_exists(file)
            except OSError:
                return None

        return resolve_settings(read(agents_path), read(config_path), agents_path, config_path)

    def _registry(self) -> Registry:
        return read_registry(self.deps.home, self.deps.is_alive) if self.deps.is_alive else read_registry(self.deps.home)

    def call_ide(self, endpoint: Endpoint, route: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            return self.deps.call_ide(endpoint, route, body)
        except IdeError as e:
            if e.status != 401:
                raise
            fresh = next((f for f in self._registry().endpoints if f.id == endpoint.id and f.token != endpoint.token), None)
            if fresh is None:
                raise
            return self.deps.call_ide(fresh, route, body)

    def _info(self, endpoint: Endpoint) -> IdeInfo | dict[str, str]:
        try:
            reply = self.call_ide(endpoint, "info")
        except Exception as e:  # noqa: BLE001
            return {"id": endpoint.id, "error": error_text(e)}
        product = reply.get("product")
        version = reply.get("version")
        return IdeInfo(
            endpoint,
            product if isinstance(product, str) else endpoint.product,
            version if isinstance(version, str) else endpoint.version,
            projects_of(reply),
        )

    def _infos(self, endpoints: Sequence[Endpoint]) -> tuple[list[IdeInfo], list[dict[str, str]]]:
        results = run_all([lambda e=e: self._info(e) for e in endpoints])
        return [r for r in results if isinstance(r, IdeInfo)], [r for r in results if isinstance(r, dict)]

    def _available_drivers(self) -> list[TerminalDriver]:
        return available_drivers(self.deps.drivers, self.ctx)

    def _driver_available(self, driver: TerminalDriver) -> bool:
        try:
            return driver.available(self.ctx)
        except DRIVER_ERRORS:
            return False

    def refresh_detection(self, available: Sequence[TerminalDriver] | None = None) -> Detection:
        drivers = available if available is not None else self._available_drivers()
        detection = detect(
            self.deps.home,
            self.deps.platform,
            self.deps.env,
            [{"id": d.name, "name": d.label} for d in drivers],
            probe=self.deps.shell_probe(True) if self.deps.shell_probe else None,
        )
        with contextlib.suppress(OSError, TimeoutError):
            write_detection(self.deps.home, detection)
        return detection

    # Detection probes every shell and takes seconds on Windows, so a caller that can live with a recent result
    # passes the age it accepts; the session start hook and the shared server refresh the file.
    def _detection(self, drivers: Sequence[TerminalDriver], max_age_ms: float | None) -> Detection | None:
        if max_age_ms is not None:
            known = read_detection(self.deps.home)
            at = parse_iso(known["detectedAt"]) if known is not None else None
            if known is not None and at is not None and 0 <= now_ms() - at < max_age_ms:
                return known
        try:
            return self.refresh_detection(drivers)
        except DRIVER_ERRORS:
            return None

    def _capabilities(self, driver: TerminalDriver) -> dict[str, str]:
        try:
            return driver.current_capabilities(self.ctx)
        except DRIVER_ERRORS:
            return driver.capabilities

    def list_ides(self, detection_max_age_ms: float | None = None) -> dict[str, Any]:
        registry = self._registry()
        settings = self.settings()
        drivers = self._available_drivers()
        jobs: list[Callable[[], Any]] = [
            lambda: self._infos(registry.endpoints),
            lambda: self._detection(drivers, detection_max_age_ms),
            self._discover,
            lambda: [self._capabilities(d) for d in drivers],
        ]
        (infos, errors), detection, installs, capabilities = run_all(jobs)
        running = [i.product for i in infos]
        listed: set[str] = set()
        installed: list[dict[str, Any]] = []
        for i in installs:
            if any(product_matches_name(product, i.key) for product in running) or i.key in listed:
                continue
            listed.add(i.key)
            row: dict[str, Any] = {"name": i.key, "product": i.product, "kind": i.kind}
            if i.version is not None:
                row["version"] = i.version
            installed.append(row)
        result: dict[str, Any] = {
            "ides": [
                {
                    "id": i.endpoint.id,
                    "ide": i.endpoint.ide,
                    "product": i.product,
                    "version": i.version,
                    "projects": [p.to_json() for p in i.projects],
                }
                for i in infos
            ],
            "terminals": [
                {
                    "id": d.name,
                    "name": d.label,
                    "capabilities": capabilities[n],
                    "preferred": settings.terminal.preferred_terminal == d.name,
                }
                for n, d in enumerate(drivers)
            ],
            "installed": installed,
            "shells": detection["shells"] if detection is not None else [],
        }
        if errors:
            result["errors"] = errors
        if registry.warnings:
            result["warnings"] = registry.warnings
        return result

    def list_agents(self) -> dict[str, Any]:
        settings = self.settings()
        detection = read_detection(self.deps.home)
        ori = detection.get("ori") if detection is not None else None
        agents: list[dict[str, Any]] = []
        for p in settings.profiles:
            row: dict[str, Any] = {
                "name": p.name,
                "label": p.label,
                "command": p.command,
                "installed": is_installed(p.command, self.ctx.path_var, self.is_windows),
                "model": p.model_flag is not None,
            }
            if ori is not None and p.name in ori["agents"] and p.name in ORI_AGENTS:
                row["ori"] = True
            agents.append(row)
        result: dict[str, Any] = {"default": settings.default_agent.name, "agents": agents, "launchVia": settings.terminal.launch_via}
        if settings.warnings:
            result["warnings"] = settings.warnings
        return result

    def _ides(self) -> IdeLauncher:
        return self.deps.ides if self.deps.ides is not None else SystemIdes(self.deps.platform, self.deps.env)

    def _discover(self) -> list[IdeInstall]:
        try:
            return self._ides().discover()
        except DRIVER_ERRORS:
            return []

    def settled(self) -> None:
        while True:
            with self._lock:
                pending = list(self._background)
            if not pending:
                return
            for thread in pending:
                thread.join()

    def open_tab(self, given: Mapping[str, Any], wait: str = "full", on_progress: Progress | None = None) -> dict[str, Any]:
        try:
            request = validate_open(dict(given))
        except ValueError as e:
            raise ToolError(error_text(e)) from e
        endpoints = self._registry().endpoints
        settings = self.settings()
        request = request.with_focus(resolve_focus(settings.terminal.focus_new_tabs, request.focus))
        if request.ide is not None:
            driver = next((d for d in self.deps.drivers if d.name == request.ide), None)
            if driver is not None:
                if not self._driver_available(driver):
                    raise ToolError(f"terminal {driver.name} is not available on this machine")
                return self._open_in_terminal(driver, request, "named by ide")
            endpoint = next((e for e in endpoints if e.id == request.ide), None)
            if endpoint is not None:
                return self._open_in_ide(endpoint, request, "named by ide")
            return self._open_named(request.ide, request, endpoints, settings, wait, on_progress)
        return self._route(request, endpoints, settings)

    def _caller_host(self) -> tuple[str | None, str | None]:
        own = self.deps.env.get(TAB_ID_ENV)
        if not own:
            return own, None
        try:
            return own, self.find_host(own)
        except (OSError, ValueError, IdeError):
            return own, None

    def _route(self, request: OpenRequest, endpoints: Sequence[Endpoint], settings: AgentSettings) -> dict[str, Any]:
        own, caller_host = self._caller_host()
        caller_terminal = next((d for d in self.deps.drivers if d.name == caller_host), None)
        if settings.terminal.tab_routing == "caller" and caller_terminal is not None and self._driver_available(caller_terminal):
            near = next((t for t in self.store.read() if t["id"] == own), None)
            return self._open_in_terminal(caller_terminal, request, "tabRouting is caller; the caller's terminal window", near)
        infos, errors = self._infos(endpoints)
        candidates = [IdeCandidate(i.endpoint.id, i.endpoint.started_at, i.projects) for i in infos]
        choice = choose_ide(candidates, request.path, self.is_windows, caller_host, settings.terminal.tab_routing)
        if choice is not None:
            endpoint = next(i.endpoint for i in infos if i.endpoint.id == choice.name)
            return self._open_in_ide(endpoint, request, choice.reason)
        available = [d.name for d in self._available_drivers()]
        terminal = choose_terminal(settings.terminal.preferred_terminal, default_terminal_name(self.deps.platform, available), available)
        if isinstance(terminal, ChoiceError):
            detail = f" IDE errors: {'; '.join(e['error'] for e in errors)}" if errors else ""
            raise ToolError(f"{terminal.error}.{detail}")
        driver = next(d for d in self.deps.drivers if d.name == terminal.name)
        return self._open_in_terminal(driver, request, terminal.reason)

    def _open_named(
        self,
        name: str,
        request: OpenRequest,
        endpoints: Sequence[Endpoint],
        settings: AgentSettings,
        wait: str,
        on_progress: Progress | None,
    ) -> dict[str, Any]:
        entry = find_ide_entry(name)
        matching = [e for e in endpoints if product_matches_name(e.product, name)]
        if matching:
            infos, _ = self._infos(matching)

            def depth_of(i: IdeInfo) -> int:
                depths = [project_depth(p.path, request.path, self.is_windows) for p in i.projects]
                return max([-1, *(d if d is not None else -1 for d in depths)])

            ranked = sorted(((i, depth_of(i)) for i in infos), key=lambda r: (-r[1], -r[0].endpoint.started_at))
            best = ranked[0] if ranked else None
            endpoint = best[0].endpoint if best is not None else max(matching, key=lambda e: e.started_at)
            label = entry.name if entry is not None else endpoint.product
            if best is not None and best[1] >= 0:
                reason = f"{label} is running; an open project contains the path"
            else:
                reason = f"{label} is running; most recently started"
            return self._open_in_ide(endpoint, request, reason)
        if entry is None:
            raise ToolError(
                f"no running IDE or terminal with id {name}, and no IDE by that name; pass an IDE name such as vscode, idea or "
                "android-studio, or an id that the Agent Tabs command line's list-ides prints"
            )
        install = next((i for i in self._discover() if i.key == entry.key), None)
        if install is None:
            return self._fallback(request, f"{entry.name} isn't installed")
        budget_ms = settings.terminal.ide_start_timeout_sec * 1000
        started = self._start_ide(entry, install, request.path, {e.id for e in endpoints}, budget_ms)

        def finish(s: Started) -> dict[str, Any]:
            if s.endpoint is None:
                return self._fallback(request, s.why or "")
            try:
                return self._open_in_ide(s.endpoint, request, f"started {entry.name}")
            except Exception as e:  # noqa: BLE001
                return self._fallback(request, f"{entry.name} started but couldn't open the tab: {error_text(e)}")

        message = f"Waiting for {entry.name} to load"
        if wait != "background":
            early = self._wait_for(started, budget_ms, budget_ms, message, on_progress)
            return finish(early if early is not None else started.result())
        sync_ms = min(self.deps.ide_wait.sync_ms, budget_ms)
        early = self._wait_for(started, sync_ms, budget_ms, message, on_progress)
        if early is not None:
            return finish(early)

        def later() -> None:
            try:
                opened = finish(started.result())
                self._log(f"opened tab {js_string(opened.get('id'))} in {js_string(opened.get('product'))} after starting {entry.name}")
            except Exception as e:  # noqa: BLE001
                self._log(f"couldn't open the tab after starting {entry.name}: {error_text(e)}")
            finally:
                with self._lock:
                    self._background.discard(threading.current_thread())

        thread = threading.Thread(target=later, name=f"open-after-{entry.key}", daemon=True)
        with self._lock:
            self._background.add(thread)
        thread.start()
        seconds = settings.terminal.ide_start_timeout_sec
        return {
            "pending": True,
            "ide": entry.key,
            "product": entry.name,
            "agent": request.agent if request.agent is not None else settings.default_agent.name,
            "path": request.path,
            "reason": f"started {entry.name}; it is still loading",
            "note": (
                f"{entry.name} is starting. The agent tab opens there once it loads, up to {js_string(seconds)} s after the launch. "
                "If it doesn't load by then, the tab opens in the caller's IDE or terminal instead."
            ),
        }

    def _log(self, message: str) -> None:
        if self.deps.log is not None:
            self.deps.log(message)

    def _start_ide(self, entry: IdeEntry, install: IdeInstall, folder: str, before: set[str], budget_ms: float) -> Task[Started]:
        with self._lock:
            running = self._starting.get(entry.key)
            if running is not None:
                return running
        poll_ms = self.deps.ide_wait.poll_ms
        seconds = js_string(js_round(budget_ms / 100) / 10)
        piece = "plugin" if entry.kind == "jetbrains" else "extension"

        def start() -> Started:
            deadline = now_ms() + budget_ms
            try:
                self._ides().launch(install, folder)
            except Exception as e:  # noqa: BLE001
                return Started(why=f"{entry.name} couldn't start: {error_text(e)}")
            registered = False
            while now_ms() < deadline:
                time.sleep(min(poll_ms, max(0, deadline - now_ms())) / 1000)
                fresh = [e for e in self._registry().endpoints if e.id not in before and matches_product(entry, e.product)]
                for endpoint in fresh:
                    try:
                        reply = self.call_ide(endpoint, "info")
                    except Exception:  # noqa: BLE001, S112
                        continue
                    registered = True
                    if projects_of(reply):
                        return Started(endpoint=endpoint)
            if registered:
                return Started(why=f"{entry.name} started but opened no project within {seconds} s")
            return Started(
                why=(
                    f"{entry.name} started but didn't register within {seconds} s; if the Agent Tabs {piece} isn't installed in it, "
                    "run /ide-agent-tabs:setup"
                )
            )

        def run() -> Started:
            try:
                return start()
            finally:
                with self._lock:
                    self._starting.pop(entry.key, None)

        with self._lock:
            task = self._starting[entry.key] = Task(run, f"start-{entry.key}")
        return task

    def _wait_for(
        self, started: Task[Started], limit_ms: float, total_ms: float, message: str, on_progress: Progress | None
    ) -> Started | None:
        begin = now_ms()
        deadline = begin + limit_ms
        step = self.deps.ide_wait.progress_ms
        if on_progress is not None:
            on_progress(0, total_ms, message)
        while True:
            left = deadline - now_ms()
            if left <= 0:
                return started.result() if started.done() else None
            try:
                return started.result(timeout=(min(left, step) if on_progress is not None else left) / 1000)
            except TimeoutError:
                if on_progress is not None and now_ms() < deadline:
                    on_progress(now_ms() - begin, total_ms, message)

    def _fallback(self, request: OpenRequest, why: str) -> dict[str, Any]:
        endpoints = self._registry().endpoints
        settings = self.settings()
        own, host = self._caller_host()
        endpoint = next((e for e in endpoints if e.id == host), None)
        driver = next((d for d in self.deps.drivers if d.name == host), None)
        if endpoint is not None:
            opened = self._open_in_ide(endpoint, request, f"{why}; the caller's IDE")
        elif driver is not None and self._driver_available(driver):
            near = next((t for t in self.store.read() if t["id"] == own), None)
            opened = self._open_in_terminal(driver, request, f"{why}; the caller's terminal", near)
        else:
            routed = self._route(request, endpoints, settings)
            opened = {**routed, "reason": f"{why}; {routed['reason']}"}
        note = f"{why}, so the tab opened in {js_string(opened.get('product'))} instead."
        return {**opened, "note": note if opened.get("note") is None else f"{note} {opened['note']}"}

    def _open_in_ide(self, endpoint: Endpoint, request: OpenRequest, reason: str) -> dict[str, Any]:
        body: dict[str, Any] = {"path": request.path}
        if request.agent is not None:
            body["agent"] = request.agent
        if request.prompt is not None:
            body["prompt"] = request.prompt
        if request.args:
            body["args"] = request.args
        if request.env:
            body["env"] = request.env
        if request.model is not None:
            body["model"] = request.model
        if request.via is not None:
            body["via"] = request.via
        body["focus"] = request.focus is True
        try:
            reply = self.call_ide(endpoint, "open", body)
        except Exception as e:
            raise ToolError(error_text(e)) from e
        launch: dict[str, Any] = {"via": "ori" if reply.get("via") == "ori" else "direct"}
        project = reply.get("project")
        if isinstance(project, str) and project != "":
            launch["project"] = project
        if request.model is not None:
            launch["model"] = request.model
        launch["product"] = endpoint.product
        self._mark_opened(reply.get("id"), endpoint.id, request.prompt is None, launch)
        result: dict[str, Any] = {}
        _copy_present(reply, ["id"], result)
        result["ide"] = endpoint.id
        result["product"] = endpoint.product
        _copy_present(reply, ["agent", "project", "path"], result)
        result["reason"] = reason
        if reply.get("via") == "ori":
            result["via"] = "ori"
        return result

    def _power_shell(self, configured: str | None) -> str | None:
        if not self.is_windows:
            return None
        probe = self.deps.shell_probe(False) if self.deps.shell_probe else system_probe(self.deps.env, False)
        detection = read_detection(self.deps.home)
        detected = detection["shells"] if detection is not None else []
        shells = detected if any(probe.exists(s["path"]) for s in detected) else list_power_shells(probe)
        return pick_power_shell(shells, configured, probe.exists)

    def _open_in_terminal(
        self, driver: TerminalDriver, request: OpenRequest, reason: str, near: TerminalTab | None = None
    ) -> dict[str, Any]:
        settings = self.settings()
        if request.agent is None:
            profile = settings.default_agent
        else:
            found = next((p for p in settings.profiles if p.name == request.agent), None)
            if found is None:
                raise ToolError(f"unknown agent: {request.agent}")
            profile = found
        detection = read_detection(self.deps.home)
        try:
            plan = plan_launch(
                profile,
                LaunchRequest(
                    args=request.args,
                    env=request.env,
                    launch_via=settings.terminal.launch_via,
                    ori=detection.get("ori") if detection is not None else None,
                    platform=self.deps.platform,
                    prompt=request.prompt,
                    model=request.model,
                    via=request.via,
                    cmd_shim=self.is_windows and is_cmd_shim(profile.command, self.ctx.path_var),
                    python=codex_python(self.deps.home, self.is_windows),
                    claude_settings=claude_tab_settings(self.deps.home, self.is_windows),
                ),
            )
        except ValueError as e:
            raise ToolError(error_text(e)) from e
        spec = launch_spec((self.deps.new_id or random_uuid)(), request.path, plan.launch)
        remove_stale_files(os.path.join(self.deps.home, "launch"), [".json", ".spec"], SPEC_MAX_AGE_MS)
        power_shell = self._power_shell(settings.terminal.shell)
        ctx = self.ctx if power_shell is None else self.ctx.with_power_shell(power_shell)
        try:
            opened = driver.open(ctx, spec, profile.label, OpenOptions(settings.terminal.terminal_window, near, request.focus is True))
        except Exception as e:
            raise ToolError(f"{driver.label}: {error_text(e)}") from e
        tab = {k: v for k, v in opened.items() if k != "note"}
        note = opened.get("note")
        self.store.add(tab)
        launch: dict[str, Any] = {"via": plan.via}
        if request.model is not None:
            launch["model"] = request.model
        launch["product"] = driver.label
        self._mark_opened(tab["id"], driver.name, request.prompt is None, launch)
        result: dict[str, Any] = {
            "id": tab["id"],
            "ide": driver.name,
            "product": driver.label,
            "agent": profile.name,
            "path": request.path,
            "reason": reason,
        }
        if plan.via == "ori":
            result["via"] = "ori"
        if note is not None:
            result["note"] = note
        return result

    # Some CLIs, such as Codex, run no start hook until their first turn, so a tab opened without a prompt would
    # stay unknown and never be woken. It waits at its prompt once the CLI has had FRESH_TAB_START_MS to start.
    def _mark_opened(self, tab_id: Any, host: str, fresh: bool, launch: dict[str, Any]) -> None:
        presence = self.deps.presence
        from .messaging.sessions import is_session_id

        if presence is None or not isinstance(tab_id, str) or not is_session_id(tab_id):
            return
        at = now_ms() + FRESH_TAB_START_MS

        def change(current: dict[str, Any] | None) -> dict[str, Any]:
            base = {**(current if current is not None else {"id": tab_id}), **launch}
            state = current.get("state") if current is not None else None
            if not fresh or (state is not None and state != "unknown"):
                return base
            return presence.with_state({**base, "host": host}, "idle", at)

        with contextlib.suppress(Exception):
            presence.update(self.deps.home, tab_id, change)

    def live_host(self, host: str | None, product: str | None) -> str | None:
        driver = next((d for d in self.deps.drivers if d.name == host), None) if host is not None else None
        if driver is not None:
            return driver.name if self._driver_available(driver) else None
        endpoints = self._registry().endpoints
        found = next((e for e in endpoints if e.id == host), None) or next(
            (e for e in endpoints if product is not None and e.product == product), None
        )
        return found.id if found is not None else None

    def describe_host(self, host: str) -> str | None:
        driver = next((d for d in self.deps.drivers if d.name == host), None)
        if driver is not None:
            return driver.label
        return next((e.product for e in self._registry().endpoints if e.id == host), None)

    def _terminal_tabs(self, only: TerminalDriver | None = None) -> tuple[list[TerminalTab], list[dict[str, str]]]:
        tabs = self.store.read()
        alive: list[TerminalTab] = []
        dead: set[str] = set()
        errors: list[dict[str, str]] = []
        for driver in [only] if only is not None else self._available_drivers():
            mine = [t for t in tabs if t["terminal"] == driver.name]
            if not mine:
                continue
            try:
                ids = driver.alive(self.ctx, mine)
            except Exception as e:  # noqa: BLE001
                errors.append({"id": driver.name, "error": error_text(e)})
                continue
            for t in mine:
                if t["id"] in ids:
                    alive.append(t)
                else:
                    dead.add(t["id"])
        if dead:
            self.store.remove(dead)
            for t in tabs:
                if t["id"] in dead and t.get("pidFile"):
                    with contextlib.suppress(OSError):
                        os.remove(t["pidFile"])
        return alive, errors

    def list_tabs(self, ide: str | None = None) -> dict[str, Any]:
        endpoints = self._registry().endpoints
        driver = next((d for d in self.deps.drivers if d.name == ide), None) if ide is not None else None
        chosen = endpoints if ide is None else [e for e in endpoints if e.id == ide]
        if ide is not None and driver is None and not chosen:
            raise ToolError(f"no running IDE or terminal with id {ide}; the Agent Tabs command line's list-ides prints the ids")
        tabs: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []

        def listed(endpoint: Endpoint) -> tuple[Endpoint, Any]:
            try:
                return endpoint, self.call_ide(endpoint, "list")
            except Exception as e:  # noqa: BLE001
                return endpoint, e

        for endpoint, reply in run_all([lambda e=e: listed(e) for e in ([] if driver is not None else chosen)]):
            if isinstance(reply, BaseException):
                errors.append({"id": endpoint.id, "error": error_text(reply)})
                continue
            for t in reply.get("tabs") if isinstance(reply.get("tabs"), list) else []:
                row = dict(t) if isinstance(t, dict) else {}
                row["ide"] = endpoint.id
                tabs.append(row)
        if ide is None or driver is not None:
            alive, terminal_errors = self._terminal_tabs(driver)
            tabs.extend({"id": t["id"], "agent": t["agent"], "path": t["path"], "ide": t["terminal"]} for t in alive)
            errors.extend(terminal_errors)
        result: dict[str, Any] = {"tabs": tabs}
        if errors:
            result["errors"] = errors
        return result

    def reveal(self, target: str, preferred: str | None, session_folders: Sequence[str]) -> dict[str, Any]:
        endpoints = self._registry().endpoints
        infos, _ = self._infos(endpoints)
        deps = self.deps.reveal if self.deps.reveal is not None else system_reveal(self.deps.platform)
        try:
            real = check_reveal_target(target, [*session_folders, *(p.path for i in infos for p in i.projects)], deps)
        except ValueError as e:
            return {"ok": False, "reason": error_text(e)}
        order = [*(e for e in endpoints if e.id == preferred), *(e for e in endpoints if e.id != preferred)]
        errors: list[str] = []
        for endpoint in order:
            try:
                self.call_ide(endpoint, "reveal", {"path": real})
                return {"ok": True, "ide": endpoint.id, "product": endpoint.product, "path": real}
            except Exception as e:  # noqa: BLE001
                errors.append(error_text(e))
        if not order:
            errors.append("no IDE is running")
        try:
            deps.open(real)
            return {"ok": True, "ide": "system", "product": file_manager_command(self.deps.platform), "path": real}
        except Exception as e:  # noqa: BLE001
            errors.append(error_text(e))
        return {"ok": False, "reason": "; ".join(errors)}

    def close_tab(self, tab_id: str | None = None) -> dict[str, Any]:
        own = self.deps.env.get(TAB_ID_ENV)
        target = tab_id if tab_id is not None else own
        if not target:
            raise ToolError(f"no id given, and {TAB_ID_ENV} is not set, so this session was not opened as an agent tab")
        is_self = target == own
        ending: dict[str, Any] | None = None
        if self.deps.presence is not None:
            with contextlib.suppress(Exception):
                ending = self.deps.presence.read(self.deps.home, target)

        def ended() -> None:
            if ending is None:
                return
            with contextlib.suppress(Exception):
                from .closed import record_ended, transcript_dirs

                record_ended(self.deps.home, ending, now_ms(), self.deps.transcripts or transcript_dirs(self.deps.env))

        record = next((t for t in self.store.read() if t["id"] == target), None)
        if record is not None:
            driver = next((d for d in self.deps.drivers if d.name == record["terminal"]), None)
            if driver is None:
                raise ToolError(f"tab {target} belongs to terminal {record['terminal']}, which this server can't drive")

            def close() -> None:
                driver.close(self.ctx, record)
                self.store.remove({target})

            if is_self:
                ended()

                def close_quietly() -> None:
                    with contextlib.suppress(Exception):
                        close()

                threading.Timer(self.deps.self_close_delay_ms / 1000, close_quietly).start()
                return {"id": target, "ide": driver.name, "closing": True}
            try:
                close()
            except Exception as e:
                raise ToolError(f"{driver.label}: {error_text(e)}") from e
            ended()
            return {"id": target, "ide": driver.name, "closed": True}
        owner = self._ide_owner(target)
        if owner is None:
            raise ToolError(f"no open agent tab with id {target}; list_tabs shows the open ones")
        try:
            self.call_ide(owner, "close", {"id": target})
        except Exception as e:
            raise ToolError(error_text(e)) from e
        ended()
        return {"id": target, "ide": owner.id, "closed": True}

    def _ide_owner(self, tab_id: str) -> Endpoint | None:
        endpoints = self._registry().endpoints

        def owns(endpoint: Endpoint) -> bool:
            try:
                reply = self.call_ide(endpoint, "list")
            except Exception:  # noqa: BLE001
                return False
            tabs = reply.get("tabs")
            return isinstance(tabs, list) and any(isinstance(t, dict) and t.get("id") == tab_id for t in tabs)

        flags = run_all([lambda e=e: owns(e) for e in endpoints])
        return next((e for e, owned in zip(endpoints, flags) if owned), None)

    def find_host(self, tab_id: str) -> str | None:
        record = next((t for t in self.store.read() if t["id"] == tab_id), None)
        if record is not None:
            return record["terminal"]
        owner = self._ide_owner(tab_id)
        return owner.id if owner is not None else None

    def type_into(self, tab_id: str, host: str, text: str) -> dict[str, Any]:
        driver = next((d for d in self.deps.drivers if d.name == host), None)
        if driver is not None:
            if not driver.can_input:
                return {"ok": False, "reason": f"{driver.label} can't take input from outside"}
            record = next((t for t in self.store.read() if t["id"] == tab_id and t["terminal"] == host), None)
            if record is None:
                return {"ok": False, "reason": f"no {driver.label} tab with id {tab_id}"}
            try:
                driver.input(self.ctx, record, text)
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "reason": f"{driver.label}: {error_text(e)}"}
            return {"ok": True}
        endpoint = next((e for e in self._registry().endpoints if e.id == host), None)
        if endpoint is None:
            return {"ok": False, "reason": f"no running IDE or terminal with id {host}"}
        try:
            self.call_ide(endpoint, "input", {"id": tab_id, "text": text})
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "reason": error_text(e)}
        return {"ok": True}

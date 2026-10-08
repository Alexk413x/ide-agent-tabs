from __future__ import annotations

import io
import json
import os
import threading
import time
import unittest
from typing import Any

from fake_ide import FakeIde, fake_ide
from ide_agent_tabs import jsjson
from ide_agent_tabs.clock import iso, now_ms
from ide_agent_tabs.ide_client import IdeError, ide_caller
from ide_agent_tabs.ide_installs import IdeInstall
from ide_agent_tabs.list_ides_cli import cli_list_ides
from ide_agent_tabs.registry import read_registry
from ide_agent_tabs.service import IdeWait, Service, ServiceDeps, ToolError
from ide_agent_tabs.spec import LaunchSpec
from ide_agent_tabs.terminals.driver import OpenOptions, TerminalContext, TerminalDriver, TerminalTab, caps
from ide_agent_tabs.terminals.powershell import ShellProbe
from support import temp_home


class FakeTerminal(TerminalDriver):
    name = "fake-term"
    label = "Fake Terminal"
    capabilities = caps("tab", "yes", "yes")
    can_input = True

    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.opened: list[tuple[LaunchSpec, str, OpenOptions | None]] = []
        self.closed: list[str] = []
        self.typed: list[tuple[str, str]] = []
        self.live: set[str] | None = None

    def available(self, ctx: TerminalContext) -> bool:
        return self.ok

    def open(self, ctx: TerminalContext, spec: LaunchSpec, title: str, options: OpenOptions | None = None) -> TerminalTab:
        self.opened.append((spec, title, options))
        return {"id": spec.id, "terminal": self.name, "agent": spec.agent, "path": spec.cwd, "createdAt": now_ms(), "note": "hello"}

    def alive(self, ctx: TerminalContext, tabs: list[TerminalTab]) -> set[str]:
        return {t["id"] for t in tabs} if self.live is None else self.live

    def close(self, ctx: TerminalContext, tab: TerminalTab) -> None:
        self.closed.append(tab["id"])

    def input(self, ctx: TerminalContext, tab: TerminalTab, text: str) -> None:
        self.typed.append((tab["id"], text))


class FakeLauncher:
    def __init__(self, installs: list[IdeInstall], on_launch: Any = None) -> None:
        self.installs = installs
        self.launches: list[tuple[IdeInstall, str]] = []
        self.on_launch = on_launch

    def discover(self) -> list[IdeInstall]:
        return self.installs

    def launch(self, install: IdeInstall, folder: str) -> None:
        self.launches.append((install, folder))
        if self.on_launch is not None:
            self.on_launch(folder)


class Presence:
    def __init__(self) -> None:
        self.files: dict[str, dict[str, Any]] = {}

    def read(self, home: str, session_id: str) -> dict[str, Any] | None:
        return self.files.get(session_id)

    def update(self, home: str, session_id: str, change: Any) -> Any:
        self.files[session_id] = change(self.files.get(session_id))
        return self.files[session_id]

    def with_state(self, p: dict[str, Any], state: str, now: int) -> dict[str, Any]:
        return {**p, "state": state, "stateAt": iso(now)}


STUDIO = IdeInstall("android-studio", "Android Studio", "jetbrains", "/opt/android-studio/bin/studio.sh", "2026.1")


def write(path: str, value: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(value if isinstance(value, str) else json.dumps(value))


class Setup:
    def __init__(
        self, test: unittest.TestCase, config: dict[str, Any] | None = None, env: dict[str, str] | None = None, **deps: Any
    ) -> None:
        self.home = temp_home(test)
        self.work = temp_home(test, "iat-work-")
        self.other = temp_home(test, "iat-other-")
        self.terminal = deps.pop("terminal", FakeTerminal())
        self.presence = Presence()
        self.logs: list[str] = []
        self.ids = iter(f"term-{i}" for i in range(1, 100))
        write(os.path.join(self.home, "config.json"), config if config is not None else {"terminal": "fake-term"})
        self.service = Service(
            ServiceDeps(
                home=self.home,
                scripts_dir=self.home,
                platform="linux",
                env=env if env is not None else {"PATH": ""},
                call_ide=ide_caller(5_000),
                drivers=[self.terminal],
                is_alive=lambda pid: True,
                new_id=lambda: next(self.ids),
                self_close_delay_ms=10,
                ides=deps.pop("ides", FakeLauncher([])),
                ide_wait=IdeWait(10, deps.pop("sync_ms", 60), 20),
                log=self.logs.append,
                presence=self.presence,
                **deps,
            )
        )

    def tabs_file(self) -> list[dict[str, Any]]:
        with open(os.path.join(self.home, "terminal-tabs.json"), encoding="utf-8") as f:
            return json.load(f)["tabs"]


def fresh_detection(home: str, shells: list[dict[str, str]] | None = None) -> None:
    write(
        os.path.join(home, "detected.json"),
        {"version": 1, "detectedAt": iso(now_ms()), "platform": "linux", "terminals": [], "shells": shells or [], "ori": None},
    )


class ListIdesTest(unittest.TestCase):
    def test_lists_running_ides_terminals_installs_and_cached_shells(self) -> None:
        s = Setup(self, ides=FakeLauncher([STUDIO, IdeInstall("idea", "IntelliJ IDEA", "jetbrains", "/opt/idea/bin/idea.sh", "2026.1")]))
        ide = fake_ide(self, projects=[{"name": "app", "path": s.work, "focused": True}, {"name": 5}, "junk"])
        ide.register(s.home, "jetbrains-1")
        write(os.path.join(s.home, "endpoints", "broken.json"), "not json")
        shells = [{"path": "C:\\pwsh.exe", "label": "PowerShell 7.5.0 (MSI)", "version": "7.5.0", "source": "msi"}]
        fresh_detection(s.home, shells)
        listed = s.service.list_ides(60 * 60 * 1000)
        self.assertEqual(
            jsjson.stringify(listed),
            jsjson.stringify(
                {
                    "ides": [
                        {
                            "id": "jetbrains-1",
                            "ide": "jetbrains",
                            "product": "IntelliJ IDEA",
                            "version": "2026.1",
                            "projects": [{"name": "app", "path": s.work, "focused": True}, {"name": "5", "path": "", "focused": False}],
                        }
                    ],
                    "terminals": [
                        {"id": "fake-term", "name": "Fake Terminal", "capabilities": caps("tab", "yes", "yes"), "preferred": True}
                    ],
                    "installed": [{"name": "android-studio", "product": "Android Studio", "kind": "jetbrains", "version": "2026.1"}],
                    "shells": shells,
                    "warnings": ["Skipping broken.json is not JSON"],
                }
            ),
        )

    def test_an_ide_that_fails_is_listed_under_errors(self) -> None:
        s = Setup(self)
        ide = fake_ide(self, token="right")
        ide.register(s.home, "jetbrains-2", token="wrong")
        fresh_detection(s.home)
        listed = s.service.list_ides(60_000)
        self.assertEqual(listed["ides"], [])
        self.assertEqual(
            listed["errors"],
            [
                {
                    "id": "jetbrains-2",
                    "error": "jetbrains-2 info answered HTTP 401: bad token. IntelliJ IDEA refused the token in jetbrains-2, so that endpoint is "
                    "stale. Run the Agent Tabs command line's list-ides for the current ids",
                }
            ],
        )

    def test_a_401_retries_once_with_the_token_the_ide_wrote_since(self) -> None:
        s = Setup(self)
        ide = fake_ide(self, token="new")
        ide.register(s.home, "jetbrains-3", token="new")
        endpoint = read_registry(s.home).endpoints[0]._replace(token="old")
        self.assertEqual(s.service.call_ide(endpoint, "info")["product"], "IntelliJ IDEA")
        self.assertEqual([r for r, _ in ide.requests], ["info", "info"])

    def test_detection_older_than_the_limit_is_refreshed(self) -> None:
        probes: list[bool] = []

        def probe(run_shells: bool) -> ShellProbe:
            probes.append(run_shells)
            return ShellProbe({}, lambda f: False, lambda d: None, lambda f: None, lambda f: None)

        s = Setup(self, shell_probe=probe)
        write(
            os.path.join(s.home, "detected.json"),
            {"version": 1, "detectedAt": "2020-01-01T00:00:00.000Z", "platform": "linux", "terminals": [], "shells": [], "ori": None},
        )
        s.service.list_ides(60_000)
        with open(os.path.join(s.home, "detected.json"), encoding="utf-8") as f:
            refreshed = json.load(f)
        self.assertNotEqual(refreshed["detectedAt"], "2020-01-01T00:00:00.000Z")
        self.assertEqual(refreshed["terminals"], [{"id": "fake-term", "name": "Fake Terminal"}])
        self.assertEqual(list(refreshed), ["version", "detectedAt", "platform", "terminals", "shells", "ori"])
        fresh_detection(s.home)
        before = os.path.getmtime(os.path.join(s.home, "detected.json"))
        s.service.list_ides(60_000)
        self.assertEqual(os.path.getmtime(os.path.join(s.home, "detected.json")), before)

    def test_dead_and_silent_endpoints_are_removed(self) -> None:
        home = temp_home(self)
        ide = FakeIde()
        self.addCleanup(ide.close)
        dead = ide.register(home, "dead", pid=999_999)
        silent = ide.register(home, "silent", beatMs=10)
        os.utime(silent, (time.time() - 60, time.time() - 60))
        live = ide.register(home, "live", beatMs=60_000)
        registry = read_registry(home, lambda pid: pid != 999_999)
        self.assertEqual([e.id for e in registry.endpoints], ["live"])
        self.assertFalse(os.path.exists(dead) or os.path.exists(silent))
        self.assertTrue(os.path.exists(live))


class OpenTabTest(unittest.TestCase):
    def test_a_tab_opens_in_the_ide_whose_project_holds_the_path(self) -> None:
        s = Setup(self)
        near = fake_ide(self, projects=[{"name": "app", "path": s.work, "focused": False}])
        far = fake_ide(self, projects=[{"name": "other", "path": s.other, "focused": True}])
        near.register(s.home, "jetbrains-1", started_at=1)
        far.register(s.home, "jetbrains-2", started_at=2)
        opened = s.service.open_tab(
            {"path": s.work, "agent": "codex", "prompt": "hi", "args": ["--x"], "env": {"A": "1"}, "model": "gpt-5"}
        )
        self.assertEqual(
            jsjson.stringify(opened),
            jsjson.stringify(
                {
                    "id": "ide-tab-1",
                    "ide": "jetbrains-1",
                    "product": "IntelliJ IDEA",
                    "agent": "codex",
                    "project": "app",
                    "path": s.work,
                    "reason": "open project app contains the path",
                }
            ),
        )
        route, body = near.requests[-1]
        self.assertEqual(route, "open")
        self.assertEqual(list(body), ["path", "agent", "prompt", "args", "env", "model", "focus"])
        self.assertIs(body["focus"], False)

    def test_without_an_ide_the_configured_terminal_takes_the_tab(self) -> None:
        s = Setup(self)
        opened = s.service.open_tab({"path": s.work, "focus": True})
        self.assertEqual(
            opened,
            {
                "id": "term-1",
                "ide": "fake-term",
                "product": "Fake Terminal",
                "agent": "claude",
                "path": s.work,
                "reason": "no IDE is running; preferred terminal from config.json",
                "note": "hello",
            },
        )
        spec, title, options = s.terminal.opened[0]
        self.assertEqual((spec.command, spec.args, title), ("claude", [], "Claude Code"))
        self.assertEqual(options, OpenOptions("last", None, True))
        self.assertEqual([t["id"] for t in s.tabs_file()], ["term-1"])
        self.assertNotIn("note", s.tabs_file()[0])
        self.assertEqual(s.presence.files["term-1"]["state"], "idle")
        self.assertEqual(s.presence.files["term-1"]["host"], "fake-term")

    def test_focus_follows_focusNewTabs(self) -> None:
        s = Setup(self, config={"terminal": "fake-term", "focusNewTabs": "never"})
        s.service.open_tab({"path": s.work, "focus": True})
        opened = s.terminal.opened[0][2]
        assert opened is not None
        self.assertIs(opened.focus, False)

    def test_bad_requests_are_tool_errors(self) -> None:
        s = Setup(self)
        for given, message in (
            ({"path": "relative"}, "path must be absolute"),
            ({"path": os.path.join(s.work, "missing")}, "path is not a directory"),
            ({"path": s.work, "model": "a b"}, "model must match"),
            ({"path": s.work, "agent": "nope"}, "unknown agent: nope"),
            ({"path": s.work, "ide": "notepad"}, "no running IDE or terminal with id notepad, and no IDE by that name"),
        ):
            with self.subTest(given=given), self.assertRaisesRegex(ToolError, message):
                s.service.open_tab(given)

    def test_no_ide_and_no_terminal_is_an_error(self) -> None:
        s = Setup(self, config={}, terminal=FakeTerminal(ok=False))
        with self.assertRaisesRegex(ToolError, "no IDE is running and no supported terminal is available on this OS"):
            s.service.open_tab({"path": s.work})

    def test_a_named_ide_that_is_not_running_starts_and_takes_the_tab(self) -> None:
        holder: dict[str, Setup] = {}

        def register_later(folder: str) -> None:
            def later() -> None:
                time.sleep(0.05)
                ide = FakeIde("Android Studio", [{"name": "w", "path": folder, "focused": True}])
                self.addCleanup(ide.close)
                ide.register(holder["s"].home, "jetbrains-9")

            threading.Thread(target=later, daemon=True).start()

        launcher = FakeLauncher([STUDIO], register_later)
        s = Setup(self, config={"terminal": "fake-term", "ideStartTimeoutSec": 5}, ides=launcher)
        holder["s"] = s
        progress: list[tuple[float, float, str]] = []
        opened = s.service.open_tab({"path": s.work, "ide": "studio"}, on_progress=lambda *a: progress.append(a))
        self.assertEqual(opened["ide"], "jetbrains-9")
        self.assertEqual(opened["reason"], "started Android Studio")
        self.assertEqual(launcher.launches[0][1], s.work)
        self.assertEqual(progress[0], (0, 5000, "Waiting for Android Studio to load"))

    def test_a_named_ide_that_never_loads_falls_back_to_a_terminal(self) -> None:
        launcher = FakeLauncher([STUDIO])
        s = Setup(self, config={"terminal": "fake-term", "ideStartTimeoutSec": 0.2}, ides=launcher)
        opened = s.service.open_tab({"path": s.work, "ide": "android-studio"})
        why = (
            "Android Studio started but didn't register within 0.2 s; if the Agent Tabs plugin isn't installed in it, run "
            "/ide-agent-tabs:setup"
        )
        self.assertEqual(opened["ide"], "fake-term")
        self.assertEqual(opened["reason"], f"{why}; no IDE is running; preferred terminal from config.json")
        self.assertEqual(opened["note"], f"{why}, so the tab opened in Fake Terminal instead. hello")

    def test_background_wait_returns_pending_and_opens_later(self) -> None:
        launcher = FakeLauncher([STUDIO])
        s = Setup(self, config={"terminal": "fake-term", "ideStartTimeoutSec": 0.3}, ides=launcher, sync_ms=20)
        pending = s.service.open_tab({"path": s.work, "ide": "android-studio"}, wait="background")
        self.assertEqual(
            pending,
            {
                "pending": True,
                "ide": "android-studio",
                "product": "Android Studio",
                "agent": "claude",
                "path": s.work,
                "reason": "started Android Studio; it is still loading",
                "note": "Android Studio is starting. The agent tab opens there once it loads, up to 0.3 s after the launch. "
                "If it doesn't load by then, the tab opens in the caller's IDE or terminal instead.",
            },
        )
        s.service.settled()
        self.assertEqual(s.logs, ["opened tab term-1 in Fake Terminal after starting Android Studio"])

    def test_a_named_ide_that_is_not_installed_falls_back(self) -> None:
        s = Setup(self)
        opened = s.service.open_tab({"path": s.work, "ide": "pycharm"})
        self.assertEqual(opened["note"], "PyCharm isn't installed, so the tab opened in Fake Terminal instead. hello")


class TabsTest(unittest.TestCase):
    def test_list_and_close_cover_ide_and_terminal_tabs(self) -> None:
        s = Setup(self)
        ide = fake_ide(self)
        ide.tabs = [{"id": "ide-a", "agent": "claude", "path": s.work, "ide": "ignored"}]
        ide.register(s.home, "jetbrains-1")
        s.service.open_tab({"path": s.work})
        s.service.open_tab({"path": s.work, "ide": "fake-term"})
        s.terminal.live = {"term-2"}
        listed = s.service.list_tabs()
        self.assertEqual(
            listed,
            {
                "tabs": [
                    {"id": "ide-a", "agent": "claude", "path": s.work, "ide": "jetbrains-1"},
                    {"id": "term-2", "agent": "claude", "path": s.work, "ide": "fake-term"},
                ]
            },
        )
        self.assertEqual([t["id"] for t in s.tabs_file()], ["term-2"])
        self.assertEqual(s.service.close_tab("term-2"), {"id": "term-2", "ide": "fake-term", "closed": True})
        self.assertEqual(s.service.close_tab("ide-a"), {"id": "ide-a", "ide": "jetbrains-1", "closed": True})
        with self.assertRaisesRegex(ToolError, "no open agent tab with id gone; list_tabs shows the open ones"):
            s.service.close_tab("gone")
        with self.assertRaisesRegex(ToolError, "no running IDE or terminal with id nope"):
            s.service.list_tabs("nope")

    def test_a_tab_closing_itself_answers_first(self) -> None:
        s = Setup(self, env={"PATH": "", "IDE_AGENT_TABS_ID": "term-1"})
        s.service.open_tab({"path": s.work})
        self.assertEqual(s.service.close_tab(), {"id": "term-1", "ide": "fake-term", "closing": True})
        deadline = time.time() + 5
        while not s.terminal.closed and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(s.terminal.closed, ["term-1"])

    def test_close_without_an_id_outside_a_tab_is_an_error(self) -> None:
        s = Setup(self)
        with self.assertRaisesRegex(ToolError, "no id given, and IDE_AGENT_TABS_ID is not set"):
            s.service.close_tab()

    def test_type_into_reaches_a_terminal_or_an_ide(self) -> None:
        s = Setup(self)
        ide = fake_ide(self)
        ide.register(s.home, "jetbrains-1")
        s.service.open_tab({"path": s.work})
        self.assertEqual(s.service.type_into("term-1", "fake-term", "wake"), {"ok": True})
        self.assertEqual(s.terminal.typed, [("term-1", "wake")])
        self.assertEqual(s.service.type_into("t", "jetbrains-1", "wake"), {"ok": True})
        self.assertEqual(ide.requests[-1], ("input", {"id": "t", "text": "wake"}))
        self.assertEqual(s.service.type_into("t", "gone", "x"), {"ok": False, "reason": "no running IDE or terminal with id gone"})


class AgentsTest(unittest.TestCase):
    def test_list_agents_marks_installed_commands_and_warnings(self) -> None:
        s = Setup(self)
        write(os.path.join(s.home, "agents.json"), "[]")
        listed = s.service.list_agents()
        self.assertEqual(listed["default"], "claude")
        self.assertEqual(
            listed["agents"][0], {"name": "claude", "label": "Claude Code", "command": "claude", "installed": False, "model": True}
        )
        self.assertEqual(listed["launchVia"], "direct")
        self.assertEqual(
            listed["warnings"],
            [f"Ignoring {os.path.join(s.home, 'agents.json')} and using the built-in agent profiles: agents.json must hold a JSON object"],
        )


class IdeClientTest(unittest.TestCase):
    def endpoint(self, ide: FakeIde) -> Any:
        home = temp_home(self)
        ide.register(home, "jetbrains-1")
        return read_registry(home).endpoints[0]

    def test_errors_carry_the_next_step(self) -> None:
        ide = fake_ide(self)
        ide.handlers["open"] = lambda body: (409, {"ok": False, "error": "busy"})
        ide.handlers["info"] = lambda body: (503, {"ok": False, "error": "modal"})
        ide.handlers["agents"] = lambda body: (200, {"ok": False, "error": "unknown agent: x"})
        call = ide_caller(2_000)
        e = self.endpoint(ide)
        with self.assertRaisesRegex(IdeError, r"jetbrains-1 open answered HTTP 409: busy\. To open the tab in a terminal instead"):
            call(e, "open", {})
        with self.assertRaisesRegex(IdeError, r"answered HTTP 503: modal\. A modal dialog is likely open in IntelliJ IDEA"):
            call(e, "info", None)
        with self.assertRaisesRegex(IdeError, r"answered HTTP 200: unknown agent: x\. Call list_agents for the profile names"):
            call(e, "agents", None)

    def test_non_json_and_chunked_replies(self) -> None:
        ide = fake_ide(self)
        ide.raw["info"] = (500, b"<html>" + b"x" * 600, {})
        chunks = [b'{"ok": true,', b' "tabs":[]', b"}"]
        body = b"".join(b"%x\r\n%s\r\n" % (len(c), c) for c in chunks) + b"0\r\n\r\n"
        ide.raw["list"] = (200, body, {"Transfer-Encoding": "chunked"})
        e = self.endpoint(ide)
        call = ide_caller(2_000)
        with self.assertRaises(IdeError) as caught:
            call(e, "info", None)
        self.assertEqual(str(caught.exception), "jetbrains-1 info answered HTTP 500 with non-JSON: <html>" + "x" * 494)
        self.assertEqual(caught.exception.status, 500)
        self.assertEqual(call(e, "list", None), {"ok": True, "tabs": []})

    def test_a_slow_ide_times_out_and_a_closed_port_fails(self) -> None:
        ide = fake_ide(self)
        ide.delay_s = 1.0
        e = self.endpoint(ide)
        with self.assertRaisesRegex(IdeError, r"jetbrains-1 info failed: no answer within 0\.2 s\. IntelliJ IDEA may be busy"):
            ide_caller(200)(e, "info", None)
        gone = e._replace(url="http://127.0.0.1:9/x")
        with self.assertRaisesRegex(IdeError, "jetbrains-1 info failed: fetch failed"):
            ide_caller(10_000)(gone, "info", None)

    def test_the_request_carries_the_token_and_a_json_body(self) -> None:
        ide = fake_ide(self, token="tok")
        e = self.endpoint(ide)
        ide_caller(2_000)(e, "close", {"id": "caf\u00e9"})
        self.assertEqual(ide.requests, [("close", {"id": "caf\u00e9"})])


class CliTest(unittest.TestCase):
    def test_list_ides_refuses_arguments(self) -> None:
        out, err = io.BytesIO(), io.BytesIO()
        self.assertEqual(cli_list_ides(["extra"], out, err), 1)
        self.assertEqual(
            json.loads(err.getvalue()),
            {"error": "list-ides takes no arguments. Usage: agent-tabs list-ides | jev <subcommand> | server status|stop [--port <port>]"},
        )
        self.assertEqual(out.getvalue(), b"")

    def test_list_ides_prints_pretty_json(self) -> None:
        home = temp_home(self)
        fresh_detection(home)
        out = io.BytesIO()
        previous = os.environ.get("IDE_AGENT_TABS_HOME")
        os.environ["IDE_AGENT_TABS_HOME"] = home
        try:
            self.assertEqual(cli_list_ides([], out, io.BytesIO()), 0)
        finally:
            if previous is None:
                del os.environ["IDE_AGENT_TABS_HOME"]
            else:
                os.environ["IDE_AGENT_TABS_HOME"] = previous
        text = out.getvalue().decode("utf-8")
        listed = json.loads(text)
        self.assertEqual(text, jsjson.stringify(listed, 2) + "\n")
        self.assertEqual(list(listed), ["ides", "terminals", "installed", "shells"])


if __name__ == "__main__":
    unittest.main()

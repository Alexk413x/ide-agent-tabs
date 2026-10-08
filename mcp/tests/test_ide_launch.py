from __future__ import annotations

import json
import os
import threading
import time
import unittest
from typing import Any, Callable

from ide_agent_tabs.clock import now_ms
from ide_agent_tabs.handoff import HandoffDeps, Handoffs, handoff_path
from ide_agent_tabs.ide_installs import IdeInstall
from ide_agent_tabs.registry import Endpoint
from ide_agent_tabs.service import IdeWait, Service, ServiceDeps, ToolError
from ide_agent_tabs.spec import LaunchSpec
from ide_agent_tabs.terminals.driver import OpenOptions, TerminalContext, TerminalDriver, TerminalTab, caps
from support import temp_home

STUDIO = IdeInstall("android-studio", "Android Studio", "jetbrains", "/opt/android-studio/bin/studio.sh", "2026.1")
CODE = IdeInstall("vscode", "VS Code", "vscode", "/usr/bin/code")


class Terminal(TerminalDriver):
    name = "fake-term"
    label = "Fake Terminal"
    capabilities = caps("tab", "yes", "yes")

    def __init__(self, ok: bool) -> None:
        self.ok = ok
        self.opens: list[str] = []

    def available(self, ctx: TerminalContext) -> bool:
        return self.ok

    def open(self, ctx: TerminalContext, spec: LaunchSpec, title: str, options: OpenOptions | None = None) -> TerminalTab:
        self.opens.append(spec.id)
        return {"id": spec.id, "terminal": self.name, "agent": spec.agent, "path": spec.cwd, "createdAt": now_ms()}

    def alive(self, ctx: TerminalContext, tabs: list[TerminalTab]) -> set[str]:
        return {t["id"] for t in tabs}

    def close(self, ctx: TerminalContext, tab: TerminalTab) -> None:
        pass


class Launcher:
    def __init__(self, setup: Setup, installs: list[IdeInstall], error: str | None, register: Callable[[str], None] | None) -> None:
        self.setup = setup
        self.installs = installs
        self.error = error
        self.register = register

    def discover(self) -> list[IdeInstall]:
        return self.installs

    def launch(self, install: IdeInstall, folder: str) -> None:
        self.setup.launches.append((install.key, folder))
        if self.error is not None:
            raise OSError(self.error)
        if self.register is not None:
            self.register(folder)


class Setup:
    def __init__(
        self,
        test: unittest.TestCase,
        installs: list[IdeInstall] | None = None,
        timeout_s: float = 0.4,
        caller_tab: str | None = None,
        terminal: bool = True,
        register: Callable[[str], None] | None = None,
        launch_error: str | None = None,
        sync_ms: float = 60,
    ) -> None:
        self.home = temp_home(test, "iat-launch-")
        self.work = temp_home(test, "iat-launch-w-")
        self.other = temp_home(test, "iat-launch-o-")
        os.mkdir(os.path.join(self.home, "endpoints"))
        with open(os.path.join(self.home, "config.json"), "w", encoding="utf-8") as f:
            json.dump({"ideStartTimeoutSec": timeout_s, "terminal": "fake-term"}, f)
        self.ides: dict[str, dict[str, Any]] = {}
        self.opens: list[tuple[str, dict[str, Any]]] = []
        self.launches: list[tuple[str, str]] = []
        self.logs: list[str] = []
        self.terminal = Terminal(terminal)
        self.lock = threading.Lock()
        self.count = 0
        env = {"PATH": ""}
        if caller_tab is not None:
            env["IDE_AGENT_TABS_ID"] = caller_tab
        self.service = Service(
            ServiceDeps(
                home=self.home,
                scripts_dir=self.home,
                platform="linux",
                env=env,
                call_ide=self.call_ide,
                drivers=[self.terminal],
                is_alive=lambda _pid: True,
                new_id=lambda: f"term-{self.next()}",
                ides=Launcher(self, installs or [], launch_error, register),
                ide_wait=IdeWait(10, sync_ms, 20),
                log=self.logs.append,
            )
        )

    def next(self) -> int:
        with self.lock:
            self.count += 1
            return self.count

    def add_ide(
        self, ide_id: str, product: str, projects: list[str], started_at: float | None = None, tabs: list[str] | None = None
    ) -> None:
        started = now_ms() if started_at is None else started_at
        self.ides[ide_id] = {"product": product, "projects": projects, "tabs": tabs or []}
        endpoint = {
            "protocol": 1,
            "ide": "jetbrains",
            "product": product,
            "version": "1",
            "pid": os.getpid(),
            "url": "http://127.0.0.1:9/ide-agent-tabs",
            "token": "t",
            "startedAt": started,
        }
        with open(os.path.join(self.home, "endpoints", f"{ide_id}.json"), "w", encoding="utf-8") as f:
            json.dump(endpoint, f)

    def call_ide(self, endpoint: Endpoint, route: str, body: Any = None) -> dict[str, Any]:
        ide = self.ides.get(endpoint.id)
        if ide is None:
            raise OSError(f"{endpoint.id} is gone")
        if route == "info":
            projects = [{"name": os.path.basename(p), "path": p, "focused": False} for p in ide["projects"]]
            return {"product": ide["product"], "projects": projects}
        if route == "list":
            return {"tabs": [{"id": t} for t in ide["tabs"]]}
        if route == "open":
            tab_id = f"tab-{self.next()}"
            self.opens.append((endpoint.id, body))
            ide["tabs"].append(tab_id)
            return {"id": tab_id, "agent": "claude", "project": "p", "path": body["path"]}
        return {}


def later(seconds: float, fn: Callable[[], None]) -> None:
    timer = threading.Timer(seconds, fn)
    timer.daemon = True
    timer.start()


class NamedIdeTest(unittest.TestCase):
    def test_a_named_ide_that_runs_takes_the_tab_the_one_whose_project_holds_the_path_else_the_most_recent(self) -> None:
        t = Setup(self, [STUDIO])
        t.add_ide("jetbrains-1", "Android Studio", [t.work], 1000)
        t.add_ide("jetbrains-2", "Android Studio", [t.other], 2000)
        t.add_ide("jetbrains-3", "IntelliJ IDEA", [t.work], 3000)
        near = t.service.open_tab({"path": t.work, "ide": "Android Studio"})
        self.assertEqual(near["ide"], "jetbrains-1")
        self.assertIn("Android Studio is running; an open project contains the path", near["reason"])
        far = t.service.open_tab({"path": os.path.dirname(t.work), "ide": "STUDIO"})
        self.assertEqual(far["ide"], "jetbrains-2")
        self.assertIn("most recently started", far["reason"])
        self.assertEqual(t.launches, [])
        self.assertEqual(t.service.open_tab({"path": t.work, "ide": "jetbrains-3"})["ide"], "jetbrains-3")

    def test_an_unknown_name_is_an_error_and_a_product_outside_the_catalog_matches_a_running_ide(self) -> None:
        t = Setup(self)
        with self.assertRaisesRegex(ToolError, "no running IDE or terminal with id notepad, and no IDE by that name"):
            t.service.open_tab({"path": t.work, "ide": "notepad"})
        t.add_ide("jetbrains-9", "Fake Studio", [t.work])
        self.assertEqual(t.service.open_tab({"path": t.work, "ide": "fake studio"})["ide"], "jetbrains-9")

    def test_an_installed_ide_starts_with_the_folder_and_the_tab_opens_once_a_new_endpoint_answers_with_a_project(self) -> None:
        holder: dict[str, Setup] = {}

        def register(folder: str) -> None:
            def endpoint_then_project() -> None:
                holder["t"].add_ide("jetbrains-new", "Android Studio", [])
                time.sleep(0.03)
                holder["t"].ides["jetbrains-new"].update(projects=[folder])

            later(0.02, endpoint_then_project)

        t = Setup(self, [CODE, STUDIO], timeout_s=5, register=register, sync_ms=3000)
        holder["t"] = t
        t.add_ide("jetbrains-old", "IntelliJ IDEA", [t.work])
        progress: list[float] = []
        opened = t.service.open_tab({"path": t.work, "ide": "android-studio"}, "background", lambda ms, *_: progress.append(ms))
        self.assertEqual((opened["ide"], opened["reason"]), ("jetbrains-new", "started Android Studio"))
        self.assertNotIn("pending", opened)
        self.assertEqual(t.launches, [("android-studio", t.work)])
        self.assertEqual(progress[0], 0)

    def test_a_slow_ide_returns_pending_at_once_and_a_second_call_waits_on_the_same_launch(self) -> None:
        holder: dict[str, Setup] = {}
        t = Setup(
            self,
            [STUDIO],
            timeout_s=2,
            register=lambda folder: later(0.6, lambda: holder["t"].add_ide("jetbrains-new", "Android Studio", [folder])),
        )
        holder["t"] = t
        result = t.service.open_tab({"path": t.work, "ide": "Android Studio", "prompt": "hi"}, "background")
        self.assertIs(result["pending"], True)
        self.assertEqual(result["product"], "Android Studio")
        self.assertNotIn("id", result)
        self.assertRegex(result["note"], r"Android Studio is starting\. The agent tab opens there once it loads, up to 2 s")
        self.assertEqual(t.opens, [])

        self.assertIs(t.service.open_tab({"path": t.work, "ide": "studio"}, "background")["pending"], True)
        self.assertEqual(len(t.launches), 1, "a second call waits on the same launch")

        t.service.settled()
        self.assertEqual([o[0] for o in t.opens], ["jetbrains-new", "jetbrains-new"])
        self.assertEqual(sorted(o[1].get("prompt", "") for o in t.opens), ["", "hi"], "each waiting call opens its own tab")
        self.assertRegex("\n".join(t.logs), r"opened tab tab-\d+ in Android Studio after starting Android Studio")


class FallbackTest(unittest.TestCase):
    def test_an_ide_that_never_registers_falls_back_in_the_background_to_the_callers_ide(self) -> None:
        t = Setup(self, [STUDIO], timeout_s=0.2, caller_tab="caller-tab")
        t.add_ide("vscode-caller", "Visual Studio Code", [t.other], tabs=["caller-tab"])
        result = t.service.open_tab({"path": t.work, "ide": "android studio"}, "background")
        self.assertIs(result["pending"], True)
        t.service.settled()
        self.assertEqual([o[0] for o in t.opens], ["vscode-caller"])
        self.assertRegex("\n".join(t.logs), "in Visual Studio Code after starting Android Studio")

    def test_a_full_wait_falls_back_with_the_reason_and_names_the_plugin_to_install(self) -> None:
        t = Setup(self, [STUDIO], timeout_s=0.1, caller_tab="caller-tab")
        t.add_ide("vscode-caller", "Visual Studio Code", [t.other], tabs=["caller-tab"])
        result = t.service.open_tab({"path": t.work, "ide": "android-studio"})
        self.assertEqual(result["ide"], "vscode-caller")
        self.assertRegex(
            result["reason"],
            r"\AAndroid Studio started but didn't register within 0\.1 s; if the Agent Tabs plugin isn't installed in it, "
            r"run /ide-agent-tabs:setup; the caller's IDE\Z",
        )
        self.assertRegex(result["note"], r"so the tab opened in Visual Studio Code instead\.\Z")

    def test_a_name_that_is_not_installed_opens_in_the_callers_ide_else_its_terminal_else_the_automatic_route(self) -> None:
        ide = Setup(self, [CODE], caller_tab="caller-tab")
        ide.add_ide("jetbrains-caller", "IntelliJ IDEA", [ide.other], tabs=["caller-tab"])
        in_ide = ide.service.open_tab({"path": ide.work, "ide": "Android Studio"})
        self.assertEqual(in_ide["ide"], "jetbrains-caller")
        self.assertEqual(in_ide["note"], "Android Studio isn't installed, so the tab opened in IntelliJ IDEA instead.")
        self.assertEqual(ide.launches, [])

        term = Setup(self, caller_tab="term-1")
        term.service.open_tab({"path": term.work, "ide": "fake-term"})
        in_terminal = term.service.open_tab({"path": term.work, "ide": "pycharm"})
        self.assertEqual(in_terminal["ide"], "fake-term")
        self.assertEqual(in_terminal["reason"], "PyCharm isn't installed; the caller's terminal")
        self.assertEqual(term.terminal.opens, ["term-1", "term-2"])

        plain = Setup(self)
        plain.add_ide("jetbrains-5", "IntelliJ IDEA", [plain.work])
        routed = plain.service.open_tab({"path": plain.work, "ide": "rider"})
        self.assertEqual(routed["ide"], "jetbrains-5")
        self.assertRegex(routed["reason"], r"\ARider isn't installed; open project .* contains the path\Z")

    def test_a_launch_that_fails_falls_back_at_once(self) -> None:
        t = Setup(self, [STUDIO], launch_error="spawn EACCES")
        result = t.service.open_tab({"path": t.work, "ide": "android-studio"}, "background")
        self.assertEqual(result["ide"], "fake-term")
        self.assertRegex(result["note"], r"\AAndroid Studio couldn't start: spawn EACCES, so the tab opened in Fake Terminal instead\.")

    def test_a_handoff_to_a_named_ide_waits_for_it_and_closes_nothing_when_no_tab_opens(self) -> None:
        t = Setup(self, [STUDIO], timeout_s=0.1, caller_tab="old-session", terminal=False)
        handoffs = Handoffs(
            HandoffDeps(
                home=t.home,
                env={"IDE_AGENT_TABS_ID": "old-session"},
                session_id=lambda: "old-session",
                open_tab=t.service.open_tab,
                find_host=t.service.find_host,
                random_id=lambda: "h-0123456789ab",
            )
        )
        with self.assertRaisesRegex(ToolError, "the new tab did not open, so this session keeps the work and nothing was closed"):
            handoffs.start({"path": t.work, "goal": "g", "ide": "android-studio"})
        self.assertFalse(os.path.exists(handoff_path(t.home, "h-0123456789ab", "json")))
        self.assertEqual(t.opens, [])

        holder: dict[str, Setup] = {}
        s = Setup(
            self,
            [STUDIO],
            timeout_s=2,
            register=lambda folder: later(0.15, lambda: holder["s"].add_ide("jetbrains-new", "Android Studio", [folder])),
        )
        holder["s"] = s
        result = Handoffs(
            HandoffDeps(
                home=s.home,
                env={},
                session_id=lambda: "old-session",
                open_tab=s.service.open_tab,
                find_host=s.service.find_host,
                random_id=lambda: "h-0123456789ab",
            )
        ).start({"path": s.work, "goal": "g", "ide": "Android Studio"})
        self.assertEqual(result["ide"], "jetbrains-new")
        self.assertEqual(len(s.opens), 1)


if __name__ == "__main__":
    unittest.main()

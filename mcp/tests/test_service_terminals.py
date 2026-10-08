from __future__ import annotations

import json
import ntpath
import os
import unittest
from typing import Any

from fake_ide import fake_ide
from ide_agent_tabs.clock import iso, now_ms, parse_iso
from ide_agent_tabs.detection import detect_ori
from ide_agent_tabs.processes import RunResult
from ide_agent_tabs.service import FRESH_TAB_START_MS, Service, ServiceDeps, ToolError
from ide_agent_tabs.spec import LaunchSpec
from ide_agent_tabs.terminals.driver import OpenOptions, TerminalContext, TerminalDriver, TerminalTab, caps
from ide_agent_tabs.terminals.powershell import ShellProbe
from support import temp_home
from test_service import Setup, write

MSI = "C:/Program Files/PowerShell/7/pwsh.exe"
STORE = "C:/Users/a/AppData/Local/Microsoft/WindowsApps/pwsh.exe"
CUSTOM = "D:/tools/pwsh.exe"
MSI_WIN = ntpath.normpath(MSI)


class ShellRecorder(TerminalDriver):
    name = "fake-term"
    label = "Fake Terminal"
    capabilities = caps("tab", "yes", "yes")

    def __init__(self) -> None:
        self.seen: list[str | None] = []

    def available(self, ctx: TerminalContext) -> bool:
        return True

    def open(self, ctx: TerminalContext, spec: LaunchSpec, title: str, options: OpenOptions | None = None) -> TerminalTab:
        self.seen.append(ctx.power_shell)
        return {"id": spec.id, "terminal": self.name, "agent": spec.agent, "path": spec.cwd, "createdAt": now_ms()}

    def alive(self, ctx: TerminalContext, tabs: list[TerminalTab]) -> set[str]:
        return set()

    def close(self, ctx: TerminalContext, tab: TerminalTab) -> None:
        pass


class WindowsShells:
    def __init__(self, test: unittest.TestCase, files: list[str]) -> None:
        self.home = temp_home(test, "iat-shell-")
        self.work = temp_home(test, "iat-shell-w-")
        self.ran: list[str] = []
        self.driver = ShellRecorder()
        ids = iter(f"tab-{i}" for i in range(1, 100))

        def version(exe: str) -> str:
            self.ran.append(exe)
            return "7.5.2"

        def probe(run_shells: bool) -> ShellProbe:
            return ShellProbe(
                {"ProgramFiles": "C:\\Program Files", "PATH": ""},
                lambda f: f in files,
                lambda d: ["7"] if d.lower().endswith("powershell") else None,
                lambda _f: None,
                lambda _f: 0,
                version if run_shells else None,
            )

        self.service = Service(
            ServiceDeps(
                home=self.home,
                scripts_dir=self.home,
                platform="win32",
                env={"PATH": ""},
                call_ide=lambda *_a: {},
                drivers=[self.driver],
                new_id=lambda: next(ids),
                shell_probe=probe,
            )
        )


class PowerShellChoiceTest(unittest.TestCase):
    def test_a_windows_terminal_tab_gets_the_power_shell_from_detection_the_setting_or_a_quick_look(self) -> None:
        s = WindowsShells(self, [MSI_WIN, STORE, CUSTOM])
        s.service.open_tab({"path": s.work, "ide": "fake-term"})
        self.assertEqual(s.driver.seen[-1], MSI_WIN)
        self.assertEqual(s.ran, [], "a tab launch never runs a shell")

        shells = [{"path": STORE, "label": "PowerShell 7.6.0 (Store)", "version": "7.6.0", "source": "store"}]
        write(
            os.path.join(s.home, "detected.json"),
            {"version": 1, "detectedAt": iso(now_ms()), "platform": "win32", "terminals": [], "shells": shells},
        )
        s.service.open_tab({"path": s.work, "ide": "fake-term"})
        self.assertEqual(s.driver.seen[-1], STORE)

        write(os.path.join(s.home, "config.json"), {"shell": CUSTOM})
        s.service.open_tab({"path": s.work, "ide": "fake-term"})
        self.assertEqual(s.driver.seen[-1], CUSTOM)

    def test_refresh_detection_writes_the_terminals_and_shells_running_each_shell_once_at_most(self) -> None:
        s = WindowsShells(self, [MSI_WIN])
        detection = s.service.refresh_detection()
        self.assertEqual(detection["terminals"], [{"id": "fake-term", "name": "Fake Terminal"}])
        self.assertEqual(detection["shells"], [{"path": MSI_WIN, "label": "PowerShell 7.5.2 (MSI)", "version": "7.5.2", "source": "msi"}])
        with open(os.path.join(s.home, "detected.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f), detection)
        self.assertEqual(len(s.ran), 1)
        s.service.refresh_detection()
        self.assertEqual(len(s.ran), 1, "an unchanged shell keeps its detected version")


class OriDetectionTest(unittest.TestCase):
    def test_ori_detection_keeps_only_the_agents_ori_can_launch_and_a_missing_ori_is_none(self) -> None:
        calls: list[list[str]] = []

        def runner(_exe: str, args: list[str]) -> RunResult:
            calls.append(args)
            launchable: list[dict[str, Any]] = [
                {"kind": "claude", "installed": True},
                {"kind": "grok", "installed": False},
                {"kind": "codex", "installed": True},
            ]
            data: dict[str, Any] = {"version": "0.14.3+6e62568"} if args[0] == "--version" else {"launchable": launchable}
            return RunResult(0, json.dumps({"ok": True, "data": data}), "")

        self.assertEqual(
            detect_ori("C:\\bin\\ori.exe", runner), {"path": "C:\\bin\\ori.exe", "version": "0.14.3", "agents": ["claude", "codex"]}
        )
        self.assertEqual(sorted(" ".join(a) for a in calls), ["--version --json", "harness list --json"])
        self.assertIsNone(detect_ori(None, runner))
        self.assertEqual(detect_ori("ori", lambda _e, _a: RunResult(1, "not json", "")), {"path": "ori", "version": "", "agents": []})


class TerminalRoutingTest(unittest.TestCase):
    def test_tab_routing_caller_opens_a_terminal_callers_tab_in_its_own_window_even_when_an_ide_has_the_project(self) -> None:
        s = Setup(self, env={"PATH": "", "IDE_AGENT_TABS_ID": "term-1"})
        ide = fake_ide(self, projects=[{"name": "app", "path": s.work, "focused": True}])
        ide.register(s.home, "jetbrains-1")
        caller = s.service.open_tab({"path": s.other, "ide": "fake-term"})
        self.assertEqual(caller["id"], "term-1")

        self.assertEqual(s.service.open_tab({"path": s.work, "prompt": "p"})["ide"], "jetbrains-1", "the default keeps project routing")

        write(os.path.join(s.home, "config.json"), {"terminal": "fake-term", "tabRouting": "caller", "terminalWindow": "dedicated"})
        by_caller = s.service.open_tab({"path": s.work, "prompt": "c"})
        self.assertEqual((by_caller["ide"], by_caller["reason"]), ("fake-term", "tabRouting is caller; the caller's terminal window"))
        options = s.terminal.opened[-1][2]
        assert options is not None and options.near is not None
        self.assertEqual((options.window, options.near["id"]), ("dedicated", "term-1"))

        named = s.service.open_tab({"path": s.work, "prompt": "n", "ide": "jetbrains-1"})
        self.assertEqual(named["ide"], "jetbrains-1", "a named ide wins over tabRouting")

    def test_open_tab_passes_a_model_and_via_to_the_ide_and_builds_the_model_flag_into_a_terminal_launch(self) -> None:
        s = Setup(self)
        ide = fake_ide(self, projects=[{"name": "app", "path": s.work, "focused": True}])
        ide.register(s.home, "jetbrains-1")
        s.service.open_tab({"path": s.work, "agent": "codex", "model": "gpt-5.5", "via": "direct"})
        route, body = ide.requests[-1]
        self.assertEqual(route, "open")
        self.assertEqual(body, {"path": s.work, "agent": "codex", "model": "gpt-5.5", "via": "direct", "focus": False})

        s.service.open_tab({"path": s.work, "agent": "claude", "model": "opus", "ide": "fake-term"})
        self.assertEqual(s.terminal.opened[-1][0].args[:2], ["--model", "opus"])
        with self.assertRaisesRegex(ToolError, "claude can't launch through Ori: Ori isn't installed"):
            s.service.open_tab({"path": s.work, "agent": "claude", "via": "ori", "ide": "fake-term"})
        with self.assertRaisesRegex(ToolError, "model must match"):
            s.service.open_tab({"path": s.work, "model": "two words"})

    def test_a_tab_opened_without_a_prompt_counts_as_idle_once_it_has_had_time_to_start(self) -> None:
        s = Setup(self)
        ide = fake_ide(self, product="Fake Studio", projects=[{"name": "proj", "path": s.work, "focused": True}])
        ide.register(s.home, "jetbrains-1")
        prompted = s.service.open_tab({"path": s.work, "prompt": "go", "model": "claude-opus-5-5"})
        self.assertEqual(
            s.presence.files[prompted["id"]],
            {"id": prompted["id"], "via": "direct", "project": "app", "model": "claude-opus-5-5", "product": "Fake Studio"},
            "a tab with a prompt reports its state itself and keeps the model and IDE product it opened with",
        )
        before = now_ms()
        fresh = s.service.open_tab({"path": s.work})
        presence = s.presence.files[fresh["id"]]
        self.assertEqual((presence["state"], presence["host"]), ("idle", "jetbrains-1"))
        self.assertNotIn("model", presence)
        self.assertGreaterEqual(parse_iso(presence["stateAt"]) or 0, before + FRESH_TAB_START_MS)


if __name__ == "__main__":
    unittest.main()

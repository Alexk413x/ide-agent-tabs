from __future__ import annotations

import unittest
from typing import Any, Callable

from ide_agent_tabs.terminals import ghostty, iterm2, kitty, tmux, wezterm
from ide_agent_tabs.terminals import windows_terminal as wt
from ide_agent_tabs.terminals.driver import TerminalContext
from test_ide_parity import ADAPTERS, DATA, SPECIAL, launch_spec, login, options, outcome


def ghostty_place_in(place: dict[str, Any] | None) -> ghostty.GhosttyPlace | None:
    if place is None:
        return None
    return ghostty.NearTab(place["nearTab"]) if "nearTab" in place else ghostty.Dedicated(place.get("dedicated"))


def ghostty_place_out(place: ghostty.GhosttyPlace | None) -> Any:
    if place is None:
        return None
    if isinstance(place, ghostty.NearTab):
        return {"nearTab": place.near_tab}
    return {} if place.dedicated is None else {"dedicated": place.dedicated}


def kitty_place_in(place: dict[str, Any] | None) -> kitty.KittyPlace | None:
    if place is None:
        return None
    return kitty.OsWindow() if place.get("osWindow") else kitty.InWindow(place["windowId"])


def kitty_place_out(place: kitty.KittyPlace | None) -> Any:
    if place is None:
        return None
    return {"osWindow": True} if isinstance(place, kitty.OsWindow) else {"windowId": place.window_id}


def wezterm_place_in(place: dict[str, Any] | None) -> wezterm.WeztermPlace | None:
    if place is None:
        return None
    if "paneId" in place:
        return wezterm.PanePlace(place["paneId"])
    if "windowId" in place:
        return wezterm.WindowPlace(place["windowId"])
    return wezterm.NewWindow()


def wezterm_place_out(place: wezterm.WeztermPlace | None) -> Any:
    if isinstance(place, wezterm.PanePlace):
        return {"paneId": place.pane_id}
    if isinstance(place, wezterm.WindowPlace):
        return {"windowId": place.window_id}
    return {"newWindow": True}


def tmux_target_in(t: dict[str, Any]) -> tmux.TmuxTarget:
    if "after" in t:
        return tmux.After(t["after"], t["socket"])
    if "session" in t:
        return tmux.InSession(t["session"], t["detached"])
    return tmux.NewSession(t["newSession"])


def tmux_target_out(t: tmux.TmuxTarget) -> dict[str, Any]:
    if isinstance(t, tmux.After):
        return {"after": t.after, "socket": t.socket}
    if isinstance(t, tmux.InSession):
        return {"session": t.session, "detached": t.detached}
    return {"newSession": t.new_session}


def kitty_target(o: Any, address: str, near: list[str] | None, remembered: Any, osw: list[list[Any]] | None) -> dict[str, Any]:
    t = kitty.plan_kitty_place(
        options(o), address, set(near) if near is not None else None, remembered, {k: v for k, v in osw} if osw else None
    )
    out: dict[str, Any] = {"address": t.address}
    if t.place is not None:
        out["place"] = kitty_place_out(t.place)
    return out


def wezterm_targets(o: Any, near: Any, remembered: Any, panes: Any) -> list[dict[str, Any]]:
    targets = wezterm.plan_wezterm_targets(options(o), dict(near) if near else None, remembered, dict(panes) if panes else None)
    out = []
    for t in targets:
        row: dict[str, Any] = {}
        if t.socket is not None:
            row["socket"] = t.socket
        if t.place is not None:
            row["place"] = wezterm_place_out(t.place)
        row["remember"] = t.remember
        out.append(row)
    return out


def tmux_sessions(text: str) -> list[dict[str, Any]]:
    return [{"attached": s.attached, "lastAttached": s.last_attached, "id": s.id, "name": s.name} for s in tmux.parse_tmux_sessions(text)]


def tmux_window(text: str) -> dict[str, Any]:
    w = tmux.parse_tmux_window(text)
    return {"windowId": w.window_id, "sessionId": w.session_id, "serverPid": w.server_pid, "socket": w.socket}


def open_answer(text: str) -> dict[str, Any]:
    a = iterm2.parse_open_answer(text)
    return {"sessionId": a.session_id, **({"windowId": a.window_id} if a.window_id is not None else {})}


def open_result(text: str) -> dict[str, Any]:
    r = ghostty.parse_open_result(text)
    return {"tabId": r.tab_id, "terminalId": r.terminal_id, **({"windowId": r.window_id} if r.window_id is not None else {})}


TERMINAL_ADAPTERS: dict[str, Callable[..., Any]] = {
    "wtTitle": wt.wt_title,
    "wtWindow": lambda o: wt.wt_window(options(o)),
    "powerShellArgv": wt.power_shell_argv,
    "wtArgs": wt.wt_args,
    "parseTasklist": lambda csv: [[k, v] for k, v in wt.parse_tasklist(csv).items()],
    "ghosttyCapabilities": ghostty.ghostty_capabilities,
    "ghosttyLinuxArgs": lambda cwd, s: ghostty.ghostty_linux_args(cwd, login(s)),
    "appleScriptString": ghostty.apple_script_string,
    "openScript": lambda command, env, place, keep=False: ghostty.open_script(command, env, ghostty_place_in(place), bool(keep)),
    "ghosttyPlace": lambda o, r: ghostty_place_out(ghostty.ghostty_place(options(o), r)),
    "listScript": ghostty.list_script,
    "closeScript": ghostty.close_script,
    "inputScript": ghostty.input_script,
    "parseOpenResult": open_result,
    "kittyLaunchArgs": lambda a, cwd, title, launcher, sp, argv, place, focus: kitty.kitty_launch_args(
        a, cwd, title, launcher, sp, argv, kitty_place_in(place), focus
    ),
    "kittySpawnArgs": kitty.kitty_spawn_args,
    "kittyInputCalls": lambda a, w, t: [c._asdict() for c in kitty.kitty_input_calls(a, w, t)],
    "parseKittyWindowId": kitty.parse_kitty_window_id,
    "parseKittyOsWindows": lambda s: [[k, v] for k, v in kitty.parse_kitty_os_windows(s).items()],
    "planKittyPlace": kitty_target,
    "weztermCliArgs": wezterm.wezterm_cli_args,
    "weztermSpawnArgs": lambda cwd, argv, place: wezterm.wezterm_spawn_args(cwd, argv, wezterm_place_in(place)),
    "weztermStartArgs": wezterm.wezterm_start_args,
    "weztermInputArgs": lambda p, t: list(wezterm.wezterm_input_args(p, t)),
    "parsePaneId": wezterm.parse_pane_id,
    "parseWeztermPaneWindows": lambda s: [[k, v] for k, v in wezterm.parse_wezterm_pane_windows(s).items()],
    "planWeztermTargets": wezterm_targets,
    "tmuxTitle": tmux.tmux_title,
    "tmuxOpenArgs": lambda t, title, launcher, sp, argv, focus: tmux.tmux_open_args(tmux_target_in(t), title, launcher, sp, argv, focus),
    "tmuxInputArgs": lambda s, w, t: list(tmux.tmux_input_args(s, w, t)),
    "parseTmuxSessions": tmux_sessions,
    "planTmuxTarget": lambda s: tmux_target_out(tmux.plan_tmux_target(tmux.parse_tmux_sessions(s))),
    "planDedicatedTmuxTarget": lambda s: tmux_target_out(tmux.plan_dedicated_tmux_target(tmux.parse_tmux_sessions(s))),
    "parseTmuxWindow": tmux_window,
    "parseTmuxWindowList": lambda s: {"serverPid": (r := tmux.parse_tmux_window_list(s)).server_pid, "windows": sorted(r.windows)},
    "iterm2Command": iterm2.iterm2_command,
    "iterm2Placement": lambda o, r: iterm2.iterm2_placement(options(o), r),
    "parseOpenAnswer": open_answer,
    "classifyOsascriptError": iterm2.classify_osascript_error,
    "osascriptErrorMessage": iterm2.osascript_error_message,
}


class TerminalParityTest(unittest.TestCase):
    maxDiff = None

    def test_every_fixture_key_has_an_adapter(self) -> None:
        self.assertEqual(sorted(set(DATA) - set(ADAPTERS) - set(TERMINAL_ADAPTERS) - {"iterm2Scripts", "iterm2Opens"} - SPECIAL), [])

    def test_argv_builders_match_the_typescript_build(self) -> None:
        for name, fn in TERMINAL_ADAPTERS.items():
            for case in DATA[name]:
                with self.subTest(name=name, args=case["args"]):
                    expected = {"error": case["error"]} if "error" in case else {"result": case["result"]}
                    self.assertEqual(outcome(fn, case["args"]), expected)

    def test_iterm2_scripts_match(self) -> None:
        expected = DATA["iterm2Scripts"][0]["result"]
        self.assertEqual(iterm2.OPEN_SCRIPT, expected["OPEN_SCRIPT"])
        self.assertEqual(iterm2.LIST_SCRIPT, expected["LIST_SCRIPT"])
        self.assertEqual(iterm2.CLOSE_SCRIPT, expected["CLOSE_SCRIPT"])
        self.assertEqual(iterm2.INPUT_SCRIPT, expected["INPUT_SCRIPT"])

    def test_iterm2_open_sends_the_same_osascript_calls(self) -> None:
        for expected in DATA["iterm2Opens"][0]["result"]:
            calls: list[dict[str, Any]] = []
            remembered: list[Any] = []

            def osascript(script: str, args: list[str], calls: list[dict[str, Any]] = calls) -> Any:
                calls.append({"script": "OPEN" if script == iterm2.OPEN_SCRIPT else script, "args": args})
                return iterm2.RunResult(0, "w0t1p0:XYZ\n77\n", "")

            driver = iterm2.create_iterm2(
                platform="darwin",
                find_app=lambda: "/Applications/iTerm.app",
                osascript=osascript,
                write_spec=lambda _file, _content: None,
                remove_spec=lambda _file: None,
                read_window=lambda _home: {"id": "5"},
                remember_window=lambda _home, w, remembered=remembered: remembered.append(w),
            )
            ctx = TerminalContext("/h", "/p/launch", "", {"SHELL": "/bin/zsh"})
            sp = launch_spec(DATA["posixSpec"][0]["args"][0])
            tab = driver.open(ctx, sp, "Claude Code", options(expected["options"]))
            tab["createdAt"] = 0
            with self.subTest(options=expected["options"]):
                self.assertEqual(calls, expected["calls"])
                self.assertEqual(remembered, expected["remembered"])
                self.assertEqual(tab, expected["tab"])


if __name__ == "__main__":
    unittest.main()

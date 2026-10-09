from __future__ import annotations

import json
import math
import os
import threading
import time
import unittest
from typing import Any, Callable

from ide_agent_tabs import closed
from ide_agent_tabs.clock import iso, now_ms
from ide_agent_tabs.closed import TranscriptDirs
from ide_agent_tabs.messaging import sessions, store
from ide_agent_tabs.messaging.codex_config import parse_codex_config
from ide_agent_tabs.messaging.db import MailError
from ide_agent_tabs.messaging.hook import run_hook
from ide_agent_tabs.messaging.messaging import (
    AGY_MAX_WAIT_S,
    Messaging,
    MessagingDeps,
    folder_slug,
    harness_of,
    session_names,
    short_names,
)
from ide_agent_tabs.messaging.notice import unread_reminder, wake_line
from ide_agent_tabs.scheduler import Scheduler
from support import temp_home

THREAD = "019a2b3c-4d5e-7f60-8a9b-0c1d2e3f4a5b"
FULL_ID = "f99f0a1b-2222-4333-8444-555566667777"


class FakeHosts:
    def __init__(self, host: str | None = None, ok: bool = True, labels: dict[str, str] | None = None) -> None:
        self.host = host
        self.ok = ok
        self.labels = labels or {}
        self.typed: list[tuple[str, str, str]] = []

    def find_host(self, tab_id: str) -> str | None:
        return self.host

    def type_into(self, tab_id: str, host: str, text: str) -> dict[str, Any]:
        self.typed.append((tab_id, host, text))
        return {"ok": True} if self.ok else {"ok": False, "reason": "no input here"}

    def describe_host(self, host: str) -> str | None:
        return self.labels.get(host)


class ThrowingHosts(FakeHosts):
    def type_into(self, tab_id: str, host: str, text: str) -> dict[str, Any]:
        raise RuntimeError("no input here")


class MovingHosts(FakeHosts):
    def type_into(self, tab_id: str, host: str, text: str) -> dict[str, Any]:
        if host != self.host:
            self.typed.append((tab_id, host, text))
            return {"ok": False, "reason": f"no running IDE or terminal with id {host}"}
        return super().type_into(tab_id, host, text)


class Clock:
    def __init__(self, start: float) -> None:
        self.at = start

    def __call__(self) -> float:
        return self.at


def session(
    test: unittest.TestCase,
    home: str,
    tab_id: str | None,
    agent: str,
    pid: int,
    hosts: FakeHosts | None = None,
    start: bool = True,
    **over: Any,
) -> Messaging:
    scheduler = Scheduler()
    test.addCleanup(scheduler.stop)
    env = {} if tab_id is None else {"IDE_AGENT_TABS_ID": tab_id, "IDE_AGENT_TABS_AGENT": agent}
    deps = {
        "home": home,
        "env": env,
        "pid": pid,
        "cwd": f"/w/{tab_id or 'x'}",
        "hosts": hosts if hosts is not None else FakeHosts(),
        "is_alive": lambda _pid: True,
        "scheduler": scheduler,
        **over,
    }
    messaging = Messaging(MessagingDeps(**deps))
    test.addCleanup(messaging.stop_sync)
    if start:
        messaging.start()
    return messaging


def pair(test: unittest.TestCase, home: str, hosts: FakeHosts | None = None, **over: Any) -> tuple[Messaging, Messaging]:
    a = session(test, home, "tab-a", "codex", 1, hosts, **over)
    b = session(test, home, "tab-b", "claude", 2)
    return a, b


def patch(home: str, tab_id: str, **fields: Any) -> None:
    sessions.update_presence(home, tab_id, lambda p: p if p is None else {**p, **fields})


def until(done: Callable[[], object], seconds: float) -> bool:
    end = time.monotonic() + seconds
    while not done() and time.monotonic() < end:
        time.sleep(0.01)
    return bool(done())


def out(to: str, text: str, sender: str = "tab-a", **over: Any) -> dict[str, Any]:
    return {"from": {"id": sender, "agent": "codex", "path": f"/w/{sender}"}, "to": to, "text": text, **over}


def unread_texts(home: str, tab_id: str) -> list[str]:
    return [m["text"] for m in store.peek_unread(home, tab_id)]


def in_thread(work: Callable[[], Any]) -> tuple[threading.Thread, dict[str, Any]]:
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["value"] = work()
        except BaseException as e:  # noqa: BLE001
            box["error"] = e

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, box


class NamesTest(unittest.TestCase):
    def test_maps_mcp_client_names_to_agent_names(self) -> None:
        cases = {
            "claude-code": "claude",
            "codex-mcp-client": "codex",
            "gemini-cli-mcp-client": "gemini",
            "github-copilot-cli": "copilot",
            "opencode": "opencode",
            "antigravity-client": "agy",
            "grok-shell-ide-agent-tabs": "grok",
            "qwen-cli-mcp-client-ide-agent-tabs": "qwen",
            "goose-cli": "goose",
            "pi": "pi",
            "pipeline": "pipeline",
            "My Agent!": "MyAgent",
        }
        for client, agent in cases.items():
            with self.subTest(client=client):
                self.assertEqual(sessions.agent_from_client(client), agent)
        self.assertEqual(sessions.agent_from_client(None), "unknown")

    def test_the_wake_line_and_the_reminder_hold_only_sanitized_names_and_short_ids(self) -> None:
        self.assertEqual(wake_line("codex", "0123456789abcdef"), "Agent Tabs: new message from codex 01234567. Call read_messages.")
        self.assertEqual(
            wake_line("x; rm -rf ~\r\n", "s-00ff00ff00ff"), "Agent Tabs: new message from xrm-rf s-00ff00. Call read_messages."
        )
        self.assertIsNone(unread_reminder([]))
        two = [
            {"from": {"id": "tab-a", "agent": "codex", "path": "/w"}},
            {"from": {"id": "s-1234567890", "agent": "gemini", "path": "/"}},
        ]
        self.assertEqual(
            unread_reminder(two), "Agent Tabs: 2 unread messages from codex tab-a, gemini s-123456. read_messages returns them."
        )
        self.assertEqual(unread_reminder(two[:1]), "Agent Tabs: 1 unread message from codex tab-a. read_messages returns it.")

    def test_short_names_take_four_id_characters_after_the_agent_and_more_only_on_a_collision(self) -> None:
        names = short_names(
            [
                {"id": FULL_ID, "agent": "codex"},
                {"id": "codex-019a2b3c-dead-beef", "agent": "codex"},
                {"id": "codex-019a2b9f-dead-beef", "agent": "codex"},
                {"id": "s-019a2b3c4d5e", "agent": "claude"},
                {"id": "0bad", "agent": "agy"},
            ]
        )
        self.assertEqual(list(names.values()), ["codex-f99f", "codex-019a2b3", "codex-019a2b9", "claude-019a", "agy-0bad"])

    def test_a_short_name_is_the_full_id_when_two_ids_of_one_agent_share_every_character_or_hold_none(self) -> None:
        names = short_names([{"id": "codex-abcd", "agent": "codex"}, {"id": "s-abcd", "agent": "codex"}, {"id": "---", "agent": "codex"}])
        self.assertEqual(names, {"codex-abcd": "codex-abcd", "s-abcd": "s-abcd", "---": "---"})

    def test_names_follow_the_native_style_the_folder_slug_and_two_id_hex_characters_longer_only_on_a_collision(self) -> None:
        self.assertEqual(folder_slug("C:\\Users\\me\\Projects\\Plugins"), "plugins")
        self.assertEqual(folder_slug("/home/me/The Index (old)/"), "the-index--old")
        self.assertEqual(folder_slug("/home/me/a-very-long-folder-name-that-goes-on"), "a-very-long-folder-name")
        self.assertEqual(folder_slug("/"), "session")
        self.assertEqual(folder_slug("/x/a\U0001f600b"), "a--b")
        names = session_names(
            [
                {"id": "tab-1", "agent": "claude", "path": "/p/plugins", "nativeName": "plugins-82"},
                {"id": "s-82aa00000000", "agent": "codex", "path": "/q/Plugins"},
                {"id": "codex-c66c0000-dead", "agent": "codex", "path": "/p/the-index"},
                {"id": "c6d70000-1111", "agent": "agy", "path": "/p/the-index"},
                {"id": "45f20000-2222", "agent": "claude", "path": "/p/calc"},
            ]
        )
        self.assertEqual(list(names.values()), ["plugins-82", "plugins-82a", "the-index-c66", "the-index-c6d", "calc-45"])
        again = session_names([{"id": "45f20000-2222", "agent": "claude", "path": "/p/calc"}])
        self.assertEqual(again["45f20000-2222"], "calc-45")

    def test_a_native_name_only_counts_for_claude(self) -> None:
        names = session_names([{"id": "tab-2", "agent": "codex", "path": "/p/app", "nativeName": "plugins-82"}])
        self.assertEqual(names["tab-2"], "app-ab")

    def test_harness_names_the_builtin_label_and_the_openrouter_route(self) -> None:
        self.assertEqual(harness_of("claude"), "Claude Code")
        self.assertEqual(harness_of("codex", "ori"), "Codex via OpenRouter")
        self.assertEqual(harness_of("codex", "direct"), "Codex")
        self.assertEqual(harness_of("zed-agent"), "zed-agent")


class EffectiveStateTest(unittest.TestCase):
    def test_a_waking_session_counts_as_idle_again_once_the_wake_line_had_time_to_start_a_turn(self) -> None:
        at = 1_790_000_000_000
        waking = {"state": "waking", "stateAt": iso(at)}
        self.assertEqual(sessions.effective_state(waking, at + sessions.WAKE_TIMEOUT_MS - 1), "waking")
        self.assertEqual(sessions.effective_state(waking, at + sessions.WAKE_TIMEOUT_MS), "idle")
        self.assertEqual(sessions.effective_state(waking, at - 1), "idle")
        self.assertEqual(sessions.effective_state({"state": "waking"}, at), "idle")
        busy = {"state": "busy", "stateAt": iso(at)}
        self.assertEqual(sessions.effective_state(busy, at + sessions.BUSY_STALE_MS - 1), "busy")
        self.assertEqual(sessions.effective_state(busy, at + sessions.BUSY_STALE_MS), "idle")
        permission = {"state": "permission", "stateAt": busy["stateAt"]}
        self.assertEqual(sessions.effective_state(permission, at + 10 * sessions.BUSY_STALE_MS), "permission")
        self.assertEqual(sessions.effective_state({}, at), "unknown")


class PresenceTest(unittest.TestCase):
    def test_presence_files_are_written_at_start_stale_ones_ignored_and_removed_and_deleted_by_their_own_server_only(self) -> None:
        home = temp_home(self)
        alive_pids = {100, 200}

        def alive(pid: int) -> bool:
            return pid in alive_pids

        a = session(self, home, "tab-a", "codex", 100, is_alive=alive)
        with open(sessions.presence_path(home, "tab-a"), encoding="utf-8") as f:
            presence = json.load(f)
        presence.pop("startedAt")
        self.assertEqual(
            presence,
            {"id": "tab-a", "agent": "codex", "path": "/w/tab-a", "pid": 100, "state": "unknown", "beatMs": 60_000, "mail": 2},
        )

        child = session(self, home, "tab-a", "codex", 200, is_alive=alive, random_id=lambda: "s-child0000001")
        self.assertEqual(child.id, "s-child0000001")

        loose = session(self, home, None, "", 200, is_alive=alive, random_id=lambda: "s-loose0000001")
        self.assertEqual(loose.id, "s-loose0000001")
        loose.set_client("gemini-cli-mcp-client")
        self.assertEqual((sessions.read_presence(home, loose.id) or {}).get("agent"), "gemini")
        a.set_client("gemini-cli-mcp-client")
        self.assertEqual((sessions.read_presence(home, "tab-a") or {}).get("agent"), "codex")

        sessions.update_presence(
            home, "dead", lambda _: {"id": "dead", "agent": "x", "path": "/", "pid": 999, "startedAt": "t", "state": "idle"}
        )
        sessions.update_presence(home, "stub", lambda _: {"id": "stub", "state": "idle"})
        live = sessions.live_sessions(home, alive)
        self.assertEqual(sorted(s["id"] for s in live), ["s-child0000001", "s-loose0000001", "tab-a"])
        self.assertFalse(os.path.exists(sessions.presence_path(home, "dead")))
        self.assertTrue(os.path.exists(sessions.presence_path(home, "stub")))

        stubs = temp_home(self)
        sessions.update_presence(stubs, "stub", lambda _: {"id": "stub", "state": "idle"})
        sessions.live_sessions(stubs, alive, now_ms() + 2 * 60 * 60 * 1000)
        self.assertFalse(os.path.exists(sessions.presence_path(stubs, "stub")))

        child.stop_sync()
        self.assertTrue(os.path.exists(sessions.presence_path(home, "tab-a")))
        a.stop_sync()
        self.assertFalse(os.path.exists(sessions.presence_path(home, "tab-a")))

    def test_set_client_without_a_name_records_unknown(self) -> None:
        home = temp_home(self)
        loose = session(self, home, None, "", 1, random_id=lambda: "s-loose0000002")
        loose.set_client("codex-mcp-client")
        loose.set_client(None)
        self.assertEqual((sessions.read_presence(home, loose.id) or {}).get("agent"), "unknown")

    def test_an_agent_variable_that_is_not_a_name_is_ignored(self) -> None:
        home = temp_home(self)
        m = session(self, home, "tab-a", "${IDE_AGENT_TABS_AGENT}", 1)
        self.assertEqual(m.agent, "unknown")
        m.set_client("claude-code")
        self.assertEqual(m.agent, "claude")

    def test_a_server_keeps_the_state_a_hook_wrote_before_it_started(self) -> None:
        home = temp_home(self)
        sessions.update_presence(home, "tab-a", lambda _: {"id": "tab-a", "state": "idle", "stateAt": "then", "nudges": 2})
        session(self, home, "tab-a", "codex", 100)
        p = sessions.read_presence(home, "tab-a") or {}
        self.assertEqual([p.get("state"), p.get("stateAt"), p.get("nudges"), p.get("pid")], ["idle", "then", 2, 100])

    def test_a_restarted_server_forgets_the_busy_state_and_nudges_its_dead_predecessor_left_behind(self) -> None:
        home = temp_home(self)
        old = iso(now_ms() - 5 * 60_000)
        sessions.update_presence(
            home,
            "tab-b",
            lambda _: {
                "id": "tab-b",
                "agent": "claude",
                "path": "/b",
                "pid": 7,
                "startedAt": old,
                "state": "busy",
                "stateAt": old,
                "nudges": 3,
                "owner": "gone",
            },
        )
        session(self, home, "tab-b", "codex", 8, is_alive=lambda pid: pid != 7)
        p = sessions.read_presence(home, "tab-b") or {}
        self.assertEqual(p.get("state"), "unknown")
        self.assertNotIn("nudges", p)
        self.assertNotIn("owner", p)

    def test_a_restarted_server_keeps_a_state_its_own_agent_set_just_before_it_started(self) -> None:
        home = temp_home(self)
        now = iso(now_ms())
        sessions.update_presence(
            home,
            "tab-b",
            lambda _: {"id": "tab-b", "agent": "claude", "path": "/b", "pid": 7, "startedAt": now, "state": "busy", "stateAt": now},
        )
        session(self, home, "tab-b", "codex", 8, is_alive=lambda pid: pid != 7)
        self.assertEqual((sessions.read_presence(home, "tab-b") or {}).get("state"), "busy")

    def test_a_presence_whose_heartbeat_stopped_counts_as_gone_even_if_its_pid_is_reused(self) -> None:
        home = temp_home(self)
        now = iso(now_ms())
        sessions.update_presence(
            home,
            "tab-b",
            lambda _: {
                "id": "tab-b",
                "agent": "claude",
                "path": "/b",
                "pid": 7,
                "startedAt": now,
                "state": "idle",
                "stateAt": now,
                "beatMs": 1_000,
            },
        )
        file = sessions.presence_path(home, "tab-b")
        self.assertEqual(len(sessions.live_sessions(home, lambda _pid: True)), 1)
        old = time.time() - sessions.PRESENCE_BEATS_MISSED - 1
        os.utime(file, (old, old))
        self.assertEqual(len(sessions.live_sessions(home, lambda _pid: True)), 0)
        self.assertFalse(os.path.exists(file))

    def test_a_presence_from_a_server_without_a_heartbeat_still_lives_by_its_pid(self) -> None:
        home = temp_home(self)
        now = iso(now_ms())
        sessions.update_presence(
            home,
            "tab-b",
            lambda _: {"id": "tab-b", "agent": "claude", "path": "/b", "pid": 7, "startedAt": now, "state": "idle", "stateAt": now},
        )
        old = time.time() - 24 * 60 * 60
        os.utime(sessions.presence_path(home, "tab-b"), (old, old))
        self.assertEqual(len(sessions.live_sessions(home, lambda _pid: True)), 1)

    def test_a_server_keeps_its_presence_fresh_and_writes_it_again_if_it_goes_missing(self) -> None:
        home = temp_home(self)
        b = session(self, home, "tab-b", "claude", 2, heartbeat_ms=20)
        file = sessions.presence_path(home, "tab-b")
        self.assertEqual((sessions.read_presence(home, "tab-b") or {}).get("beatMs"), 20)
        os.remove(file)
        self.assertTrue(until(lambda: os.path.exists(file), 3))
        b.stop_heartbeat()

    def test_the_heartbeat_leaves_the_presence_of_another_server_alone(self) -> None:
        home = temp_home(self)
        b = session(self, home, "tab-b", "claude", 2, heartbeat_ms=20)
        patch(home, "tab-b", pid=99)
        time.sleep(0.15)
        b.stop_heartbeat()
        self.assertEqual((sessions.read_presence(home, "tab-b") or {}).get("pid"), 99)


class CodexThreadTest(unittest.TestCase):
    def test_a_codex_session_keeps_the_tab_id_of_an_open_tab_and_records_its_thread(self) -> None:
        home = temp_home(self)
        a = session(self, home, "tab-a", "codex", 100, FakeHosts("jetbrains-1"))
        threads = [threading.Thread(target=a.note_thread, args=(THREAD,)) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(a.id, "tab-a")
        p = sessions.read_presence(home, "tab-a") or {}
        self.assertEqual([p.get("threadId"), p.get("host"), p.get("pid")], [THREAD, "jetbrains-1", 100])

    def test_a_codex_session_whose_tab_id_names_no_open_tab_becomes_codex_thread(self) -> None:
        home = temp_home(self)
        stale = session(self, home, "tab-gone", "codex", 100)
        patch(home, "tab-gone", state="busy")
        stale.note_thread("not a thread id")
        stale.note_thread(None)
        self.assertEqual(stale.id, "tab-gone")
        stale.note_thread(THREAD)
        self.assertEqual(stale.id, f"codex-{THREAD}")
        self.assertFalse(os.path.exists(sessions.presence_path(home, "tab-gone")))
        p = sessions.read_presence(home, stale.id) or {}
        self.assertEqual(
            [p.get("id"), p.get("threadId"), p.get("pid"), p.get("state"), p.get("path")],
            [f"codex-{THREAD}", THREAD, 100, "busy", "/w/tab-gone"],
        )
        stale.note_thread(THREAD)
        self.assertEqual(stale.id, f"codex-{THREAD}")
        stale.stop_sync()
        self.assertFalse(os.path.exists(sessions.presence_path(home, stale.id)))

    def test_a_session_without_a_tab_id_takes_codex_thread_unless_another_live_server_holds_it(self) -> None:
        home = temp_home(self)
        first = session(self, home, None, "", 100, random_id=lambda: "s-first000001")
        first.note_thread(THREAD)
        self.assertEqual(first.id, f"codex-{THREAD}")
        self.assertFalse(os.path.exists(sessions.presence_path(home, "s-first000001")))

        second = session(self, home, None, "", 200, random_id=lambda: "s-second00001")
        second.note_thread(THREAD)
        self.assertEqual(second.id, "s-second00001")
        self.assertEqual((sessions.read_presence(home, "s-second00001") or {}).get("threadId"), THREAD)
        self.assertEqual((sessions.read_presence(home, f"codex-{THREAD}") or {}).get("pid"), 100)

    def test_a_codex_session_reads_its_model_and_effort_from_config_toml_honouring_codex_home_and_the_profile(self) -> None:
        self.assertEqual(
            parse_codex_config('model = "gpt-5.5"\nmodel_reasoning_effort = "high" # comment\n[profiles.fast]\nmodel = "x"\n'),
            {"model": "gpt-5.5", "effort": "high"},
        )
        self.assertEqual(
            parse_codex_config("profile = 'fast'\nmodel = 'gpt-5.5'\n[profiles.fast]\nmodel_reasoning_effort = 'low'\n"),
            {"model": "gpt-5.5", "effort": "low"},
        )
        self.assertEqual(parse_codex_config('[tools]\nmodel = "nested"\n'), {})

        home = temp_home(self)
        codex_home = temp_home(self, "iat-codex-")
        with open(os.path.join(codex_home, "config.toml"), "w", encoding="utf-8") as f:
            f.write('model = "gpt-5.5"\nmodel_reasoning_effort = "medium"\n')
        env = {"CODEX_HOME": codex_home}
        session(self, home, "tab-x", "codex", 1, env={"IDE_AGENT_TABS_ID": "tab-x", "IDE_AGENT_TABS_AGENT": "codex", **env})
        p = sessions.read_presence(home, "tab-x") or {}
        self.assertEqual([p.get("model"), p.get("effort")], ["gpt-5.5", "medium"])

        sessions.update_presence(home, "tab-y", lambda _: {"id": "tab-y", "model": "gpt-5.5-codex"})
        session(self, home, "tab-y", "codex", 2, env={"IDE_AGENT_TABS_ID": "tab-y", "IDE_AGENT_TABS_AGENT": "codex", **env})
        p = sessions.read_presence(home, "tab-y") or {}
        self.assertEqual([p.get("model"), p.get("effort")], ["gpt-5.5-codex", "medium"])

        session(self, home, "tab-z", "claude", 3, env={"IDE_AGENT_TABS_ID": "tab-z", "IDE_AGENT_TABS_AGENT": "claude", **env})
        self.assertIsNone((sessions.read_presence(home, "tab-z") or {}).get("model"))

    def test_set_client_to_codex_learns_the_config_defaults(self) -> None:
        home = temp_home(self)
        codex_home = temp_home(self, "iat-codex-")
        with open(os.path.join(codex_home, "config.toml"), "w", encoding="utf-8") as f:
            f.write('model = "gpt-5.5"\n')
        m = session(self, home, None, "", 1, env={"CODEX_HOME": codex_home}, random_id=lambda: "s-codexcfg001")
        self.assertIsNone((sessions.read_presence(home, m.id) or {}).get("model"))
        m.set_client("codex-mcp-client")
        self.assertEqual((sessions.read_presence(home, m.id) or {}).get("model"), "gpt-5.5")


class SendTest(unittest.TestCase):
    def test_a_send_to_a_session_from_a_build_before_the_message_store_fails_with_the_restart_advice_and_stores_nothing(self) -> None:
        home = temp_home(self)
        a, _b = pair(self, home)
        sessions.update_presence(home, "tab-b", lambda p: p if p is None else {k: v for k, v in p.items() if k != "mail"})
        with self.assertRaisesRegex(MailError, "tab-b runs an older Agent Tabs; restart that session to message it"):
            a.send({"to": "tab-b", "text": "hi"})
        self.assertEqual(store.peek_unread(home, "tab-b"), [])

    def test_send_wakes_an_idle_session_with_the_fixed_line_and_only_queues_for_any_other_state(self) -> None:
        home = temp_home(self)
        hosts = FakeHosts("fake-term")
        a, _b = pair(self, home, hosts)
        self.assertEqual(a.send({"to": "tab-b", "text": "secret; $(rm -rf /)"})["delivery"], "queued")
        patch(home, "tab-b", state="busy")
        self.assertEqual(a.send({"to": "tab-b", "text": "x"})["delivery"], "queued")
        patch(home, "tab-b", state="idle")
        woken = a.send({"to": "tab-b", "text": "secret; `rm -rf /`"})
        self.assertEqual(woken["delivery"], "woken")
        self.assertEqual(hosts.typed, [("tab-b", "fake-term", "Agent Tabs: new message from codex tab-a. Call read_messages.")])
        after = sessions.read_presence(home, "tab-b") or {}
        self.assertEqual(after.get("state"), "waking")
        self.assertEqual(after.get("host"), "fake-term")
        self.assertEqual(a.send({"to": "tab-b", "text": "y"})["delivery"], "queued")
        self.assertEqual(len(hosts.typed), 1)

    def test_a_queued_send_keeps_retrying_the_wake_up_until_the_message_is_read(self) -> None:
        home = temp_home(self)
        hosts = FakeHosts("fake-term")
        a, b = pair(self, home, hosts, rewake_every_ms=20)
        self.assertEqual(a.send({"to": "tab-b", "text": "x"})["delivery"], "queued")
        time.sleep(0.1)
        self.assertEqual(hosts.typed, [])
        patch(home, "tab-b", state="idle")
        self.assertTrue(until(lambda: len(hosts.typed) > 0, 5))
        time.sleep(0.1)
        self.assertEqual(len(hosts.typed), 1)
        self.assertEqual((sessions.read_presence(home, "tab-b") or {}).get("state"), "waking")
        b.read()
        patch(home, "tab-b", state="idle")
        time.sleep(0.15)
        self.assertEqual(len(hosts.typed), 1)
        self.assertEqual(a.follow_up_peers(), [])

    def test_a_failed_wake_up_leaves_the_message_queued_and_the_session_idle(self) -> None:
        home = temp_home(self)
        a, _b = pair(self, home, FakeHosts("fake-term", ok=False))
        patch(home, "tab-b", state="idle")
        result = a.send({"to": "tab-b", "text": "x"})
        self.assertEqual(result["delivery"], "queued")
        self.assertRegex(result["note"], "no input here")
        self.assertEqual((sessions.read_presence(home, "tab-b") or {}).get("state"), "idle")

    def test_a_thrown_wake_up_leaves_the_message_queued_and_the_session_idle(self) -> None:
        home = temp_home(self)
        a, _b = pair(self, home, ThrowingHosts("fake-term"))
        patch(home, "tab-b", state="idle")
        result = a.send({"to": "tab-b", "text": "x"})
        self.assertEqual(result["delivery"], "queued")
        self.assertRegex(result["note"], "no input here")
        self.assertEqual((sessions.read_presence(home, "tab-b") or {}).get("state"), "idle")

    def test_a_wake_that_fails_on_a_cached_host_finds_the_host_again_and_types_there(self) -> None:
        home = temp_home(self)
        moving = MovingHosts("new-ide")
        a, _b = pair(self, home, moving)
        patch(home, "tab-b", host="old-ide", state="idle", stateAt=iso(now_ms() - 10_000))
        sent = a.send({"to": "tab-b", "text": "x"})
        self.assertEqual(sent["delivery"], "woken")
        self.assertEqual([t[1] for t in moving.typed], ["old-ide", "new-ide"])
        self.assertEqual((sessions.read_presence(home, "tab-b") or {}).get("host"), "new-ide")

    def test_send_refuses_bad_targets_and_ids(self) -> None:
        home = temp_home(self)
        a, _b = pair(self, home)
        with self.assertRaisesRegex(MailError, "this session"):
            a.send({"to": "tab-a", "text": "x"})
        with self.assertRaisesRegex(MailError, "no live session"):
            a.send({"to": "nobody", "text": "x"})
        with self.assertRaisesRegex(MailError, "not a session id"):
            a.send({"to": "../etc", "text": "x"})
        with self.assertRaisesRegex(MailError, "empty"):
            a.send({"to": "tab-b", "text": " "})
        with self.assertRaisesRegex(MailError, "message id"):
            a.send({"to": "tab-b", "text": "x", "replyTo": "nope"})
        with self.assertRaisesRegex(MailError, "exceeds 32000 characters"):
            a.send({"to": "tab-b", "text": "x" * (store.MAX_TEXT_CHARS + 1)})
        self.assertEqual(store.peek_unread(home, "tab-b"), [])

    def test_a_send_that_fails_to_deliver_gives_its_rate_slot_back(self) -> None:
        home = temp_home(self)
        a, _b = pair(self, home)
        for i in range(store.MAX_UNREAD):
            store.send_message(home, out("tab-b", f"m{i}", f"s-{i:012d}"))
        for i in range(store.MAX_SENT_PER_MINUTE + 1):
            with self.assertRaisesRegex(MailError, "50 unread"):
                a.send({"to": "tab-b", "text": f"x{i}"})

    def test_the_same_message_sent_again_within_dedupe_ms_is_not_delivered_twice(self) -> None:
        home = temp_home(self)
        a, _b = pair(self, home)
        first = a.send({"to": "tab-b", "text": "review this"})
        again = a.send({"to": "tab-b", "text": "review this"})
        other = a.send({"to": "tab-b", "text": "review that"})
        self.assertEqual(again["id"], first["id"])
        self.assertIs(again["duplicate"], True)
        self.assertEqual(again["delivery"], "queued")
        self.assertNotEqual(other["id"], first["id"])
        self.assertEqual(len(store.peek_unread(home, "tab-b")), 2)

    def test_send_takes_a_short_name_as_well_as_the_full_id(self) -> None:
        home = temp_home(self)
        sender = session(self, home, "tab-s", "claude", 1)
        codex = session(self, home, FULL_ID, "codex", 2)
        self.assertEqual(sender.send({"to": "codex-f99f", "text": "by name"})["to"], FULL_ID)
        self.assertEqual(sender.send({"to": FULL_ID, "text": "by id"})["to"], FULL_ID)
        self.assertEqual(sorted(unread_texts(home, FULL_ID)), ["by id", "by name"])
        with self.assertRaisesRegex(MailError, "no live session with id or name codex-0000"):
            sender.send({"to": "codex-0000", "text": "x"})
        with self.assertRaisesRegex(MailError, "to is this session"):
            codex.send({"to": "codex-f99f", "text": "x"})

    def test_send_takes_the_native_style_name_the_legacy_short_name_and_the_full_id(self) -> None:
        home = temp_home(self)
        sender = session(self, home, "tab-s", "claude", 1)
        session(self, home, FULL_ID, "codex", 2)
        row = next(s for s in sender.list_sessions()["sessions"] if s["agent"] == "codex")
        self.assertEqual(
            [row["name"], row["shortName"], row["legacyName"]], ["f99f0a1b-2222-4333-8444-f9", "f99f0a1b-2222-4333-8444-f9", "codex-f99f"]
        )
        for to in (row["name"], row["legacyName"], row["id"]):
            self.assertEqual(sender.send({"to": to, "text": f"to {to}"})["to"], row["id"])

    def test_a_tab_still_starting_is_not_typed_into_and_the_follow_up_wakes_it_once_it_is_up(self) -> None:
        home = temp_home(self)
        clock = Clock(now_ms())
        slept: list[float] = []
        hosts = FakeHosts("fake-term")
        a = session(self, home, "tab-a", "codex", 1, hosts, now=clock, sleep=slept.append, rewake_every_ms=30)
        session(self, home, "tab-b", "codex", 2)
        patch(home, "tab-b", host="fake-term", state="idle", stateAt=iso(int(clock.at) + 1_000))
        self.assertEqual(a.send({"to": "tab-b", "text": "hi"})["delivery"], "queued")
        self.assertEqual(hosts.typed, [])
        clock.at += 1_500
        self.assertTrue(until(lambda: len(hosts.typed) > 0, 5))
        time.sleep(0.1)
        self.assertEqual(len(hosts.typed), 1)
        self.assertEqual(slept, [1_500])

    def test_a_session_that_just_went_idle_is_typed_into_after_the_settle_wait(self) -> None:
        home = temp_home(self)
        clock = Clock(now_ms())
        slept: list[float] = []
        hosts = FakeHosts("fake-term")
        a = session(self, home, "tab-a", "codex", 1, hosts, now=clock, sleep=slept.append)
        session(self, home, "tab-b", "codex", 2)
        patch(home, "tab-b", state="idle", stateAt=iso(int(clock.at) - 500))
        self.assertEqual(a.send({"to": "tab-b", "text": "hi"})["delivery"], "woken")
        self.assertEqual(slept, [sessions.IDLE_SETTLE_MS - 500])
        self.assertEqual(len(hosts.typed), 1)

    def test_a_claude_tab_whose_turn_just_ended_is_not_typed_into_until_its_prompt_has_sat_idle(self) -> None:
        home = temp_home(self)
        hosts = FakeHosts("fake-term")
        a, _b = pair(self, home, hosts, rewake_every_ms=20)
        at = now_ms() - 10_000
        run_hook("claude", "Stop", {}, home, "tab-b", at)
        self.assertEqual(a.send({"to": "tab-b", "text": "hi"})["delivery"], "queued")
        time.sleep(0.1)
        self.assertEqual(hosts.typed, [])
        run_hook("claude", "Notification", {"notification_type": "idle_prompt"}, home, "tab-b", at)
        self.assertTrue(until(lambda: len(hosts.typed) > 0, 5))
        time.sleep(0.1)
        self.assertEqual(len(hosts.typed), 1)

    def test_a_cli_without_an_input_idle_signal_is_woken_as_soon_as_its_turn_ends(self) -> None:
        home = temp_home(self)
        a = session(self, home, "tab-a", "codex", 1, FakeHosts("fake-term"))
        session(self, home, "tab-b", "codex", 2)
        run_hook("codex", "Stop", {}, home, "tab-b", now_ms() - 10_000)
        self.assertEqual(a.send({"to": "tab-b", "text": "hi"})["delivery"], "woken")

    def test_stop_sync_ends_the_follow_ups_and_a_stopped_session_starts_none(self) -> None:
        home = temp_home(self)
        a, _b = pair(self, home)
        a.send({"to": "tab-b", "text": "one"})
        self.assertEqual(a.follow_up_peers(), ["tab-b"])
        a.stop_sync()
        self.assertEqual(a.follow_up_peers(), [])
        session(self, home, "tab-a", "codex", 1)
        a.send({"to": "tab-b", "text": "two"})
        self.assertEqual(a.follow_up_peers(), [])


class ReadTest(unittest.TestCase):
    def test_read_marks_read_and_wraps_the_text_as_untrusted(self) -> None:
        home = temp_home(self)
        a, b = pair(self, home)
        sent = a.send({"to": "tab-b", "text": "please review"})
        patch(home, "tab-b", nudges=2)
        read = b.read()
        self.assertEqual((sessions.read_presence(home, "tab-b") or {}).get("nudges"), 0)
        self.assertRegex(read["notice"], "not from your user")
        self.assertEqual(
            [[m["id"], m["text"], m["from"]] for m in read["messages"]],
            [[sent["id"], "please review", {"id": "tab-a", "agent": "codex", "path": "/w/tab-a"}]],
        )
        self.assertEqual(b.read(), {"messages": []})

    def test_one_read_returns_at_most_max_read_chars_of_text_and_leaves_the_rest_unread(self) -> None:
        home = temp_home(self)
        a, b = pair(self, home)
        for i in range(3):
            a.send({"to": "tab-b", "text": str(i) * store.MAX_TEXT_CHARS})
        a.stop_follow_ups()
        first = b.read()
        self.assertEqual(len(first["messages"]), max(1, store.MAX_READ_CHARS // store.MAX_TEXT_CHARS))
        self.assertEqual(first["remaining"], 3 - len(first["messages"]))
        self.assertRegex(first["next"], "more unread")
        self.assertEqual(len(store.peek_unread(home, "tab-b")), first["remaining"])
        rest: list[str] = []
        reply = b.read()
        while reply["messages"]:
            rest.extend(m["text"][0] for m in reply["messages"])
            reply = b.read()
        self.assertEqual([*(m["text"][0] for m in first["messages"]), *rest], ["0", "1", "2"])

    def test_a_read_cancelled_before_it_answers_leaves_the_messages_unread(self) -> None:
        home = temp_home(self)
        a, b = pair(self, home)
        a.send({"to": "tab-b", "text": "keep me"})
        a.stop_follow_ups()
        cancel = threading.Event()
        cancel.set()
        with self.assertRaisesRegex(MailError, "cancelled"):
            b.read(cancel)
        self.assertEqual(unread_texts(home, "tab-b"), ["keep me"])
        self.assertEqual(b.read()["messages"][0]["text"], "keep me")


class WaitTest(unittest.TestCase):
    def test_wait_returns_at_once_when_a_message_waits_filters_and_times_out(self) -> None:
        home = temp_home(self)
        a, b = pair(self, home)
        first = a.send({"to": "tab-b", "text": "one"})
        started = time.monotonic()
        got = b.wait({"timeout": 30})
        self.assertEqual(got["message"]["id"], first["id"])
        self.assertLess(time.monotonic() - started, 1)

        late, box = in_thread(lambda: b.wait({"timeout": 10, "replyTo": first["id"]}))
        a.send({"to": "tab-b", "text": "unrelated"})
        time.sleep(0.2)
        a.send({"to": "tab-b", "text": "answer", "replyTo": first["id"]})
        late.join(10)
        self.assertFalse(late.is_alive())
        self.assertEqual(box["value"]["message"]["text"], "answer")
        self.assertEqual(b.read()["messages"][0]["text"], "unrelated")

        t0 = time.monotonic()
        self.assertEqual(b.wait({"timeout": 0.3}), {"message": None, "timedOut": True, "waitedSeconds": 0.3})
        self.assertGreaterEqual(time.monotonic() - t0, 0.25)

        cancel = threading.Event()
        timer = threading.Timer(0.1, cancel.set)
        self.addCleanup(timer.cancel)
        timer.start()
        t1 = time.monotonic()
        self.assertIsNone(b.wait({"timeout": 60}, cancel)["message"])
        self.assertLess(time.monotonic() - t1, 2)

        self.assertIsNone(store.wait_for_message(home, "tab-b", {"from": "tab-z"}, 0))

    def test_wait_refuses_a_sender_that_is_not_a_session_id_and_a_reply_to_that_is_not_a_message_id(self) -> None:
        home = temp_home(self)
        _a, b = pair(self, home)
        with self.assertRaisesRegex(MailError, "from is not a session id"):
            b.wait({"from": "../x", "timeout": 0})
        with self.assertRaisesRegex(MailError, "message id"):
            b.wait({"replyTo": "nope", "timeout": 0})

    def test_a_wait_clamps_a_negative_timeout_to_zero(self) -> None:
        home = temp_home(self)
        _a, b = pair(self, home)
        self.assertEqual(b.wait({"timeout": -5}), {"message": None, "timedOut": True, "waitedSeconds": 0})

    def test_a_wait_for_a_sender_keeps_waking_that_sender_while_it_waits(self) -> None:
        home = temp_home(self)
        hosts = FakeHosts("fake-term")
        a = session(self, home, "tab-a", "codex", 1)
        b = session(self, home, "tab-b", "claude", 2, hosts, rewake_every_ms=20)
        b.send({"to": "tab-a", "text": "need an answer"})
        b.stop_follow_ups()
        patch(home, "tab-a", state="idle")
        self.assertTrue(b.wait({"from": "tab-a", "timeout": 0.5})["timedOut"])
        self.assertEqual([t[0] for t in hosts.typed], ["tab-a"])
        self.assertEqual(a.read()["messages"][0]["text"], "need an answer")

    def test_an_antigravity_cli_session_waits_at_most_agy_max_wait_s_inside_its_three_minute_tool_limit(self) -> None:
        home = temp_home(self)
        agy = session(self, home, "tab-agy", "agy", 3)
        cancel = threading.Event()
        cancel.set()
        self.assertEqual(agy.wait({"timeout": 600}, cancel)["waitedSeconds"], AGY_MAX_WAIT_S)
        self.assertLess(AGY_MAX_WAIT_S, 180)

    def test_a_session_served_over_http_waits_at_most_its_cap_and_writes_its_agent_pid_and_start_time_into_presence(self) -> None:
        home = temp_home(self)
        m = session(self, home, "tab-http", "claude", 4, pid_start=1_700_000_000_000, max_wait_s=240)
        cancel = threading.Event()
        cancel.set()
        self.assertEqual(m.wait({"timeout": 600}, cancel)["waitedSeconds"], 240)
        p = sessions.read_presence(home, "tab-http") or {}
        self.assertEqual([p.get("pid"), p.get("pidStart")], [4, 1_700_000_000_000])


class FailingStart(Messaging):
    def __init__(self, deps: MessagingDeps, failures: float) -> None:
        super().__init__(deps)
        self.failures = failures
        self.attempts = 0

    def start(self) -> None:
        self.attempts += 1
        if self.attempts <= self.failures:
            raise RuntimeError("timed out waiting for the presence lock")
        super().start()


class StartRetryTest(unittest.TestCase):
    def failing(self, failures: float) -> FailingStart:
        scheduler = Scheduler()
        self.addCleanup(scheduler.stop)
        deps = MessagingDeps(
            home=temp_home(self),
            env={"IDE_AGENT_TABS_ID": "tab-aaaa-1"},
            pid=101,
            cwd="/work/a",
            hosts=FakeHosts(),
            is_alive=lambda _pid: True,
            scheduler=scheduler,
        )
        messaging = FailingStart(deps, failures)
        self.addCleanup(messaging.stop_sync)
        return messaging

    def test_a_start_that_fails_every_attempt_is_logged_and_reported_by_list_sessions_send_and_read(self) -> None:
        messaging = self.failing(math.inf)
        logged: list[str] = []
        messaging.start_registered(delays_ms=[1, 2], log=logged.append)
        self.assertTrue(until(lambda: len(logged) == 3, 5))
        time.sleep(0.05)
        self.assertEqual(messaging.attempts, 3)
        message = "this session isn't registered: timed out waiting for the presence lock"
        self.assertEqual(logged, [message] * 3)

        listed = messaging.list_sessions()
        self.assertEqual(listed["sessions"], [])
        self.assertEqual(listed["warnings"], [message])
        self.assertEqual(messaging.read()["warnings"], [message])
        with self.assertRaisesRegex(
            MailError, r"no live session with id or name tab-bbbb-2; call list_sessions\. Warning: this session isn't registered: timed out"
        ):
            messaging.send({"to": "tab-bbbb-2", "text": "hi"})

    def test_a_start_that_succeeds_on_a_retry_registers_the_session_and_drops_the_warning(self) -> None:
        messaging = self.failing(2)
        logged: list[str] = []
        messaging.start_registered(delays_ms=[1, 2, 3], log=logged.append)

        def registered() -> bool:
            listed = messaging.list_sessions()
            return len(listed["sessions"]) == 1 and "warnings" not in listed

        self.assertTrue(until(registered, 5))
        self.assertEqual(messaging.attempts, 3)
        self.assertEqual(len(logged), 2)
        listed = messaging.list_sessions()
        self.assertNotIn("warnings", listed)
        self.assertEqual([[s["id"], s["self"]] for s in listed["sessions"]], [["tab-aaaa-1", True]])

    def test_a_start_that_succeeds_first_time_makes_no_retry_and_no_warning(self) -> None:
        messaging = self.failing(0)
        messaging.start_registered(delays_ms=[1])
        time.sleep(0.05)
        self.assertEqual(messaging.attempts, 1)
        self.assertNotIn("warnings", messaging.list_sessions())

    def test_a_stopped_session_makes_no_further_start_attempt(self) -> None:
        messaging = self.failing(math.inf)
        messaging.start_registered(delays_ms=[200, 200])
        messaging.stop_sync()
        time.sleep(0.5)
        self.assertEqual(messaging.attempts, 1)

    def test_an_empty_delay_list_reports_the_failure_and_makes_no_retry(self) -> None:
        messaging = self.failing(math.inf)
        logged: list[str] = []
        messaging.start_registered(delays_ms=[], log=logged.append)
        self.assertEqual(messaging.attempts, 1)
        self.assertEqual(len(logged), 1)


class ListSessionsTest(unittest.TestCase):
    def test_rows_carry_name_route_tab_host_and_via_in_the_agent_order(self) -> None:
        home = temp_home(self)
        labels = {"jetbrains-1": "IntelliJ IDEA", "windows-terminal": "Windows Terminal"}
        sessions.update_presence(home, "tab-c", lambda _: {"id": "tab-c", "host": "jetbrains-1", "project": "Plugins", "via": "direct"})
        sessions.update_presence(home, "tab-x", lambda _: {"id": "tab-x", "host": "windows-terminal", "via": "ori"})
        made = [
            session(self, home, "tab-g", "gemini", 1, FakeHosts(None, labels=labels)),
            session(self, home, "tab-x", "codex", 2, FakeHosts(None, labels=labels)),
            session(self, home, "tab-c", "claude", 3, FakeHosts(None, labels=labels)),
            session(self, home, "tab-z", "zed-agent", 4, FakeHosts(None, labels=labels)),
            session(self, home, "tab-a", "agy", 5, FakeHosts(None, labels=labels)),
        ]
        made[2].mod_presence({"driver": True, "nativeName": "plugins-fa [6a3948]", "state": "idle", "model": "opus", "effort": "high"})
        rows = made[0].list_sessions()["sessions"]
        self.assertEqual([r["agent"] for r in rows], ["claude", "codex", "agy", "gemini", "zed-agent"])
        claude_row, codex_row = rows[0], rows[1]
        self.assertEqual(
            [claude_row[k] for k in ("name", "id", "route", "tab", "host", "ide", "via", "state")],
            ["plugins-fa [6a3948]", "tab-c", "native", "tab-c", "IntelliJ IDEA (Plugins)", "jetbrains-1", "direct", "idle"],
        )
        self.assertEqual([codex_row[k] for k in ("name", "route", "host", "via")], ["tab-x-ab", "agent-tabs", "Windows Terminal", "ori"])
        self.assertEqual(
            [codex_row[k] for k in ("shortName", "session", "harness", "where", "folder", "model", "effort")],
            ["tab-x-ab", "tab-x", "Codex via OpenRouter", "Windows Terminal", "/w/tab-x", None, None],
        )
        self.assertEqual(
            [claude_row[k] for k in ("shortName", "legacyName", "harness", "where", "model", "effort")],
            ["plugins-fa [6a3948]", "claude-tabc", "Claude Code", "IntelliJ IDEA", "opus", "high"],
        )
        self.assertIs(next(r for r in rows if r["agent"] == "gemini")["self"], True)
        self.assertEqual([r["self"] for r in rows if r["agent"] != "gemini"], [False] * 4)

        patch(home, "tab-c", modBeat=now_ms() - sessions.MOD_STALE_MS - 1)
        stale = made[0].list_sessions()["sessions"][0]
        self.assertEqual([stale["name"], stale["route"]], ["plugins-fa [6a3948]", "agent-tabs"])
        self.assertEqual(stale["nativeName"], "plugins-fa [6a3948]")

    def test_where_shows_the_stored_ide_product_when_the_endpoint_is_gone_refreshes_it_when_the_tab_is_re_adopted_and_is_none_when_unknown(
        self,
    ) -> None:
        home = temp_home(self)
        kept = session(self, home, "tab-v", "codex", 1)
        session(self, home, "tab-u", "codex", 2)
        patch(home, "tab-v", host="vscode-1-old", product="Visual Studio Code", project="proj")
        patch(home, "tab-u", host="vscode-2-gone")

        def row(tab_id: str) -> dict[str, Any]:
            return next(s for s in kept.list_sessions()["sessions"] if s["id"] == tab_id)

        self.assertEqual([row("tab-v")["where"], row("tab-v")["host"]], ["Visual Studio Code", "Visual Studio Code (proj)"])
        self.assertEqual([row("tab-u")["where"], row("tab-u")["host"]], [None, None])

        readopt = MovingHosts("vscode-3-new", labels={"vscode-3-new": "Cursor"})
        sender = session(self, home, None, "", 3, readopt, random_id=lambda: "s-000000000003")
        patch(home, "tab-v", state="idle", stateAt=iso(now_ms() - 10_000), inputIdle=True)
        self.assertEqual(sender.send({"to": "tab-v", "text": "wake"})["delivery"], "woken")
        self.assertEqual([t[1] for t in readopt.typed], ["vscode-1-old", "vscode-3-new"])
        after = sessions.read_presence(home, "tab-v") or {}
        self.assertEqual([after.get("host"), after.get("product")], ["vscode-3-new", "Cursor"])

    def test_the_mod_records_the_agent_type_and_its_color_lists_them_and_refuses_a_color_outside_the_palette(self) -> None:
        home = temp_home(self)
        claude = session(self, home, "tab-c", "claude", 1)
        claude.mod_presence({"driver": True, "agentType": "reviewer", "agentColor": "purple"})
        row = claude.list_sessions()["sessions"][0]
        self.assertEqual([row["agentType"], row["agentColor"]], ["reviewer", "purple"])
        with self.assertRaisesRegex(MailError, "agentColor must be one of red, blue"):
            claude.mod_presence({"agentColor": "chartreuse"})
        with self.assertRaisesRegex(MailError, "agentType must be"):
            claude.mod_presence({"agentType": "two words"})
        session(self, home, "tab-d", "codex", 2)
        other = next(s for s in claude.list_sessions()["sessions"] if s["id"] == "tab-d")
        self.assertEqual([other["agentType"], other["agentColor"]], [None, None])

    def test_a_handed_off_session_lists_where_it_went(self) -> None:
        home = temp_home(self)
        a, _b = pair(self, home)
        patch(home, "tab-b", handedOffTo="tab-new")
        row = next(s for s in a.list_sessions()["sessions"] if s["id"] == "tab-b")
        self.assertEqual(row["handedOffTo"], "tab-new")

    def test_session_folders_lists_each_live_folder_once(self) -> None:
        home = temp_home(self)
        a, _b = pair(self, home)
        session(self, home, "tab-c", "codex", 3, cwd="/w/tab-a")
        self.assertEqual(sorted(a.session_folders()), ["/w/tab-a", "/w/tab-b"])


class EndedSessionTest(unittest.TestCase):
    def dirs(self, home: str) -> TranscriptDirs:
        return TranscriptDirs(os.path.join(home, "claude-config"), os.path.join(home, "codex-config"))

    def test_record_end_writes_a_closed_record_for_a_resumable_session_of_this_server(self) -> None:
        home = temp_home(self)
        m = session(self, home, "tab-x", "codex", 1, FakeHosts("fake-term"), transcripts=self.dirs(home))
        m.note_thread(THREAD)
        m.record_end()
        records = closed.read_closed(home)
        self.assertEqual([[r["id"], r["agent"], r["folder"], r["tab"]] for r in records], [[THREAD, "codex", "/w/tab-x", "tab-x"]])

    def test_record_end_ignores_a_presence_that_another_server_holds(self) -> None:
        home = temp_home(self)
        m = session(self, home, "tab-x", "codex", 1, FakeHosts("fake-term"), transcripts=self.dirs(home))
        m.note_thread(THREAD)
        patch(home, "tab-x", pid=99)
        m.record_end()
        self.assertEqual(closed.read_closed(home), [])

    def test_record_end_skips_a_session_with_nothing_to_resume(self) -> None:
        home = temp_home(self)
        m = session(self, home, "tab-x", "codex", 1, transcripts=self.dirs(home))
        m.record_end()
        self.assertEqual(closed.read_closed(home), [])

    def test_listing_sessions_records_the_ones_whose_server_died(self) -> None:
        home = temp_home(self)
        sessions.update_presence(
            home,
            "tab-dead",
            lambda _: {"id": "tab-dead", "agent": "codex", "path": "/w/dead", "pid": 999, "startedAt": iso(now_ms()), "threadId": THREAD},
        )
        m = session(self, home, "tab-x", "codex", 1, is_alive=lambda pid: pid != 999, transcripts=self.dirs(home))
        self.assertEqual([s["id"] for s in m.list_sessions()["sessions"]], ["tab-x"])
        self.assertFalse(os.path.exists(sessions.presence_path(home, "tab-dead")))
        self.assertEqual([[r["id"], r["tab"]] for r in closed.read_closed(home)], [[THREAD, "tab-dead"]])


if __name__ == "__main__":
    unittest.main()

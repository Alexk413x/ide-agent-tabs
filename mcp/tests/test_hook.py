from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import unittest
from typing import Any

from ide_agent_tabs.agent_config import HookTarget, claude_tab_settings
from ide_agent_tabs.clock import now_iso
from ide_agent_tabs.jsjson import stringify
from ide_agent_tabs.messaging import store
from ide_agent_tabs.messaging.hook import HOOK_EVENTS, run_hook
from ide_agent_tabs.messaging.sessions import presence_path, read_presence, update_presence
from support import ROOT, temp_home

ID = "tab-hook-1"
REMINDER = "Agent Tabs: 1 unread message from codex abcdef01. read_messages returns it."
HOOK_SCRIPT = os.path.join(ROOT, "claude-plugin", "mcp", "launch", "agent_hook.py")
_mails = [0]


def git_bash() -> str | None:
    candidates = [os.environ.get("CLAUDE_CODE_GIT_BASH_PATH")]
    git = shutil.which("git")
    if git is not None:
        candidates.append(os.path.join(os.path.dirname(os.path.dirname(git)), "bin", "bash.exe"))
    candidates.append(os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Git", "bin", "bash.exe"))
    return next((c for c in candidates if c and os.path.exists(c)), None)


def hook(home: str, cli: str, event: str, data: dict[str, Any] | None = None) -> Any:
    return run_hook(cli, event, data or {}, home, ID)


def mail(home: str) -> None:
    _mails[0] += 1
    store.send_message(
        home, {"from": {"id": "abcdef0123456", "agent": "codex", "path": "/w"}, "to": ID, "text": f"secret text {_mails[0]}"}
    )


def take(home: str) -> None:
    store.take_batch(home, ID)


def presence(home: str) -> dict[str, Any]:
    return read_presence(home, ID) or {}


def state(home: str) -> Any:
    return presence(home).get("state")


def context(event: str, text: str = REMINDER) -> dict[str, Any]:
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}


class HookStateTest(unittest.TestCase):
    def test_hooks_set_busy_permission_and_idle_for_each_cli(self) -> None:
        cases = [
            ("claude", "SessionStart", {"source": "startup"}, "idle"),
            ("claude", "UserPromptSubmit", {}, "busy"),
            ("claude", "Notification", {"notification_type": "permission_prompt"}, "permission"),
            ("claude", "PostToolUse", {}, "busy"),
            ("claude", "Notification", {"notification_type": "idle_prompt"}, "idle"),
            ("claude", "Stop", {"stop_hook_active": False}, "idle"),
            ("claude", "PostToolUseFailure", {"is_interrupt": False}, "busy"),
            ("claude", "PostToolUseFailure", {"is_interrupt": True}, "idle"),
            ("claude", "UserPromptSubmit", {}, "busy"),
            ("claude", "StopFailure", {"error": "rate_limit"}, "idle"),
            ("codex", "UserPromptSubmit", {}, "busy"),
            ("codex", "PermissionRequest", {}, "permission"),
            ("codex", "PostToolUse", {}, "busy"),
            ("codex", "Stop", {}, "idle"),
            ("codex", "UserPromptSubmit", {}, "busy"),
            ("codex", "Interrupt", {}, "idle"),
            ("gemini", "BeforeAgent", {}, "busy"),
            ("gemini", "Notification", {"notification_type": "ToolPermission"}, "permission"),
            ("gemini", "AfterTool", {}, "busy"),
            ("gemini", "AfterAgent", {}, "idle"),
            ("copilot", "sessionStart", {"source": "new"}, "idle"),
            ("copilot", "userPromptSubmitted", {}, "busy"),
            ("copilot", "notification", {"notificationType": "permission_prompt"}, "permission"),
            ("copilot", "postToolUse", {}, "busy"),
            ("copilot", "agentStop", {}, "idle"),
            ("agy", "PreInvocation", {"invocationNum": 0}, "busy"),
            ("agy", "Stop", {"fullyIdle": True, "terminationReason": "NO_TOOL_CALL"}, "idle"),
            ("agy", "PostToolUse", {}, "busy"),
            ("agy", "Stop", {"fullyIdle": True}, "idle"),
            ("grok", "UserPromptSubmit", {}, "busy"),
            ("grok", "Notification", {"notificationType": "permission_prompt"}, "permission"),
            ("grok", "PreToolUse", {}, "busy"),
            ("grok", "PostToolUse", {}, "busy"),
            ("grok", "Stop", {}, "idle"),
            ("grok", "UserPromptSubmit", {}, "busy"),
            ("grok", "StopCancelled", {}, "idle"),
            ("grok", "UserPromptSubmit", {}, "busy"),
            ("grok", "StopFailure", {}, "idle"),
            ("hermes", "pre_llm_call", {}, "busy"),
            ("hermes", "pre_approval_request", {}, "permission"),
            ("hermes", "post_approval_response", {}, "busy"),
            ("hermes", "post_tool_call", {}, "busy"),
            ("hermes", "pre_verify", {}, "idle"),
            ("hermes", "pre_llm_call", {}, "busy"),
            ("hermes", "on_session_end", {"extra": {"interrupted": True}}, "idle"),
            ("qwen", "UserPromptSubmit", {}, "busy"),
            ("qwen", "PermissionRequest", {}, "permission"),
            ("qwen", "PreToolUse", {}, "busy"),
            ("qwen", "Notification", {"notification_type": "permission_prompt"}, "permission"),
            ("qwen", "PostToolUse", {}, "busy"),
            ("qwen", "Stop", {}, "idle"),
            ("goose", "UserPromptSubmit", {}, "busy"),
            ("goose", "PostToolUse", {}, "busy"),
            ("goose", "Stop", {}, "idle"),
        ]
        home = temp_home(self, "iat-hook-")
        for cli, event, data, want in cases:
            self.assertIsNone(hook(home, cli, event, data), f"{cli} {event} prints nothing without mail")
            self.assertEqual(state(home), want, f"{cli} {event}")
        hook(home, "claude", "Notification", {"notification_type": "auth_success"})
        self.assertEqual(state(home), "idle")

    def test_each_cli_gets_the_reminder_in_its_own_context_format(self) -> None:
        cases = [
            ("claude", "SessionStart", context("SessionStart")),
            ("claude", "UserPromptSubmit", context("UserPromptSubmit")),
            ("claude", "PostToolUse", context("PostToolUse")),
            ("codex", "PreToolUse", None),
            ("codex", "UserPromptSubmit", context("UserPromptSubmit")),
            ("codex", "PostToolUse", context("PostToolUse")),
            ("gemini", "BeforeAgent", context("BeforeAgent")),
            ("gemini", "AfterTool", context("AfterTool")),
            ("copilot", "userPromptSubmitted", None),
            ("copilot", "postToolUse", {"additionalContext": REMINDER}),
            ("agy", "PreInvocation", {"injectSteps": [{"ephemeralMessage": REMINDER}]}),
            ("agy", "PostToolUse", None),
            ("grok", "UserPromptSubmit", None),
            ("grok", "PreToolUse", None),
            ("grok", "PostToolUse", context("PostToolUse")),
            ("hermes", "pre_llm_call", {"context": REMINDER}),
            ("hermes", "post_tool_call", None),
            ("qwen", "UserPromptSubmit", context("UserPromptSubmit")),
            ("qwen", "PostToolUse", context("PostToolUse")),
            ("qwen", "PreToolUse", None),
            ("goose", "UserPromptSubmit", None),
            ("goose", "PostToolUse", None),
        ]
        for cli, event, want in cases:
            home = temp_home(self, "iat-hook-")
            mail(home)
            got = hook(home, cli, event)
            self.assertEqual(got, want, f"{cli} {event}")
            self.assertNotIn("secret text", json.dumps(got or {}))

    def test_a_message_is_reminded_once_and_a_new_message_brings_a_new_reminder(self) -> None:
        home = temp_home(self, "iat-hook-")
        mail(home)
        self.assertEqual(hook(home, "claude", "PostToolUse"), context("PostToolUse"))
        self.assertIsNone(hook(home, "claude", "PostToolUse"))
        self.assertIsNone(hook(home, "claude", "UserPromptSubmit"))
        mail(home)
        two = "Agent Tabs: 2 unread messages from codex abcdef01. read_messages returns them."
        self.assertEqual(hook(home, "claude", "PostToolUse"), context("PostToolUse", two))
        self.assertIsNone(hook(home, "claude", "PostToolUse"))
        take(home)
        hook(home, "claude", "PostToolUse")
        self.assertNotIn("reminded", presence(home))

    def test_turn_end_blocks_at_most_three_times_and_a_prompt_or_read_resets_the_count(self) -> None:
        home = temp_home(self, "iat-hook-")
        mail(home)
        block = {"decision": "block", "reason": f"Agent Tabs kept this turn open. {REMINDER}"}
        for i in range(1, 4):
            self.assertEqual(hook(home, "claude", "Stop", {"stop_hook_active": i > 1}), block)
            self.assertEqual(state(home), "busy")
            self.assertEqual(presence(home)["nudges"], i)
        self.assertIsNone(hook(home, "claude", "Stop", {"stop_hook_active": True}))
        self.assertEqual(state(home), "idle")
        hook(home, "claude", "UserPromptSubmit")
        self.assertEqual(presence(home)["nudges"], 0)
        self.assertEqual(hook(home, "codex", "Stop"), block)
        self.assertEqual(hook(home, "gemini", "AfterAgent"), {"decision": "deny", "reason": block["reason"]})
        self.assertEqual(hook(home, "copilot", "agentStop"), block)
        hook(home, "agy", "PreInvocation", {"invocationNum": 0})
        self.assertEqual(hook(home, "agy", "Stop"), {"decision": "continue", "reason": block["reason"]})
        hook(home, "agy", "PreInvocation", {"invocationNum": 0})
        for cli, prompt, stop in (
            ("grok", "UserPromptSubmit", "Stop"),
            ("hermes", "pre_llm_call", "pre_verify"),
            ("qwen", "UserPromptSubmit", "Stop"),
            ("goose", "UserPromptSubmit", "Stop"),
        ):
            hook(home, cli, prompt)
            self.assertEqual(hook(home, cli, stop), block, cli)
        hook(home, "grok", "UserPromptSubmit")
        self.assertIsNone(hook(home, "grok", "StopCancelled"))
        self.assertEqual(state(home), "idle")
        hook(home, "hermes", "pre_llm_call")
        self.assertIsNone(hook(home, "hermes", "on_session_end"))
        self.assertEqual(state(home), "idle")
        take(home)
        self.assertIsNone(hook(home, "claude", "Stop"))
        self.assertEqual(presence(home)["nudges"], 0)

    def test_a_hook_without_a_session_id_or_for_an_unknown_cli_or_event_does_nothing(self) -> None:
        home = temp_home(self, "iat-hook-")
        self.assertIsNone(run_hook("claude", "Stop", {}, home, None))
        self.assertIsNone(run_hook("claude", "Stop", {}, home, "../x"))
        self.assertIsNone(hook(home, "vim", "Stop"))
        self.assertIsNone(hook(home, "claude", "SessionEnd"))
        self.assertFalse(os.path.exists(presence_path(home, ID)))
        self.assertEqual(sorted(HOOK_EVENTS), ["agy", "claude", "codex", "copilot", "gemini", "goose", "grok", "hermes", "qwen"])


class HookOwnershipTest(unittest.TestCase):
    def test_a_headless_agent_started_inside_a_tab_cannot_change_the_tab_session(self) -> None:
        home = temp_home(self, "iat-hook-")
        hook(home, "claude", "SessionStart", {"source": "startup", "session_id": "tab-agent"})
        hook(home, "claude", "UserPromptSubmit", {"session_id": "tab-agent"})
        mail(home)
        for event in ("SessionStart", "UserPromptSubmit", "PostToolUse", "Stop"):
            self.assertIsNone(hook(home, "claude", event, {"source": "startup", "session_id": "child"}), event)
        self.assertEqual(state(home), "busy")
        self.assertEqual(presence(home).get("nudges", 0), 0)
        self.assertEqual(hook(home, "claude", "Stop", {"session_id": "tab-agent"})["decision"], "block")

    def test_a_clear_or_resume_hands_the_session_to_the_new_agent_session_id(self) -> None:
        home = temp_home(self, "iat-hook-")
        hook(home, "claude", "UserPromptSubmit", {"session_id": "first"})
        hook(home, "claude", "SessionStart", {"source": "clear", "session_id": "second"})
        self.assertEqual(state(home), "idle")
        hook(home, "claude", "UserPromptSubmit", {"session_id": "second"})
        self.assertEqual(state(home), "busy")
        hook(home, "claude", "Stop", {"session_id": "first"})
        self.assertEqual(state(home), "busy")

    def test_an_antigravity_turn_resets_the_count_on_its_first_model_call_only(self) -> None:
        home = temp_home(self, "iat-hook-")
        mail(home)
        hook(home, "agy", "Stop")
        hook(home, "agy", "PreInvocation", {"invocationNum": 3})
        self.assertEqual(presence(home)["nudges"], 1)
        hook(home, "agy", "PreInvocation", {"invocationNum": 0})
        self.assertEqual(presence(home)["nudges"], 0)

    def test_a_child_agent_of_the_same_cli_is_refused_while_the_tab_is_in_a_turn(self) -> None:
        cases = [
            ("codex", "UserPromptSubmit", "Stop", {"session_id": "tab"}, {"session_id": "child"}),
            ("agy", "PreInvocation", "Stop", {"conversationId": "tab", "invocationNum": 0}, {"conversationId": "child"}),
            ("gemini", "BeforeAgent", "AfterAgent", {"session_id": "tab"}, {"session_id": "child"}),
        ]
        for cli, prompt, stop, own, child in cases:
            home = temp_home(self, "iat-hook-")
            hook(home, cli, prompt, own)
            mail(home)
            self.assertIsNone(hook(home, cli, stop, child), cli)
            self.assertEqual(state(home), "busy", cli)
            self.assertEqual(presence(home).get("nudges", 0), 0, cli)

    def test_a_new_session_in_an_idle_tab_takes_it_over_without_a_start_hook(self) -> None:
        home = temp_home(self, "iat-hook-")
        hook(home, "agy", "PreInvocation", {"conversationId": "first", "invocationNum": 0})
        hook(home, "agy", "Stop", {"conversationId": "first"})
        self.assertEqual(state(home), "idle")
        hook(home, "agy", "PreInvocation", {"conversationId": "second", "invocationNum": 0})
        self.assertEqual(state(home), "busy")
        self.assertEqual(presence(home)["owner"], "second")
        hook(home, "agy", "Stop", {"conversationId": "first"})
        self.assertEqual(state(home), "busy")

    def test_a_hook_from_another_cli_than_the_tab_agent_is_ignored(self) -> None:
        home = temp_home(self, "iat-hook-")
        update_presence(home, ID, lambda _: {"id": ID, "agent": "codex", "state": "busy", "stateAt": now_iso()})
        self.assertIsNone(hook(home, "claude", "Stop", {"session_id": "child"}))
        self.assertIsNone(hook(home, "claude", "SessionStart", {"source": "startup", "session_id": "child"}))
        self.assertEqual(state(home), "busy")

    def test_a_mod_driven_session_is_left_alone(self) -> None:
        home = temp_home(self, "iat-hook-")
        update_presence(home, ID, lambda _: {"id": ID, "state": "busy", "driver": "mod", "modBeat": int(__import__("time").time() * 1000)})
        mail(home)
        self.assertIsNone(hook(home, "claude", "Stop"))
        self.assertEqual(state(home), "busy")


class HookInputTest(unittest.TestCase):
    def test_claude_marks_its_input_busy_at_a_turn_end_and_idle_at_startup_or_after_idle_prompt(self) -> None:
        home = temp_home(self, "iat-hook-")
        hook(home, "claude", "SessionStart", {"source": "startup"})
        self.assertIs(presence(home)["inputIdle"], True)
        hook(home, "claude", "UserPromptSubmit")
        hook(home, "claude", "Stop")
        self.assertEqual(state(home), "idle")
        self.assertIs(presence(home)["inputIdle"], False)
        hook(home, "claude", "Notification", {"notification_type": "idle_prompt"})
        self.assertIs(presence(home)["inputIdle"], True)
        hook(home, "claude", "SessionStart", {"source": "clear"})
        self.assertIs(presence(home)["inputIdle"], False)
        hook(home, "claude", "Notification", {"notification_type": "permission_prompt"})
        self.assertIs(presence(home)["inputIdle"], False)

    def test_a_copilot_background_agent_going_idle_leaves_the_state_alone(self) -> None:
        home = temp_home(self, "iat-hook-")
        hook(home, "copilot", "userPromptSubmitted")
        hook(home, "copilot", "notification", {"notification_type": "agent_idle"})
        self.assertEqual(state(home), "busy")
        hook(home, "copilot", "notification", {"notification_type": "elicitation_dialog"})
        self.assertEqual(state(home), "permission")

    def test_grok_tracks_input_idle_like_claude_and_qwen_does_not(self) -> None:
        home = temp_home(self, "iat-hook-")
        hook(home, "grok", "UserPromptSubmit")
        hook(home, "grok", "Stop")
        self.assertEqual(state(home), "idle")
        self.assertIs(presence(home)["inputIdle"], False)
        hook(home, "grok", "Notification", {"notificationType": "idle_prompt"})
        self.assertIs(presence(home)["inputIdle"], True)
        hook(home, "grok", "UserPromptSubmit")
        hook(home, "grok", "StopCancelled")
        self.assertIs(presence(home)["inputIdle"], False)
        qwen = temp_home(self, "iat-hook-")
        hook(qwen, "qwen", "UserPromptSubmit")
        hook(qwen, "qwen", "Stop")
        hook(qwen, "qwen", "Notification", {"notification_type": "idle_prompt"})
        self.assertNotIn("inputIdle", read_presence(qwen, ID) or {})

    def test_hooks_record_model_and_effort_and_keep_them_when_absent(self) -> None:
        home = temp_home(self, "iat-hook-")
        hook(home, "codex", "UserPromptSubmit", {"session_id": "a", "model": "gpt-5.5", "reasoning_effort": "high"})
        self.assertEqual([presence(home).get("model"), presence(home).get("effort")], ["gpt-5.5", "high"])
        hook(home, "codex", "PostToolUse", {"session_id": "a"})
        self.assertEqual([presence(home).get("model"), presence(home).get("effort")], ["gpt-5.5", "high"])
        agy = temp_home(self, "iat-hook-")
        hook(agy, "agy", "PreInvocation", {"conversationId": "c", "modelName": "gemini-3-pro", "invocationNum": 0})
        self.assertEqual((read_presence(agy, ID) or {}).get("model"), "gemini-3-pro")
        claude = temp_home(self, "iat-hook-")
        hook(claude, "claude", "PostToolUse", {"session_id": "c", "effort": {"level": "xhigh"}})
        self.assertEqual((read_presence(claude, ID) or {}).get("effort"), "xhigh")
        hook(claude, "claude", "Notification", {"session_id": "c", "notification_type": "other", "model": "claude-opus-5-5"})
        self.assertEqual((read_presence(claude, ID) or {}).get("model"), "claude-opus-5-5")
        hook(claude, "claude", "PostToolUse", {"session_id": "c", "model": "bad\nmodel", "effort": {"level": "way too high"}})
        p = read_presence(claude, ID) or {}
        self.assertEqual([p.get("model"), p.get("effort")], ["claude-opus-5-5", "xhigh"])


class HookScriptTest(unittest.TestCase):
    def run_script(self, home: str, args: list[str], stdin: str, env: dict[str, str]) -> subprocess.CompletedProcess[bytes]:
        clean = {k: v for k, v in os.environ.items() if not k.startswith("IDE_AGENT_TABS")}
        return subprocess.run(
            [sys.executable, "-I", "-S", HOOK_SCRIPT, *args],
            input=stdin.encode("utf-8"),
            capture_output=True,
            env={**clean, "IDE_AGENT_TABS_HOME": home, **env},
            timeout=30,
            check=False,
        )

    def test_reads_stdin_prints_one_json_line_and_exits_0_even_on_bad_input(self) -> None:
        home = temp_home(self, "iat-hook-")
        mail(home)
        blocked = self.run_script(home, ["claude", "Stop"], '{"stop_hook_active":false}', {"IDE_AGENT_TABS_ID": ID})
        self.assertEqual(blocked.returncode, 0)
        self.assertEqual(json.loads(blocked.stdout)["decision"], "block")
        self.assertTrue(blocked.stdout.endswith(b"\n") and blocked.stdout.count(b"\n") == 1)
        garbage = self.run_script(home, ["claude", "UserPromptSubmit"], "not json", {"IDE_AGENT_TABS_ID": ID})
        self.assertEqual(garbage.returncode, 0)
        self.assertEqual(json.loads(garbage.stdout)["hookSpecificOutput"]["additionalContext"], REMINDER)
        no_id = self.run_script(home, ["claude", "Stop"], "{}", {"IDE_AGENT_TABS_ID": ""})
        self.assertEqual([no_id.returncode, no_id.stdout], [0, b""])
        mod = self.run_script(home, ["claude", "Stop"], "{}", {"IDE_AGENT_TABS_ID": ID, "IDE_AGENT_TABS_MOD": ID})
        self.assertEqual([mod.returncode, mod.stdout], [0, b""])
        child = self.run_script(
            home, ["claude", "Stop"], '{"stop_hook_active":false}', {"IDE_AGENT_TABS_ID": ID, "IDE_AGENT_TABS_MOD": "another-tab"}
        )
        self.assertEqual(json.loads(child.stdout)["decision"], "block")

    def test_output_is_json_stringify_compact(self) -> None:
        home = temp_home(self, "iat-hook-")
        mail(home)
        out = self.run_script(home, ["claude", "PostToolUse"], "{}", {"IDE_AGENT_TABS_ID": ID})
        self.assertEqual(out.stdout.decode("utf-8"), stringify(context("PostToolUse")) + "\n")

    def test_a_broken_store_still_exits_0(self) -> None:
        home = temp_home(self, "iat-hook-")
        with open(os.path.join(home, "sessions"), "w", encoding="utf-8") as f:
            f.write("a file where a folder belongs")
        out = self.run_script(home, ["claude", "UserPromptSubmit"], "{}", {"IDE_AGENT_TABS_ID": ID})
        self.assertEqual([out.returncode, out.stdout], [0, b""])

    def hook_commands(self) -> dict[str, str]:
        with open(os.path.join(ROOT, "claude-plugin", "hooks", "hooks.json"), encoding="utf-8") as f:
            hooks = json.load(f)["hooks"]
        commands = {
            event: h["command"]
            for event, groups in hooks.items()
            for g in groups
            for h in g["hooks"]
            if "agent-hook.ps1" in h.get("command", "")
        }
        self.assertEqual(sorted(commands), sorted(HOOK_EVENTS["claude"]))
        for event, command in commands.items():
            self.assertNotIn("args", command)
            self.assertEqual(command, f'set -- claude {event}; . "${{CLAUDE_PLUGIN_ROOT}}/mcp/launch/agent-hook.ps1"')
        return commands

    def check_hook_commands(self, shell: list[str]) -> None:
        commands = self.hook_commands()
        home = temp_home(self, "iat-hook-")
        mail(home)
        root = os.path.join(ROOT, "claude-plugin").replace("\\", "/")
        clean = {k: v for k, v in os.environ.items() if not k.startswith(("IDE_AGENT_TABS", "CLAUDE_PLUGIN"))}
        env = {**clean, "IDE_AGENT_TABS_HOME": home, "CLAUDE_PLUGIN_ROOT": root}

        def run(event: str, extra: dict[str, str]) -> subprocess.CompletedProcess[bytes]:
            command = commands[event].replace("${CLAUDE_PLUGIN_ROOT}", root)
            return subprocess.run(
                [*shell, command], input=b'{"session_id":"s"}', capture_output=True, env={**env, **extra}, timeout=60, check=False
            )

        cache = os.path.join(home, "mcp", "hook-python")
        for attempt in range(2):
            blocked = run("Stop", {"IDE_AGENT_TABS_ID": ID})
            self.assertEqual([blocked.returncode, blocked.stderr], [0, b""])
            self.assertEqual(json.loads(blocked.stdout)["decision"], "block", attempt)
            with open(cache, encoding="utf-8") as f:
                self.assertTrue(os.path.isfile(f.read().strip()))
        with open(cache, "w", encoding="utf-8") as f:
            f.write(os.path.join(home, "missing-python") + "\n")
        self.assertEqual(json.loads(run("Stop", {"IDE_AGENT_TABS_ID": ID}).stdout)["decision"], "block")
        for extra in ({}, {"IDE_AGENT_TABS_ID": ID, "IDE_AGENT_TABS_MOD": ID}, {"IDE_AGENT_TABS_ID": ID, "IDE_AGENT_TABS_HOOKS": "1"}):
            quiet = run("Stop", extra)
            self.assertEqual([quiet.returncode, quiet.stdout.strip(), quiet.stderr], [0, b"", b""])

    def test_the_hook_commands_in_hooks_json_run_the_python_hook_through_sh(self) -> None:
        shell = git_bash() if sys.platform == "win32" else "/bin/sh"
        if shell is None or not os.path.exists(shell):
            self.skipTest("no POSIX shell")
        self.check_hook_commands([shell, "-c"])

    @unittest.skipUnless(sys.platform == "win32", "Claude Code runs hooks in PowerShell only on Windows without Git Bash")
    def test_the_hook_commands_in_hooks_json_run_the_python_hook_through_powershell(self) -> None:
        for name in ("powershell", "pwsh"):
            shell = shutil.which(name)
            if shell is None:
                continue
            with self.subTest(shell=name):
                self.check_hook_commands([shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command"])

    def test_the_tab_settings_run_the_plugin_hooks_in_exec_form_and_set_the_variable_that_quiets_them(self) -> None:
        with open(os.path.join(ROOT, "claude-plugin", "hooks", "hooks.json"), encoding="utf-8") as f:
            plugin = json.load(f)["hooks"]
        ours = {
            event: (g.get("matcher"), h["timeout"])
            for event, groups in plugin.items()
            for g in groups
            for h in g["hooks"]
            if "agent-hook.ps1" in h.get("command", "")
        }
        settings = claude_tab_settings(HookTarget(sys.executable, HOOK_SCRIPT, sys.platform))
        self.assertEqual(settings["env"], {"IDE_AGENT_TABS_HOOKS": "1"})
        tab = {event: (groups[0].get("matcher"), groups[0]["hooks"][0]["timeout"]) for event, groups in settings["hooks"].items()}
        self.assertEqual(tab, ours)
        home = temp_home(self, "iat-hook-")
        mail(home)
        clean = {k: v for k, v in os.environ.items() if not k.startswith("IDE_AGENT_TABS")}
        env = {**clean, "IDE_AGENT_TABS_HOME": home, **settings["env"], "IDE_AGENT_TABS_ID": ID}
        handler = settings["hooks"]["Stop"][0]["hooks"][0]
        self.assertEqual(handler["args"], ["-I", "-S", HOOK_SCRIPT, "claude", "Stop"])
        out = subprocess.run([handler["command"], *handler["args"]], input=b"{}", capture_output=True, env=env, timeout=30, check=False)
        self.assertEqual(json.loads(out.stdout)["decision"], "block")

    def test_the_hook_imports_no_heavy_modules(self) -> None:
        code = (
            "import sys; sys.path.insert(0, sys.argv[1]); import ide_agent_tabs.messaging.hook; "
            "print(sorted(m for m in ('shutil', 'subprocess', 'secrets', 'hashlib', 'ctypes', 'random', 'asyncio') if m in sys.modules))"
        )
        out = subprocess.run(
            [sys.executable, "-I", "-S", "-c", code, os.path.join(ROOT, "claude-plugin", "mcp", "src")], capture_output=True, check=True
        )
        self.assertEqual(out.stdout.decode().strip(), "[]")


if __name__ == "__main__":
    unittest.main()

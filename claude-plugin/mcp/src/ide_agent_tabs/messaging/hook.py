from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import Any

from ..clock import now_ms
from .db import HOOK_DEADLINE_MS, MailError
from .notice import unread_reminder
from .sessions import (
    Presence,
    effective_state,
    is_effort,
    is_mod_driven,
    is_model,
    is_session_id,
    read_presence,
    update_presence,
    with_state,
)
from .store import peek_unread

MAX_NUDGES = 3

Action = Mapping[str, Any]

_BUSY: Action = {"state": "busy"}
_PROMPT: Action = {"state": "busy", "prompt": True, "remind": True}
_AFTER_TOOL: Action = {"state": "busy", "remind": True}
_STOP: Action = {"stop": True}
_STARTED: Action = {"start": True, "state": "idle", "remind": True}
_TOOL_FAILED: Action = {"failure": True, "remind": True}
_INVOCATION: Action = {"invocation": True, "state": "busy", "remind": True}
_SETTLED: Action = {"state": "idle"}
_PERMISSION: Action = {"state": "permission"}
_NOTIFICATION: Action = {"notification": True}

HOOK_EVENTS: dict[str, dict[str, Action]] = {
    "claude": {
        "SessionStart": {**_STARTED, "inputIdle": False},
        "UserPromptSubmit": {**_PROMPT, "inputIdle": False},
        "PostToolUse": _AFTER_TOOL,
        "PostToolUseFailure": _TOOL_FAILED,
        "Notification": _NOTIFICATION,
        "Stop": {**_STOP, "inputIdle": False},
        "StopFailure": {"state": "idle", "inputIdle": False},
    },
    "codex": {
        "UserPromptSubmit": _PROMPT,
        "PermissionRequest": _PERMISSION,
        "PostToolUse": _AFTER_TOOL,
        "Stop": _STOP,
        "Interrupt": _SETTLED,
    },
    "copilot": {
        "sessionStart": {**_STARTED, "owner": True},
        "userPromptSubmitted": {"state": "busy", "prompt": True},
        "preToolUse": _BUSY,
        "notification": _NOTIFICATION,
        "postToolUse": _AFTER_TOOL,
        "agentStop": _STOP,
    },
    "agy": {"PreInvocation": _INVOCATION, "PostToolUse": _BUSY, "Stop": _STOP},
    "grok": {
        "UserPromptSubmit": {"state": "busy", "prompt": True, "inputIdle": False},
        "PreToolUse": _BUSY,
        "PostToolUse": _AFTER_TOOL,
        "Notification": _NOTIFICATION,
        "Stop": {**_STOP, "inputIdle": False},
        "StopCancelled": {**_SETTLED, "inputIdle": False},
        "StopFailure": {**_SETTLED, "inputIdle": False},
    },
    "hermes": {
        "pre_llm_call": _PROMPT,
        "post_tool_call": _BUSY,
        "pre_approval_request": _PERMISSION,
        "post_approval_response": _BUSY,
        "pre_verify": _STOP,
        "on_session_end": _SETTLED,
    },
    "qwen": {
        "UserPromptSubmit": _PROMPT,
        "PreToolUse": _BUSY,
        "PostToolUse": _AFTER_TOOL,
        "PermissionRequest": _PERMISSION,
        "Notification": _NOTIFICATION,
        "Stop": _STOP,
    },
    "goose": {"UserPromptSubmit": {"state": "busy", "prompt": True}, "PostToolUse": _BUSY, "Stop": _STOP},
}

# Claude Code and Grok Build send idle_prompt once the input has sat unused after a turn end.
_INPUT_IDLE_CLIS = ("claude", "grok")
_SESSION_FIELDS = ("session_id", "sessionId", "conversationId")
_IN_TURN = ("busy", "permission")


def is_hook_cli(cli: str) -> bool:
    return cli in HOOK_EVENTS


def _notification_state(data: Mapping[str, Any]) -> str | None:
    kind = next((v for v in (data.get("notification_type"), data.get("notificationType"), data.get("type")) if isinstance(v, str)), None)
    if kind in ("permission_prompt", "ToolPermission", "elicitation_dialog"):
        return "permission"
    if kind == "idle_prompt":
        return "idle"
    return None


def _context_output(cli: str, event: str, text: str) -> dict[str, Any]:
    if cli == "agy":
        return {"injectSteps": [{"ephemeralMessage": text}]}
    if cli == "hermes":
        return {"context": text}
    if cli == "copilot":
        return {"additionalContext": text}
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}


def _stop_output(cli: str, reason: str) -> dict[str, Any]:
    return {"decision": "continue" if cli == "agy" else "block", "reason": reason}


def _agent_session(data: Mapping[str, Any]) -> str | None:
    return next((v for v in (data.get(f) for f in _SESSION_FIELDS) if isinstance(v, str) and v != ""), None)


_FOREIGN = object()


# A headless agent started from inside a tab inherits IDE_AGENT_TABS_ID, so its hooks name the tab too. Such a
# child runs inside the tab agent's turn, while a session switch in the tab (/clear, /new, resume) happens
# between turns, so a new agent session takes the tab over only while it is not mid-turn. That needs no start
# hook, which Antigravity CLI lacks and Codex fires only at the first turn.
def _owned_by(cli: str, base: Presence, action: Action, data: Mapping[str, Any], now: float) -> object:
    agent = base.get("agent")
    if agent is not None and is_hook_cli(agent) and agent != cli:
        return _FOREIGN
    session = _agent_session(data)
    owner = base.get("owner")
    if session is None or owner is None or owner == session:
        return session
    if action.get("start") and data.get("source") is not None and data.get("source") != "startup":
        return session
    return _FOREIGN if effective_state(base, now) in _IN_TURN else session


# A turn end comes while the user may already be typing the next prompt; the idle_prompt notification of
# Claude Code and Grok Build is the one signal that the input has sat unused. A session that just started has no typing yet.
def _input_idle_after(cli: str, action: Action, data: Mapping[str, Any]) -> bool | None:
    if action.get("notification"):
        return True if cli in _INPUT_IDLE_CLIS and _notification_state(data) == "idle" else None
    if action.get("start") and action.get("inputIdle") is not None:
        return data.get("source") == "startup"
    return action.get("inputIdle")


def reported_model(data: Mapping[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    model = next((v for v in (data.get("model"), data.get("modelName")) if isinstance(v, str) and is_model(v)), None)
    if model is not None:
        out["model"] = model

    def level(v: object) -> object:
        return v.get("level") if isinstance(v, dict) else v

    effort = next((v for v in (level(data.get("effort")), data.get("reasoning_effort")) if isinstance(v, str) and is_effort(v)), None)
    if effort is not None:
        out["effort"] = effort
    return out


def _hook_state(action: Action, data: Mapping[str, Any]) -> str | None:
    if action.get("notification"):
        return _notification_state(data)
    if action.get("failure"):
        return "idle" if data.get("is_interrupt") is True else "busy"
    return action.get("state")


def _unread_for(action: Action, home: str, session_id: str, at: float) -> list[dict[str, Any]]:
    if not (action.get("remind") or action.get("stop")):
        return []
    try:
        return peek_unread(home, session_id, at, HOOK_DEADLINE_MS)
    except (MailError, sqlite3.Error, OSError):
        return []


class _PresenceChange:
    def __init__(
        self,
        cli: str,
        session_id: str,
        action: Action,
        data: Mapping[str, Any],
        at: float,
        agent_process: tuple[int, int] | None,
        unread: list[dict[str, Any]],
    ) -> None:
        self.cli = cli
        self.session_id = session_id
        self.action = action
        self.data = data
        self.at = at
        self.agent_process = agent_process
        self.unread = unread
        self.input_idle = _input_idle_after(cli, action, data)
        self.state = _hook_state(action, data)
        self.reminder = unread_reminder(unread)
        self.reported = reported_model(data)
        self.block = False
        self.remind = False
        self.foreign = False

    def __call__(self, current: Presence | None) -> Presence | None:
        owned = (
            _FOREIGN
            if current is not None and is_mod_driven(current, self.at)
            else _owned_by(self.cli, current or {"id": self.session_id}, self.action, self.data, self.at)
        )
        if owned is _FOREIGN:
            self.foreign = True
            return current
        base = self._base(current, owned)
        if self.action.get("stop"):
            return self._after_stop(base)
        if self.state is None:
            return current if self._unchanged(current, owned) else base
        return self._after_event(base, self.state)

    def _base(self, current: Presence | None, owned: object) -> Presence:
        base: Presence = {**(current or {"id": self.session_id})}
        if owned is not None:
            base["owner"] = owned
        if self.input_idle is not None:
            base["inputIdle"] = self.input_idle
        base.update(self.reported)
        if self.agent_process is not None and self.action.get("owner"):
            base["agentPid"], base["agentPidStart"] = self.agent_process
        return base

    def _after_stop(self, base: Presence) -> Presence:
        nudges = base.get("nudges") or 0
        self.block = self.reminder is not None and nudges < MAX_NUDGES
        if self.block:
            return with_state(base, "busy", self.at, nudges + 1)
        return with_state(base, "idle", self.at, 0 if self.reminder is None else nudges)

    def _unchanged(self, current: Presence | None, owned: object) -> bool:
        known = current or {}
        learned = any(known.get(k) != v for k, v in self.reported.items())
        return (
            not learned
            and (owned is None or known.get("owner") == owned)
            and (self.input_idle is None or known.get("inputIdle") == self.input_idle)
        )

    def _after_event(self, base: Presence, state: str) -> Presence:
        prompt = self.action.get("prompt") or (self.action.get("invocation") and self.data.get("invocationNum") == 0)
        following = with_state(base, state, self.at, 0 if prompt else None)
        if not self.action.get("remind"):
            return following
        seen = set(base.get("reminded") or [])
        self.remind = any(m["id"] not in seen for m in self.unread)
        rest = {k: v for k, v in following.items() if k != "reminded"}
        return {**rest, "reminded": [m["id"] for m in self.unread]} if self.unread else rest

    def output(self, event: str) -> dict[str, Any] | None:
        if self.foreign:
            return None
        if self.action.get("stop"):
            return _stop_output(self.cli, f"Agent Tabs kept this turn open. {self.reminder}") if self.block else None
        if self.remind and self.reminder is not None:
            return _context_output(self.cli, event, self.reminder)
        return None


# A Copilot tab on the shared server sends no process id, so its start hook records the Copilot process, and the
# server ends the tab's session when that process exits.
def run_hook(
    cli: str,
    event: str,
    data: Mapping[str, Any],
    home: str,
    session_id: str | None,
    now: float | None = None,
    agent_process: tuple[int, int] | None = None,
) -> dict[str, Any] | None:
    if not session_id or not is_session_id(session_id) or not is_hook_cli(cli):
        return None
    action = HOOK_EVENTS[cli].get(event)
    if action is None:
        return None
    at = now_ms() if now is None else now
    before = read_presence(home, session_id)
    if before is not None and is_mod_driven(before, at):
        return None
    change = _PresenceChange(cli, session_id, action, data, at, agent_process, _unread_for(action, home, session_id, at))
    update_presence(home, session_id, change)
    return change.output(event)

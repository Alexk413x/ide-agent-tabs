from __future__ import annotations

import json
import os
import threading
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Callable, NamedTuple

from .jsjson import js_round, stringify

if TYPE_CHECKING:
    from .jev.inputs import Schema
    from .messaging.history import Who
    from .service import Progress

SERVER_NAME = "ide-agent-tabs"
HOOK_TOOL = "agent_tabs_hook"
MOD_TOOL = "agent_tabs_mod"
CATALOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "catalog.json")
INVALID_PARAMS = -32602
JEV_PREFIX = "jev_"


class Call(NamedTuple):
    meta: Mapping[str, Any]
    cancel: threading.Event
    notify: Callable[[str, dict[str, Any]], None]


class Catalog(NamedTuple):
    tools: list[dict[str, Any]]
    only: dict[str, list[str]]
    instructions: dict[str, str]


_catalog: Catalog | None = None


def catalog() -> Catalog:
    global _catalog
    if _catalog is None:
        with open(CATALOG_FILE, encoding="utf-8") as f:
            data = json.load(f)
        _catalog = Catalog(data["tools"], data["only"], data["instructions"])
    return _catalog


def server_instructions(jev: bool) -> str:
    return catalog().instructions["jev" if jev else "plain"]


def _offered(name: str, agent: str | None, jev: bool) -> bool:
    only = catalog().only
    return (jev or not name.startswith(JEV_PREFIX)) and (name not in only or agent in only[name])


def tools_for(client_name: str | None, jev: bool) -> list[dict[str, Any]]:
    from .messaging.sessions import agent_from_client

    agent = agent_from_client(client_name)
    return [t for t in catalog().tools if _offered(t["name"], agent, jev)]


def tool_for(client_name: str | None, name: str, jev: bool) -> dict[str, Any] | None:
    from .messaging.sessions import agent_from_client

    tool = next((t for t in catalog().tools if t["name"] == name), None)
    return tool if tool is not None and _offered(name, agent_from_client(client_name), jev) else None


def text_result(text: str, error: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if error:
        result["isError"] = True
    return result


def error_message(e: BaseException) -> str:
    return str(e) if e.args else type(e).__name__


class McpError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"MCP error {code}: {message}")
        self.code = code


_schemas: dict[str, Schema | None] = {}
_schemas_lock = threading.Lock()


def _schema(name: str, tool: Mapping[str, Any]) -> Schema | None:
    from .jev.inputs import INPUTS
    from .tool_input import compile_schema

    with _schemas_lock:
        if name not in _schemas:
            if name in INPUTS:
                _schemas[name] = INPUTS[name]
            elif "$schema" in tool["inputSchema"]:
                _schemas[name] = compile_schema(tool["inputSchema"])
            else:
                _schemas[name] = None
        return _schemas[name]


def need(op: str, value: Any, field: str) -> Any:
    if value is None:
        raise ValueError(f"{op} needs {field}")
    return value


def _who(given: Mapping[str, Any]) -> Who:
    from .messaging.history import Who

    return Who(given.get("session"), list(given.get("names") or []))


class ToolDeps(NamedTuple):
    service: Any
    jev: Any
    messaging: Any
    handoffs: Any
    resumes: Any


class Tools:
    def __init__(self, deps: Callable[[], ToolDeps], jev_enabled: bool) -> None:
        self._deps = deps
        self.jev_enabled = jev_enabled

    def instructions(self) -> str:
        return server_instructions(self.jev_enabled)

    def list(self, client_name: str | None) -> list[dict[str, Any]]:
        return tools_for(client_name, self.jev_enabled)

    def call(self, client_name: str | None, name: str, arguments: Any, call: Call) -> dict[str, Any]:
        from .jev.inputs import ABSENT
        from .tool_input import issues_text, parse_args

        try:
            tool = tool_for(client_name, name, self.jev_enabled)
            if tool is None:
                raise McpError(INVALID_PARAMS, f"Tool {name} not found")
            schema = _schema(name, tool)
            args: dict[str, Any] = {}
            if schema is not None:
                parsed, issues = parse_args(schema, ABSENT if arguments is None else arguments)
                if issues:
                    raise McpError(INVALID_PARAMS, f"Input validation error: Invalid arguments for tool {name}: {issues_text(issues)}")
                args = parsed
            deps = self._deps()
            if name == HOOK_TOOL:
                return self._hook(deps, args, call)
            deps.messaging.note_thread(call.meta.get("threadId"))
            return text_result(stringify(self._run(deps, name, args, call)))
        except Exception as e:  # noqa: BLE001
            return text_result(error_message(e), error=True)

    def _hook(self, deps: ToolDeps, args: dict[str, Any], call: Call) -> dict[str, Any]:
        thread = call.meta.get("threadId")
        deps.messaging.note_thread(thread if thread is not None else args.get("session_id"))
        output = deps.messaging.hook(args["event"], dict(args))
        return {"content": [] if output is None else [{"type": "text", "text": stringify(output)}]}

    def _progress(self, call: Call) -> Progress | None:
        token = call.meta.get("progressToken")
        if token is None:
            return None

        def report(elapsed_ms: float, total_ms: float, message: str) -> None:
            if call.cancel.is_set():
                return
            call.notify(
                "notifications/progress",
                {"progressToken": token, "progress": js_round(elapsed_ms / 1000), "total": js_round(total_ms / 1000), "message": message},
            )

        return report

    def _run(self, deps: ToolDeps, name: str, args: dict[str, Any], call: Call) -> Any:
        service = deps.service
        messaging = deps.messaging
        runs: dict[str, Callable[[], Any]] = {
            "list_agents": lambda: service.list_agents(),
            "list_tabs": lambda: service.list_tabs(args.get("ide")),
            "open_tab": lambda: service.open_tab(args, wait="background", on_progress=self._progress(call)),
            "close_tab": lambda: self._close_tab(deps, args.get("id")),
            MOD_TOOL: lambda: self._mod(deps, args),
            "list_sessions": lambda: messaging.list_sessions(),
            "send_message": lambda: messaging.send(args),
            "read_messages": lambda: messaging.read(call.cancel),
            "wait_for_message": lambda: messaging.wait(args, call.cancel),
            "handoff": lambda: deps.handoffs.start(args),
            "closed_sessions": lambda: deps.resumes.list(),
            "resume_tab": lambda: deps.resumes.resume(args),
        }
        run = runs.get(name)
        if run is not None:
            return run()
        if name.startswith(JEV_PREFIX) and deps.jev is not None:
            from .jev.tools import RUNNERS

            return RUNNERS[name](deps.jev, args)
        raise McpError(INVALID_PARAMS, f"Tool {name} not found")

    def _close_tab(self, deps: ToolDeps, tab_id: str | None) -> Any:
        deps.handoffs.check_close(tab_id, deps.messaging.id)
        return deps.service.close_tab(tab_id)

    def _mod(self, deps: ToolDeps, given: dict[str, Any]) -> Any:
        messaging = deps.messaging
        op = given["op"]
        ops: dict[str, Callable[[], Any]] = {
            "presence": lambda: messaging.mod_presence(_presence_fields(given)),
            "unread": lambda: messaging.mod_unread(),
            "send": lambda: messaging.send(_sent(op, given)),
            "take": lambda: messaging.mod_take(),
            "ack": lambda: messaging.mod_settle(need(op, given.get("claim"), "claim"), op),
            "release": lambda: messaging.mod_settle(need(op, given.get("claim"), "claim"), op),
            "sessions": lambda: messaging.list_sessions(),
            "log": lambda: messaging.mod_log(_logged(op, given)),
            "history": lambda: messaging.mod_history(_who(given), given.get("before")),
            "message": lambda: messaging.mod_message(_who(given), need(op, given.get("id"), "id"), given.get("offset") or 0),
            "reveal": lambda: deps.service.reveal(need(op, given.get("path"), "path"), messaging.host_id(), messaging.session_folders()),
            "counts": lambda: messaging.mod_counts([_who(a) for a in given.get("agents") or []]),
            "settings": lambda: {"claudeMod": deps.service.settings().terminal.claude_mod},
        }
        run = ops.get(op)
        if run is None:
            raise ValueError(f"unknown op {op}")
        return run()


def _presence_fields(given: Mapping[str, Any]) -> dict[str, Any]:
    fields = {k: given[k] for k in ("driver", "nativeName", "state", "model", "effort", "agentType", "agentColor") if k in given}
    if "session" in given:
        fields["owner"] = given["session"]
    return fields


def _sent(op: str, given: Mapping[str, Any]) -> dict[str, Any]:
    sent = {"to": need(op, given.get("to"), "to"), "text": need(op, given.get("text"), "text")}
    if given.get("replyTo") is not None:
        sent["replyTo"] = given["replyTo"]
    return sent


def _logged(op: str, given: Mapping[str, Any]) -> dict[str, Any]:
    logged = {
        "direction": need(op, given.get("direction"), "direction"),
        "peer": need(op, given.get("peer"), "peer"),
        "text": need(op, given.get("text"), "text"),
    }
    for key in ("id", "at", "delivery"):
        if given.get(key) is not None:
            logged[key] = given[key]
    return logged

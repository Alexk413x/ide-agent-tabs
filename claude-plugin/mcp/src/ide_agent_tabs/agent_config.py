from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Any, NamedTuple

from .jsjson import stringify
from .messaging.hook import HOOK_EVENTS
from .profiles import AGENT_ENV, TAB_ID_ENV
from .yaml_block import YamlDoc

HOOK_AGENTS = ("codex", "gemini", "copilot", "agy", "grok", "hermes", "qwen", "goose")
SETTINGS_AGENTS = ("codex", "gemini", "qwen")
AGY_HOOK_GROUP = "ide-agent-tabs"
# Only tools that read or message: open_tab, close_tab and handoff start or stop agents, and jev_ sends text
# off the machine, so those keep Antigravity CLI's own per-call confirmation.
AGY_ALLOW_RULES = tuple(
    f"mcp(ide-agent-tabs/{tool})"
    for tool in ("send_message", "read_messages", "wait_for_message", "list_sessions", "list_agents", "list_tabs")
)
AGY_WILDCARD_RULE = "mcp(ide-agent-tabs/*)"
COPILOT_HOOKS_FILE = "ide-agent-tabs.json"
GROK_HOOKS_FILE = "ide-agent-tabs.json"
GOOSE_PLUGIN = "ide-agent-tabs"
CODEX_ENV_VARS = (TAB_ID_ENV, AGENT_ENV, "IDE_AGENT_TABS_HOME")
CODEX_TOOL_TIMEOUT_S = 660
HOOK_TIMEOUT_S = 5
PYTHON_FLAGS = ("-I", "-S")

_CONTROL = re.compile("[\x00-\x1f\x7f-\x9f]")
_SHELL_UNSAFE = re.compile('["%$`!\x00-\x1f\x7f-\x9f]')
_AGY_UNSAFE = re.compile('[\\s"%^&|<>()!\x00-\x1f\x7f-\x9f]')
_BARE_WINDOWS = re.compile(r"[A-Za-z0-9_\-./:\\]+\Z")
_BARE_POSIX = re.compile(r"[A-Za-z0-9_\-./:]+\Z")
_POSIX_SAFE = re.compile(r"[A-Za-z0-9_@%+=:,./-]+\Z")
_HOOK_PATH_END = re.compile(r"(?:[\\/]agent-hook\.mjs|[\\/]mcp[\\/]py[\\/]launch[\\/]agent_hook\.py)(?:[\"'\s]|$)")
_CODEX_TABLE = re.compile(r"^\[\s*mcp_servers\s*\.\s*(?:ide-agent-tabs|\"ide-agent-tabs\"|'ide-agent-tabs')\s*\]\s*(?:#.*)?$")


class ConfigError(ValueError):
    pass


class HookTarget(NamedTuple):
    python: str
    path: str
    platform: str


def is_hook_agent(agent: str) -> bool:
    return agent in HOOK_AGENTS


def _env(env: Mapping[str, str], name: str) -> str | None:
    return env.get(name) or None


def grok_home(env: Mapping[str, str], user_home: str) -> str:
    return _env(env, "GROK_HOME") or os.path.join(user_home, ".grok")


def hermes_home(env: Mapping[str, str], user_home: str, platform: str) -> str:
    override = _env(env, "HERMES_HOME")
    if override:
        return override
    if platform == "win32":
        return os.path.join(_env(env, "LOCALAPPDATA") or os.path.join(user_home, "AppData", "Local"), "hermes")
    return os.path.join(user_home, ".hermes")


def qwen_home(env: Mapping[str, str], user_home: str) -> str:
    override = _env(env, "QWEN_HOME")
    if not override:
        return os.path.join(user_home, ".qwen")
    return os.path.abspath(re.sub(r"^~(?=$|[\\/])", lambda _: user_home, override))


def goose_root(env: Mapping[str, str]) -> str | None:
    root = _env(env, "GOOSE_PATH_ROOT")
    return root if root and os.path.isabs(root) else None


def goose_plugin_dir(env: Mapping[str, str], user_home: str) -> str:
    return os.path.join(goose_root(env) or user_home, ".agents", "plugins", GOOSE_PLUGIN)


def hermes_allowlist_file(env: Mapping[str, str], user_home: str, platform: str) -> str:
    return os.path.join(hermes_home(env, user_home, platform), "shell-hooks-allowlist.json")


def agy_settings_file(user_home: str) -> str:
    return os.path.join(user_home, ".gemini", "antigravity-cli", "settings.json")


def hook_config_file(agent: str, env: Mapping[str, str], user_home: str, platform: str) -> str:
    if agent == "codex":
        return os.path.join(_env(env, "CODEX_HOME") or os.path.join(user_home, ".codex"), "hooks.json")
    if agent == "gemini":
        return os.path.join(_env(env, "GEMINI_CLI_HOME") or user_home, ".gemini", "settings.json")
    if agent == "copilot":
        return os.path.join(_env(env, "COPILOT_HOME") or os.path.join(user_home, ".copilot"), "hooks", COPILOT_HOOKS_FILE)
    if agent == "agy":
        return os.path.join(user_home, ".gemini", "config", "hooks.json")
    if agent == "grok":
        return os.path.join(grok_home(env, user_home), "hooks", GROK_HOOKS_FILE)
    if agent == "hermes":
        return os.path.join(hermes_home(env, user_home, platform), "config.yaml")
    if agent == "qwen":
        return os.path.join(qwen_home(env, user_home), "settings.json")
    if agent == "goose":
        return os.path.join(goose_plugin_dir(env, user_home), "hooks", "hooks.json")
    raise ConfigError(f"{agent} takes no hooks")


# Codex, Gemini CLI, Qwen Code and Grok Build run a hook command through a shell (cmd.exe or PowerShell on
# Windows), so the paths may not hold characters any of those shells expands inside double quotes. PowerShell
# reads a quoted first word as a string, not a command, so the interpreter goes in bare or as `py -3`.
def hook_command(target: HookTarget, agent: str, event: str) -> str:
    for part in (target.python, target.path):
        if _SHELL_UNSAFE.search(part):
            raise ConfigError(f"can't put the hook path in a shell command: {part}")
    windows = target.platform == "win32"
    if (_BARE_WINDOWS if windows else _BARE_POSIX).match(target.python):
        head = target.python
    else:
        head = "py -3" if windows else f'"{target.python}"'
    return f'{head} {" ".join(PYTHON_FLAGS)} "{target.path}" {agent} {event}'


def mentions_hook(part: Any) -> bool:
    return isinstance(part, str) and _HOOK_PATH_END.search(part) is not None


def _is_ours(handler: Any) -> bool:
    if not isinstance(handler, dict):
        return False
    args = handler.get("args")
    return any(mentions_hook(p) for p in [handler.get("command"), handler.get("exec"), *(args if isinstance(args, list) else [])])


def _our_handler(agent: str, target: HookTarget, event: str) -> dict[str, Any]:
    command = hook_command(target, agent, event)
    if agent == "gemini":
        return {"type": "command", "name": "ide-agent-tabs", "command": command, "timeout": HOOK_TIMEOUT_S * 1000}
    return {"type": "command", "command": command, "timeout": HOOK_TIMEOUT_S}


def _without_ours(hooks: dict[str, Any]) -> dict[str, Any]:
    kept: dict[str, Any] = {}
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            kept[event] = groups
            continue
        left: list[Any] = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                left.append(group)
                continue
            handlers = [h for h in group["hooks"] if not _is_ours(h)]
            if len(handlers) == len(group["hooks"]):
                left.append(group)
            elif handlers:
                left.append({**group, "hooks": handlers})
        if left:
            kept[event] = left
    return kept


def has_any_settings_hooks(root: dict[str, Any] | None) -> bool:
    hooks = (root or {}).get("hooks")
    return isinstance(hooks, dict) and stringify(_without_ours(hooks)) != stringify(hooks)


def merge_hook_settings(root: dict[str, Any], file: str, agent: str, target: HookTarget | None) -> dict[str, Any]:
    current = root.get("hooks", {})
    if not isinstance(current, dict):
        raise ConfigError(f'{file}: "hooks" isn\'t an object')
    hooks = _without_ours(current)
    if target is not None:
        for event in HOOK_EVENTS[agent]:
            existing = hooks.get(event)
            hooks[event] = [*(existing if isinstance(existing, list) else []), {"hooks": [_our_handler(agent, target, event)]}]
    nxt = dict(root)
    if hooks:
        nxt["hooks"] = hooks
    else:
        nxt.pop("hooks", None)
    return nxt


def has_our_hooks(root: dict[str, Any], agent: str, target: HookTarget) -> bool:
    hooks = root.get("hooks")
    if not isinstance(hooks, dict):
        return False
    for event in HOOK_EVENTS[agent]:
        groups = hooks.get(event)
        want = hook_command(target, agent, event)
        if not isinstance(groups, list) or not any(
            isinstance(g, dict)
            and isinstance(g.get("hooks"), list)
            and any(isinstance(h, dict) and h.get("command") == want for h in g["hooks"])
            for g in groups
        ):
            return False
    return True


_CLAUDE_MATCHERS = {"SessionStart": "startup|resume|clear", "Notification": "permission_prompt|idle_prompt"}


def claude_tab_settings(target: HookTarget) -> dict[str, Any]:
    def group(event: str) -> dict[str, Any]:
        handler = {
            "type": "command",
            "command": target.python,
            "args": [*PYTHON_FLAGS, target.path, "claude", event],
            "timeout": HOOK_TIMEOUT_S,
        }
        matcher = _CLAUDE_MATCHERS.get(event)
        return {"matcher": matcher, "hooks": [handler]} if matcher is not None else {"hooks": [handler]}

    return {"hooks": {event: [group(event)] for event in HOOK_EVENTS["claude"]}}


def copilot_hooks(target: HookTarget) -> dict[str, Any]:
    return {
        "version": 1,
        "hooks": {
            event: [
                {
                    "type": "command",
                    "exec": target.python,
                    "args": [*PYTHON_FLAGS, target.path, "copilot", event],
                    "timeoutSec": HOOK_TIMEOUT_S,
                }
            ]
            for event in HOOK_EVENTS["copilot"]
        },
    }


# Antigravity CLI hands the command to cmd.exe with its quotes escaped, so a quoted path reaches the
# interpreter with the quotes in it; the path goes in bare and may not hold anything cmd.exe splits or expands.
# It also refuses a command path with a space, so on Windows it starts Python through `py -3`.
def agy_hook_command(target: HookTarget, event: str) -> str:
    if _AGY_UNSAFE.search(target.path):
        raise ConfigError(f"can't put the hook path in an Antigravity CLI hook command: {target.path}")
    return f"{' '.join(agy_python(target.python, target.platform))} {' '.join(PYTHON_FLAGS)} {target.path} agy {event}"


def agy_python(python: str, platform: str) -> list[str]:
    if platform == "win32":
        return ["py", "-3"]
    return [python] if not _AGY_UNSAFE.search(python) else ["python3"]


def agy_hooks(target: HookTarget) -> dict[str, Any]:
    def handler(event: str) -> dict[str, Any]:
        return {"type": "command", "command": agy_hook_command(target, event), "timeout": HOOK_TIMEOUT_S}

    return {
        event: [{"matcher": "*", "hooks": [handler(event)]}] if event == "PostToolUse" else [handler(event)] for event in HOOK_EVENTS["agy"]
    }


def with_agy_hooks(root: dict[str, Any], target: HookTarget | None) -> dict[str, Any]:
    rest = {k: v for k, v in root.items() if k != AGY_HOOK_GROUP}
    return rest if target is None else {**rest, AGY_HOOK_GROUP: agy_hooks(target)}


def has_agy_hooks(root: dict[str, Any], target: HookTarget) -> bool:
    return AGY_HOOK_GROUP in root and stringify(root[AGY_HOOK_GROUP]) == stringify(agy_hooks(target))


def _has_any_agy_rule(root: dict[str, Any]) -> bool:
    permissions = root.get("permissions")
    allow = permissions.get("allow") if isinstance(permissions, dict) else None
    return isinstance(allow, list) and any(r == AGY_WILDCARD_RULE or r in AGY_ALLOW_RULES for r in allow)


def with_agy_allow_rule(root: dict[str, Any], file: str, allow: bool) -> dict[str, Any]:
    if not allow and not _has_any_agy_rule(root):
        return root
    permissions = root.get("permissions", {})
    if not isinstance(permissions, dict):
        raise ConfigError(f'{file}: "permissions" isn\'t an object')
    rules = permissions.get("allow", [])
    if not isinstance(rules, list):
        raise ConfigError(f'{file}: "permissions.allow" isn\'t a list')
    kept = [r for r in rules if r != AGY_WILDCARD_RULE and r not in AGY_ALLOW_RULES]
    return {**root, "permissions": {**permissions, "allow": [*kept, *AGY_ALLOW_RULES] if allow else kept}}


def has_agy_allow_rule(root: dict[str, Any] | None) -> bool:
    permissions = (root or {}).get("permissions")
    allow = permissions.get("allow") if isinstance(permissions, dict) else None
    return isinstance(allow, list) and (all(r in allow for r in AGY_ALLOW_RULES) or AGY_WILDCARD_RULE in allow)


# Codex passes a stdio MCP server only a fixed set of environment variables; env_vars forwards more by name.
# Its default tool timeout of 60 seconds would cut wait_for_message short.
def with_codex_settings(text: str, file: str) -> str | None:
    eol = "\r\n" if "\r\n" in text else "\n"
    lines = re.split(r"\r?\n", text)
    start = next((i for i, line in enumerate(lines) if _CODEX_TABLE.search(line.strip())), -1)
    if start < 0:
        raise ConfigError(f"{file} has no [mcp_servers.ide-agent-tabs] table")
    end = next((i for i, line in enumerate(lines) if i > start and re.match(r"^\s*\[", line)), len(lines))
    table = lines[start + 1 : end]
    env_vars = next((line for line in table if re.match(r"^\s*env_vars\s*=", line)), None)
    if env_vars is not None and not all(f'"{name}"' in env_vars for name in CODEX_ENV_VARS):
        raise ConfigError(f"{file}: [mcp_servers.ide-agent-tabs] already sets env_vars; add {', '.join(CODEX_ENV_VARS)} to it by hand")
    add: list[str] = []
    if env_vars is None:
        names = ", ".join(f'"{n}"' for n in CODEX_ENV_VARS)
        add.append(f"env_vars = [{names}]")
    if not any(re.match(r"^\s*tool_timeout_sec\s*=", line) for line in table):
        add.append(f"tool_timeout_sec = {CODEX_TOOL_TIMEOUT_S}")
    if not add:
        return None
    lines[start + 1 : start + 1] = add
    return eol.join(lines)


def has_codex_settings(text: str | None) -> bool:
    if text is None:
        return False
    try:
        return with_codex_settings(text, "") is None
    except ValueError:
        return False


_GROK_MATCHERS = {"Notification": "permission_prompt|idle_prompt"}


def grok_hooks(target: HookTarget) -> dict[str, Any]:
    return {
        "hooks": {
            event: [
                {
                    **({"matcher": _GROK_MATCHERS[event]} if event in _GROK_MATCHERS else {}),
                    "hooks": [{"type": "command", "command": hook_command(target, "grok", event), "timeout": HOOK_TIMEOUT_S}],
                }
            ]
            for event in HOOK_EVENTS["grok"]
        }
    }


def posix_quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def posix_word(s: str) -> str:
    return s if _POSIX_SAFE.match(s) else posix_quote(s)


# Goose runs a hook command with sh -c and Hermes splits one with shlex.split (shell=False); both take a
# single-quoted POSIX word literally, backslashes included.
def posix_hook_command(target: HookTarget, agent: str, event: str) -> str:
    for part in (target.python, target.path):
        if _CONTROL.search(part):
            raise ConfigError(f"can't put the hook path in a hook command: {part}")
    return f"{posix_word(target.python)} {' '.join(PYTHON_FLAGS)} {posix_word(target.path)} {agent} {event}"


def goose_manifest() -> dict[str, Any]:
    return {"name": GOOSE_PLUGIN, "version": "1.0.0", "description": "Agent Tabs session state and message reminders"}


def goose_hooks(target: HookTarget) -> dict[str, Any]:
    return {
        "hooks": {
            event: [{"hooks": [{"type": "command", "command": posix_hook_command(target, "goose", event), "timeout": HOOK_TIMEOUT_S}]}]
            for event in HOOK_EVENTS["goose"]
        }
    }


def hermes_hook_items(target: HookTarget) -> list[dict[str, Any]]:
    return [{"event": e, "command": posix_hook_command(target, "hermes", e), "timeout": HOOK_TIMEOUT_S} for e in HOOK_EVENTS["hermes"]]


def _is_our_hermes_hook(item: Any) -> bool:
    return isinstance(item, dict) and mentions_hook(item.get("command"))


def with_hermes_hooks(doc: YamlDoc, target: HookTarget | None) -> None:
    doc.filter_lists("hooks", _is_our_hermes_hook)
    if target is None:
        return
    for item in hermes_hook_items(target):
        doc.add_list_item("hooks", item["event"], {"command": item["command"], "timeout": item["timeout"]})


def has_hermes_hooks(hooks: Any, target: HookTarget) -> bool:
    if not isinstance(hooks, dict):
        return False
    return all(
        isinstance(hooks.get(i["event"]), list) and any(isinstance(h, dict) and h.get("command") == i["command"] for h in hooks[i["event"]])
        for i in hermes_hook_items(target)
    )


def has_any_hermes_hooks(hooks: Any) -> bool:
    return isinstance(hooks, dict) and any(isinstance(v, list) and any(_is_our_hermes_hook(h) for h in v) for v in hooks.values())


# Hermes asks before it first runs each (event, command) pair and skips the hook when nobody can answer; the
# allowlist approves exactly these pairs and nothing else.
def with_hermes_approvals(root: dict[str, Any], file: str, target: HookTarget | None) -> dict[str, Any]:
    approvals = root.get("approvals", [])
    if not isinstance(approvals, list):
        raise ConfigError(f'{file}: "approvals" isn\'t a list')
    kept = [a for a in approvals if not (isinstance(a, dict) and mentions_hook(a.get("command")))]
    ours = [] if target is None else [{"event": i["event"], "command": i["command"]} for i in hermes_hook_items(target)]
    if target is None and len(kept) == len(approvals):
        return root
    return {**root, "approvals": [*kept, *ours]}


def has_hermes_approvals(root: dict[str, Any] | None, target: HookTarget) -> bool:
    approvals = (root or {}).get("approvals")
    return isinstance(approvals, list) and all(
        any(isinstance(a, dict) and a.get("event") == i["event"] and a.get("command") == i["command"] for a in approvals)
        for i in hermes_hook_items(target)
    )

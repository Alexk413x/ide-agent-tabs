from __future__ import annotations

import os
import re
import stat
import sys
from collections.abc import Mapping, Sequence
from typing import Any, Callable, NamedTuple

from .agent_config import (
    PYTHON_FLAGS,
    ConfigError,
    HookTarget,
    agy_python,
    agy_settings_file,
    claude_tab_settings,
    copilot_hooks,
    goose_hooks,
    goose_manifest,
    goose_plugin_dir,
    goose_root,
    grok_home,
    grok_hooks,
    has_agy_allow_rule,
    has_agy_hooks,
    has_any_hermes_hooks,
    has_any_settings_hooks,
    has_codex_settings,
    has_hermes_approvals,
    has_hermes_hooks,
    has_our_hooks,
    hermes_allowlist_file,
    hermes_home,
    hook_config_file,
    is_hook_agent,
    merge_hook_settings,
    qwen_home,
    with_agy_allow_rule,
    with_agy_hooks,
    with_codex_settings,
    with_hermes_approvals,
    with_hermes_hooks,
)
from .cli_run import cli_failure, run_cli_result
from .clock import now_ms
from .editor_clis import find_cli_on_path, path_var_of
from .files import ensure_private_dir, read_text_if_exists, remove_file, replace_retrying, write_atomically
from .installed import is_installed
from .jsjson import parse, stringify
from .processes import RunResult
from .profiles import AGENT_ENV, BUILTIN_PROFILES, CLAUDE_TAB_SETTINGS_FILE, TAB_ID_ENV
from .server_copy import (
    Source,
    build_name,
    copy_root,
    current_build,
    hook_copy_path,
    old_hook_path,
    old_server_path,
    refresh_server_copy,
    server_copy_path,
    slashes,
    source_files,
    source_hash,
    write_python,
)
from .toml_table import read_toml_table, toml_table, with_toml_table
from .yaml_block import YamlDoc, edit_yaml, yaml_value

SERVER_NAME = "ide-agent-tabs"
AGENTS = ("codex", "agy", "copilot", "gemini", "grok", "pi", "hermes", "opencode", "qwen", "goose")
CLI_TIMEOUT_S = 30.0
OPENCODE_SCHEMA = "https://opencode.ai/config.json"
OPENCODE_TIMEOUT_MS = 660_000
TOOL_TIMEOUT_S = 660
GOOSE_TIMEOUT_S = 700
QWEN_TIMEOUT_MS = 700_000
_SERVER_FILE = re.compile(r"\.(?:[cm]?js|py)$", re.IGNORECASE)

# The Codex desktop app shares ~/.codex with the CLI, and on Windows, Codex before 0.159 opens a console
# window for each MCP server the app starts.
CODEX_WINDOWS_REFUSAL = (
    "not registered on Windows: the Codex desktop app reads the same config and would open a console window for each session. "
    "Open Codex in an agent tab instead; each Codex tab brings its own Agent Tabs server and hooks"
)


class RegisterContext(NamedTuple):
    source: Source
    home: str
    platform: str
    env: Mapping[str, str]
    user_home: str
    python: str
    server_command: tuple[str, ...] | None = None


def is_agent_name(name: str) -> bool:
    return name in AGENTS


# Codex tabs bring their own hooks, and the shared Codex daemon would run global ones with another tab's
# IDE_AGENT_TABS_ID, so Codex gets none.
def takes_hooks(agent: str) -> bool:
    return is_hook_agent(agent) and agent != "codex"


def _profile_of(agent: str) -> tuple[str, str]:
    builtin = next((p for p in BUILTIN_PROFILES if p.name == agent), None)
    return (builtin.command, builtin.label) if builtin is not None else (agent, agent)


def config_file(
    agent: str, env: Mapping[str, str], user_home: str, exists: Callable[[str], bool] = os.path.exists, platform: str = sys.platform
) -> str:
    def var(name: str) -> str | None:
        return env.get(name) or None

    if agent == "codex":
        return os.path.join(var("CODEX_HOME") or os.path.join(user_home, ".codex"), "config.toml")
    if agent == "gemini":
        return os.path.join(var("GEMINI_CLI_HOME") or user_home, ".gemini", "settings.json")
    if agent == "copilot":
        return os.path.join(var("COPILOT_HOME") or os.path.join(user_home, ".copilot"), "mcp-config.json")
    if agent == "agy":
        return os.path.join(user_home, ".gemini", "config", "mcp_config.json")
    if agent == "opencode":
        folder = os.path.join(var("XDG_CONFIG_HOME") or os.path.join(user_home, ".config"), "opencode")
        plain = os.path.join(folder, "opencode.json")
        jsonc = os.path.join(folder, "opencode.jsonc")
        return jsonc if not exists(plain) and exists(jsonc) else plain
    if agent == "grok":
        return os.path.join(grok_home(env, user_home), "config.toml")
    if agent == "pi":
        return os.path.join(var("PI_CODING_AGENT_DIR") or os.path.join(user_home, ".pi", "agent"), "mcp.json")
    if agent == "hermes":
        return os.path.join(hermes_home(env, user_home, platform), "config.yaml")
    if agent == "qwen":
        return os.path.join(qwen_home(env, user_home), "settings.json")
    if agent == "goose":
        root = goose_root(env)
        if root is not None:
            return os.path.join(root, "config", "config.yaml")
        if platform == "win32":
            return os.path.join(var("APPDATA") or os.path.join(user_home, "AppData", "Roaming"), "Block", "goose", "config", "config.yaml")
        return os.path.join(user_home, ".config", "goose", "config.yaml")
    raise ValueError(f"unknown agent: {agent}")


def server_argv(ctx: RegisterContext) -> list[str]:
    if ctx.server_command is not None:
        return list(ctx.server_command)
    return [slashes(ctx.python, ctx.platform), *PYTHON_FLAGS, server_copy_path(ctx.home, ctx.platform)]


def hook_target(ctx: RegisterContext) -> HookTarget:
    return HookTarget(slashes(ctx.python, ctx.platform), hook_copy_path(ctx.home, ctx.platform), ctx.platform)


def register_args(argv: Sequence[str]) -> list[str]:
    return ["mcp", "add", SERVER_NAME, "--", *argv]


def unregister_args() -> list[str]:
    return ["mcp", "remove", SERVER_NAME]


def _tab_env() -> dict[str, str]:
    return {TAB_ID_ENV: f"${{{TAB_ID_ENV}}}", AGENT_ENV: f"${{{AGENT_ENV}}}"}


# Copilot CLI passes an MCP server only PATH from its environment; the rest must be named here.
def copilot_entry(argv: Sequence[str]) -> dict[str, Any]:
    return {"type": "local", "command": argv[0], "args": list(argv[1:]), "env": _tab_env(), "tools": ["*"]}


def agy_argv(argv: Sequence[str], platform: str) -> list[str]:
    return [*agy_python(argv[0], platform), *argv[1:]]


def agy_entry(argv: Sequence[str], platform: str) -> dict[str, Any]:
    full = agy_argv(argv, platform)
    return {"command": full[0], "args": full[1:]}


def gemini_entry(argv: Sequence[str]) -> dict[str, Any]:
    return {"command": argv[0], "args": list(argv[1:])}


# OpenCode applies timeout (ms) to tool calls too, and its default would cut wait_for_message short.
def opencode_entry(argv: Sequence[str]) -> dict[str, Any]:
    return {"type": "local", "command": list(argv), "enabled": True, "timeout": OPENCODE_TIMEOUT_MS}


# Grok Build passes its own environment to the server, and may refuse a ${VAR} it can't expand, so no env table.
def grok_table(argv: Sequence[str]) -> list[str]:
    return toml_table("mcp_servers", SERVER_NAME, {"command": argv[0], "args": list(argv[1:])})


# Pi hides MCP tools behind its codemode tool unless the server is exposed directly, and times a request out after 60 s.
def pi_entry(argv: Sequence[str]) -> dict[str, Any]:
    return {"command": argv[0], "args": list(argv[1:]), "env": _tab_env(), "timeout": TOOL_TIMEOUT_S, "exposure": "direct"}


# Hermes passes an MCP server only the variables its env names, and times a tool call out after 300 s.
def hermes_entry(argv: Sequence[str]) -> dict[str, Any]:
    return {"command": argv[0], "args": list(argv[1:]), "env": _tab_env(), "timeout": TOOL_TIMEOUT_S}


def qwen_entry(argv: Sequence[str]) -> dict[str, Any]:
    return {"command": argv[0], "args": list(argv[1:]), "env": _tab_env(), "timeout": QWEN_TIMEOUT_MS}


def goose_entry(argv: Sequence[str]) -> dict[str, Any]:
    return {
        "name": SERVER_NAME,
        "type": "stdio",
        "cmd": argv[0],
        "args": list(argv[1:]),
        "enabled": True,
        "timeout": GOOSE_TIMEOUT_S,
        "envs": {},
        "env_keys": [],
        "description": "Agent Tabs",
    }


def strip_json_comments(text: str) -> str:
    out: list[str] = []
    in_string = False
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if in_string:
            out.append(c)
            if c == "\\":
                out.append(text[i + 1] if i + 1 < n else "")
                i += 1
            elif c == '"':
                in_string = False
        elif c == '"':
            in_string = True
            out.append(c)
        elif c == "/" and text[i + 1 : i + 2] == "/":
            while i < n and text[i] != "\n":
                i += 1
            out.append("\n")
        elif c == "/" and text[i + 1 : i + 2] == "*":
            close = text.find("*/", i + 2)
            i = n if close < 0 else close + 1
            out.append(" ")
        else:
            out.append(c)
        i += 1
    return "".join(out)


def parse_config(text: str, file: str, allow_comments: bool) -> dict[str, Any]:
    try:
        value = parse(strip_json_comments(text) if allow_comments else text)
    except ValueError as e:
        raise ValueError(f"{file} isn't plain JSON ({e}); add the {SERVER_NAME} entry by hand") from e
    if not isinstance(value, dict):
        raise ConfigError(f"{file} doesn't hold a JSON object")
    return value


def _detect_indent(text: str) -> str:
    match = re.search(r"^([ \t]+)\S", text, re.MULTILINE)
    return match.group(1) if match else "  "


def _render(root: dict[str, Any], text: str | None) -> str:
    eol = "\r\n" if text is not None and "\r\n" in text else "\n"
    return (stringify(root, _detect_indent(text or "")) + "\n").replace("\n", eol)


def edit_json(text: str | None, file: str, change: Callable[[dict[str, Any]], dict[str, Any]]) -> str | None:
    root = {} if text is None or not text.strip() else parse_config(text, file, False)
    nxt = change(root)
    if stringify(nxt) == stringify(root):
        return None
    return _render(nxt, text)


def with_server_entry(text: str | None, file: str, section: str, entry: Any, skeleton: dict[str, Any] | None = None) -> str | None:
    blank = text is None or not text.strip()
    if blank and entry is None:
        return None
    root = dict(skeleton or {}) if blank else parse_config(text or "", file, False)
    current = root.get(section, {})
    if not isinstance(current, dict):
        raise ConfigError(f'{file}: "{section}" isn\'t an object')
    servers = dict(current)
    if entry is None:
        if SERVER_NAME not in servers:
            return None
        del servers[SERVER_NAME]
    else:
        if SERVER_NAME in servers and stringify(servers[SERVER_NAME]) == stringify(entry):
            return None
        servers[SERVER_NAME] = entry
    root[section] = servers
    return _render(root, text)


def entry_argv(entry: Any) -> list[str]:
    if not isinstance(entry, dict):
        return []
    command = entry.get("command")
    args = entry.get("args")
    parts = command if isinstance(command, list) else [command, *(args if isinstance(args, list) else [])]
    return [p for p in parts if isinstance(p, str)]


def entry_server_path(entry: Any) -> str | None:
    parts = entry_argv(entry)
    return next((p for p in parts if _SERVER_FILE.search(p)), parts[-1] if parts else None)


def same_path(a: str, b: str, platform: str) -> bool:
    def norm(p: str) -> str:
        forward = re.sub(r"/+", "/", p.replace("\\", "/"))
        return forward.lower() if platform in ("win32", "darwin") else forward

    return norm(a) == norm(b)


def same_argv(a: Sequence[str], b: Sequence[str], platform: str) -> bool:
    return len(a) == len(b) and all(same_path(x, y, platform) for x, y in zip(a, b))


def parse_codex_get(stdout: str) -> dict[str, Any]:
    start = stdout.find("{")
    if start < 0:
        raise ValueError("codex mcp get printed no JSON")
    value = parse(stdout[start:])
    transport = value.get("transport") if isinstance(value, dict) else None
    transport = transport if isinstance(transport, dict) else {}
    return {"command": transport.get("command"), "args": transport.get("args")}


def _is_codex_not_found(output: str) -> bool:
    return re.search(r"No MCP server named", output, re.IGNORECASE) is not None


def write_config(file: str, text: str) -> None:
    target = os.path.realpath(file) if os.path.lexists(file) else file
    os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
    try:
        mode = stat.S_IMODE(os.stat(target).st_mode)
    except OSError:
        mode = 0o600
    temp = f"{target}.{os.getpid()}.{now_ms()}.tmp"
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), mode)
        with os.fdopen(fd, "wb") as f:
            f.write(text.encode("utf-8"))
        if sys.platform != "win32":
            os.chmod(temp, mode)
        replace_retrying(temp, target)
    except BaseException:
        remove_file(temp)
        raise


# Agent CLIs also read project config from their working folder, so every call runs from the Agent Tabs home,
# which holds none.
def _run_agent_cli(ctx: RegisterContext, agent: str, args: list[str]) -> tuple[str, RunResult]:
    cli = find_cli_on_path(_profile_of(agent)[0], ctx.platform, ctx.env)
    if not cli:
        raise RuntimeError(f"{agent} isn't on PATH as a runnable file")
    ensure_private_dir(ctx.home)
    return cli, run_cli_result(ctx.platform, ctx.env, cli, args, CLI_TIMEOUT_S, ctx.home)


def _change_with_cli(ctx: RegisterContext, agent: str, args: list[str]) -> None:
    cli, result = _run_agent_cli(ctx, agent, args)
    if result.code != 0:
        raise cli_failure(cli, args, result)


def _read_json_if_exists(file: str) -> dict[str, Any] | None:
    text = read_text_if_exists(file)
    return None if text is None or not text.strip() else parse_config(text, file, True)


def _hook_file(ctx: RegisterContext, agent: str) -> str:
    return hook_config_file(agent, ctx.env, ctx.user_home, ctx.platform)


def _same_json(a: Any, b: Any) -> bool:
    return stringify(a) == stringify(b)


def hooks_installed(ctx: RegisterContext, agent: str) -> bool:
    target = hook_target(ctx)
    file = _hook_file(ctx, agent)
    if agent == "hermes":
        allowlist = _read_json_if_exists(hermes_allowlist_file(ctx.env, ctx.user_home, ctx.platform))
        return has_hermes_hooks(yaml_value(read_text_if_exists(file), file, "hooks"), target) and has_hermes_approvals(allowlist, target)
    root = _read_json_if_exists(file)
    if agent == "grok":
        return _same_json(root, grok_hooks(target))
    if agent == "goose":
        return _same_json(root, goose_hooks(target)) and os.path.exists(
            os.path.join(goose_plugin_dir(ctx.env, ctx.user_home), "plugin.json")
        )
    if agent == "copilot":
        return root is not None and _same_json(root, copilot_hooks(target))
    if agent == "agy":
        return root is not None and has_agy_hooks(root, target)
    return root is not None and has_our_hooks(root, agent, target)


def has_any_hooks(ctx: RegisterContext, agent: str) -> bool:
    file = _hook_file(ctx, agent)
    if agent in ("copilot", "grok", "goose"):
        return os.path.exists(file)
    if agent == "hermes":
        return has_any_hermes_hooks(yaml_value(read_text_if_exists(file), file, "hooks"))
    root = _read_json_if_exists(file)
    if agent == "agy":
        return root is not None and "ide-agent-tabs" in root
    return has_any_settings_hooks(root)


def hooks_name_this_home(ctx: RegisterContext, agent: str) -> bool:
    text = read_text_if_exists(_hook_file(ctx, agent))
    if text is None:
        return False
    paths = [hook_copy_path(ctx.home, ctx.platform), old_hook_path(ctx.home, ctx.platform)]
    if ctx.platform == "win32":
        text = text.replace("\\\\", "/").replace("\\", "/")
        paths = [p.replace("\\", "/") for p in paths]
    if ctx.platform in ("win32", "darwin"):
        text = text.lower()
        paths = [p.lower() for p in paths]
    return any(p in text for p in paths)


def _change_json(file: str, change: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
    nxt = edit_json(read_text_if_exists(file), file, change)
    if nxt is not None:
        write_config(file, nxt)


def _change_yaml(file: str, change: Callable[[YamlDoc], None]) -> None:
    nxt = edit_yaml(read_text_if_exists(file), file, change)
    if nxt is not None:
        write_config(file, nxt)


def _install_codex_settings(ctx: RegisterContext) -> None:
    config = config_file("codex", ctx.env, ctx.user_home, platform=ctx.platform)
    nxt = with_codex_settings(read_text_if_exists(config) or "", config)
    if nxt is not None:
        write_config(config, nxt)


def install_hooks(ctx: RegisterContext, agent: str) -> None:
    target = hook_target(ctx)
    file = _hook_file(ctx, agent)
    if agent == "copilot":
        _change_json(file, lambda _: copilot_hooks(target))
    elif agent == "grok":
        _change_json(file, lambda _: grok_hooks(target))
    elif agent == "goose":
        _change_json(os.path.join(goose_plugin_dir(ctx.env, ctx.user_home), "plugin.json"), lambda _: goose_manifest())
        _change_json(file, lambda _: goose_hooks(target))
    elif agent == "hermes":
        _change_yaml(file, lambda doc: with_hermes_hooks(doc, target))
        allowlist = hermes_allowlist_file(ctx.env, ctx.user_home, ctx.platform)
        _change_json(allowlist, lambda root: with_hermes_approvals(root, allowlist, target))
    elif agent == "agy":
        _change_json(file, lambda root: with_agy_hooks(root, target))
    else:
        _change_json(file, lambda root: merge_hook_settings(root, file, agent, target))


def _remove_hooks(ctx: RegisterContext, agent: str) -> None:
    file = _hook_file(ctx, agent)
    if agent in ("copilot", "grok"):
        remove_file(file)
        return
    if agent == "goose":
        folder = goose_plugin_dir(ctx.env, ctx.user_home)
        remove_file(file)
        remove_file(os.path.join(folder, "plugin.json"))
        for empty in (os.path.dirname(file), folder):
            try:
                os.rmdir(empty)
            except OSError:
                pass
        return
    if agent == "hermes":
        allowlist = hermes_allowlist_file(ctx.env, ctx.user_home, ctx.platform)
        if read_text_if_exists(allowlist) is not None:
            _change_json(allowlist, lambda root: with_hermes_approvals(root, allowlist, None))
    if read_text_if_exists(file) is None:
        return
    if agent == "hermes":
        _change_yaml(file, lambda doc: with_hermes_hooks(doc, None))
    elif agent == "agy":
        _change_json(file, lambda root: with_agy_hooks(root, None))
    else:
        _change_json(file, lambda root: merge_hook_settings(root, file, agent, None))


def _section_of(agent: str) -> str:
    return "mcp" if agent == "opencode" else "mcpServers"


def _yaml_section(agent: str) -> str:
    return "mcp_servers" if agent == "hermes" else "extensions"


def _read_entry(ctx: RegisterContext, agent: str, file: str) -> Any:
    if agent == "codex":
        args = ["mcp", "get", SERVER_NAME, "--json"]
        cli, result = _run_agent_cli(ctx, agent, args)
        if result.code == 0:
            return parse_codex_get(result.stdout)
        if _is_codex_not_found(result.stderr + result.stdout):
            return None
        raise cli_failure(cli, args, result)
    text = read_text_if_exists(file)
    if text is None or not text.strip():
        return None
    if agent == "grok":
        return read_toml_table(text, "mcp_servers", SERVER_NAME)
    if agent in ("hermes", "goose"):
        section = yaml_value(text, file, _yaml_section(agent))
        entry = section.get(SERVER_NAME) if isinstance(section, dict) else None
        return {"command": entry.get("cmd"), "args": entry.get("args")} if agent == "goose" and isinstance(entry, dict) else entry
    section = parse_config(text, file, True).get(_section_of(agent))
    return section.get(SERVER_NAME) if isinstance(section, dict) else None


def desired_argv(ctx: RegisterContext, agent: str) -> list[str]:
    argv = server_argv(ctx)
    return agy_argv(argv, ctx.platform) if agent == "agy" else argv


def agent_status(ctx: RegisterContext, agent: str) -> dict[str, Any]:
    command, label = _profile_of(agent)
    config = config_file(agent, ctx.env, ctx.user_home, os.path.exists, ctx.platform)
    installed = is_installed(command, path_var_of(ctx.env), ctx.platform == "win32")
    status: dict[str, Any] = {
        "agent": agent,
        "label": label,
        "installed": installed,
        "registered": False,
        "path": None,
        "stable": False,
        "config": config,
        "hooks": False if takes_hooks(agent) else None,
    }
    if agent == "codex" and not installed:
        return status
    try:
        if takes_hooks(agent):
            status["hooks"] = hooks_installed(ctx, agent)
        entry = _read_entry(ctx, agent, config)
        if entry is None:
            return status
        if agent == "codex":
            settings = has_codex_settings(read_text_if_exists(config))
        elif agent == "agy":
            settings = has_agy_allow_rule(_read_json_if_exists(agy_settings_file(ctx.user_home)))
        else:
            settings = True
        status.update(
            registered=True,
            path=entry_server_path(entry),
            stable=settings and same_argv(entry_argv(entry), desired_argv(ctx, agent), ctx.platform),
        )
        return status
    except (OSError, ValueError, RuntimeError, TimeoutError) as e:
        return {**status, "error": str(e)}


_JSON_ENTRIES: dict[str, Callable[[RegisterContext, list[str]], Any]] = {
    "copilot": lambda _, argv: copilot_entry(argv),
    "agy": lambda ctx, argv: agy_entry(argv, ctx.platform),
    "opencode": lambda _, argv: opencode_entry(argv),
    "pi": lambda _, argv: pi_entry(argv),
    "qwen": lambda _, argv: qwen_entry(argv),
    "gemini": lambda _, argv: gemini_entry(argv),
}


def _edit_config(ctx: RegisterContext, file: str, agent: str, argv: list[str] | None) -> None:
    text = read_text_if_exists(file)
    if agent == "grok":
        nxt = with_toml_table(text, file, "mcp_servers", SERVER_NAME, None if argv is None else grok_table(argv))
    elif agent in ("hermes", "goose"):
        if argv is None and text is None:
            return
        entry = None if argv is None else hermes_entry(argv) if agent == "hermes" else goose_entry(argv)
        nxt = edit_yaml(text, file, lambda doc: doc.set_entry(_yaml_section(agent), SERVER_NAME, entry))
    else:
        skeleton = {"$schema": OPENCODE_SCHEMA} if agent == "opencode" else {}
        nxt = with_server_entry(text, file, _section_of(agent), None if argv is None else _JSON_ENTRIES[agent](ctx, argv), skeleton)
    if nxt is not None:
        write_config(file, nxt)


# Antigravity CLI asks before each call to an MCP tool it has no allow rule for, and denies the call in -p runs.
def _set_agy_allow_rule(ctx: RegisterContext, allow: bool) -> None:
    file = agy_settings_file(ctx.user_home)
    if not allow and read_text_if_exists(file) is None:
        return
    _change_json(file, lambda root: with_agy_allow_rule(root, file, allow))


def register(ctx: RegisterContext, agent: str, file: str) -> None:
    argv = server_argv(ctx)
    if agent == "codex":
        _change_with_cli(ctx, agent, register_args(argv))
        _install_codex_settings(ctx)
    else:
        _edit_config(ctx, file, agent, argv)
    if agent == "agy":
        _set_agy_allow_rule(ctx, True)
    if takes_hooks(agent):
        install_hooks(ctx, agent)


def unregister(ctx: RegisterContext, agent: str, file: str) -> None:
    if takes_hooks(agent):
        _remove_hooks(ctx, agent)
    if agent == "codex":
        _change_with_cli(ctx, agent, unregister_args())
    else:
        _edit_config(ctx, file, agent, None)
    if agent == "agy":
        _set_agy_allow_rule(ctx, False)


def server_status(ctx: RegisterContext) -> dict[str, Any]:
    build = current_build(ctx.home)
    try:
        bundled: str | None = build_name(ctx.source.version, source_hash(source_files(ctx.source)))
    except OSError:
        bundled = None
    return {
        "path": server_copy_path(ctx.home, ctx.platform),
        "exists": build is not None,
        "current": build is not None and build == bundled,
    }


def agents_report(ctx: RegisterContext) -> dict[str, Any]:
    return {"server": server_status(ctx), "agents": [agent_status(ctx, a) for a in AGENTS]}


def _outcome(status: dict[str, Any], error: str | None = None) -> dict[str, Any]:
    out = {**status, "ok": error is None}
    if error is not None:
        out["error"] = error
    return out


def _parse_agent_names(names: Sequence[str], errors: list[str]) -> list[str]:
    agents: list[str] = []
    for name in names:
        if name == "claude":
            errors.append("claude: Claude Code gets the server from the plugin; nothing to register")
        elif not is_agent_name(name):
            errors.append(f"{name}: unknown agent; use {', '.join(AGENTS)}")
        elif name not in agents:
            agents.append(name)
    return agents


def _register_one(ctx: RegisterContext, agent: str, copy_error: str | None) -> dict[str, Any]:
    before = agent_status(ctx, agent)
    server = server_copy_path(ctx.home, ctx.platform)
    if not os.path.exists(server):
        return _outcome(before, copy_error or f"{server} is missing")
    if not before["installed"]:
        return _outcome(before, "not installed")
    if agent == "codex" and ctx.platform == "win32":
        return _outcome(before, CODEX_WINDOWS_REFUSAL)
    try:
        register(ctx, agent, before["config"])
    except (OSError, ValueError, RuntimeError, TimeoutError) as e:
        return _outcome(before, str(e))
    after = agent_status(ctx, agent)
    missing = None
    if not after["registered"] or not after["stable"]:
        missing = f"{after['config']} doesn't hold the {SERVER_NAME} entry after registering"
    elif after["hooks"] is False:
        missing = f"the Agent Tabs hooks aren't in {_hook_file(ctx, agent) if is_hook_agent(agent) else after['config']} after registering"
    return _outcome(after, after.get("error") or missing)


def _unregister_one(ctx: RegisterContext, agent: str) -> dict[str, Any]:
    before = agent_status(ctx, agent)
    if before.get("error") or (not before["registered"] and not before["hooks"]):
        return _outcome(before, before.get("error"))
    try:
        unregister(ctx, agent, before["config"])
    except (OSError, ValueError, RuntimeError, TimeoutError) as e:
        return _outcome(before, str(e))
    after = agent_status(ctx, agent)
    left = None
    if after["registered"]:
        left = f"{after['config']} still holds the {SERVER_NAME} entry"
    elif after["hooks"]:
        left = "the Agent Tabs hooks are still installed"
    return _outcome(after, after.get("error") or left)


def _errors_of(outcomes: list[dict[str, Any]]) -> list[str]:
    return [f"{o['agent']}: {o['error']}" for o in outcomes if o.get("error") is not None]


def tab_settings_file(ctx: RegisterContext) -> str:
    return os.path.join(copy_root(ctx.home), CLAUDE_TAB_SETTINGS_FILE)


def tab_settings_text(ctx: RegisterContext) -> str:
    return stringify(claude_tab_settings(hook_target(ctx)), 2) + "\n"


def refresh_copy(ctx: RegisterContext) -> tuple[str | None, bool]:
    build = refresh_server_copy(ctx.source, ctx.home)
    python_changed = write_python(ctx.home, ctx.python)
    text = tab_settings_text(ctx)
    if read_text_if_exists(tab_settings_file(ctx)) != text:
        write_atomically(tab_settings_file(ctx), text)
    return build, python_changed


def register_agents(ctx: RegisterContext, names: Sequence[str]) -> dict[str, Any]:
    errors: list[str] = []
    agents = _parse_agent_names(names, errors)
    copy_error = None
    try:
        refresh_copy(ctx)
    except (OSError, ValueError, TimeoutError) as e:
        copy_error = f"server copy: {e}"
        errors.append(copy_error)
    results = [_register_one(ctx, agent, copy_error) for agent in agents]
    return {"server": server_status(ctx), "agents": results, "errors": [*errors, *_errors_of(results)]}


def unregister_agents(ctx: RegisterContext, names: Sequence[str]) -> dict[str, Any]:
    errors: list[str] = []
    results = [_unregister_one(ctx, agent) for agent in _parse_agent_names(names, errors)]
    return {"agents": results, "errors": [*errors, *_errors_of(results)]}


def _is_our_server(path: str | None, ctx: RegisterContext) -> bool:
    return path is not None and any(
        same_path(path, ours, ctx.platform) for ours in (server_copy_path(ctx.home, ctx.platform), old_server_path(ctx.home, ctx.platform))
    )


# Registrations and hooks written by an older plugin, or for an interpreter that changed, move to the
# current command; an agent registered with another server, or not at all, stays as it is.
def migrate_registrations(ctx: RegisterContext) -> tuple[list[str], list[str]]:
    migrated: list[str] = []
    errors: list[str] = []
    for agent in AGENTS:
        try:
            status = agent_status(ctx, agent)
            if status.get("error"):
                errors.append(f"{agent}: {status['error']}")
                continue
            if agent == "codex" and ctx.platform == "win32":
                if (
                    status["registered"]
                    and status["path"] is not None
                    and same_path(status["path"], old_server_path(ctx.home, ctx.platform), ctx.platform)
                ):
                    errors.append(f"codex: registered with {status['path']}; run sync-ides --unregister codex")
                continue
            if status["registered"] and _is_our_server(status["path"], ctx) and not status["stable"]:
                register(ctx, agent, status["config"])
                migrated.append(agent)
            elif takes_hooks(agent) and status["hooks"] is False and has_any_hooks(ctx, agent) and hooks_name_this_home(ctx, agent):
                install_hooks(ctx, agent)
                migrated.append(agent)
        except (OSError, ValueError, RuntimeError, TimeoutError) as e:
            errors.append(f"{agent}: {e}")
    return migrated, errors

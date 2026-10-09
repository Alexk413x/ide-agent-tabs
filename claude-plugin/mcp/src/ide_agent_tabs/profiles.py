from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Any, NamedTuple

from .jev.settings import JEV_OFF, JevSettings, parse_jev_settings
from .jsjson import entries, is_finite, parse, trim, utf16_len
from .jspath import posix_is_absolute, win32_is_absolute

DEFAULT_AGENT = "claude"
AGENTS_FILE = "agents.json"
CONFIG_FILE = "config.json"
MAX_PROMPT_CHARS = 30_000
MAX_ENTRIES = 64
PLUGIN_ENV_PREFIX = "IDE_AGENT_TABS_"
STARTUP_ENV = "JEDITERM_SOURCE"
TAB_ID_ENV = f"{PLUGIN_ENV_PREFIX}ID"
AGENT_ENV = f"{PLUGIN_ENV_PREFIX}AGENT"
TAB_HOOKS_ENV = f"{PLUGIN_ENV_PREFIX}HOOKS"
CLAUDE_TAB_SETTINGS_FILE = "claude-tab-settings.json"
ALLOW_RESUME_KEY = "allowResume"
IDE_START_TIMEOUT_KEY = "ideStartTimeoutSec"
DEFAULT_IDE_START_TIMEOUT_SEC = 180
MAX_IDE_START_TIMEOUT_SEC = 3600

_PROFILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_BAD_ENV_NAME = re.compile("[=\\s\ufeff\x00]")

Env = dict[str, str]
_NO_ENV: Mapping[str, str] = MappingProxyType({})


class AgentProfile(NamedTuple):
    name: str
    label: str
    command: str
    args: tuple[str, ...] = ()
    prompt_flag: str | None = None
    model_flag: str | None = None
    env: Mapping[str, str] = _NO_ENV
    icon: str | None = None


class AgentLaunch(NamedTuple):
    agent: str
    command: str
    args: list[str]
    prompt: str | None
    env: dict[str, str]


class ConfigError(ValueError):
    pass


def _profile(
    name: str, label: str, command: str, model_flag: str | None, prompt_flag: str | None = None, args: tuple[str, ...] = ()
) -> AgentProfile:
    return AgentProfile(name, label, command, tuple(args), prompt_flag or None, model_flag or None)


# Codex's shared daemon runs MCP servers and hooks with its own environment and a stale IDE_AGENT_TABS_ID, so a
# Codex tab runs in-process with its own server and trusted hooks. The IDE copies hold the same strings.
# No '"' or '%', and a space in any argument with a cmd.exe metacharacter: PowerShell 5.1 and codex.cmd mangle them.
CODEX_SERVER_SCRIPT = (
    "import os,runpy;h=os.environ.get('IDE_AGENT_TABS_HOME') or os.path.join(os.path.expanduser('~'),'.ide-agent-tabs');"
    "runpy.run_path(os.path.join(h,'mcp','py','launch','mcp_server.py'),run_name='__main__')"
)
CODEX_SERVER_TAIL = (
    f"'-I', '-S', '-c', '''{CODEX_SERVER_SCRIPT}'''], env_vars = ['IDE_AGENT_TABS_ID', 'IDE_AGENT_TABS_AGENT', 'IDE_AGENT_TABS_HOME'], "
    "tool_timeout_sec = 660 }"
)
CODEX_SERVER_ARG = f"mcp_servers.ide-agent-tabs={{ command = 'python3', args = [{CODEX_SERVER_TAIL}"
CODEX_TAB_ARGS: tuple[str, ...] = (
    "--no-daemon",
    "-c",
    CODEX_SERVER_ARG,
    "-c",
    "hooks.UserPromptSubmit=[{ hooks = [{ type = 'mcp_tool', server = 'ide-agent-tabs', tool = 'agent_tabs_hook', input = { event = 'UserPromptSubmit', session_id = '${session_id}', turn_id = '${turn_id}' }, timeout = 10 }] }]",
    "-c",
    "hooks.PostToolUse=[{ hooks = [{ type = 'mcp_tool', server = 'ide-agent-tabs', tool = 'agent_tabs_hook', input = { event = 'PostToolUse', session_id = '${session_id}', turn_id = '${turn_id}' }, timeout = 10 }] }]",
    "-c",
    "hooks.PermissionRequest=[{ hooks = [{ type = 'mcp_tool', server = 'ide-agent-tabs', tool = 'agent_tabs_hook', input = { event = 'PermissionRequest', session_id = '${session_id}', turn_id = '${turn_id}' }, timeout = 10 }] }]",
    "-c",
    "hooks.Stop=[{ hooks = [{ type = 'mcp_tool', server = 'ide-agent-tabs', tool = 'agent_tabs_hook', input = { event = 'Stop', session_id = '${session_id}', turn_id = '${turn_id}' }, timeout = 10 }] }]",
    "-c",
    "hooks.Interrupt=[{ hooks = [{ type = 'mcp_tool', server = 'ide-agent-tabs', tool = 'agent_tabs_hook', input = { event = 'Interrupt', session_id = '${session_id}', turn_id = '${turn_id}' }, timeout = 3 }] }]",
    "-c",
    "hooks.state={ '/<session-flags>/config.toml:user_prompt_submit:0:0' = { trusted_hash = 'sha256:aac36b4c0cfafe0f4ae641176bcc1ab25ae590dbe3be9268f7570b55ab4afe89' }, 'C:\\<session-flags>\\config.toml:user_prompt_submit:0:0' = { trusted_hash = 'sha256:aac36b4c0cfafe0f4ae641176bcc1ab25ae590dbe3be9268f7570b55ab4afe89' }, '/<session-flags>/config.toml:post_tool_use:0:0' = { trusted_hash = 'sha256:75aa06c6f44c8918fe729537b56d5f498c931e032f5d89499593b4cd67ba335e' }, 'C:\\<session-flags>\\config.toml:post_tool_use:0:0' = { trusted_hash = 'sha256:75aa06c6f44c8918fe729537b56d5f498c931e032f5d89499593b4cd67ba335e' }, '/<session-flags>/config.toml:permission_request:0:0' = { trusted_hash = 'sha256:5e1483151807db1577272adc730b9ffe56c96d22b1a8fe7c7ff6d6efe42f3626' }, 'C:\\<session-flags>\\config.toml:permission_request:0:0' = { trusted_hash = 'sha256:5e1483151807db1577272adc730b9ffe56c96d22b1a8fe7c7ff6d6efe42f3626' }, '/<session-flags>/config.toml:stop:0:0' = { trusted_hash = 'sha256:a97c883d6b41f88f6879ce99d0d343a7069f3fded56573aaa2b15fc5bbd01c6f' }, 'C:\\<session-flags>\\config.toml:stop:0:0' = { trusted_hash = 'sha256:a97c883d6b41f88f6879ce99d0d343a7069f3fded56573aaa2b15fc5bbd01c6f' }, '/<session-flags>/config.toml:interrupt:0:0' = { trusted_hash = 'sha256:c2704217d5db401ed47f178ff9db1a7be09662f73b3e55753f8600e42bd53165' }, 'C:\\<session-flags>\\config.toml:interrupt:0:0' = { trusted_hash = 'sha256:c2704217d5db401ed47f178ff9db1a7be09662f73b3e55753f8600e42bd53165' } }",
)


# A Codex tab runs its server on the interpreter that python.json records, so no py.exe stays behind as its parent.
# The path goes into a TOML literal string and through cmd.exe, so it may not hold a quote, % or !, or end in \.
_CODEX_PYTHON_SAFE = re.compile(r"[^'\"%!\x00-\x1f\x7f]*[^'\"%!\x00-\x1f\x7f\\]\Z")


def codex_server_arg(python: Sequence[str]) -> str:
    head = "".join(f"'{a}', " for a in python[1:])
    return f"mcp_servers.ide-agent-tabs={{ command = '{python[0]}', args = [{head}{CODEX_SERVER_TAIL}"


def with_codex_python(args: Sequence[str], python: Sequence[str] | None) -> tuple[str, ...]:
    if not python:
        return tuple(args)
    arg = codex_server_arg(python)
    return tuple(arg if a == CODEX_SERVER_ARG else a for a in args)


def recorded_python(home: str, windows: bool) -> str | None:
    try:
        with open(os.path.join(home, "mcp", "python.json"), encoding="utf-8") as f:
            python = json.load(f).get("python")
    except (OSError, ValueError, AttributeError):
        python = None
    absolute = win32_is_absolute if windows else posix_is_absolute
    if isinstance(python, str) and absolute(python) and _CODEX_PYTHON_SAFE.match(python) and os.path.isfile(python):
        return python
    return None


def codex_python(home: str, windows: bool) -> tuple[str, ...]:
    python = recorded_python(home, windows)
    if python is not None:
        return (python,)
    return ("py", "-3") if windows else ("python3",)


# The settings path reaches cmd.exe when claude is a .cmd shim, so it may not hold a double quote or a cmd.exe metacharacter.
_CMD_SAFE_PATH = re.compile(r'[^"%!^&|<>\x00-\x1f\x7f]+\Z')
_CLAUDE_COMMAND = re.compile(r"(?:.*[\\/])?claude(?:\.(?:exe|cmd|bat|ps1))?\Z", re.IGNORECASE)


def claude_tab_settings(home: str, windows: bool) -> str | None:
    path = os.path.join(home, "mcp", CLAUDE_TAB_SETTINGS_FILE)
    if recorded_python(home, windows) is None or not _CMD_SAFE_PATH.match(path) or not os.path.isfile(path):
        return None
    return path


def with_claude_settings(command: str, args: Sequence[str], caller_args: Sequence[str], settings: str | None) -> tuple[str, ...]:
    if settings is None or not _CLAUDE_COMMAND.match(command):
        return tuple(args)
    if any(a == "--settings" or a.startswith("--settings=") for a in (*args, *caller_args)):
        return tuple(args)
    return (*args, "--settings", settings)


# goose run -s takes the first message from -t and stays interactive, but refuses to start without one.
GOOSE_RUN_ARGS: tuple[str, ...] = ("run", "-s")
GOOSE_EMPTY_ARGS: tuple[str, ...] = ("session",)

BUILTIN_PROFILES: tuple[AgentProfile, ...] = (
    _profile("claude", "Claude Code", "claude", "--model"),
    _profile("codex", "Codex", "codex", "-m", None, CODEX_TAB_ARGS),
    _profile("agy", "Antigravity CLI", "agy", "--model", "-i"),
    _profile("copilot", "Copilot CLI", "copilot", "--model", "-i"),
    _profile("gemini", "Gemini CLI", "gemini", "-m", "-i"),
    _profile("grok", "Grok Build", "grok", "-m"),
    _profile("pi", "Pi", "pi", "--model"),
    _profile("hermes", "Hermes", "hermes", "-m", "-q", ("chat",)),
    _profile("opencode", "OpenCode", "opencode", "-m", "--prompt"),
    # A positional prompt makes Qwen Code answer once and exit.
    _profile("qwen", "Qwen Code", "qwen", "-m", "-i"),
    _profile("goose", "Goose", "goose", "--model", "-t", GOOSE_RUN_ARGS),
    # Without --local-provider, --oss stops at a picker between LM Studio and Ollama.
    _profile("codex-local", "Codex (local)", "codex", "-m", None, (*CODEX_TAB_ARGS, "--oss", "--local-provider", "ollama")),
)


def launch_of(
    p: AgentProfile, prompt: str | None = None, caller_args: list[str] | None = None, caller_env: Env | None = None
) -> AgentLaunch:
    flag = [p.prompt_flag] if p.prompt_flag is not None and prompt is not None else []
    env = dict(p.env)
    env.update(caller_env or {})
    return AgentLaunch(p.name, p.command, [*p.args, *(caller_args or []), *flag], prompt, env)


def is_reserved_env(name: str) -> bool:
    upper = name.upper()
    return upper.startswith((PLUGIN_ENV_PREFIX, STARTUP_ENV))


def _is_blank(s: str) -> bool:
    return trim(s) == ""


def check_env(env: dict[str, Any], field_name: str) -> None:
    items = entries(env)
    if len(items) > MAX_ENTRIES:
        raise ConfigError(f"{field_name} exceeds {MAX_ENTRIES} entries")
    for name, value in items:
        if _is_blank(name) or _BAD_ENV_NAME.search(name):
            raise ConfigError(f"{field_name} name is not a valid variable name: '{name}'")
        if is_reserved_env(name):
            raise ConfigError(f"{field_name} name {name} is reserved by the plugin")
        if utf16_len(value) > MAX_PROMPT_CHARS or "\0" in value:
            raise ConfigError(f"{field_name} {name} is longer than {MAX_PROMPT_CHARS} characters or holds a NUL")


def parse_json_object(text: str, file: str) -> dict[str, Any]:
    try:
        value = parse(text)
    except ValueError as e:
        raise ConfigError(f"{file} is not JSON: {e}") from e
    if not isinstance(value, dict):
        raise ConfigError(f"{file} must hold a JSON object")
    return value


def _opt_string(obj: dict[str, Any], name: str, key: str) -> str | None:
    value = obj.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError(f"{name} must be a string")
    return value


def _opt_string_list(obj: dict[str, Any], name: str, key: str) -> list[str]:
    value = obj.get(key)
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        raise ConfigError(f"{name} must be an array of strings")
    return list(value)


def _opt_string_map(obj: dict[str, Any], name: str, key: str) -> Env:
    value = obj.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{name} must be an object of strings")
    out: Env = {}
    for k, v in entries(value):
        if not isinstance(v, str):
            raise ConfigError(f"{name}.{k} must be a string")
        out[k] = v
    return out


def parse_profiles(text: str) -> list[AgentProfile]:
    root = parse_json_object(text, AGENTS_FILE)
    profiles: list[AgentProfile] = []
    for name, value in entries(root):
        if not _PROFILE_NAME.fullmatch(name):
            raise ConfigError(f"profile name '{name}' must be letters, digits, '.', '_' or '-'")
        if not isinstance(value, dict):
            raise ConfigError(f"profile {name} must be an object")
        command = _opt_string(value, f"{name}.command", "command")
        if command is None or _is_blank(command) or "\0" in command:
            raise ConfigError(f"profile {name} needs a command")
        args = _opt_string_list(value, f"{name}.args", "args")
        if len(args) > MAX_ENTRIES:
            raise ConfigError(f"{name}.args exceeds {MAX_ENTRIES} entries")
        if any("\0" in a for a in args):
            raise ConfigError(f"{name}.args holds a NUL")
        prompt_flag = _opt_string(value, f"{name}.promptFlag", "promptFlag")
        if prompt_flag is not None and (_is_blank(prompt_flag) or "\0" in prompt_flag):
            raise ConfigError(f"{name}.promptFlag must not be blank")
        model_flag = _opt_string(value, f"{name}.modelFlag", "modelFlag")
        if model_flag is not None and (_is_blank(model_flag) or "\0" in model_flag):
            raise ConfigError(f"{name}.modelFlag must not be blank")
        env = _opt_string_map(value, f"{name}.env", "env")
        check_env(env, f"{name}.env")
        label = _opt_string(value, f"{name}.label", "label")
        icon = _opt_string(value, f"{name}.icon", "icon")
        profiles.append(
            AgentProfile(
                name=name,
                label=label if label is not None and not _is_blank(label) else name,
                command=command,
                args=tuple(args),
                prompt_flag=prompt_flag,
                model_flag=model_flag,
                env=env,
                icon=icon if icon is not None and not _is_blank(icon) else None,
            )
        )
    return profiles


def merge_profiles(builtins: tuple[AgentProfile, ...] | list[AgentProfile], custom: list[AgentProfile]) -> list[AgentProfile]:
    by_name = {c.name: c for c in custom}
    names = {b.name for b in builtins}
    return [*(by_name.get(b.name, b) for b in builtins), *(c for c in custom if c.name not in names)]


def read_default_agent(text: str) -> str | None:
    value = parse_json_object(text, CONFIG_FILE).get("defaultAgent")
    return value if isinstance(value, str) else None


def read_jev_settings(text: str) -> JevSettings:
    return parse_jev_settings(parse_json_object(text, CONFIG_FILE).get("jev"))


AUTO = "auto"


class TerminalSettings(NamedTuple):
    tab_routing: str = "project"
    terminal_window: str = "last"
    launch_via: str = "direct"
    focus_new_tabs: str = "auto"
    claude_mod: str = "on"
    allow_resume: bool = True
    ide_start_timeout_sec: float = DEFAULT_IDE_START_TIMEOUT_SEC
    preferred_terminal: str | None = None
    shell: str | None = None


def resolve_focus(setting: str, requested: bool | None) -> bool:
    if setting == "auto":
        return bool(requested)
    return setting == "always"


def _flag(config: dict[str, Any], key: str, fallback: bool, warnings: list[str]) -> bool:
    value = config.get(key)
    if value is None:
        return fallback
    if isinstance(value, bool):
        return value
    warnings.append(f"Ignoring {key} in {CONFIG_FILE}: it must be true or false")
    return fallback


def _seconds(config: dict[str, Any], key: str, fallback: float, maximum: float, warnings: list[str]) -> float:
    value = config.get(key)
    if value is None:
        return fallback
    if is_finite(value) and 0 < value <= maximum:
        return value
    warnings.append(f"Ignoring {key} in {CONFIG_FILE}: it must be a number of seconds above 0 and at most {maximum}")
    return fallback


def _choice(config: dict[str, Any], key: str, values: tuple[str, ...], warnings: list[str]) -> str:
    value = config.get(key)
    if value is None:
        return values[0]
    if isinstance(value, str) and value in values:
        return value
    listed = " or ".join(f'"{v}"' for v in values)
    warnings.append(f"Ignoring {key} in {CONFIG_FILE}: it must be {listed}")
    return values[0]


def read_terminal_settings(config: dict[str, Any], warnings: list[str]) -> TerminalSettings:
    tab_routing = _choice(config, "tabRouting", ("project", "caller"), warnings)
    terminal_window = _choice(config, "terminalWindow", ("last", "dedicated"), warnings)
    launch_via = _choice(config, "launchVia", ("direct", "ori"), warnings)
    focus_new_tabs = _choice(config, "focusNewTabs", ("auto", "always", "never"), warnings)
    claude_mod = _choice(config, "claudeMod", ("on", "off"), warnings)
    allow_resume = _flag(config, ALLOW_RESUME_KEY, True, warnings)
    timeout = _seconds(config, IDE_START_TIMEOUT_KEY, DEFAULT_IDE_START_TIMEOUT_SEC, MAX_IDE_START_TIMEOUT_SEC, warnings)
    preferred: str | None = None
    terminal = config.get("terminal")
    if isinstance(terminal, str):
        preferred = terminal if not _is_blank(terminal) and terminal != AUTO else None
    elif terminal is not None:
        warnings.append(f"Ignoring terminal in {CONFIG_FILE}: it must be a string")
    shell: str | None = None
    shell_value = config.get("shell")
    if isinstance(shell_value, str) and (_is_blank(shell_value) or shell_value == AUTO):
        shell = None
    elif isinstance(shell_value, str) and "\0" not in shell_value and (win32_is_absolute(shell_value) or posix_is_absolute(shell_value)):
        shell = shell_value
    elif shell_value is not None:
        warnings.append(f'Ignoring shell in {CONFIG_FILE}: it must be "auto" or the absolute path of a shell executable')
    return TerminalSettings(
        tab_routing=tab_routing,
        terminal_window=terminal_window,
        launch_via=launch_via,
        focus_new_tabs=focus_new_tabs,
        claude_mod=claude_mod,
        allow_resume=allow_resume,
        ide_start_timeout_sec=timeout,
        preferred_terminal=preferred or None,
        shell=shell or None,
    )


class AgentSettings(NamedTuple):
    profiles: list[AgentProfile]
    default_agent: AgentProfile
    terminal: TerminalSettings
    jev: JevSettings
    warnings: list[str]


def resolve_settings(
    agents_text: str | None,
    config_text: str | None,
    agents_path: str = AGENTS_FILE,
    config_path: str = CONFIG_FILE,
) -> AgentSettings:
    warnings: list[str] = []
    profiles: list[AgentProfile] = list(BUILTIN_PROFILES)
    if agents_text is not None:
        try:
            profiles = merge_profiles(BUILTIN_PROFILES, parse_profiles(agents_text))
        except ValueError as e:
            warnings.append(f"Ignoring {agents_path} and using the built-in agent profiles: {e}")
    configured: str | None = None
    terminal = TerminalSettings()
    jev = JEV_OFF
    if config_text is not None:
        readable = True
        try:
            configured = read_default_agent(config_text)
            terminal = read_terminal_settings(parse_json_object(config_text, CONFIG_FILE), warnings)
        except ValueError as e:
            readable = False
            warnings.append(f"Ignoring {config_path}: {e}")
        if readable:
            try:
                jev = read_jev_settings(config_text)
            except (ValueError, TypeError) as e:
                warnings.append(f"Ignoring jev in {config_path}, so Jev is off: {e}")
    default = next((p for p in profiles if p.name == configured), None) or next(p for p in profiles if p.name == DEFAULT_AGENT)
    return AgentSettings(profiles, default, terminal, jev, warnings)


def with_args(p: AgentProfile, args: tuple[str, ...]) -> AgentProfile:
    return p._replace(args=tuple(args))

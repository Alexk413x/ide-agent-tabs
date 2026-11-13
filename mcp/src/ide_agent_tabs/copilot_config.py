from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .agent_config import HookTarget, copilot_hooks, mentions_hook
from .home import HOME_ENV
from .profiles import AGENT_ENV, TAB_ID_ENV, ConfigError


def server_entry(argv: Sequence[str], home: str) -> dict[str, Any]:
    env = {name: f"${{{name}}}" for name in (TAB_ID_ENV, AGENT_ENV)}
    return {"type": "local", "command": argv[0], "args": list(argv[1:]), "env": {**env, HOME_ENV: home}, "tools": ["*"]}


def _is_ours(handler: Any) -> bool:
    if not isinstance(handler, dict):
        return False
    args = handler.get("args")
    return any(mentions_hook(part) for part in (handler.get("exec"), handler.get("command"), *(args if isinstance(args, list) else [])))


def desired_hooks(target: HookTarget, home: str) -> dict[str, Any]:
    root = copilot_hooks(target)
    for handlers in root["hooks"].values():
        for handler in handlers:
            handler["env"] = {HOME_ENV: home}
    return root


def merge_hooks(root: dict[str, Any], file: str, target: HookTarget | None, home: str) -> dict[str, Any]:
    if root.get("version", 1) != 1:
        raise ConfigError(f"{file}: unsupported hooks version")
    current = root.get("hooks", {})
    if not isinstance(current, dict):
        raise ConfigError(f'{file}: "hooks" isn\'t an object')
    hooks: dict[str, Any] = {}
    for event, handlers in current.items():
        if not isinstance(handlers, list):
            raise ConfigError(f'{file}: "hooks.{event}" isn\'t a list')
        kept = [handler for handler in handlers if not _is_ours(handler)]
        if kept:
            hooks[event] = kept
    if target is not None:
        for event, handlers in desired_hooks(target, home)["hooks"].items():
            hooks[event] = [*hooks.get(event, []), *handlers]
    return {**root, "version": 1, "hooks": hooks}


def has_hooks(root: dict[str, Any], target: HookTarget, home: str) -> bool:
    hooks = root.get("hooks")
    if root.get("version") != 1 or not isinstance(hooks, dict):
        return False
    return all(
        isinstance(hooks.get(event), list) and all(handler in hooks[event] for handler in handlers)
        for event, handlers in desired_hooks(target, home)["hooks"].items()
    )

from __future__ import annotations

import os
from typing import Any, Callable, NamedTuple

from .jsjson import js_trim, utf16_len
from .jspath import is_absolute, normalize
from .launch_plan import MODEL_SOURCE, is_model
from .profiles import MAX_ENTRIES, MAX_PROMPT_CHARS, ConfigError, check_env


class OpenRequest(NamedTuple):
    path: str
    args: list[str]
    env: dict[str, str]
    agent: str | None = None
    prompt: str | None = None
    ide: str | None = None
    model: str | None = None
    via: str | None = None
    focus: bool | None = None

    def with_focus(self, focus: bool) -> OpenRequest:
        return self._replace(focus=focus)


def _is_directory(p: str) -> bool:
    try:
        return os.path.isdir(p)
    except (OSError, ValueError):
        return False


def validate_open(given: dict[str, Any], is_directory: Callable[[str], bool] = _is_directory) -> OpenRequest:
    folder = given.get("path")
    if not isinstance(folder, str) or js_trim(folder) == "":
        raise ConfigError("path is required")
    if not is_absolute(folder):
        raise ConfigError("path must be absolute")
    if "\0" in folder or not is_directory(folder):
        raise ConfigError(f"path is not a directory: {folder}")
    prompt = given.get("prompt")
    if prompt is not None and utf16_len(prompt) > MAX_PROMPT_CHARS:
        raise ConfigError(f"prompt exceeds {MAX_PROMPT_CHARS} characters")
    if prompt is not None and "\0" in prompt:
        raise ConfigError("prompt holds a NUL")
    args = list(given.get("args") or [])
    if len(args) > MAX_ENTRIES:
        raise ConfigError(f"args exceeds {MAX_ENTRIES} entries")
    if any(utf16_len(a) > MAX_PROMPT_CHARS for a in args):
        raise ConfigError(f"an arg exceeds {MAX_PROMPT_CHARS} characters")
    if any("\0" in a for a in args):
        raise ConfigError("an arg holds a NUL")
    env = dict(given.get("env") or {})
    check_env(env, "env")
    agent = given.get("agent")
    ide = given.get("ide")
    model = given.get("model")
    via = given.get("via")
    focus = given.get("focus")
    if agent is not None and js_trim(agent) == "":
        raise ConfigError("agent must not be blank")
    if ide is not None and js_trim(ide) == "":
        raise ConfigError("ide must not be blank")
    if model is not None and not is_model(model):
        raise ConfigError(f"model must match {MODEL_SOURCE}")
    if via is not None and via not in ("ori", "direct"):
        raise ConfigError('via must be "ori" or "direct"')
    if focus is not None and not isinstance(focus, bool):
        raise ConfigError("focus must be true or false")
    return OpenRequest(
        path=normalize(folder),
        args=args,
        env=env,
        agent=agent,
        prompt=prompt if prompt is not None and js_trim(prompt) != "" else None,
        ide=ide,
        model=model,
        via=via,
        focus=focus,
    )

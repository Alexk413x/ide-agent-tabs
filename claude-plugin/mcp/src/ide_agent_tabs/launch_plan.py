from __future__ import annotations

import re
from typing import Any, NamedTuple

from .profiles import GOOSE_EMPTY_ARGS, GOOSE_RUN_ARGS, AgentLaunch, AgentProfile, ConfigError, launch_of, with_codex_python

ORI_AGENTS: tuple[str, ...] = ("claude", "codex", "grok", "hermes", "opencode", "pi", "prime-agent")
MODEL_SOURCE = "^[A-Za-z0-9._:/@+-]{1,200}$"
MODEL_PATTERN = re.compile(r"[A-Za-z0-9._:/@+-]{1,200}")
# Ori refuses to pass these to a .cmd shim such as the npm codex.cmd.
_ORI_CMD_SPECIALS = re.compile(r'["%^&|<>]')


def is_model(value: str) -> bool:
    return MODEL_PATTERN.fullmatch(value) is not None


class LaunchRequest(NamedTuple):
    args: list[str]
    env: dict[str, str]
    launch_via: str
    ori: dict[str, Any] | None
    platform: str
    prompt: str | None = None
    model: str | None = None
    via: str | None = None
    cmd_shim: bool | None = None
    python: tuple[str, ...] | None = None


class LaunchPlan(NamedTuple):
    launch: AgentLaunch
    via: str


def _ori_refusal(p: AgentProfile, r: LaunchRequest) -> str | None:
    if not r.ori:
        return "Ori isn't installed"
    if p.name not in ORI_AGENTS:
        return f"Ori has no {p.name} launcher"
    if p.name not in r.ori.get("agents", []):
        return f"Ori lists {p.name} as not installed"
    passed = [*p.args, *r.args, *([r.model] if r.model is not None else []), *([r.prompt] if r.prompt is not None else [])]
    if r.platform == "win32" and r.cmd_shim is not False and any(_ORI_CMD_SPECIALS.search(a) for a in passed):
        return "an argument holds one of \" % ^ & | < >, which Ori won't pass to a cmd.exe shim on Windows"
    return None


def _without_prompt(p: AgentProfile, prompt: str | None) -> AgentProfile:
    if prompt is not None or p.command != "goose" or tuple(p.args) != GOOSE_RUN_ARGS:
        return p
    return p._replace(args=GOOSE_EMPTY_ARGS)


def plan_launch(profile: AgentProfile, r: LaunchRequest) -> LaunchPlan:
    p = _without_prompt(profile, r.prompt)
    p = p._replace(args=with_codex_python(p.args, r.python))
    if r.model is not None and not is_model(r.model):
        raise ConfigError(f"model must match {MODEL_SOURCE}")
    via = r.via if r.via is not None else r.launch_via
    if via == "ori":
        refusal = _ori_refusal(p, r)
        if refusal is not None and r.via == "ori":
            raise ConfigError(f"{p.name} can't launch through Ori: {refusal}")
        if refusal is not None:
            via = "direct"
    if via == "ori":
        inner = launch_of(p, r.prompt, r.args, r.env)
        model = ["--model", r.model] if r.model is not None else []
        return LaunchPlan(AgentLaunch(inner.agent, "ori", [p.name, *model, *inner.args], inner.prompt, inner.env), via)
    if r.model is not None and p.model_flag is None:
        raise ConfigError(f"{p.name} has no model option; open it without model, or set modelFlag for it in agents.json")
    model = [p.model_flag, r.model] if r.model is not None and p.model_flag is not None else []
    return LaunchPlan(launch_of(p._replace(args=(*p.args, *model)), r.prompt, r.args, r.env), via)

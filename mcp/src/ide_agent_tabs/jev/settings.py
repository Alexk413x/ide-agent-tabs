from __future__ import annotations

import math
import os
import re
from typing import Any, NamedTuple

from ..files import read_text_if_exists
from ..jsjson import JS_SPACE, parse, trim, utf16_len

JEV_MODEL = "jev-latest"
DEFAULT_SURE = 0.85
DEFAULT_PRICE_PER_MILLION_INPUT = 0.042
CONFIG_FILE = "config.json"

_TIER_NAME = re.compile("[A-Za-z0-9][A-Za-z0-9._-]{0,63}(?::([^" + re.escape(JS_SPACE) + ":]{1,128}))?")


class JevSettings(NamedTuple):
    enabled: bool
    sure: float
    tiers: dict[str, str]
    price_per_million_input: float


JEV_OFF = JevSettings(False, DEFAULT_SURE, {}, DEFAULT_PRICE_PER_MILLION_INPUT)


class SettingsError(ValueError):
    pass


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _tier_name_ok(name: str) -> bool:
    match = _TIER_NAME.fullmatch(name)
    return match is not None and (match.group(1) is None or utf16_len(match.group(1)) <= 128)


def _tiers_of(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SettingsError("jev.tiers must be an object of tier name to description")
    tiers: dict[str, str] = {}
    for name, text in value.items():
        if not _tier_name_ok(name):
            raise SettingsError(f"jev.tiers name '{name}' must be <profile> or <profile>:<model>")
        if not isinstance(text, str) or trim(text) == "":
            raise SettingsError(f"jev.tiers.{name} must be a description")
        tiers[name] = text
    return tiers


def parse_jev_settings(value: Any) -> JevSettings:
    if value is None:
        return JEV_OFF
    if not isinstance(value, dict):
        raise SettingsError("jev must be an object")
    enabled = value.get("enabled")
    sure = value.get("sure")
    tiers = value.get("tiers")
    price = value.get("pricePerMillionInput")
    if enabled is not None and not isinstance(enabled, bool):
        raise SettingsError("jev.enabled must be true or false")
    if sure is not None and (not is_number(sure) or not (0 < sure <= 1)):
        raise SettingsError("jev.sure must be a number above 0 and at most 1")
    if price is not None and (not is_number(price) or not math.isfinite(price) or price < 0):
        raise SettingsError("jev.pricePerMillionInput must be a number of dollars, 0 or more")
    return JevSettings(
        enabled=enabled is True,
        sure=DEFAULT_SURE if sure is None else sure,
        tiers=_tiers_of(tiers),
        price_per_million_input=DEFAULT_PRICE_PER_MILLION_INPUT if price is None else price,
    )


def config_path(home: str) -> str:
    return os.path.join(home, CONFIG_FILE)


def read_jev_config(home: str) -> tuple[JevSettings, list[str]]:
    path = config_path(home)
    try:
        text = read_text_if_exists(path)
    except OSError:
        text = None
    if text is None:
        return JEV_OFF, []
    try:
        config = parse(text)
    except ValueError as e:
        return JEV_OFF, [f"Ignoring {path}: {CONFIG_FILE} is not JSON: {e}"]
    if not isinstance(config, dict):
        return JEV_OFF, [f"Ignoring {path}: {CONFIG_FILE} must hold a JSON object"]
    try:
        return parse_jev_settings(config.get("jev")), []
    except SettingsError as e:
        return JEV_OFF, [f"Ignoring jev in {path}, so Jev is off: {e}"]

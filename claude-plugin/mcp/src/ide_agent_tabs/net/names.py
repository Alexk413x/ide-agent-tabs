from __future__ import annotations

import os
import re
import socket

from ..files import read_text_if_exists
from ..jev.settings import CONFIG_FILE
from ..jsjson import parse
from .pairing import valid_device_name
from .policy import Policy

_NAME_UNSAFE = re.compile(r"[^A-Za-z0-9 ._-]+")
_LABEL_UNSAFE = re.compile(r"[^a-z0-9-]+")
BAD_DEVICE_NAME = "network.deviceName in config.json must be 1 to 32 letters, digits, spaces, - _ or ., with no run of six digits"


class DeviceNameError(ValueError):
    pass


def configured_device_name(home: str) -> object:
    try:
        text = read_text_if_exists(os.path.join(home, CONFIG_FILE))
        section = parse(text).get("network") if text and text.strip() else None  # pyright: ignore[reportUnknownMemberType]
    except (OSError, ValueError, AttributeError):
        return None
    return section.get("deviceName") if isinstance(section, dict) else None  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]


def device_name_of(home: str) -> str:
    configured = configured_device_name(home)
    if configured is not None:
        if not isinstance(configured, str) or not valid_device_name(configured):
            raise DeviceNameError(BAD_DEVICE_NAME)
        return configured
    cleaned = _NAME_UNSAFE.sub("-", socket.gethostname()).strip(" -.")[:32].strip(" -.")
    return cleaned if cleaned and valid_device_name(cleaned) else "this-machine"


def default_label(name: str, policy: Policy) -> str:
    base = _LABEL_UNSAFE.sub("-", name.lower()).strip("-")[:32].strip("-") or "peer"
    taken = {peer.label for peer in policy.peers}
    label, number = base, 2
    while label in taken:
        suffix = f"-{number}"
        label, number = base[: 32 - len(suffix)] + suffix, number + 1
    return label

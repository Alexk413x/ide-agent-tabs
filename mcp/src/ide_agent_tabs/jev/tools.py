from __future__ import annotations

from typing import Any, Callable

from .inputs import INPUTS, parse_input
from .service import Jev

RUNNERS: dict[str, Callable[[Jev, dict[str, Any]], dict[str, Any]]] = {
    "jev_status": lambda jev, _: jev.status(),
    "jev_ask": Jev.ask,
    "jev_choose": Jev.choose,
    "jev_check": Jev.check,
    "jev_rank": Jev.rank,
    "jev_route": Jev.route,
}

TOOL_NAMES = tuple(RUNNERS)


def takes_input(tool: str) -> bool:
    return bool(INPUTS[tool].shape)


def run_tool(jev: Jev, tool: str, arguments: Any) -> dict[str, Any]:
    return RUNNERS[tool](jev, parse_input(tool, arguments))

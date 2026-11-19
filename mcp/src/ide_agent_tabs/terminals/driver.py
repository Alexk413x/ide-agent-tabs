from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar, NamedTuple

from ..spec import LaunchSpec

Capabilities = dict[str, str]
TerminalTab = dict[str, Any]


def caps(open_: str, list_: str, close: str) -> Capabilities:
    return {"open": open_, "list": list_, "close": close}


class TerminalContext(NamedTuple):
    home: str
    scripts_dir: str
    path_var: str
    env: Mapping[str, str]
    power_shell: str | None = None

    def with_power_shell(self, shell: str) -> TerminalContext:
        return self._replace(power_shell=shell)


class OpenOptions(NamedTuple):
    window: str = "last"
    near: TerminalTab | None = None
    focus: bool | None = None


# A terminal driver never passes caller text on a command line: the caller's command, arguments, prompt
# and env travel in the spec file the launcher reads. A command line holds only fixed flags, our own paths,
# the checked folder path and a title cleaned by tab_title. input gets only the fixed wake line from the
# messaging notice, never message text.
class TerminalDriver:
    name = ""
    label = ""
    capabilities: ClassVar[Capabilities] = {}
    can_input = False

    def current_capabilities(self, ctx: TerminalContext) -> Capabilities:
        return self.capabilities

    def available(self, ctx: TerminalContext) -> bool:
        raise NotImplementedError

    def open(self, ctx: TerminalContext, spec: LaunchSpec, title: str, options: OpenOptions | None = None) -> TerminalTab:
        raise NotImplementedError

    def alive(self, ctx: TerminalContext, tabs: list[TerminalTab]) -> set[str]:
        raise NotImplementedError

    def close(self, ctx: TerminalContext, tab: TerminalTab) -> None:
        raise NotImplementedError

    def input(self, ctx: TerminalContext, tab: TerminalTab, text: str) -> None:
        raise NotImplementedError

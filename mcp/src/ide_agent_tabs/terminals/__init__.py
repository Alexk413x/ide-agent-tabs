from __future__ import annotations

from .driver import TerminalDriver
from .ghostty import GHOSTTY
from .ghostty import ghostty as _ghostty
from .iterm2 import ITERM2
from .iterm2 import iterm2 as _iterm2
from .kitty import KITTY
from .kitty import kitty as _kitty
from .tmux import TMUX
from .tmux import tmux as _tmux
from .wezterm import WEZTERM
from .wezterm import wezterm as _wezterm
from .windows_terminal import WINDOWS_TERMINAL
from .windows_terminal import windows_terminal as _windows_terminal

TERMINAL_DRIVERS: list[TerminalDriver] = [_windows_terminal, _ghostty, _iterm2, _kitty, _wezterm, _tmux]

_DEFAULT_ORDER: dict[str, list[str]] = {
    "win32": [WINDOWS_TERMINAL, WEZTERM],
    "darwin": [GHOSTTY, ITERM2, KITTY, WEZTERM, TMUX],
    "linux": [GHOSTTY, KITTY, WEZTERM, TMUX],
}


def default_terminal_name(platform: str, available: list[str]) -> str | None:
    return next((name for name in _DEFAULT_ORDER.get(platform, []) if name in available), None)

from __future__ import annotations

from .driver import TerminalDriver

TERMINAL_DRIVERS: list[TerminalDriver] = []

_DEFAULT_ORDER: dict[str, list[str]] = {
    "win32": ["windows-terminal", "wezterm"],
    "darwin": ["ghostty", "iterm2", "kitty", "wezterm", "tmux"],
    "linux": ["ghostty", "kitty", "wezterm", "tmux"],
}


def default_terminal_name(platform: str, available: list[str]) -> str | None:
    return next((name for name in _DEFAULT_ORDER.get(platform, []) if name in available), None)

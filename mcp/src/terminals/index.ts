import { ghostty, GHOSTTY } from './ghostty.js';
import { kitty, KITTY } from './kitty.js';
import { tmux, TMUX } from './tmux.js';
import type { TerminalDriver } from './types.js';
import { wezterm, WEZTERM } from './wezterm.js';
import { windowsTerminal, WINDOWS_TERMINAL } from './windowsTerminal.js';

export const TERMINAL_DRIVERS: TerminalDriver[] = [windowsTerminal, ghostty, kitty, wezterm, tmux];

const DEFAULT_ORDER: Partial<Record<NodeJS.Platform, string[]>> = {
  win32: [WINDOWS_TERMINAL, WEZTERM],
  darwin: [GHOSTTY, KITTY, WEZTERM, TMUX],
  linux: [GHOSTTY, KITTY, WEZTERM, TMUX],
};

export function defaultTerminalName(platform: NodeJS.Platform, available: string[]): string | undefined {
  return DEFAULT_ORDER[platform]?.find((name) => available.includes(name));
}

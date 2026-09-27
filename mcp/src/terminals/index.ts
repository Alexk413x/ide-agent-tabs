import { ghostty, GHOSTTY } from './ghostty.js';
import type { TerminalDriver } from './types.js';
import { windowsTerminal, WINDOWS_TERMINAL } from './windowsTerminal.js';

export const TERMINAL_DRIVERS: TerminalDriver[] = [windowsTerminal, ghostty];

export function defaultTerminalName(platform: NodeJS.Platform): string | undefined {
  if (platform === 'win32') return WINDOWS_TERMINAL;
  if (platform === 'darwin') return GHOSTTY;
  return undefined;
}

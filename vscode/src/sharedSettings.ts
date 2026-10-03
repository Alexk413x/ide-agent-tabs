import { parseObject } from './request';

export const DETECTED_FILE = 'detected.json';
export const AUTO = 'auto';

export const TAB_ROUTINGS = ['project', 'caller'] as const;
export const TERMINAL_WINDOWS = ['last', 'dedicated'] as const;

export type TabRouting = (typeof TAB_ROUTINGS)[number];
export type TerminalWindow = (typeof TERMINAL_WINDOWS)[number];

export interface SharedSettings {
  tabRouting: TabRouting;
  terminal: string;
  shell: string;
  terminalWindow: TerminalWindow;
}

export const SHARED_DEFAULTS: Readonly<SharedSettings> = Object.freeze({
  tabRouting: 'project',
  terminal: AUTO,
  shell: AUTO,
  terminalWindow: 'last',
});

export interface DetectedTerminal {
  id: string;
  name: string;
}

export interface DetectedShell {
  path: string;
  label: string;
}

export interface Detected {
  terminals: DetectedTerminal[];
  shells: DetectedShell[];
}

function oneOf<T extends string>(allowed: readonly T[], value: unknown): T | undefined {
  return allowed.find(a => a === value);
}

function pathOrAuto(value: unknown): string | undefined {
  if (typeof value !== 'string') return undefined;
  return value.trim() === '' ? AUTO : value;
}

export function readSharedSettings(text: string, file: string): Partial<SharedSettings> {
  const root = parseObject(text, file);
  const found: Partial<SharedSettings> = {};
  const tabRouting = oneOf(TAB_ROUTINGS, root.tabRouting);
  if (tabRouting !== undefined) found.tabRouting = tabRouting;
  const terminal = pathOrAuto(root.terminal);
  if (terminal !== undefined) found.terminal = terminal;
  const shell = pathOrAuto(root.shell);
  if (shell !== undefined) found.shell = shell;
  const terminalWindow = oneOf(TERMINAL_WINDOWS, root.terminalWindow);
  if (terminalWindow !== undefined) found.terminalWindow = terminalWindow;
  return found;
}

export function withSharedValue(existing: string | undefined, file: string, key: keyof SharedSettings, value: string): string {
  const root = existing === undefined || existing.trim() === '' ? {} : parseObject(existing, file);
  if (value === AUTO || value.trim() === '') delete root[key];
  else root[key] = value;
  return JSON.stringify(root, null, 2) + '\n';
}

export function parseDetected(text: string): Detected {
  const root = parseObject(text, DETECTED_FILE);
  const terminals: DetectedTerminal[] = [];
  if (Array.isArray(root.terminals)) {
    for (const entry of root.terminals) {
      if (entry === null || typeof entry !== 'object') continue;
      const { id, name } = entry as Record<string, unknown>;
      if (typeof id === 'string' && id !== '') terminals.push({ id, name: typeof name === 'string' && name !== '' ? name : id });
    }
  }
  const shells: DetectedShell[] = [];
  if (Array.isArray(root.shells)) {
    for (const entry of root.shells) {
      if (entry === null || typeof entry !== 'object') continue;
      const { path, label } = entry as Record<string, unknown>;
      if (typeof path === 'string' && path !== '') shells.push({ path, label: typeof label === 'string' && label !== '' ? label : path });
    }
  }
  return { terminals, shells };
}

export interface InspectedSetting {
  globalValue?: unknown;
  workspaceValue?: unknown;
  workspaceFolderValue?: unknown;
}

export function userSettingValue(key: keyof SharedSettings, inspected: InspectedSetting | undefined): string {
  const value = inspected?.globalValue;
  return typeof value === 'string' && value.trim() !== '' ? value : SHARED_DEFAULTS[key];
}

import { parseObject } from './request';

export const DETECTED_FILE = 'detected.json';
export const AUTO = 'auto';

export const TAB_ROUTINGS = ['project', 'caller'] as const;
export const TERMINAL_WINDOWS = ['last', 'dedicated'] as const;
export const LAUNCH_VIA = ['direct', 'ori'] as const;
export const FOCUS_NEW_TABS = ['auto', 'always', 'never'] as const;
export const CLAUDE_MOD = ['on', 'off'] as const;

export type TabRouting = (typeof TAB_ROUTINGS)[number];
export type TerminalWindow = (typeof TERMINAL_WINDOWS)[number];
export type LaunchVia = (typeof LAUNCH_VIA)[number];
export type FocusNewTabs = (typeof FOCUS_NEW_TABS)[number];
export type ClaudeMod = (typeof CLAUDE_MOD)[number];

export interface SharedSettings {
  tabRouting: TabRouting;
  terminal: string;
  shell: string;
  terminalWindow: TerminalWindow;
  launchVia: LaunchVia;
  closeAfterHandoff: boolean;
  allowResume: boolean;
  focusNewTabs: FocusNewTabs;
  claudeMod: ClaudeMod;
}

export const SHARED_DEFAULTS: Readonly<SharedSettings> = Object.freeze({
  tabRouting: 'project',
  terminal: AUTO,
  shell: AUTO,
  terminalWindow: 'last',
  launchVia: 'direct',
  closeAfterHandoff: true,
  allowResume: true,
  focusNewTabs: AUTO,
  claudeMod: 'on',
});

export interface DetectedTerminal {
  id: string;
  name: string;
}

export interface DetectedShell {
  path: string;
  label: string;
}

export interface DetectedOri {
  path: string;
  version?: string;
  agents: string[];
}

export interface Detected {
  terminals: DetectedTerminal[];
  shells: DetectedShell[];
  ori: DetectedOri | null;
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
  const launchVia = oneOf(LAUNCH_VIA, root.launchVia);
  if (launchVia !== undefined) found.launchVia = launchVia;
  if (typeof root.closeAfterHandoff === 'boolean') found.closeAfterHandoff = root.closeAfterHandoff;
  if (typeof root.allowResume === 'boolean') found.allowResume = root.allowResume;
  const focusNewTabs = oneOf(FOCUS_NEW_TABS, root.focusNewTabs);
  if (focusNewTabs !== undefined) found.focusNewTabs = focusNewTabs;
  const claudeMod = oneOf(CLAUDE_MOD, root.claudeMod);
  if (claudeMod !== undefined) found.claudeMod = claudeMod;
  return found;
}

export function withSharedValue(existing: string | undefined, file: string, key: keyof SharedSettings, value: string | boolean): string {
  const root = existing === undefined || existing.trim() === '' ? {} : parseObject(existing, file);
  if (typeof value === 'boolean') {
    if (value === SHARED_DEFAULTS[key]) delete root[key];
    else root[key] = value;
  } else if (value === AUTO || value.trim() === '' || (key === 'claudeMod' && value === SHARED_DEFAULTS.claudeMod)) delete root[key];
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
  return { terminals, shells, ori: parseOri(root.ori) };
}

function parseOri(value: unknown): DetectedOri | null {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return null;
  const { path, version, agents } = value as Record<string, unknown>;
  if (typeof path !== 'string' || path === '') return null;
  return {
    path,
    version: typeof version === 'string' && version !== '' ? version : undefined,
    agents: Array.isArray(agents) ? agents.filter((a): a is string => typeof a === 'string' && a !== '') : [],
  };
}

export interface InspectedSetting {
  globalValue?: unknown;
  workspaceValue?: unknown;
  workspaceFolderValue?: unknown;
}

export function userSettingValue<K extends keyof SharedSettings>(key: K, inspected: InspectedSetting | undefined): SharedSettings[K] {
  const value = inspected?.globalValue;
  const fallback = SHARED_DEFAULTS[key];
  if (typeof fallback === 'boolean') return (typeof value === 'boolean' ? value : fallback) as SharedSettings[K];
  return (typeof value === 'string' && value.trim() !== '' ? value : fallback) as SharedSettings[K];
}

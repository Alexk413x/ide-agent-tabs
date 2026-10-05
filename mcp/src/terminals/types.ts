import type { LaunchSpec } from '../spec.js';

export interface TerminalCapabilities {
  open: 'tab' | 'window';
  list: 'yes' | 'tracked' | 'no';
  close: 'yes' | 'best-effort' | 'no';
}

export interface TerminalTab {
  id: string;
  terminal: string;
  agent: string;
  path: string;
  createdAt: number;
  pidFile?: string;
  terminalTabId?: string;
  terminalId?: string;
  socket?: string;
  serverPid?: number;
  window?: string;
}

export type OpenedTab = TerminalTab & { note?: string };

export interface TerminalContext {
  home: string;
  scriptsDir: string;
  pathVar: string;
  env: NodeJS.ProcessEnv;
  powerShell?: string;
}

export type TerminalWindowMode = 'last' | 'dedicated';

export interface OpenOptions {
  window: TerminalWindowMode;
  near?: TerminalTab;
  focus?: boolean;
}

// A terminal driver never passes caller text on a command line: the caller's command, arguments, prompt
// and env travel in the spec file the launcher reads. A command line holds only fixed flags, our own paths,
// the checked folder path and a title cleaned by tabTitle. input gets only the fixed wake line from
// messaging/notice.ts, never message text.
export interface TerminalDriver {
  name: string;
  label: string;
  capabilities: TerminalCapabilities;
  currentCapabilities?(ctx: TerminalContext): Promise<TerminalCapabilities>;
  available(ctx: TerminalContext): Promise<boolean>;
  open(ctx: TerminalContext, spec: LaunchSpec, title: string, options?: OpenOptions): Promise<OpenedTab>;
  alive(ctx: TerminalContext, tabs: TerminalTab[]): Promise<Set<string>>;
  close(ctx: TerminalContext, tab: TerminalTab): Promise<void>;
  input?(ctx: TerminalContext, tab: TerminalTab, text: string): Promise<void>;
}

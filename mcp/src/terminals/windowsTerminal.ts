import { promises as fs, lstatSync } from 'node:fs';
import path from 'node:path';
import { writeNewPrivateFile } from '../files.js';
import { findOnPath } from '../installed.js';
import { run } from '../process.js';
import { powerShellSpec, type LaunchSpec } from '../spec.js';
import { defaultPowerShell } from './powershell.js';
import { readPid, startDetached, STARTUP_GRACE_MS, terminalEnvironment } from './processes.js';
import { checkArgvPaths, tabTitle } from './shell.js';
import type { OpenOptions, TerminalDriver, TerminalTab } from './types.js';
import { DEDICATED_NAME } from './windowMemory.js';

export const WINDOWS_TERMINAL = 'windows-terminal';
export const LAUNCHER_PS1 = 'agent-launch.ps1';
const WT_SETTLE_MS = 10_000;
const SHELL_IMAGES = new Set(['pwsh.exe', 'powershell.exe']);

export function findWindowsTerminal(pathVar: string, localAppData: string | undefined): string | undefined {
  const onPath = findOnPath(pathVar, 'wt.exe');
  if (onPath) return onPath;
  if (!localAppData) return undefined;
  const alias = path.join(localAppData, 'Microsoft', 'WindowsApps', 'wt.exe');
  try {
    lstatSync(alias);
    return alias;
  } catch {
    return undefined;
  }
}

// wt reads ';' in its command line as a subcommand separator, so no argument may hold one.
export function wtTitle(label: string): string {
  return tabTitle(label.replace(/;/g, ' '));
}

export function powerShellArgv(shell: string, launcher: string, spec: string): string[] {
  return [shell, '-NoLogo', '-NoExit', '-ExecutionPolicy', 'Bypass', '-File', launcher, spec];
}

export const WT_LAST_WINDOW = '0';

export function wtWindow(options?: OpenOptions): string {
  if (options?.near) return options.near.window ?? WT_LAST_WINDOW;
  return options?.window === 'dedicated' ? DEDICATED_NAME : WT_LAST_WINDOW;
}

export function wtArgs(o: { title: string; shell: string; launcher: string; spec: string; window?: string }): string[] {
  checkArgvPaths('Windows Terminal', [o.shell, o.launcher, o.spec], ';');
  return ['-w', o.window ?? WT_LAST_WINDOW, 'new-tab', '--title', wtTitle(o.title), ...powerShellArgv(o.shell, o.launcher, o.spec)];
}

export function parseTasklist(csv: string): Map<number, string> {
  const images = new Map<number, string>();
  for (const line of csv.split(/\r?\n/)) {
    const cells = [...line.matchAll(/"([^"]*)"/g)].map((m) => m[1]!);
    const pid = Number(cells[1]);
    if (cells.length >= 2 && Number.isSafeInteger(pid)) images.set(pid, cells[0]!.toLowerCase());
  }
  return images;
}

async function shellImages(): Promise<Map<number, string>> {
  const result = await run('tasklist.exe', ['/FO', 'CSV', '/NH'], { timeoutMs: 15_000 });
  return parseTasklist(result.stdout);
}

export const windowsTerminal: TerminalDriver = {
  name: WINDOWS_TERMINAL,
  label: 'Windows Terminal',
  capabilities: { open: 'tab', list: 'tracked', close: 'best-effort' },

  async available(ctx) {
    return process.platform === 'win32' && findWindowsTerminal(ctx.pathVar, ctx.env.LOCALAPPDATA) !== undefined;
  },

  async open(ctx, spec: LaunchSpec, title, options) {
    const wt = findWindowsTerminal(ctx.pathVar, ctx.env.LOCALAPPDATA);
    if (!wt) throw new Error('Windows Terminal (wt.exe) was not found');
    const dir = path.join(ctx.home, 'launch');
    const specFile = path.join(dir, `${spec.id}.json`);
    const pidFile = path.join(dir, `${spec.id}.pid`);
    const window = wtWindow(options);
    const shell = ctx.powerShell ?? defaultPowerShell(ctx.env);
    const args = wtArgs({ title, shell, launcher: path.join(ctx.scriptsDir, LAUNCHER_PS1), spec: specFile, window });
    await writeNewPrivateFile(specFile, powerShellSpec({ ...spec, pidFile }));
    try {
      await startDetached(wt, args, terminalEnvironment(ctx.env), WT_SETTLE_MS);
    } catch (e) {
      await fs.rm(specFile, { force: true });
      throw e;
    }
    return {
      id: spec.id,
      terminal: WINDOWS_TERMINAL,
      agent: spec.agent,
      path: spec.cwd,
      createdAt: Date.now(),
      pidFile,
      ...(window !== WT_LAST_WINDOW ? { window } : {}),
    };
  },

  async alive(_ctx, tabs) {
    const images = await shellImages();
    const alive = new Set<string>();
    for (const tab of tabs) {
      const pid = await readPid(tab.pidFile);
      if (pid !== undefined ? SHELL_IMAGES.has(images.get(pid) ?? '') : Date.now() - tab.createdAt < STARTUP_GRACE_MS) {
        alive.add(tab.id);
      }
    }
    return alive;
  },

  async close(_ctx, tab: TerminalTab) {
    const pid = await readPid(tab.pidFile);
    if (pid === undefined) throw new Error(`tab ${tab.id} has no running shell yet, or its shell has ended`);
    const images = await shellImages();
    if (SHELL_IMAGES.has(images.get(pid) ?? '')) {
      const result = await run('taskkill.exe', ['/PID', String(pid), '/T', '/F'], { timeoutMs: 15_000 });
      if (result.code !== 0) throw new Error(`taskkill failed: ${result.stderr.trim() || result.stdout.trim()}`);
    }
    if (tab.pidFile) await fs.rm(tab.pidFile, { force: true });
  },
};

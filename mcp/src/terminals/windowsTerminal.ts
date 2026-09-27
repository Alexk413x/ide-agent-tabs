import { spawn } from 'node:child_process';
import { promises as fs, lstatSync } from 'node:fs';
import path from 'node:path';
import { writeNewPrivateFile } from '../files.js';
import { findOnPath } from '../installed.js';
import { run } from '../process.js';
import { powerShellSpec, type LaunchSpec } from '../spec.js';
import type { TerminalDriver, TerminalTab } from './types.js';

export const WINDOWS_TERMINAL = 'windows-terminal';
export const LAUNCHER_PS1 = 'agent-launch.ps1';
const STARTUP_GRACE_MS = 60_000;
const SHELL_IMAGES = new Set(['pwsh.exe', 'powershell.exe']);

export function findPowerShell(pathVar: string): string {
  return findOnPath(pathVar, 'pwsh.exe') ?? findOnPath(pathVar, 'powershell.exe') ?? 'powershell.exe';
}

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
  const clean = label.replace(/[;\p{Cc}]/gu, ' ').trim();
  return clean === '' ? 'Agent' : clean;
}

export function wtArgs(o: { title: string; shell: string; launcher: string; spec: string }): string[] {
  for (const p of [o.shell, o.launcher, o.spec]) {
    if (p.includes(';')) throw new Error(`Windows Terminal can't start a path that holds ';': ${p}`);
  }
  return [
    '-w', '0', 'new-tab', '--title', wtTitle(o.title),
    o.shell, '-NoLogo', '-NoExit', '-ExecutionPolicy', 'Bypass', '-File', o.launcher, o.spec,
  ];
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

const SESSION_ENV = new Set([
  'CLAUDECODE',
  'CLAUDE_CODE_ENTRYPOINT',
  'CLAUDE_CODE_SSE_PORT',
  'CLAUDE_CODE_SESSION_ID',
  'CLAUDE_CODE_CHILD_SESSION',
  'CLAUDE_CODE_BRIDGE_SESSION_ID',
  'CLAUDE_CODE_MESSAGING_SOCKET',
  'CLAUDE_CODE_MESSAGING_TOKEN',
  'CLAUDE_CODE_SESSION_ATTENDED',
  'CLAUDE_CODE_EXECPATH',
  'CLAUDE_PID',
  'CLAUDE_EFFORT',
  'CLAUDE_PLUGIN_ROOT',
  'CLAUDE_PLUGIN_DATA',
  'IDE_AGENT_TABS_ID',
  'IDE_AGENT_TABS_AGENT',
]);

// When Windows Terminal isn't running, wt.exe starts it with this environment and every later tab in that
// window inherits it, so variables that identify the calling agent session are dropped.
export function wtEnvironment(env: NodeJS.ProcessEnv): NodeJS.ProcessEnv {
  return Object.fromEntries(Object.entries(env).filter(([name]) => !SESSION_ENV.has(name.toUpperCase())));
}

function startDetached(command: string, args: string[], env: NodeJS.ProcessEnv): Promise<void> {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { stdio: 'ignore', windowsHide: true, detached: true, env });
    const timer = setTimeout(() => {
      child.unref();
      resolve();
    }, 10_000);
    child.on('error', (e) => {
      clearTimeout(timer);
      reject(e);
    });
    child.on('exit', (code) => {
      clearTimeout(timer);
      if (code === 0) resolve();
      else reject(new Error(`wt.exe exited with code ${code}`));
    });
  });
}

async function readPid(file: string | undefined): Promise<number | undefined> {
  if (!file) return undefined;
  const text = await fs.readFile(file, 'utf8').catch(() => undefined);
  const pid = Number(text?.trim());
  return Number.isSafeInteger(pid) && pid > 0 ? pid : undefined;
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

  async open(ctx, spec: LaunchSpec, title) {
    const wt = findWindowsTerminal(ctx.pathVar, ctx.env.LOCALAPPDATA);
    if (!wt) throw new Error('Windows Terminal (wt.exe) was not found');
    const dir = path.join(ctx.home, 'launch');
    const specFile = path.join(dir, `${spec.id}.json`);
    const pidFile = path.join(dir, `${spec.id}.pid`);
    const args = wtArgs({ title, shell: findPowerShell(ctx.pathVar), launcher: path.join(ctx.scriptsDir, LAUNCHER_PS1), spec: specFile });
    await writeNewPrivateFile(specFile, powerShellSpec({ ...spec, pidFile }));
    try {
      await startDetached(wt, args, wtEnvironment(ctx.env));
    } catch (e) {
      await fs.rm(specFile, { force: true });
      throw e;
    }
    return { id: spec.id, terminal: WINDOWS_TERMINAL, agent: spec.agent, path: spec.cwd, createdAt: Date.now(), pidFile };
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

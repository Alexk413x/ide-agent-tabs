import { spawn } from 'node:child_process';
import { promises as fs, existsSync } from 'node:fs';
import path from 'node:path';
import { findOnPath } from '../installed.js';
import { run } from '../process.js';
import type { TerminalTab } from './types.js';

export const STARTUP_GRACE_MS = 60_000;
export const GUI_SETTLE_MS = 2_000;
const SHELL_NAMES = new Set(['bash', 'zsh', 'fish']);

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
  'CODEX_SANDBOX',
  'CODEX_SANDBOX_NETWORK_DISABLED',
  'GEMINI_CLI',
  'OPENCODE_SESSION_ID',
]);

// A terminal the server starts, or a tmux server it starts, keeps this environment for every later tab, so
// variables that identify the calling agent session are dropped.
export function terminalEnvironment(env: NodeJS.ProcessEnv): NodeJS.ProcessEnv {
  return Object.fromEntries(Object.entries(env).filter(([name]) => !SESSION_ENV.has(name.toUpperCase())));
}

export function findExecutable(pathVar: string, name: string, fallbacks: string[]): string | undefined {
  return findOnPath(pathVar, name) ?? fallbacks.find((p) => existsSync(p));
}

export function startDetached(command: string, args: string[], env: NodeJS.ProcessEnv, settleMs: number): Promise<void> {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { stdio: 'ignore', windowsHide: true, detached: true, env });
    const timer = setTimeout(() => {
      child.unref();
      resolve();
    }, settleMs);
    child.on('error', (e) => {
      clearTimeout(timer);
      reject(e);
    });
    child.on('exit', (code) => {
      clearTimeout(timer);
      if (code === 0) resolve();
      else reject(new Error(`${path.basename(command)} exited with code ${code}`));
    });
  });
}

export async function readPid(file: string | undefined): Promise<number | undefined> {
  if (!file) return undefined;
  const text = await fs.readFile(file, 'utf8').catch(() => undefined);
  const pid = Number(text?.trim());
  return Number.isSafeInteger(pid) && pid > 0 ? pid : undefined;
}

export function isShellName(comm: string): boolean {
  return SHELL_NAMES.has(path.posix.basename(comm.trim()).replace(/^-/, ''));
}

async function processName(pid: number): Promise<string | undefined> {
  if (process.platform === 'linux') return fs.readFile(`/proc/${pid}/comm`, 'utf8').catch(() => undefined);
  const result = await run('ps', ['-p', String(pid), '-o', 'comm='], { timeoutMs: 10_000 }).catch(() => undefined);
  return result?.code === 0 ? result.stdout : undefined;
}

export async function shellRunning(pid: number): Promise<boolean> {
  try {
    process.kill(pid, 0);
  } catch {
    return false;
  }
  const name = await processName(pid);
  return name !== undefined && isShellName(name);
}

export async function pidTabsAlive(tabs: TerminalTab[], now = Date.now()): Promise<Set<string>> {
  const alive = new Set<string>();
  for (const tab of tabs) {
    const pid = await readPid(tab.pidFile);
    if (pid !== undefined ? await shellRunning(pid) : now - tab.createdAt < STARTUP_GRACE_MS) alive.add(tab.id);
  }
  return alive;
}

// The shell leads its own process group in a new terminal window. When it gets SIGHUP it passes the signal
// to its jobs, and the window closes once it exits.
export async function hangUp(tab: TerminalTab): Promise<void> {
  const pid = await readPid(tab.pidFile);
  if (pid === undefined) throw new Error(`tab ${tab.id} has no running shell yet, or its shell has ended`);
  if (await shellRunning(pid)) {
    try {
      process.kill(-pid, 'SIGHUP');
    } catch {
      process.kill(pid, 'SIGHUP');
    }
  }
  if (tab.pidFile) await fs.rm(tab.pidFile, { force: true });
}

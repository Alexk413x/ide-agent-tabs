import { promises as fs, readdirSync, statSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { writeNewPrivateFile } from '../files.js';
import { run } from '../process.js';
import { checkPosixEnvNames, posixSpec, powerShellSpec, type LaunchSpec } from '../spec.js';
import { findExecutable, GUI_SETTLE_MS, startDetached, STARTUP_GRACE_MS, terminalEnvironment } from './processes.js';
import { argvModeCommand, checkArgvPaths, checkInputLine, ENTER_DELAY_MS, launcherName, loginShell } from './shell.js';
import type { TerminalContext, TerminalDriver, TerminalTab } from './types.js';
import { findPowerShell, LAUNCHER_PS1, powerShellArgv } from './windowsTerminal.js';

export const WEZTERM = 'wezterm';
const START_WAIT_MS = 10_000;
const GUI_SOCKET = /^gui-sock-(\d+)$/;

export function weztermLocations(platform: NodeJS.Platform, home: string, programFiles: string | undefined): string[] {
  if (platform === 'win32') return programFiles ? [path.win32.join(programFiles, 'WezTerm', 'wezterm.exe')] : [];
  if (platform === 'darwin') {
    return [
      '/Applications/WezTerm.app/Contents/MacOS/wezterm',
      path.posix.join(home, 'Applications', 'WezTerm.app', 'Contents', 'MacOS', 'wezterm'),
    ];
  }
  return [];
}

function findWezterm(ctx: TerminalContext): string | undefined {
  const name = process.platform === 'win32' ? 'wezterm.exe' : 'wezterm';
  return findExecutable(ctx.pathVar, name, weztermLocations(process.platform, os.homedir(), ctx.env.ProgramFiles ?? ctx.env.PROGRAMFILES));
}

export function weztermRuntimeDir(platform: NodeJS.Platform, env: NodeJS.ProcessEnv, home: string): string {
  if (platform === 'linux' && env.XDG_RUNTIME_DIR) return path.posix.join(env.XDG_RUNTIME_DIR, 'wezterm');
  return path.join(home, '.local', 'share', 'wezterm');
}

function processRunning(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch (e) {
    return (e as NodeJS.ErrnoException).code === 'EPERM';
  }
}

// Each WezTerm GUI listens on gui-sock-<pid>, and the files outlive a GUI that was killed, so only sockets
// whose GUI still runs count. Newest first where the file can be stat'ed: on Windows a socket file is a
// reparse point that stat refuses with EACCES.
export function findGuiSockets(dir: string, running: (pid: number) => boolean = processRunning): string[] {
  let names: string[];
  try {
    names = readdirSync(dir);
  } catch {
    return [];
  }
  return names
    .flatMap((name) => {
      const pid = Number(GUI_SOCKET.exec(name)?.[1]);
      if (!Number.isSafeInteger(pid) || !running(pid)) return [];
      const file = path.join(dir, name);
      try {
        return [{ file, mtime: statSync(file).mtimeMs }];
      } catch {
        return [{ file, mtime: 0 }];
      }
    })
    .sort((a, b) => b.mtime - a.mtime)
    .map((s) => s.file);
}

function guiSockets(env: NodeJS.ProcessEnv): string[] {
  const found = findGuiSockets(weztermRuntimeDir(process.platform, env, os.homedir()));
  const own = env.WEZTERM_UNIX_SOCKET;
  return own && !found.includes(own) ? [own, ...found] : found;
}

// `wezterm cli` names the socket to use explicitly: on Windows it drops the folder from the GUI's socket
// and fails to connect (wezterm#4456), and without --no-auto-start it starts a hidden mux server.
export function weztermCliArgs(args: string[]): string[] {
  return ['cli', '--no-auto-start', ...args];
}

function cli(exe: string, socket: string, args: string[], env: NodeJS.ProcessEnv, timeoutMs = 15_000) {
  return run(exe, weztermCliArgs(args), { env: { ...env, WEZTERM_UNIX_SOCKET: socket }, timeoutMs });
}

export function weztermSpawnArgs(cwd: string, argv: string[]): string[] {
  checkArgvPaths('WezTerm', [cwd]);
  return ['spawn', '--cwd', cwd, '--', ...argv];
}

export function weztermStartArgs(cwd: string, argv: string[]): string[] {
  checkArgvPaths('WezTerm', [cwd]);
  return ['start', '--cwd', cwd, '--', ...argv];
}

// --no-paste sends the bytes as typed input. As a bracketed paste, the CR would not submit the line.
export function weztermInputArgs(paneId: string, text: string): [string[], string[]] {
  checkInputLine(text);
  return [
    ['send-text', '--pane-id', paneId, '--no-paste', '--', text],
    ['send-text', '--pane-id', paneId, '--no-paste', '--', '\r'],
  ];
}

export function parsePaneId(stdout: string): string {
  const id = stdout.trim();
  if (!/^\d+$/.test(id)) throw new Error(`unexpected answer from wezterm cli spawn: ${id}`);
  return id;
}

export function parseWeztermPanes(stdout: string): string[] {
  const panes: unknown = JSON.parse(stdout);
  if (!Array.isArray(panes)) throw new Error('unexpected answer from wezterm cli list');
  return panes
    .map((p) => (typeof p === 'object' && p !== null ? (p as { pane_id?: unknown }).pane_id : undefined))
    .filter((id): id is number => typeof id === 'number')
    .map(String);
}

export function isNoGuiError(stderr: string): boolean {
  return /connect|socket|no running|not running/i.test(stderr);
}

async function listPanes(exe: string, socket: string, env: NodeJS.ProcessEnv): Promise<Set<string> | undefined> {
  const result = await cli(exe, socket, ['list', '--format', 'json'], env);
  if (result.code === 0) return new Set(parseWeztermPanes(result.stdout));
  if (isNoGuiError(result.stderr)) return undefined;
  throw new Error(`wezterm cli list failed: ${result.stderr.trim()}`);
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

// A GUI the server starts holds only the agent's pane, so its pane id is known once its socket answers.
async function startGui(exe: string, args: string[], env: NodeJS.ProcessEnv): Promise<{ socket: string; paneId?: string } | undefined> {
  const before = new Set(guiSockets(env));
  await startDetached(exe, args, env, GUI_SETTLE_MS);
  const deadline = Date.now() + START_WAIT_MS;
  while (Date.now() < deadline) {
    const socket = guiSockets(env).find((s) => !before.has(s));
    const panes = socket ? await listPanes(exe, socket, env).catch(() => undefined) : undefined;
    if (socket && panes && panes.size > 0) return panes.size === 1 ? { socket, paneId: [...panes][0]! } : { socket };
    await sleep(250);
  }
  return undefined;
}

function paneArgv(ctx: TerminalContext, spec: LaunchSpec, specFile: string): string[] {
  if (process.platform === 'win32') {
    const argv = powerShellArgv(findPowerShell(ctx.pathVar), path.join(ctx.scriptsDir, LAUNCHER_PS1), specFile);
    checkArgvPaths('WezTerm', argv);
    return argv;
  }
  checkPosixEnvNames(spec.env);
  const shell = loginShell(ctx.env.SHELL, process.platform);
  return argvModeCommand(shell, path.join(ctx.scriptsDir, launcherName(shell)), specFile);
}

async function spawnInGui(exe: string, args: string[], env: NodeJS.ProcessEnv): Promise<{ socket: string; paneId: string } | undefined> {
  for (const socket of guiSockets(env)) {
    const result = await cli(exe, socket, args, env, 30_000);
    if (result.code === 0) return { socket, paneId: parsePaneId(result.stdout) };
    if (!isNoGuiError(result.stderr)) throw new Error(`wezterm cli spawn failed: ${result.stderr.trim()}`);
  }
  return undefined;
}

function weztermPath(ctx: TerminalContext): string {
  const exe = findWezterm(ctx);
  if (!exe) throw new Error('wezterm was not found');
  return exe;
}

export const wezterm: TerminalDriver = {
  name: WEZTERM,
  label: 'WezTerm',
  capabilities: { open: 'tab', list: 'yes', close: 'yes' },

  async available(ctx) {
    return findWezterm(ctx) !== undefined;
  },

  async open(ctx, spec: LaunchSpec) {
    const exe = weztermPath(ctx);
    const windows = process.platform === 'win32';
    const specFile = path.join(ctx.home, 'launch', `${spec.id}${windows ? '.json' : '.spec'}`);
    const argv = paneArgv(ctx, spec, specFile);
    const env = terminalEnvironment(ctx.env);
    const spawnArgs = weztermSpawnArgs(spec.cwd, argv);
    await writeNewPrivateFile(specFile, windows ? powerShellSpec(spec) : posixSpec(spec));
    let pane: { socket: string; paneId?: string } | undefined;
    try {
      pane = (await spawnInGui(exe, spawnArgs, env)) ?? (await startGui(exe, weztermStartArgs(spec.cwd, argv), env));
    } catch (e) {
      await fs.rm(specFile, { force: true });
      throw e;
    }
    return {
      id: spec.id,
      terminal: WEZTERM,
      agent: spec.agent,
      path: spec.cwd,
      createdAt: Date.now(),
      ...(pane?.paneId !== undefined ? { terminalId: pane.paneId, socket: pane.socket } : {}),
    };
  },

  async alive(ctx, tabs) {
    const now = Date.now();
    const alive = new Set(tabs.filter((t) => t.terminalId === undefined && now - t.createdAt < STARTUP_GRACE_MS).map((t) => t.id));
    const tracked = tabs.filter((t) => t.terminalId !== undefined && t.socket);
    if (tracked.length === 0) return alive;
    const exe = weztermPath(ctx);
    const env = terminalEnvironment(ctx.env);
    for (const socket of new Set(tracked.map((t) => t.socket!))) {
      const panes = await listPanes(exe, socket, env);
      for (const t of tracked) if (t.socket === socket && panes?.has(t.terminalId!)) alive.add(t.id);
    }
    return alive;
  },

  async close(ctx, tab: TerminalTab) {
    if (tab.terminalId === undefined || !/^\d+$/.test(tab.terminalId) || !tab.socket) {
      throw new Error(`tab ${tab.id} has no WezTerm pane id; close it in WezTerm`);
    }
    const exe = weztermPath(ctx);
    const env = terminalEnvironment(ctx.env);
    const panes = await listPanes(exe, tab.socket, env);
    if (!panes?.has(tab.terminalId)) throw new Error(`WezTerm has no pane ${tab.terminalId}; the tab is already closed`);
    const result = await cli(exe, tab.socket, ['kill-pane', '--pane-id', tab.terminalId], env);
    if (result.code !== 0) throw new Error(`wezterm cli kill-pane failed: ${result.stderr.trim()}`);
  },

  async input(ctx, tab: TerminalTab, text) {
    if (tab.terminalId === undefined || !/^\d+$/.test(tab.terminalId) || !tab.socket) throw new Error(`tab ${tab.id} has no WezTerm pane id`);
    const [type, enter] = weztermInputArgs(tab.terminalId, text);
    const exe = weztermPath(ctx);
    const env = terminalEnvironment(ctx.env);
    if (!(await listPanes(exe, tab.socket, env))?.has(tab.terminalId)) throw new Error(`WezTerm has no pane ${tab.terminalId}`);
    for (const args of [type, enter]) {
      if (args === enter) await sleep(ENTER_DELAY_MS);
      const result = await cli(exe, tab.socket, args, env);
      if (result.code !== 0) throw new Error(`wezterm cli send-text failed: ${result.stderr.trim()}`);
    }
  },
};

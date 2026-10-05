import { promises as fs } from 'node:fs';
import path from 'node:path';
import { writeNewPrivateFile } from '../files.js';
import { run } from '../process.js';
import { checkPosixEnvNames, posixSpec, type LaunchSpec } from '../spec.js';
import { findExecutable, terminalEnvironment } from './processes.js';
import { checkArgvPaths, checkInputLine, ENTER_DELAY_MS, launcherName, loginShell, sleep, surfaceArgv, tabTitle } from './shell.js';
import type { OpenOptions, TerminalContext, TerminalDriver, TerminalTab } from './types.js';
import { DEDICATED_NAME } from './windowMemory.js';

export const TMUX = 'tmux';
export const TMUX_SESSION = 'agents';
export const TMUX_DEDICATED_SESSION = DEDICATED_NAME;
const TMUX_LOCATIONS = ['/opt/homebrew/bin/tmux', '/usr/local/bin/tmux', '/usr/bin/tmux', '/home/linuxbrew/.linuxbrew/bin/tmux'];
const ENV = '/usr/bin/env';
const SESSION_FORMAT = '#{session_attached} #{session_last_attached} #{session_id} #{session_name}';
const WINDOW_FORMAT = '#{window_id} #{session_id} #{pid} #{socket_path}';
const LIST_FORMAT = '#{pid} #{window_id}';

export interface TmuxSession {
  attached: number;
  lastAttached: number;
  id: string;
  name: string;
}

export type TmuxTarget = { session: string; detached: boolean } | { newSession: string } | { after: string; socket: string };

export interface TmuxWindow {
  windowId: string;
  sessionId: string;
  serverPid: number;
  socket: string;
}

function fields(line: string, count: number): string[] | undefined {
  const parts = line.split(' ');
  if (parts.length < count) return undefined;
  return [...parts.slice(0, count - 1), parts.slice(count - 1).join(' ')];
}

export function parseTmuxSessions(stdout: string): TmuxSession[] {
  return stdout.split(/\r?\n/).flatMap((line) => {
    const f = fields(line, 4);
    if (!f || !/^\$\d+$/.test(f[2]!)) return [];
    return [{ attached: Number(f[0]) || 0, lastAttached: Number(f[1]) || 0, id: f[2]!, name: f[3]! }];
  });
}

export function planTmuxTarget(sessions: TmuxSession[]): TmuxTarget {
  const attached = sessions.filter((s) => s.attached > 0).sort((a, b) => b.lastAttached - a.lastAttached)[0];
  if (attached) return { session: attached.id, detached: false };
  const agents = sessions.find((s) => s.name === TMUX_SESSION);
  return agents ? { session: agents.id, detached: true } : { newSession: TMUX_SESSION };
}

export function planDedicatedTmuxTarget(sessions: TmuxSession[]): TmuxTarget {
  const own = sessions.find((s) => s.name === TMUX_DEDICATED_SESSION);
  return own ? { session: own.id, detached: own.attached === 0 } : { newSession: TMUX_DEDICATED_SESSION };
}

// tmux expands formats in -n and reads an argument that ends in ';' as a command separator, so the title
// drops '#' and ';'. The folder isn't passed with -c, which tmux also expands; the launcher changes to it.
export function tmuxTitle(label: string): string {
  return tabTitle(label.replace(/[#;]/g, ' '));
}

export function tmuxOpenArgs(target: TmuxTarget, o: { title: string; launcher: string; spec: string; argv: string[]; focus?: boolean }): string[] {
  checkArgvPaths('tmux', [o.launcher, o.spec], ';');
  const behind = o.focus === false ? ['-d'] : [];
  const head =
    'after' in target
      ? ['-S', target.socket, 'new-window', ...behind, '-a', '-t', target.after, '-P', '-F', WINDOW_FORMAT]
      : 'session' in target
        ? ['new-window', ...behind, '-P', '-F', WINDOW_FORMAT, '-t', `${target.session}:`]
        : ['new-session', '-d', '-s', target.newSession, '-P', '-F', WINDOW_FORMAT];
  // /usr/bin/env sets the paths instead of -e: new-session -e needs tmux 3.2, and it would also leave them
  // in the session environment for later windows.
  return [
    ...head,
    '-n', tmuxTitle(o.title),
    '--', ENV, `IDE_AGENT_TABS_LAUNCHER=${o.launcher}`, `IDE_AGENT_TABS_SPEC=${o.spec}`, ...o.argv,
  ];
}

// tmux ends a command at an argument that ends in ';', so a line may not end in one.
export function tmuxInputArgs(socket: string, windowId: string, text: string): [string[], string[]] {
  checkInputLine(text);
  if (text.endsWith(';')) throw new Error("tmux input can't end in ';'");
  return [
    ['-S', socket, 'send-keys', '-t', windowId, '-l', '--', text],
    ['-S', socket, 'send-keys', '-t', windowId, 'Enter'],
  ];
}

export function parseTmuxWindow(stdout: string): TmuxWindow {
  const f = fields(stdout.trim(), 4);
  const pid = Number(f?.[2]);
  if (!f || !/^@\d+$/.test(f[0]!) || !/^\$\d+$/.test(f[1]!) || !Number.isSafeInteger(pid) || f[3] === '') {
    throw new Error(`unexpected answer from tmux: ${stdout.trim()}`);
  }
  return { windowId: f[0]!, sessionId: f[1]!, serverPid: pid, socket: f[3]! };
}

export function parseTmuxWindowList(stdout: string): { serverPid: number | undefined; windows: Set<string> } {
  let serverPid: number | undefined;
  const windows = new Set<string>();
  for (const line of stdout.split(/\r?\n/)) {
    const [pid, id] = line.split(' ');
    if (!id || !/^@\d+$/.test(id)) continue;
    serverPid = Number(pid);
    windows.add(id);
  }
  return { serverPid, windows };
}

export function isNoServerError(stderr: string): boolean {
  return /no server running|error connecting|no sessions/i.test(stderr);
}

function findTmux(ctx: TerminalContext): string | undefined {
  return findExecutable(ctx.pathVar, 'tmux', TMUX_LOCATIONS);
}

function tmuxPath(ctx: TerminalContext): string {
  const tmux = findTmux(ctx);
  if (!tmux) throw new Error('tmux was not found');
  return tmux;
}

async function liveWindows(tmux: string, socket: string, env: NodeJS.ProcessEnv) {
  const result = await run(tmux, ['-S', socket, 'list-windows', '-a', '-F', LIST_FORMAT], { env, timeoutMs: 15_000 });
  if (result.code === 0) return parseTmuxWindowList(result.stdout);
  if (isNoServerError(result.stderr) || /no such file|connection refused/i.test(result.stderr)) {
    return { serverPid: undefined, windows: new Set<string>() };
  }
  throw new Error(`tmux list-windows failed: ${result.stderr.trim()}`);
}

function isOpen(tab: TerminalTab, live: { serverPid: number | undefined; windows: Set<string> }): boolean {
  return live.serverPid !== undefined && tab.serverPid === live.serverPid && live.windows.has(tab.terminalId ?? '');
}

async function chooseTarget(exe: string, env: NodeJS.ProcessEnv, options: OpenOptions | undefined): Promise<TmuxTarget> {
  const near = options?.near;
  if (near?.socket && near.terminalId && /^@\d+$/.test(near.terminalId)) {
    const live = await liveWindows(exe, near.socket, env).catch(() => undefined);
    if (live && isOpen(near, live)) return { after: near.terminalId, socket: near.socket };
  }
  const listed = await run(exe, ['list-sessions', '-F', SESSION_FORMAT], { env, timeoutMs: 15_000 });
  if (listed.code !== 0 && !isNoServerError(listed.stderr)) throw new Error(`tmux list-sessions failed: ${listed.stderr.trim()}`);
  const sessions = listed.code === 0 ? parseTmuxSessions(listed.stdout) : [];
  return options?.window === 'dedicated' ? planDedicatedTmuxTarget(sessions) : planTmuxTarget(sessions);
}

export const tmux: TerminalDriver = {
  name: TMUX,
  label: 'tmux',
  capabilities: { open: 'tab', list: 'yes', close: 'yes' },

  async available(ctx) {
    return (process.platform === 'darwin' || process.platform === 'linux') && findTmux(ctx) !== undefined;
  },

  async open(ctx, spec: LaunchSpec, title, options) {
    const exe = tmuxPath(ctx);
    checkPosixEnvNames(spec.env);
    const shell = loginShell(ctx.env.SHELL, process.platform);
    const specFile = path.join(ctx.home, 'launch', `${spec.id}.spec`);
    const launcher = path.join(ctx.scriptsDir, launcherName(shell));
    const env = terminalEnvironment(ctx.env);
    const target = await chooseTarget(exe, env, options);
    const args = tmuxOpenArgs(target, { title, launcher, spec: specFile, argv: surfaceArgv(shell), ...(options?.focus !== undefined ? { focus: options.focus } : {}) });
    await writeNewPrivateFile(specFile, posixSpec(spec));
    let window: TmuxWindow;
    try {
      const result = await run(exe, args, { env, timeoutMs: 30_000 });
      if (result.code !== 0) throw new Error(`tmux ${args[0]} failed: ${result.stderr.trim()}`);
      window = parseTmuxWindow(result.stdout);
    } catch (e) {
      await fs.rm(specFile, { force: true });
      throw e;
    }
    const detached = 'newSession' in target || ('detached' in target && target.detached);
    const session = options?.window === 'dedicated' && !options.near ? TMUX_DEDICATED_SESSION : TMUX_SESSION;
    return {
      id: spec.id,
      terminal: TMUX,
      agent: spec.agent,
      path: spec.cwd,
      createdAt: Date.now(),
      terminalId: window.windowId,
      socket: window.socket,
      serverPid: window.serverPid,
      ...(detached
        ? { note: `No tmux client is attached, so the tab opened in the detached session "${session}". Run: tmux attach -t ${session}` }
        : {}),
    };
  },

  async alive(ctx, tabs) {
    const alive = new Set<string>();
    const tracked = tabs.filter((t) => t.socket && t.terminalId);
    if (tracked.length === 0) return alive;
    const exe = tmuxPath(ctx);
    const env = terminalEnvironment(ctx.env);
    for (const socket of new Set(tracked.map((t) => t.socket!))) {
      const live = await liveWindows(exe, socket, env);
      for (const t of tracked) if (t.socket === socket && isOpen(t, live)) alive.add(t.id);
    }
    return alive;
  },

  async close(ctx, tab: TerminalTab) {
    if (!tab.socket || !tab.terminalId || !/^@\d+$/.test(tab.terminalId)) throw new Error(`tab ${tab.id} has no tmux window id`);
    const exe = tmuxPath(ctx);
    const env = terminalEnvironment(ctx.env);
    if (!isOpen(tab, await liveWindows(exe, tab.socket, env))) {
      throw new Error(`tmux has no window ${tab.terminalId}; the tab is already closed`);
    }
    const result = await run(exe, ['-S', tab.socket, 'kill-window', '-t', tab.terminalId], { env, timeoutMs: 15_000 });
    if (result.code !== 0) throw new Error(`tmux kill-window failed: ${result.stderr.trim()}`);
  },

  async input(ctx, tab: TerminalTab, text) {
    if (!tab.socket || !tab.terminalId || !/^@\d+$/.test(tab.terminalId)) throw new Error(`tab ${tab.id} has no tmux window id`);
    const [type, enter] = tmuxInputArgs(tab.socket, tab.terminalId, text);
    const exe = tmuxPath(ctx);
    const env = terminalEnvironment(ctx.env);
    if (!isOpen(tab, await liveWindows(exe, tab.socket, env))) throw new Error(`tmux has no window ${tab.terminalId}`);
    for (const args of [type, enter]) {
      if (args === enter) await sleep(ENTER_DELAY_MS);
      const result = await run(exe, args, { env, timeoutMs: 15_000 });
      if (result.code !== 0) throw new Error(`tmux send-keys failed: ${result.stderr.trim()}`);
    }
  },
};

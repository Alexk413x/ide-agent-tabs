import { promises as fs, readdirSync, realpathSync, statSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { writeNewPrivateFile } from '../files.js';
import { findOnPath } from '../installed.js';
import { run } from '../process.js';
import { checkPosixEnvNames, posixSpec, type LaunchSpec } from '../spec.js';
import { findExecutable, GUI_SETTLE_MS, hangUp, pidTabsAlive, startDetached, terminalEnvironment } from './processes.js';
import { checkArgvPaths, launcherName, loginShell, surfaceArgv, tabTitle } from './shell.js';
import type { TerminalCapabilities, TerminalContext, TerminalDriver, TerminalTab } from './types.js';

export const KITTY = 'kitty';
export const KITTY_SOCKET_NAME = 'kitty-agent-tabs';
const REMOTE: TerminalCapabilities = { open: 'tab', list: 'yes', close: 'yes' };
const SPAWNED: TerminalCapabilities = { open: 'window', list: 'tracked', close: 'best-effort' };

export function kittyLocations(home: string): string[] {
  return [
    '/Applications/kitty.app/Contents/MacOS/kitty',
    path.posix.join(home, 'Applications', 'kitty.app', 'Contents', 'MacOS', 'kitty'),
    path.posix.join(home, '.local', 'kitty.app', 'bin', 'kitty'),
  ];
}

function findKitty(ctx: TerminalContext): string | undefined {
  return findExecutable(ctx.pathVar, 'kitty', kittyLocations(os.homedir()));
}

function findKitten(kitty: string, pathVar: string): string {
  try {
    const sibling = path.join(path.dirname(realpathSync(kitty)), 'kitten');
    statSync(sibling);
    return sibling;
  } catch {
    return findOnPath(pathVar, 'kitten') ?? path.join(path.dirname(kitty), 'kitten');
  }
}

export function kittySocketDir(platform: NodeJS.Platform, env: NodeJS.ProcessEnv): string | undefined {
  if (platform === 'linux') return env.XDG_RUNTIME_DIR || undefined;
  if (platform === 'darwin') return env.TMPDIR || os.tmpdir();
  return undefined;
}

// kitty appends -<pid> to the listen_on path, so each running kitty has its own socket.
export function findKittySockets(dir: string): string[] {
  let names: string[];
  try {
    names = readdirSync(dir);
  } catch {
    return [];
  }
  const pattern = new RegExp(`^${KITTY_SOCKET_NAME}-\\d+$`);
  return names
    .filter((n) => pattern.test(n))
    .flatMap((n) => {
      const file = path.join(dir, n);
      try {
        return [{ file, mtime: statSync(file).mtimeMs }];
      } catch {
        return [];
      }
    })
    .sort((a, b) => b.mtime - a.mtime)
    .map((s) => `unix:${s.file}`);
}

export function kittyAddresses(platform: NodeJS.Platform, env: NodeJS.ProcessEnv): string[] {
  if (env.KITTY_LISTEN_ON?.startsWith('unix:')) return [env.KITTY_LISTEN_ON];
  const dir = kittySocketDir(platform, env);
  return dir ? findKittySockets(dir) : [];
}

export function kittyLaunchArgs(o: { address: string; cwd: string; title: string; launcher: string; spec: string; argv: string[] }): string[] {
  checkArgvPaths('kitty', [o.address, o.cwd, o.launcher, o.spec]);
  return [
    '@', '--to', o.address, 'launch', '--type=tab', '--cwd', o.cwd,
    '--env', `IDE_AGENT_TABS_LAUNCHER=${o.launcher}`,
    '--env', `IDE_AGENT_TABS_SPEC=${o.spec}`,
    '--tab-title', tabTitle(o.title),
    '--', ...o.argv,
  ];
}

export function kittySpawnArgs(cwd: string, argv: string[]): string[] {
  checkArgvPaths('kitty', [cwd]);
  return ['--directory', cwd, ...argv];
}

export function parseKittyWindowId(stdout: string): string {
  const id = stdout.trim();
  if (!/^\d+$/.test(id)) throw new Error(`unexpected answer from kitten @ launch: ${id}`);
  return id;
}

export function parseKittyWindows(stdout: string): Set<string> {
  const ids = new Set<string>();
  const osWindows: unknown = JSON.parse(stdout);
  if (!Array.isArray(osWindows)) throw new Error('unexpected answer from kitten @ ls');
  for (const w of osWindows as { tabs?: { windows?: { id?: unknown }[] }[] }[]) {
    for (const tab of w?.tabs ?? []) {
      for (const win of tab?.windows ?? []) if (typeof win?.id === 'number') ids.add(String(win.id));
    }
  }
  return ids;
}

async function kittyLs(kitten: string, address: string): Promise<Set<string> | undefined> {
  const result = await run(kitten, ['@', '--to', address, 'ls'], { timeoutMs: 10_000 }).catch(() => undefined);
  return result?.code === 0 ? parseKittyWindows(result.stdout) : undefined;
}

async function reachableSocket(kitten: string, env: NodeJS.ProcessEnv): Promise<string | undefined> {
  for (const address of kittyAddresses(process.platform, env)) {
    if (await kittyLs(kitten, address).catch(() => undefined)) return address;
  }
  return undefined;
}

function tools(ctx: TerminalContext): { kitty: string; kitten: string } {
  const kitty = findKitty(ctx);
  if (!kitty) throw new Error('kitty was not found');
  return { kitty, kitten: findKitten(kitty, ctx.pathVar) };
}

export const kitty: TerminalDriver = {
  name: KITTY,
  label: 'kitty',
  capabilities: SPAWNED,

  async currentCapabilities(ctx) {
    const found = findKitty(ctx);
    if (!found) return SPAWNED;
    return (await reachableSocket(findKitten(found, ctx.pathVar), ctx.env)) ? REMOTE : SPAWNED;
  },

  async available(ctx) {
    return (process.platform === 'darwin' || process.platform === 'linux') && findKitty(ctx) !== undefined;
  },

  async open(ctx, spec: LaunchSpec, title) {
    const { kitty: exe, kitten } = tools(ctx);
    checkPosixEnvNames(spec.env);
    const shell = loginShell(ctx.env.SHELL, process.platform);
    const dir = path.join(ctx.home, 'launch');
    const specFile = path.join(dir, `${spec.id}.spec`);
    const launcher = path.join(ctx.scriptsDir, launcherName(shell));
    const argv = surfaceArgv(shell);
    const env = terminalEnvironment(ctx.env);
    const address = await reachableSocket(kitten, ctx.env);
    const base = { id: spec.id, terminal: KITTY, agent: spec.agent, path: spec.cwd };

    if (address) {
      const args = kittyLaunchArgs({ address, cwd: spec.cwd, title, launcher, spec: specFile, argv });
      await writeNewPrivateFile(specFile, posixSpec(spec));
      try {
        const result = await run(kitten, args, { env, timeoutMs: 30_000 });
        if (result.code !== 0) throw new Error(`kitten @ launch failed: ${result.stderr.trim()}`);
        return { ...base, createdAt: Date.now(), terminalId: parseKittyWindowId(result.stdout), socket: address };
      } catch (e) {
        await fs.rm(specFile, { force: true });
        throw e;
      }
    }

    const pidFile = path.join(dir, `${spec.id}.pid`);
    const args = kittySpawnArgs(spec.cwd, argv);
    await writeNewPrivateFile(specFile, posixSpec({ ...spec, pidFile }));
    try {
      await startDetached(exe, args, { ...env, IDE_AGENT_TABS_LAUNCHER: launcher, IDE_AGENT_TABS_SPEC: specFile }, GUI_SETTLE_MS);
    } catch (e) {
      await fs.rm(specFile, { force: true });
      throw e;
    }
    return { ...base, createdAt: Date.now(), pidFile };
  },

  async alive(ctx, tabs) {
    const alive = await pidTabsAlive(tabs.filter((t) => t.pidFile));
    const remote = tabs.filter((t) => t.socket && t.terminalId);
    if (remote.length === 0) return alive;
    const { kitten } = tools(ctx);
    for (const address of new Set(remote.map((t) => t.socket!))) {
      const windows = await kittyLs(kitten, address);
      for (const t of remote) if (t.socket === address && windows?.has(t.terminalId!)) alive.add(t.id);
    }
    return alive;
  },

  async close(ctx, tab: TerminalTab) {
    if (tab.pidFile) return hangUp(tab);
    if (!tab.socket || !tab.terminalId || !/^\d+$/.test(tab.terminalId)) throw new Error(`tab ${tab.id} has no kitty window id`);
    const { kitten } = tools(ctx);
    const windows = await kittyLs(kitten, tab.socket);
    if (!windows?.has(tab.terminalId)) throw new Error(`kitty has no window ${tab.terminalId}; the tab is already closed`);
    const result = await run(kitten, ['@', '--to', tab.socket, 'close-window', '--match', `id:${tab.terminalId}`], { timeoutMs: 15_000 });
    if (result.code !== 0) throw new Error(`kitten @ close-window failed: ${result.stderr.trim()}`);
  },
};

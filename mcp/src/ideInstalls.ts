import { spawn } from 'node:child_process';
import { existsSync, readdirSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { entryForProductInfo, IDE_CATALOG, type IdeKind } from './ideCatalog.js';
import { cliInvocation, findEditorClis } from './editorClis.js';
import { terminalEnvironment } from './terminals/processes.js';
import { compareVersions } from './version.js';

export interface IdeInstall {
  key: string;
  product: string;
  kind: IdeKind;
  version?: string;
  launcher: string;
}

export interface DiscoveryFs {
  exists(file: string): boolean;
  readdir(dir: string): string[];
  readText(file: string): string | undefined;
}

export interface DiscoveryContext {
  platform: NodeJS.Platform;
  env: NodeJS.ProcessEnv;
  userHome: string;
  arch: string;
  fs: DiscoveryFs;
}

export const systemDiscoveryFs: DiscoveryFs = {
  exists: existsSync,
  readdir: (dir) => {
    try {
      return readdirSync(dir);
    } catch {
      return [];
    }
  },
  readText: (file) => {
    try {
      return readFileSync(file, 'utf8');
    } catch {
      return undefined;
    }
  },
};

interface ProductInfo {
  name?: string;
  version?: string;
  buildNumber?: string;
  productCode?: string;
  launch: { os: string; arch?: string; launcherPath: string }[];
}

export function parseProductInfo(text: string | undefined): ProductInfo | undefined {
  let json: unknown;
  try {
    json = JSON.parse(text ?? '');
  } catch {
    return undefined;
  }
  if (typeof json !== 'object' || json === null || Array.isArray(json)) return undefined;
  const o = json as Record<string, unknown>;
  const str = (v: unknown) => (typeof v === 'string' && v.trim() !== '' ? v : undefined);
  const launch = (Array.isArray(o.launch) ? o.launch : []).flatMap((l: unknown) => {
    if (typeof l !== 'object' || l === null) return [];
    const r = l as Record<string, unknown>;
    const os = str(r.os);
    const launcherPath = str(r.launcherPath);
    const arch = str(r.arch);
    return os && launcherPath ? [{ os, launcherPath, ...(arch ? { arch } : {}) }] : [];
  });
  const fields = { name: str(o.name), version: str(o.version), buildNumber: str(o.buildNumber), productCode: str(o.productCode) };
  return { ...Object.fromEntries(Object.entries(fields).filter(([, v]) => v !== undefined)), launch };
}

const OS_NAMES: Partial<Record<NodeJS.Platform, string>> = { win32: 'Windows', darwin: 'macOS', linux: 'Linux' };
const ARCH_NAMES: Record<string, string> = { x64: 'amd64', arm64: 'aarch64' };

export function launcherPath(info: ProductInfo, platform: NodeJS.Platform, arch: string): string | undefined {
  const os = OS_NAMES[platform]?.toLowerCase();
  const mine = info.launch.filter((l) => l.os.toLowerCase() === os);
  return (mine.find((l) => l.arch === (ARCH_NAMES[arch] ?? arch)) ?? mine[0])?.launcherPath;
}

interface Root {
  dir: string;
  depth: number;
  suffix?: string;
}

function jetbrainsRoots(ctx: DiscoveryContext): Root[] {
  const { env, userHome } = ctx;
  if (ctx.platform === 'win32') {
    const programFiles = env.ProgramFiles ?? env.PROGRAMFILES;
    const local = env.LOCALAPPDATA;
    const join = path.win32.join;
    return [
      ...(programFiles ? [{ dir: join(programFiles, 'JetBrains'), depth: 1 }, { dir: join(programFiles, 'Android'), depth: 1 }] : []),
      ...(local ? [{ dir: join(local, 'Programs'), depth: 1 }, { dir: join(local, 'JetBrains', 'Toolbox', 'apps'), depth: 3 }] : []),
    ];
  }
  const join = path.posix.join;
  if (ctx.platform === 'darwin') {
    return [
      { dir: '/Applications', depth: 1 },
      { dir: join(userHome, 'Applications'), depth: 1 },
      { dir: join(userHome, 'Library', 'Application Support', 'JetBrains', 'Toolbox', 'apps'), depth: 4 },
    ];
  }
  if (ctx.platform === 'linux') {
    return [
      { dir: join(userHome, '.local', 'share', 'JetBrains', 'Toolbox', 'apps'), depth: 3 },
      { dir: '/opt', depth: 1 },
      { dir: '/usr/share', depth: 1 },
      { dir: '/usr/local', depth: 1 },
      { dir: '/snap', depth: 1, suffix: 'current' },
    ];
  }
  return [];
}

function productInfoFile(dir: string, ctx: DiscoveryContext): string | undefined {
  if (ctx.platform === 'darwin') {
    if (!dir.endsWith('.app')) return undefined;
    const file = path.posix.join(dir, 'Contents', 'Resources', 'product-info.json');
    return ctx.fs.exists(file) ? file : undefined;
  }
  const file = (ctx.platform === 'win32' ? path.win32 : path.posix).join(dir, 'product-info.json');
  return ctx.fs.exists(file) ? file : undefined;
}

function jetbrainsInstall(dir: string, file: string, ctx: DiscoveryContext): IdeInstall | undefined {
  const info = parseProductInfo(ctx.fs.readText(file));
  if (!info) return undefined;
  const entry = entryForProductInfo(info.name, info.productCode);
  if (!entry || entry.kind !== 'jetbrains') return undefined;
  const version = info.version ?? info.buildNumber;
  const base = { key: entry.key, product: info.name ?? entry.name, kind: 'jetbrains' as const, ...(version ? { version } : {}) };
  if (ctx.platform === 'darwin') return { ...base, launcher: dir };
  const relative = launcherPath(info, ctx.platform, ctx.arch);
  if (!relative) return undefined;
  const api = ctx.platform === 'win32' ? path.win32 : path.posix;
  const launcher = api.resolve(dir, relative);
  if (!launcher.startsWith(api.resolve(dir) + api.sep) || !ctx.fs.exists(launcher)) return undefined;
  return { ...base, launcher };
}

function scan(root: Root, ctx: DiscoveryContext, found: IdeInstall[]): void {
  const api = ctx.platform === 'win32' ? path.win32 : path.posix;
  const walk = (dir: string, depth: number) => {
    for (const name of ctx.fs.readdir(dir)) {
      if (name.startsWith('.')) continue;
      const candidate = root.suffix ? api.join(dir, name, root.suffix) : api.join(dir, name);
      const file = productInfoFile(candidate, ctx);
      if (file) {
        const install = jetbrainsInstall(candidate, file, ctx);
        if (install) found.push(install);
      } else if (depth > 1) {
        walk(candidate, depth - 1);
      }
    }
  };
  walk(root.dir, root.depth);
}

const sortable = (i: IdeInstall) => (i.version ?? '0').replace(/^[A-Za-z]+-/, '');
const buildOrder = (a: IdeInstall, b: IdeInstall) => compareVersions(sortable(b), sortable(a));

export function discoverIdes(ctx: DiscoveryContext): IdeInstall[] {
  const editors = findEditorClis(ctx, ctx.fs.exists).flatMap(({ cli, path: file }) => {
    const entry = IDE_CATALOG.find((e) => e.cli === cli);
    return entry ? [{ key: entry.key, product: entry.name, kind: 'vscode' as const, launcher: file }] : [];
  });
  const jetbrains: IdeInstall[] = [];
  for (const root of jetbrainsRoots(ctx)) scan(root, ctx, jetbrains);
  const seen = new Set<string>();
  const unique = jetbrains.sort(buildOrder).filter((i) => !seen.has(i.launcher) && seen.add(i.launcher));
  return [...editors, ...unique];
}

const DROP_NAMES = new Set(['NO_COLOR', 'FORCE_COLOR', 'CLAUDECODE', 'TERMINAL_EMULATOR', 'ELECTRON_RUN_AS_NODE']);
const DROP_PREFIXES = ['CLAUDE_', 'ANTHROPIC_', 'CODEX_', 'GEMINI_CLI', 'COPILOT_', 'IDE_AGENT_TABS_', 'JEDITERM_SOURCE', 'TERM_PROGRAM', 'VSCODE_', 'MCP_'];
const KEEP_NAMES = new Set(['IDE_AGENT_TABS_HOME']);

// An IDE passes its own environment to every terminal tab it opens, so nothing from the calling agent
// session may reach it. IDE_AGENT_TABS_HOME stays: the IDE registers its endpoint under that folder.
export function launchEnvironment(env: NodeJS.ProcessEnv): NodeJS.ProcessEnv {
  return Object.fromEntries(
    Object.entries(terminalEnvironment(env)).filter(([raw]) => {
      const name = raw.toUpperCase();
      if (KEEP_NAMES.has(name)) return true;
      return !DROP_NAMES.has(name) && !DROP_PREFIXES.some((p) => name.startsWith(p));
    }),
  );
}

export interface IdeCommand {
  command: string;
  args: string[];
  windowsVerbatimArguments: boolean;
  windowsHide: boolean;
}

export function ideLaunchCommand(install: IdeInstall, folder: string, platform: NodeJS.Platform, comspec: string | undefined): IdeCommand {
  if (/\p{Cc}/u.test(folder)) throw new Error('the folder path holds a control character');
  if (install.kind === 'vscode') {
    const inv = cliInvocation(install.launcher, [folder], platform, comspec);
    // A .cmd editor CLI runs through cmd.exe, whose console window would flash; the editor window it
    // starts is a separate process and shows anyway.
    return { ...inv, windowsHide: inv.command !== install.launcher };
  }
  if (platform === 'darwin') return { command: 'open', args: ['-na', install.launcher, '--args', folder], windowsVerbatimArguments: false, windowsHide: false };
  return { command: install.launcher, args: [folder], windowsVerbatimArguments: false, windowsHide: false };
}

export function spawnIde(cmd: IdeCommand, env: NodeJS.ProcessEnv): Promise<void> {
  return new Promise((resolve, reject) => {
    const child = spawn(cmd.command, cmd.args, {
      stdio: 'ignore',
      detached: true,
      windowsHide: cmd.windowsHide,
      windowsVerbatimArguments: cmd.windowsVerbatimArguments,
      shell: false,
      env,
    });
    child.once('error', reject);
    child.once('spawn', () => {
      child.unref();
      resolve();
    });
  });
}

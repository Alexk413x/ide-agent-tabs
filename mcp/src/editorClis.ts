import { existsSync } from 'node:fs';
import path from 'node:path';
import { findOnPath } from './installed.js';

type CliContext = { platform: NodeJS.Platform; env: NodeJS.ProcessEnv; userHome: string };

export const EDITOR_CLIS = ['code', 'code-insiders', 'cursor', 'windsurf', 'codium', 'antigravity-ide', 'kiro', 'positron', 'trae'];

export interface EditorCli {
  cli: string;
  path: string;
}

const MAC_APPS: [cli: string, app: string, shims: string[]][] = [
  ['code', 'Visual Studio Code', ['code']],
  ['code-insiders', 'Visual Studio Code - Insiders', ['code']],
  ['cursor', 'Cursor', ['code', 'cursor']],
  ['windsurf', 'Windsurf', ['windsurf', 'code']],
  ['codium', 'VSCodium', ['codium']],
  ['antigravity-ide', 'Antigravity IDE', ['antigravity-ide']],
  ['antigravity-ide', 'Antigravity', ['antigravity-ide']],
  ['kiro', 'Kiro', ['code']],
  ['positron', 'Positron', ['code']],
  ['trae', 'Trae', ['code', 'trae']],
];

const LINUX_SNAPS = new Set(['code', 'code-insiders', 'codium']);
const LINUX_FLATPAKS: Record<string, string> = { code: 'com.visualstudio.code', codium: 'com.vscodium.codium' };

export function editorCliLocations(platform: NodeJS.Platform, env: NodeJS.ProcessEnv, userHome: string): Record<string, string[]> {
  const locations: Record<string, string[]> = Object.fromEntries(EDITOR_CLIS.map((cli) => [cli, []]));
  if (platform === 'win32') {
    const programs = env.LOCALAPPDATA ? path.win32.join(env.LOCALAPPDATA, 'Programs') : undefined;
    const programFiles = env.ProgramFiles ?? env.PROGRAMFILES;
    const add = (cli: string, root: string | undefined, ...rest: string[]) => {
      if (root) locations[cli]!.push(path.win32.join(root, ...rest, `${cli}.cmd`));
    };
    add('code', programs, 'Microsoft VS Code', 'bin');
    add('code', programFiles, 'Microsoft VS Code', 'bin');
    add('code-insiders', programs, 'Microsoft VS Code Insiders', 'bin');
    add('code-insiders', programFiles, 'Microsoft VS Code Insiders', 'bin');
    add('cursor', programs, 'cursor', 'resources', 'app', 'bin');
    add('windsurf', programs, 'Windsurf', 'bin');
    add('codium', programs, 'VSCodium', 'bin');
    add('codium', programFiles, 'VSCodium', 'bin');
    add('antigravity-ide', programs, 'Antigravity IDE', 'bin');
    add('kiro', programs, 'Kiro', 'bin');
    add('positron', programs, 'Positron', 'bin');
    add('positron', programFiles, 'Positron', 'bin');
    add('trae', programs, 'Trae', 'bin');
  } else if (platform === 'darwin') {
    for (const root of ['/Applications', path.posix.join(userHome, 'Applications')]) {
      for (const [cli, app, shims] of MAC_APPS) {
        for (const shim of shims) locations[cli]!.push(path.posix.join(root, `${app}.app`, 'Contents', 'Resources', 'app', 'bin', shim));
      }
    }
  } else if (platform === 'linux') {
    for (const cli of EDITOR_CLIS) {
      const flatpak = LINUX_FLATPAKS[cli];
      locations[cli]!.push(
        path.posix.join('/usr/share', cli, 'bin', cli),
        path.posix.join('/opt', cli, 'bin', cli),
        ...(LINUX_SNAPS.has(cli) ? [path.posix.join('/snap/bin', cli)] : []),
        path.posix.join(userHome, '.local', 'bin', cli),
        ...(flatpak
          ? [path.posix.join('/var/lib/flatpak/exports/bin', flatpak), path.posix.join(userHome, '.local', 'share', 'flatpak', 'exports', 'bin', flatpak)]
          : []),
      );
    }
  }
  return locations;
}

function cliFileNames(cli: string, platform: NodeJS.Platform): string[] {
  return platform === 'win32' ? [`${cli}.cmd`, `${cli}.exe`] : [cli];
}

export function findCliOnPath(cli: string, ctx: Pick<CliContext, 'platform' | 'env'>): string | undefined {
  const pathVar = ctx.env.PATH ?? ctx.env.Path ?? '';
  return cliFileNames(cli, ctx.platform)
    .map((name) => findOnPath(pathVar, name))
    .find((p) => p !== undefined);
}

export function findEditorClis(ctx: CliContext, exists: (file: string) => boolean = existsSync): EditorCli[] {
  const locations = editorCliLocations(ctx.platform, ctx.env, ctx.userHome);
  const found: EditorCli[] = [];
  for (const cli of EDITOR_CLIS) {
    const file = findCliOnPath(cli, ctx) ?? locations[cli]!.find((p) => exists(p));
    if (file) found.push({ cli, path: file });
  }
  return found;
}

export function resolveEditorCli(nameOrPath: string, ctx: CliContext, exists: (file: string) => boolean = existsSync): EditorCli | undefined {
  const pathApi = ctx.platform === 'win32' ? path.win32 : path.posix;
  if (pathApi.isAbsolute(nameOrPath)) {
    const candidates = ctx.platform === 'win32' && pathApi.extname(nameOrPath) === '' ? cliFileNames(nameOrPath, ctx.platform) : [nameOrPath];
    const file = candidates.find((p) => exists(p));
    return file ? { cli: pathApi.basename(file, pathApi.extname(file)), path: file } : undefined;
  }
  if (/[\\/]/.test(nameOrPath)) return undefined;
  const file = findCliOnPath(nameOrPath, ctx) ?? (editorCliLocations(ctx.platform, ctx.env, ctx.userHome)[nameOrPath] ?? []).find((p) => exists(p));
  return file ? { cli: nameOrPath, path: file } : undefined;
}

// Node refuses to spawn a .cmd or .bat file without a shell, and cmd.exe gives its own meaning to these
// characters even inside quotes, so text that holds one is refused instead of escaped.
const CMD_SPECIAL = /["%^&|<>!\r\n]/;

export function cliInvocation(cli: string, args: string[], platform: NodeJS.Platform, comspec: string | undefined) {
  if (platform !== 'win32' || !/\.(cmd|bat)$/i.test(cli)) return { command: cli, args, windowsVerbatimArguments: false };
  const all = [cli, ...args];
  const unsafe = all.find((a) => CMD_SPECIAL.test(a));
  if (unsafe !== undefined) throw new Error(`cmd.exe can't safely run with the argument ${unsafe}`);
  return {
    command: comspec || 'cmd.exe',
    args: ['/d', '/s', '/c', `"${all.map((a) => `"${a}"`).join(' ')}"`],
    windowsVerbatimArguments: true,
  };
}

import { spawn } from 'node:child_process';
import { promises as fs } from 'node:fs';
import path from 'node:path';

export const BUNDLE_SEGMENT = /\.(app|bundle|framework|pkg|plugin|prefPane)$/i;

export interface RevealDeps {
  realpath(target: string): Promise<string | undefined>;
  isDirectory(target: string): Promise<boolean>;
  open(folder: string): Promise<void>;
  platform: NodeJS.Platform;
}

export const systemReveal = (platform: NodeJS.Platform): RevealDeps => ({
  realpath: (target) => fs.realpath(target).catch(() => undefined),
  isDirectory: (target) => fs.stat(target).then((s) => s.isDirectory(), () => false),
  open: (folder) => openInFileManager(platform, folder),
  platform,
});

export function fileManagerCommand(platform: NodeJS.Platform): string {
  return platform === 'win32' ? 'explorer.exe' : platform === 'darwin' ? 'open' : 'xdg-open';
}

// windowsHide stays false: Explorer applies a hidden start to the folder window it opens, so a hidden
// launch leaves an invisible window that never closes.
export function openInFileManager(platform: NodeJS.Platform, folder: string): Promise<void> {
  return new Promise((resolve, reject) => {
    const child = spawn(fileManagerCommand(platform), [folder], { stdio: 'ignore', detached: true, windowsHide: false, shell: false });
    child.once('error', reject);
    child.once('spawn', () => {
      child.unref();
      resolve();
    });
  });
}

// Only a folder of a live session or an open IDE project is revealed, and never a macOS bundle: the OS
// opens a bundle by launching it.
export async function checkRevealTarget(target: string, known: readonly string[], deps: RevealDeps): Promise<string> {
  if (!path.isAbsolute(target) || /\p{Cc}/u.test(target)) throw new Error(`not an absolute path: ${target}`);
  const real = await deps.realpath(target);
  if (real === undefined || !(await deps.isDirectory(real))) throw new Error(`not a folder on this machine: ${target}`);
  if (deps.platform === 'darwin' && real.split(/[\/]/).some((segment) => BUNDLE_SEGMENT.test(segment))) {
    throw new Error(`refused: ${target} is inside a macOS bundle`);
  }
  const key = (p: string) => {
    const trimmed = p.replace(/[\/]+$/, '') || p;
    return deps.platform === 'win32' || deps.platform === 'darwin' ? trimmed.toLowerCase() : trimmed;
  };
  const allowed = new Set<string>();
  for (const folder of known) {
    const resolved = await deps.realpath(folder);
    if (resolved !== undefined) allowed.add(key(resolved));
  }
  if (!allowed.has(key(real))) throw new Error(`refused: ${target} is not the folder of a live session or an open IDE project`);
  return real;
}

import { promises as fs } from 'node:fs';
import path from 'node:path';

export const BUNDLE_SEGMENT = /\.(app|bundle|framework|pkg|plugin|prefPane)$/i;

export interface RevealDeps {
  realpath(target: string): Promise<string | undefined>;
  isDirectory(target: string): Promise<boolean>;
  platform: NodeJS.Platform;
}

export const systemReveal = (platform: NodeJS.Platform): RevealDeps => ({
  realpath: (target) => fs.realpath(target).catch(() => undefined),
  isDirectory: (target) => fs.stat(target).then((s) => s.isDirectory(), () => false),
  platform,
});

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

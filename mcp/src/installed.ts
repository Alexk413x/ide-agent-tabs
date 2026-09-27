import { lstatSync } from 'node:fs';
import path from 'node:path';

const WINDOWS_EXTENSIONS = ['.exe', '.cmd', '.bat', '.ps1'];

// lstat, not stat: the Microsoft Store pwsh.exe and wt.exe under WindowsApps are app execution aliases that
// stat cannot follow, so a stat-based lookup misses them.
function exists(file: string): boolean {
  try {
    lstatSync(file);
    return true;
  } catch {
    return false;
  }
}

export function findOnPath(pathVar: string, executable: string): string | undefined {
  for (const raw of pathVar.split(path.delimiter)) {
    const dir = raw.trim().replace(/^"+|"+$/g, '');
    if (dir === '') continue;
    const candidate = path.join(dir, executable);
    if (exists(candidate)) return candidate;
  }
  return undefined;
}

export function isInstalled(command: string, pathVar: string, isWindows: boolean): boolean {
  const names = isWindows ? [command, ...WINDOWS_EXTENSIONS.map((e) => command + e)] : [command];
  if (path.isAbsolute(command)) return names.some(exists);
  if (command.includes('/') || command.includes(path.sep)) return false;
  return names.some((name) => findOnPath(pathVar, name) !== undefined);
}

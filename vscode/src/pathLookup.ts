import * as fs from 'node:fs';
import * as path from 'node:path';
import { isAbsolutePath } from './request';

const WINDOWS_EXTENSIONS = ['.exe', '.cmd', '.bat', '.ps1'];
const SHIM_EXTENSIONS = ['.exe', '.cmd', '.bat'];

function searchDirs(searchPath: string): string[] {
  return searchPath
    .split(path.delimiter)
    .map(raw => raw.trim().replace(/^"+|"+$/g, ''))
    .filter(dir => dir !== '');
}

// lstat, not stat or existsSync: the Microsoft Store pwsh.exe under WindowsApps is an app execution alias
// that stat cannot follow (EACCES), so a following check misses it and the tab falls back to PowerShell 5.1.
function exists(file: string): boolean {
  try {
    fs.lstatSync(file);
    return true;
  } catch {
    return false;
  }
}

export function isCmdShim(command: string, searchPath: string): boolean {
  const lower = command.toLowerCase();
  if (/\.(cmd|bat)$/.test(lower)) return true;
  if (/\.(exe|com|ps1)$/.test(lower)) return false;
  for (const dir of searchDirs(searchPath)) {
    const hit = SHIM_EXTENSIONS.find(ext => exists(path.join(dir, command + ext)));
    if (hit !== undefined) return hit !== '.exe';
  }
  return true;
}

export function findOnPath(searchPath: string, executable: string): string | undefined {
  for (const dir of searchDirs(searchPath)) {
    const candidate = path.join(dir, executable);
    if (exists(candidate)) return candidate;
  }
  return undefined;
}

export function isInstalled(command: string, searchPath: string, isWindows: boolean): boolean {
  const names = isWindows ? [command, ...WINDOWS_EXTENSIONS.map(ext => command + ext)] : [command];
  if (isAbsolutePath(command)) return names.some(exists);
  if (command.includes('/') || command.includes(path.sep)) return false;
  return names.some(name => findOnPath(searchPath, name) !== undefined);
}

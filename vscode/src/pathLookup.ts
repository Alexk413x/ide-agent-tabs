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

// The status bar refreshes on every window focus, and each refresh asks about every profile. Answers are kept
// for ttlMs and dropped as soon as PATH changes, so a new install shows up within a short while.
export class InstallScan {
  private searchPath = '';
  private since = Number.NEGATIVE_INFINITY;
  private readonly answers = new Map<string, boolean>();

  constructor(
    private readonly ttlMs: number,
    private readonly now: () => number = Date.now,
  ) {}

  isInstalled(command: string, searchPath: string, isWindows: boolean): boolean {
    const now = this.now();
    if (searchPath !== this.searchPath || now - this.since >= this.ttlMs) {
      this.answers.clear();
      this.searchPath = searchPath;
      this.since = now;
    }
    let answer = this.answers.get(command);
    if (answer === undefined) {
      answer = isInstalled(command, searchPath, isWindows);
      this.answers.set(command, answer);
    }
    return answer;
  }
}

import { lstatSync, readdirSync, readlinkSync } from 'node:fs';
import path from 'node:path';
import { run } from '../process.js';

export type ShellSource = 'path' | 'msi' | 'store' | 'preview' | 'windows';

export interface DetectedShell {
  path: string;
  label: string;
  version: string;
  source: ShellSource;
}

export interface ShellProbe {
  env: NodeJS.ProcessEnv;
  exists(file: string): boolean;
  readdir(dir: string): string[] | undefined;
  readlink(file: string): string | undefined;
  mtimeMs(file: string): number | undefined;
  version?(exe: string): Promise<string | undefined>;
}

export interface PreviousDetection {
  detectedAt: string;
  shells: DetectedShell[];
}

const win = path.win32;
const STORE_PACKAGE = /^Microsoft\.PowerShell(Preview)?_(\d+(?:\.\d+)*)_/i;
const VERSION = /^\d+(?:\.\d+)*(?:-[0-9A-Za-z.-]+)?$/;
export const WINDOWS_POWERSHELL_VERSION = '5.1';
export const VERSION_COMMAND = '$PSVersionTable.PSVersion.ToString()';

function envValue(env: NodeJS.ProcessEnv, name: string): string | undefined {
  const key = Object.keys(env).find((k) => k.toUpperCase() === name.toUpperCase());
  const value = key === undefined ? undefined : env[key];
  return value && value.trim() !== '' ? value : undefined;
}

interface Locations {
  programFiles?: string;
  windowsApps?: string;
  aliases?: string;
  system32?: string;
}

function locations(env: NodeJS.ProcessEnv): Locations {
  const programFiles = envValue(env, 'ProgramFiles');
  const localAppData = envValue(env, 'LOCALAPPDATA');
  const systemRoot = envValue(env, 'SystemRoot') ?? envValue(env, 'windir');
  return {
    ...(programFiles ? { programFiles, windowsApps: win.join(programFiles, 'WindowsApps') } : {}),
    ...(localAppData ? { aliases: win.join(localAppData, 'Microsoft', 'WindowsApps') } : {}),
    ...(systemRoot ? { system32: win.join(systemRoot, 'System32') } : {}),
  };
}

const lower = (p: string) => win.normalize(p).toLowerCase();

function isInside(file: string, dir: string | undefined): boolean {
  if (!dir) return false;
  const rel = win.relative(lower(dir), lower(file));
  return rel !== '' && !rel.startsWith('..') && !win.isAbsolute(rel);
}

function storeVersion(packageDir: string): { version: string; preview: boolean } | undefined {
  const m = STORE_PACKAGE.exec(win.basename(packageDir));
  if (!m) return undefined;
  const parts = m[2]!.split('.');
  return { version: parts.slice(0, 3).join('.'), preview: m[1] !== undefined };
}

interface Candidate {
  path: string;
  source: ShellSource;
  version?: string;
  key: string;
}

function classify(file: string, loc: Locations, probe: ShellProbe): Candidate {
  const name = win.basename(file).toLowerCase();
  const target = probe.readlink(file);
  const key = lower(target ?? file);
  if (loc.programFiles && isInside(file, win.join(loc.programFiles, 'PowerShell'))) {
    const folder = win.relative(win.join(loc.programFiles, 'PowerShell'), win.dirname(file));
    const preview = /preview/i.test(folder);
    return { path: file, source: preview ? 'preview' : 'msi', key, ...(!preview && /^\d+$/.test(folder) ? { version: folder } : {}) };
  }
  const packageDir = isInside(file, loc.windowsApps) ? win.dirname(file) : target && isInside(target, loc.windowsApps) ? win.dirname(target) : undefined;
  if (packageDir || isInside(file, loc.aliases)) {
    const store = packageDir ? storeVersion(packageDir) : undefined;
    const preview = store?.preview ?? name === 'pwsh-preview.exe';
    return { path: file, source: preview ? 'preview' : 'store', key, ...(store && !preview ? { version: store.version } : {}) };
  }
  if (loc.system32 && isInside(file, win.join(loc.system32, 'WindowsPowerShell')) && name === 'powershell.exe') {
    return { path: file, source: 'windows', key, version: WINDOWS_POWERSHELL_VERSION };
  }
  return { path: file, source: 'path', key };
}

function newestStorePackage(loc: Locations, probe: ShellProbe, preview: boolean): string | undefined {
  if (!loc.windowsApps) return undefined;
  const versions = (probe.readdir(loc.windowsApps) ?? [])
    .map((n) => storeVersion(n))
    .filter((v): v is { version: string; preview: boolean } => v !== undefined && v.preview === preview)
    .map((v) => v.version);
  return versions.sort(compareVersions).at(-1);
}

function pathDirs(env: NodeJS.ProcessEnv): string[] {
  return (envValue(env, 'PATH') ?? '')
    .split(';')
    .map((d) => d.trim().replace(/^"+|"+$/g, ''))
    .filter((d) => d !== '');
}

export function candidatePaths(probe: ShellProbe): Candidate[] {
  const loc = locations(probe.env);
  const files: string[] = [];
  for (const dir of pathDirs(probe.env)) {
    for (const name of ['pwsh.exe', 'powershell.exe']) {
      const file = win.join(dir, name);
      if (probe.exists(file)) files.push(file);
    }
  }
  if (loc.programFiles) {
    const root = win.join(loc.programFiles, 'PowerShell');
    for (const folder of (probe.readdir(root) ?? []).sort()) {
      const file = win.join(root, folder, 'pwsh.exe');
      if (probe.exists(file)) files.push(file);
    }
  }
  if (loc.aliases) {
    for (const name of ['pwsh.exe', 'pwsh-preview.exe']) {
      const file = win.join(loc.aliases, name);
      if (probe.exists(file)) files.push(file);
    }
  }
  if (loc.system32) {
    const file = win.join(loc.system32, 'WindowsPowerShell', 'v1.0', 'powershell.exe');
    if (probe.exists(file)) files.push(file);
  }
  const byKey = new Map<string, Candidate>();
  for (const file of files) {
    const c = classify(file, loc, probe);
    const seen = byKey.get(c.key) ?? byKey.get(lower(c.path));
    // A Store package folder's path changes with every update, so its stable app alias wins.
    if (!seen || (isInside(seen.path, loc.windowsApps) && isInside(c.path, loc.aliases))) {
      if (seen) byKey.delete(seen.key);
      byKey.set(c.key, c);
    }
  }
  for (const c of byKey.values()) {
    if (c.source === 'store' && c.version === undefined) {
      const version = newestStorePackage(loc, probe, false);
      if (version) c.version = version;
    }
  }
  return [...byKey.values()];
}

export function parseVersionOutput(stdout: string): string | undefined {
  const line = stdout.trim().split(/\r?\n/)[0]?.trim() ?? '';
  return VERSION.test(line) ? line : undefined;
}

export function compareVersions(a: string, b: string): number {
  const [aCore = '', aPre] = a.split(/-(.*)/s);
  const [bCore = '', bPre] = b.split(/-(.*)/s);
  const an = aCore.split('.').map(Number);
  const bn = bCore.split('.').map(Number);
  for (let i = 0; i < Math.max(an.length, bn.length); i++) {
    const d = (an[i] ?? 0) - (bn[i] ?? 0);
    if (d !== 0) return d;
  }
  if (aPre === bPre) return 0;
  if (aPre === undefined) return 1;
  if (bPre === undefined) return -1;
  return aPre.localeCompare(bPre, 'en', { numeric: true });
}

export function shellLabel(file: string, version: string, source: ShellSource): string {
  const isPwsh = win.basename(file).toLowerCase().startsWith('pwsh');
  const name = source === 'windows' || !isPwsh ? 'Windows PowerShell' : 'PowerShell';
  const head = version ? `${name} ${version}` : name;
  switch (source) {
    case 'msi':
      return `${head} (MSI)`;
    case 'store':
      return `${head} (Store)`;
    case 'preview':
      return `${head} (preview)`;
    case 'path':
      return `${head} (PATH)`;
    case 'windows':
      return head;
  }
}

function cachedVersion(c: Candidate, probe: ShellProbe, previous: PreviousDetection | undefined): string | undefined {
  const before = previous?.shells.find((s) => lower(s.path) === lower(c.path) && s.source === c.source);
  if (!before?.version || !VERSION.test(before.version)) return undefined;
  const mtime = probe.mtimeMs(c.path);
  const at = Date.parse(previous!.detectedAt);
  return mtime !== undefined && Number.isFinite(at) && mtime < at ? before.version : undefined;
}

const byRank = (a: DetectedShell, b: DetectedShell) =>
  Number(a.source === 'windows') - Number(b.source === 'windows') || compareVersions(b.version || '0', a.version || '0');

const toShell = (c: Candidate, version: string): DetectedShell => ({ path: c.path, label: shellLabel(c.path, version, c.source), version, source: c.source });

// Versions come from folder names where they carry one; otherwise detection runs the shell once. A tab
// launch never runs one: it reads detected.json, or lists the shells without versions.
export async function detectPowerShells(probe: ShellProbe, previous?: PreviousDetection): Promise<DetectedShell[]> {
  const shells = await Promise.all(
    candidatePaths(probe).map(async (c) => {
      const exact = c.version !== undefined && c.version.includes('.');
      const version =
        (exact ? c.version : undefined) ??
        cachedVersion(c, probe, previous) ??
        (probe.version ? await probe.version(c.path).catch(() => undefined) : undefined) ??
        c.version ??
        '';
      return toShell(c, version);
    }),
  );
  return shells.sort(byRank);
}

export function listPowerShells(probe: ShellProbe): DetectedShell[] {
  return candidatePaths(probe).map((c) => toShell(c, c.version ?? '')).sort(byRank);
}

function isPowerShell7(shell: DetectedShell): boolean {
  if (shell.source === 'windows') return false;
  const major = Number(shell.version.split('.')[0]);
  if (shell.version === '') return win.basename(shell.path).toLowerCase().startsWith('pwsh');
  return major >= 7;
}

const isStable = (shell: DetectedShell) => shell.source !== 'preview' && !shell.version.includes('-');

export function pickPowerShell(shells: DetectedShell[], configured: string | undefined, exists: (file: string) => boolean): string {
  if (configured && exists(configured)) return configured;
  const present = shells.filter((s) => exists(s.path));
  const seven = present.filter(isPowerShell7);
  const pool = seven.some(isStable) ? seven.filter(isStable) : seven;
  const newest = [...pool].sort((a, b) => compareVersions(b.version || '0', a.version || '0'))[0];
  if (newest) return newest.path;
  return present.find((s) => s.source === 'windows' || win.basename(s.path).toLowerCase() === 'powershell.exe')?.path ?? 'powershell.exe';
}

// lstat, not stat: the Store's pwsh.exe is an app execution alias that stat cannot follow.
export function fileExists(file: string): boolean {
  try {
    lstatSync(file);
    return true;
  } catch {
    return false;
  }
}

export function defaultPowerShell(env: NodeJS.ProcessEnv): string {
  return pickPowerShell(listPowerShells(systemProbe(env, false)), undefined, fileExists);
}

export async function runVersion(exe: string): Promise<string | undefined> {
  const result = await run(exe, ['-NoLogo', '-NoProfile', '-NonInteractive', '-Command', VERSION_COMMAND], { timeoutMs: 20_000 });
  return result.code === 0 ? parseVersionOutput(result.stdout) : undefined;
}

export function systemProbe(env: NodeJS.ProcessEnv, runShells: boolean): ShellProbe {
  return {
    env,
    exists: fileExists,
    readdir: (dir) => {
      try {
        return readdirSync(dir);
      } catch {
        return undefined;
      }
    },
    readlink: (file) => {
      try {
        return readlinkSync(file);
      } catch {
        return undefined;
      }
    },
    mtimeMs: (file) => {
      try {
        return lstatSync(file).mtimeMs;
      } catch {
        return undefined;
      }
    },
    ...(runShells ? { version: runVersion } : {}),
  };
}

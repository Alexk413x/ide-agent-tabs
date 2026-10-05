import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  compareVersions,
  detectPowerShells,
  parseVersionOutput,
  pickPowerShell,
  type DetectedShell,
  type ShellProbe,
} from '../src/terminals/powershell.js';

const PF = 'C:\\Program Files';
const LOCAL = 'C:\\Users\\a\\AppData\\Local';
const ALIASES = `${LOCAL}\\Microsoft\\WindowsApps`;
const STORE_PKG = `${PF}\\WindowsApps\\Microsoft.PowerShell_7.5.2.0_x64__8wekyb3d8bbwe`;
const WINPS = 'C:\\WINDOWS\\System32\\WindowsPowerShell\\v1.0\\powershell.exe';
const MSI = `${PF}\\PowerShell\\7\\pwsh.exe`;
const PREVIEW = `${PF}\\PowerShell\\7-preview\\pwsh.exe`;
const BASE_ENV = { ProgramFiles: PF, LOCALAPPDATA: LOCAL, SystemRoot: 'C:\\WINDOWS' };

interface FakeFs {
  files: string[];
  links?: Record<string, string>;
  dirs?: Record<string, string[]>;
  versions?: Record<string, string>;
  mtimes?: Record<string, number>;
}

function probe(fake: FakeFs, env: NodeJS.ProcessEnv, run = true) {
  const ran: string[] = [];
  const lower = new Set(fake.files.map((f) => f.toLowerCase()));
  const p: ShellProbe = {
    env,
    exists: (file) => lower.has(file.toLowerCase()),
    readdir: (dir) => fake.dirs?.[dir],
    readlink: (file) => fake.links?.[file],
    mtimeMs: (file) => fake.mtimes?.[file] ?? (lower.has(file.toLowerCase()) ? 1000 : undefined),
    ...(run
      ? {
          version: async (exe: string) => {
            ran.push(exe);
            return fake.versions?.[exe];
          },
        }
      : {}),
  };
  return { probe: p, ran };
}

test('the Store layout: the app alias wins over its package folder on PATH, with the version from the folder name', async () => {
  const fake: FakeFs = {
    files: [`${STORE_PKG}\\pwsh.exe`, `${ALIASES}\\pwsh.exe`, WINPS],
    links: { [`${ALIASES}\\pwsh.exe`]: `${STORE_PKG}\\pwsh.exe` },
  };
  const env = { ...BASE_ENV, PATH: [STORE_PKG, 'C:\\WINDOWS\\System32\\WindowsPowerShell\\v1.0', ALIASES].join(';') };
  const { probe: p, ran } = probe(fake, env);
  const shells = await detectPowerShells(p);
  assert.deepEqual(shells, [
    { path: `${ALIASES}\\pwsh.exe`, label: 'PowerShell 7.5.2 (Store)', version: '7.5.2', source: 'store' },
    { path: WINPS, label: 'Windows PowerShell 5.1', version: '5.1', source: 'windows' },
  ]);
  assert.deepEqual(ran, [], 'no shell runs when folder names give the versions');
  assert.equal(pickPowerShell(shells, undefined, p.exists), `${ALIASES}\\pwsh.exe`);
});

test('the Store alias reads its version from a listable WindowsApps folder, else runs the shell once', async () => {
  const files = [`${ALIASES}\\pwsh.exe`];
  const listed = probe({ files, dirs: { [`${PF}\\WindowsApps`]: ['Microsoft.PowerShell_7.4.1.0_x64__8w', 'Microsoft.PowerShell_7.5.0.0_x64__8w', 'Other_1.0_x64'] } }, BASE_ENV);
  assert.equal((await detectPowerShells(listed.probe))[0]!.version, '7.5.0');
  assert.deepEqual(listed.ran, []);

  const hidden = probe({ files, versions: { [`${ALIASES}\\pwsh.exe`]: '7.6.1' } }, BASE_ENV);
  assert.deepEqual(await detectPowerShells(hidden.probe), [
    { path: `${ALIASES}\\pwsh.exe`, label: 'PowerShell 7.6.1 (Store)', version: '7.6.1', source: 'store' },
  ]);
  assert.deepEqual(hidden.ran, [`${ALIASES}\\pwsh.exe`]);
});

test('the MSI layout is found by folder even with a short PATH, for any major version, and preview is labelled', async () => {
  const fake: FakeFs = {
    files: [MSI, `${PF}\\PowerShell\\8\\pwsh.exe`, PREVIEW, WINPS],
    dirs: { [`${PF}\\PowerShell`]: ['7', '8', '7-preview'] },
    versions: { [MSI]: '7.5.2', [`${PF}\\PowerShell\\8\\pwsh.exe`]: '8.0.1', [PREVIEW]: '7.6.0-preview.4' },
  };
  const { probe: p, ran } = probe(fake, { ...BASE_ENV, PATH: 'C:\\bin' });
  const shells = await detectPowerShells(p);
  assert.deepEqual(shells.map((s) => [s.label, s.source]), [
    ['PowerShell 8.0.1 (MSI)', 'msi'],
    ['PowerShell 7.6.0-preview.4 (preview)', 'preview'],
    ['PowerShell 7.5.2 (MSI)', 'msi'],
    ['Windows PowerShell 5.1', 'windows'],
  ]);
  assert.equal(ran.length, 3, 'each PowerShell without a version in its folder name runs once');
  assert.equal(pickPowerShell(shells, undefined, p.exists), `${PF}\\PowerShell\\8\\pwsh.exe`);
});

test('an MSI pwsh on PATH counts as MSI, and a pwsh elsewhere on PATH as path', async () => {
  const scoop = 'C:\\Users\\a\\scoop\\shims\\pwsh.exe';
  const fake: FakeFs = { files: [MSI, scoop], dirs: { [`${PF}\\PowerShell`]: ['7'] }, versions: { [MSI]: '7.5.2', [scoop]: '7.4.0' } };
  const { probe: p } = probe(fake, { ...BASE_ENV, PATH: `${PF}\\PowerShell\\7;C:\\Users\\a\\scoop\\shims` });
  assert.deepEqual(await detectPowerShells(p), [
    { path: MSI, label: 'PowerShell 7.5.2 (MSI)', version: '7.5.2', source: 'msi' },
    { path: scoop, label: 'PowerShell 7.4.0 (PATH)', version: '7.4.0', source: 'path' },
  ]);
});

test('detection reuses a version from the last detection when the shell has not changed since', async () => {
  const fake: FakeFs = { files: [MSI], dirs: { [`${PF}\\PowerShell`]: ['7'] }, versions: { [MSI]: '7.5.3' }, mtimes: { [MSI]: Date.parse('2026-01-01') } };
  const previous = { detectedAt: '2026-02-01T00:00:00.000Z', shells: [{ path: MSI, label: 'x', version: '7.5.2', source: 'msi' as const }] };
  const cached = probe(fake, BASE_ENV);
  assert.equal((await detectPowerShells(cached.probe, previous))[0]!.version, '7.5.2');
  assert.deepEqual(cached.ran, []);

  const updated = probe({ ...fake, mtimes: { [MSI]: Date.parse('2026-03-01') } }, BASE_ENV);
  assert.equal((await detectPowerShells(updated.probe, previous))[0]!.version, '7.5.3');
});

test('without a version runner, as at a tab launch, nothing runs and the folder name ranks the shells', async () => {
  const fake: FakeFs = { files: [MSI, WINPS], dirs: { [`${PF}\\PowerShell`]: ['7'] } };
  const { probe: p } = probe(fake, BASE_ENV, false);
  const shells = await detectPowerShells(p);
  assert.deepEqual(shells[0], { path: MSI, label: 'PowerShell 7 (MSI)', version: '7', source: 'msi' });
  assert.equal(pickPowerShell(shells, undefined, p.exists), MSI);
});

const shell = (path: string, version: string, source: DetectedShell['source']): DetectedShell => ({ path, label: '', version, source });

test('automatic picks the newest stable PowerShell 7+, then a preview, then Windows PowerShell; a configured path wins', () => {
  const all = [shell('a', '7.4.6', 'store'), shell('b', '7.5.2', 'msi'), shell('c', '7.6.0-preview.4', 'preview'), shell('w', '5.1', 'windows')];
  const exists = (f: string) => f !== 'gone';
  assert.equal(pickPowerShell(all, undefined, exists), 'b');
  assert.equal(pickPowerShell([all[2]!, all[3]!], undefined, exists), 'c');
  assert.equal(pickPowerShell([all[3]!], undefined, exists), 'w');
  assert.equal(pickPowerShell([], undefined, exists), 'powershell.exe');
  assert.equal(pickPowerShell(all, 'D:\\custom\\pwsh.exe', exists), 'D:\\custom\\pwsh.exe');
  assert.equal(pickPowerShell(all, 'gone', exists), 'b');
  assert.equal(pickPowerShell([shell('gone', '7.9.0', 'msi'), all[0]!], undefined, exists), 'a');
});

test('versions compare numerically, and a release beats its preview', () => {
  assert.ok(compareVersions('7.10.0', '7.9.9') > 0);
  assert.ok(compareVersions('7.6.0', '7.6.0-preview.4') > 0);
  assert.ok(compareVersions('7.6.0-preview.10', '7.6.0-preview.4') > 0);
  assert.equal(compareVersions('7.5', '7.5.0'), 0);
  assert.equal(parseVersionOutput('7.5.2\r\n'), '7.5.2');
  assert.equal(parseVersionOutput('7.6.0-preview.4\n'), '7.6.0-preview.4');
  assert.equal(parseVersionOutput('The term is not recognized'), undefined);
});

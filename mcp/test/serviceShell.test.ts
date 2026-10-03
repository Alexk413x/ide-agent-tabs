import assert from 'node:assert/strict';
import { readFileSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import { Service } from '../src/service.js';
import type { ShellProbe } from '../src/terminals/powershell.js';
import type { TerminalContext, TerminalDriver } from '../src/terminals/types.js';
import { tempDir } from './tempDir.js';

const MSI = 'C:/Program Files/PowerShell/7/pwsh.exe';
const STORE = 'C:/Users/a/AppData/Local/Microsoft/WindowsApps/pwsh.exe';
const CUSTOM = 'D:/tools/pwsh.exe';
const MSI_WIN = path.win32.normalize(MSI);

function setup(files: string[]) {
  const home = tempDir('iat-shell-');
  const work = tempDir('iat-shell-w-');
  const seen: (string | undefined)[] = [];
  const ran: string[] = [];
  const driver: TerminalDriver = {
    name: 'fake-term',
    label: 'Fake Terminal',
    capabilities: { open: 'tab', list: 'yes', close: 'yes' },
    available: async () => true,
    open: async (ctx: TerminalContext, spec) => {
      seen.push(ctx.powerShell);
      return { id: spec.id, terminal: 'fake-term', agent: spec.agent, path: spec.cwd, createdAt: Date.now() };
    },
    alive: async () => new Set(),
    close: async () => undefined,
  };
  const probe = (runShells: boolean): ShellProbe => ({
    env: { ProgramFiles: 'C:/Program Files', PATH: '' },
    exists: (f) => files.includes(f),
    readdir: (dir) => (dir.toLowerCase().endsWith('powershell') ? ['7'] : undefined),
    readlink: () => undefined,
    mtimeMs: () => 0,
    ...(runShells ? { version: async (exe: string) => (ran.push(exe), '7.5.2') } : {}),
  });
  let n = 0;
  const service = new Service({
    home,
    scriptsDir: home,
    platform: 'win32',
    env: { PATH: '' },
    callIde: async () => ({}),
    drivers: [driver],
    newId: () => `tab-${++n}`,
    shellProbe: probe,
  });
  return { home, work, service, seen, ran };
}

test('a Windows terminal tab gets the PowerShell from detected.json, the shell setting, or a quick look without running one', async () => {
  const { home, work, service, seen, ran } = setup([MSI_WIN, STORE, CUSTOM]);
  await service.openTab({ path: work, ide: 'fake-term' });
  assert.equal(seen.at(-1), MSI_WIN);
  assert.deepEqual(ran, [], 'a tab launch never runs a shell');

  writeFileSync(
    path.join(home, 'detected.json'),
    JSON.stringify({ version: 1, detectedAt: new Date().toISOString(), platform: 'win32', terminals: [], shells: [{ path: STORE, label: 'PowerShell 7.6.0 (Store)', version: '7.6.0', source: 'store' }] }),
  );
  await service.openTab({ path: work, ide: 'fake-term' });
  assert.equal(seen.at(-1), STORE);

  writeFileSync(path.join(home, 'config.json'), JSON.stringify({ shell: CUSTOM }));
  await service.openTab({ path: work, ide: 'fake-term' });
  assert.equal(seen.at(-1), CUSTOM);
});

test('refreshDetection writes detected.json with the terminals and the shells, running each shell once at most', async () => {
  const { home, service, ran } = setup([MSI_WIN]);
  const detection = await service.refreshDetection();
  assert.deepEqual(detection.terminals, [{ id: 'fake-term', name: 'Fake Terminal' }]);
  assert.deepEqual(detection.shells, [
    { path: MSI_WIN, label: 'PowerShell 7.5.2 (MSI)', version: '7.5.2', source: 'msi' },
  ]);
  assert.deepEqual(JSON.parse(readFileSync(path.join(home, 'detected.json'), 'utf8')), detection);
  assert.equal(ran.length, 1);
  await service.refreshDetection();
  assert.equal(ran.length, 1, 'an unchanged shell keeps its detected version');
});

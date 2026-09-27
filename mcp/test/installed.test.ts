import assert from 'node:assert/strict';
import { lstatSync, mkdirSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import { tempDir } from './tempDir.js';
import { findOnPath, isInstalled } from '../src/installed.js';

const home = tempDir('iat-installed-');
const bin = path.join(home, 'bin');
mkdirSync(bin);
for (const f of ['posix-cli', 'npm-cli.cmd', 'shim-cli.ps1', 'native-cli.exe', 'old-cli.bat', 'tool.exe']) {
  writeFileSync(path.join(bin, f), '');
}

test('installed means the command is on PATH, with Windows extensions on Windows', () => {
  const pathVar = [path.join(home, 'missing'), bin].join(path.delimiter);
  for (const command of ['posix-cli', 'npm-cli', 'shim-cli', 'native-cli', 'old-cli', 'native-cli.exe']) {
    assert.ok(isInstalled(command, pathVar, true), command);
  }
  assert.ok(isInstalled('posix-cli', pathVar, false));
  assert.ok(!isInstalled('npm-cli', pathVar, false));
  assert.ok(!isInstalled('absent', pathVar, true));
  assert.ok(isInstalled(path.join(bin, 'posix-cli'), '', false));
  assert.ok(isInstalled(path.join(bin, 'npm-cli'), '', true));
  assert.ok(!isInstalled(path.join(bin, 'absent'), pathVar, false));
  assert.ok(!isInstalled(`bin${path.sep}posix-cli`, pathVar, false));
  assert.ok(!isInstalled('bin/posix-cli', pathVar, false));
});

test('finds an executable on PATH and skips blank, quoted and invalid entries', () => {
  const pathVar = ['', '  ', path.join(home, 'no', 'such'), 'bad<>|dir', `"${bin}"`].join(path.delimiter);
  assert.equal(findOnPath(pathVar, 'tool.exe'), path.join(bin, 'tool.exe'));
  assert.equal(findOnPath(pathVar, 'absent.exe'), undefined);
});

test('finds the Microsoft Store pwsh alias', (t) => {
  const windowsApps = path.join(process.env.LOCALAPPDATA ?? '', 'Microsoft', 'WindowsApps');
  try {
    lstatSync(path.join(windowsApps, 'pwsh.exe'));
  } catch {
    t.skip('Store pwsh not installed');
    return;
  }
  assert.equal(findOnPath(windowsApps, 'pwsh.exe'), path.join(windowsApps, 'pwsh.exe'));
});

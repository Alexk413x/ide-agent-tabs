import assert from 'node:assert/strict';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { test } from 'node:test';
import { endpointFileName, endpointJson, ideAgentTabsHome, newToken, newWindowId, writeAtomically } from '../registry';

const home = fs.mkdtempSync(path.join(os.tmpdir(), 'iat-home-'));
const token = newToken();
const entry = endpointJson({ product: 'Visual Studio Code', version: '1.118.1', pid: 12345, url: 'http://127.0.0.1:50000/ide-agent-tabs', token });

test('endpoint file holds exactly the registry fields', () => {
  const file = writeAtomically(path.join(home, 'endpoints', 'vscode-12345-ab.json'), entry, true);
  const json = JSON.parse(fs.readFileSync(file, 'utf8'));
  assert.deepEqual(json, {
    protocol: 1,
    ide: 'vscode',
    product: 'Visual Studio Code',
    version: '1.118.1',
    pid: 12345,
    url: 'http://127.0.0.1:50000/ide-agent-tabs',
    token,
  });
});

test('file name carries the pid and a per-window id', () => {
  assert.match(endpointFileName(42, newWindowId()), /^vscode-42-[0-9a-f]{8}\.json$/);
  assert.notEqual(newWindowId(), newWindowId());
});

test('rewrite replaces the file and leaves no temporary files', () => {
  const target = path.join(home, 'rewrite', 'vscode-1-a.json');
  writeAtomically(target, 'first', true);
  writeAtomically(target, 'second', true);
  assert.equal(fs.readFileSync(target, 'utf8'), 'second');
  assert.deepEqual(fs.readdirSync(path.dirname(target)), ['vscode-1-a.json']);
});

test('endpoint folder and file are private on POSIX', t => {
  if (process.platform === 'win32') return t.skip('POSIX permissions are tested on macOS and Linux');
  const dir = path.join(home, 'posix', 'endpoints');
  fs.mkdirSync(dir, { recursive: true, mode: 0o755 });
  fs.chmodSync(dir, 0o755);
  const file = writeAtomically(path.join(dir, 'vscode-2-a.json'), entry, true);
  assert.equal(fs.statSync(dir).mode & 0o777, 0o700);
  assert.equal(fs.statSync(file).mode & 0o777, 0o600);
});

test('home folder follows IDE_AGENT_TABS_HOME', () => {
  assert.equal(ideAgentTabsHome({ IDE_AGENT_TABS_HOME: home }), home);
  assert.equal(ideAgentTabsHome({}), path.join(os.homedir(), '.ide-agent-tabs'));
  assert.equal(ideAgentTabsHome({ IDE_AGENT_TABS_HOME: ' ' }), path.join(os.homedir(), '.ide-agent-tabs'));
});

import assert from 'node:assert/strict';
import * as fs from 'node:fs';
import * as path from 'node:path';
import { test } from 'node:test';
import { InstallScan } from '../pathLookup';
import { startupFolder } from '../startup';
import { onWindows, tempDir } from './support';

const manifest = JSON.parse(fs.readFileSync(path.resolve(__dirname, '..', '..', 'package.json'), 'utf8'));

test('an untrusted workspace never opens an agent on startup', () => {
  const folders = ['a', 'b'];
  for (const mode of ['always', 'claudeFolder', 'never', 'bogus']) {
    assert.equal(startupFolder(mode, false, folders, () => true), undefined, mode);
  }
});

test('a trusted workspace opens by mode', () => {
  assert.equal(startupFolder('always', true, ['a', 'b'], () => false), 'a');
  assert.equal(startupFolder('always', true, [], () => true), undefined);
  assert.equal(startupFolder('claudeFolder', true, ['a', 'b'], f => f === 'b'), 'b');
  assert.equal(startupFolder('claudeFolder', true, ['a'], () => false), undefined);
  assert.equal(startupFolder('never', true, ['a'], () => true), undefined);
});

test('the settings a workspace can set are restricted while it is untrusted', () => {
  const properties = manifest.contributes.configuration[0].properties;
  const restricted: string[] = manifest.capabilities.untrustedWorkspaces.restrictedConfigurations;
  assert.deepEqual(restricted, ['ideAgentTabs.defaultAgent', 'ideAgentTabs.openOnStartup']);
  const windowScoped = Object.keys(properties).filter(name => properties[name].scope === undefined || properties[name].scope === 'window');
  assert.deepEqual(windowScoped.sort(), [...restricted].sort(), 'every setting a workspace can set is restricted, and nothing else is listed');
});

test('the extension starts after startup finishes', () => {
  assert.deepEqual(manifest.activationEvents, ['onStartupFinished']);
});

test('the install scan answers from its cache until the TTL passes or PATH changes', () => {
  const bin = tempDir('iat-scan-');
  const exe = onWindows ? 'agentx.exe' : 'agentx';
  const searchPath = bin;
  let now = 1000;
  const scan = new InstallScan(30_000, () => now);
  assert.equal(scan.isInstalled('agentx', searchPath, onWindows), false);
  fs.writeFileSync(path.join(bin, exe), '');
  now += 29_000;
  assert.equal(scan.isInstalled('agentx', searchPath, onWindows), false, 'cached');
  now += 1_000;
  assert.equal(scan.isInstalled('agentx', searchPath, onWindows), true, 'rescanned after the TTL');
  fs.rmSync(path.join(bin, exe));
  assert.equal(scan.isInstalled('agentx', searchPath, onWindows), true, 'cached again');
  assert.equal(scan.isInstalled('agentx', `${searchPath}${path.delimiter}${bin}`, onWindows), false, 'a new PATH drops the cache');
});

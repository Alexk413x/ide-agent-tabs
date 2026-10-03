import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import { DETECTED_FILE, parseDetection, readDetection, refreshDetectionFile } from '../src/detection.js';
import type { TerminalDriver } from '../src/terminals/types.js';
import { tempDir } from './tempDir.js';

const driver = (name: string, label: string, available: boolean): TerminalDriver => ({
  name,
  label,
  capabilities: { open: 'tab', list: 'yes', close: 'yes' },
  available: async () => available,
  open: async () => assert.fail('detection opens nothing'),
  alive: async () => new Set(),
  close: async () => undefined,
});

test('the session start hook writes detected.json with the available terminals by their driver labels', async () => {
  const home = tempDir('iat-detect-');
  const drivers = [driver('kitty', 'kitty', true), driver('ghostty', 'Ghostty', false), driver('tmux', 'tmux', true)];
  const detection = await refreshDetectionFile({ home, platform: 'linux', env: { PATH: '' }, drivers });
  assert.deepEqual(detection.terminals, [{ id: 'kitty', name: 'kitty' }, { id: 'tmux', name: 'tmux' }]);
  assert.deepEqual(detection.shells, [], 'shells are empty off Windows');
  assert.deepEqual(JSON.parse(readFileSync(path.join(home, DETECTED_FILE), 'utf8')), detection);
  assert.deepEqual(await readDetection(home), detection);
});

test('a detection file that is not version 1 or not well formed reads as missing, and bad shells are dropped', () => {
  assert.equal(parseDetection(undefined), undefined);
  assert.equal(parseDetection('nope'), undefined);
  assert.equal(parseDetection('{"version": 2, "detectedAt": "x", "terminals": [], "shells": []}'), undefined);
  const good = { path: 'C:/p/pwsh.exe', label: 'PowerShell 7.5.2 (MSI)', version: '7.5.2', source: 'msi' };
  const parsed = parseDetection(JSON.stringify({ version: 1, detectedAt: 'x', platform: 'win32', terminals: [], shells: [good, { path: 1 }, { ...good, source: 'other' }] }));
  assert.deepEqual(parsed?.shells, [good]);
});

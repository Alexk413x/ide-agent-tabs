import assert from 'node:assert/strict';
import { existsSync, mkdirSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import { tempDir } from './tempDir.js';
import { isLoopbackUrl, isProcessAlive, parseEndpoint, readRegistry } from '../src/registry.js';

const entry = (over: Record<string, unknown> = {}) =>
  JSON.stringify({
    protocol: 1,
    ide: 'jetbrains',
    product: 'Android Studio',
    version: '2026.2.2',
    pid: 12345,
    url: 'http://127.0.0.1:63342/ide-agent-tabs/',
    token: 'a'.repeat(64),
    ...over,
  });

test('parses a registry entry and names it after the file', () => {
  const parsed = parseEndpoint(entry(), '/h/endpoints/jetbrains-12345.json', 42);
  assert.equal(parsed.kind, 'endpoint');
  if (parsed.kind !== 'endpoint') return;
  assert.deepEqual(parsed.endpoint, {
    id: 'jetbrains-12345',
    file: '/h/endpoints/jetbrains-12345.json',
    ide: 'jetbrains',
    product: 'Android Studio',
    version: '2026.2.2',
    pid: 12345,
    url: 'http://127.0.0.1:63342/ide-agent-tabs',
    token: 'a'.repeat(64),
    startedAt: 42,
  });
  const vscode = parseEndpoint(entry({ ide: 'vscode' }), 'vscode-7-2.json', 0);
  assert.equal(vscode.kind === 'endpoint' && vscode.endpoint.id, 'vscode-7-2');
});

test('skips unknown protocols quietly and bad entries with a warning', () => {
  assert.deepEqual(parseEndpoint(entry({ protocol: 2 }), 'x.json', 0), { kind: 'skip', reason: 'x.json uses protocol 2', warn: false });
  for (const text of ['nope', '[]', entry({ pid: 'x' }), entry({ pid: -1 }), entry({ token: '' }), entry({ url: 'http://example.com/x' })]) {
    const parsed = parseEndpoint(text, 'x.json', 0);
    assert.equal(parsed.kind, 'skip', text);
    assert.equal(parsed.kind === 'skip' && parsed.warn, true, text);
  }
});

test('only loopback URLs receive the token', () => {
  assert.ok(isLoopbackUrl('http://127.0.0.1:1/x'));
  assert.ok(isLoopbackUrl('http://localhost:1/x'));
  assert.ok(isLoopbackUrl('http://[::1]:1/x'));
  assert.ok(!isLoopbackUrl('http://127.0.0.1.evil.com/x'));
  assert.ok(!isLoopbackUrl('file:///etc/passwd'));
  assert.ok(!isLoopbackUrl('not a url'));
});

test('a live pid counts as alive, including one we may not signal', () => {
  assert.ok(isProcessAlive(process.pid));
  assert.ok(!isProcessAlive(2 ** 30));
});

test('reading the registry drops and deletes dead entries and keeps live ones', async () => {
  const home = tempDir('iat-reg-');
  const dir = path.join(home, 'endpoints');
  mkdirSync(dir);
  writeFileSync(path.join(dir, 'jetbrains-1.json'), entry({ pid: 1 }));
  writeFileSync(path.join(dir, 'jetbrains-2.json'), entry({ pid: 2 }));
  writeFileSync(path.join(dir, 'future-3.json'), entry({ pid: 3, protocol: 9 }));
  writeFileSync(path.join(dir, 'broken-4.json'), '{');
  writeFileSync(path.join(dir, 'jetbrains-5.json.tmp'), entry({ pid: 5 }));
  const registry = await readRegistry(home, (pid) => pid === 2);
  assert.deepEqual(registry.endpoints.map((e) => e.id), ['jetbrains-2']);
  assert.equal(registry.warnings.length, 1);
  assert.ok(!existsSync(path.join(dir, 'jetbrains-1.json')));
  assert.ok(existsSync(path.join(dir, 'future-3.json')));
  assert.deepEqual(await readRegistry(path.join(home, 'absent')), { endpoints: [], warnings: [] });
});

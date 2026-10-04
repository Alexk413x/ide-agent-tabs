import assert from 'node:assert/strict';
import os from 'node:os';
import path from 'node:path';
import { test } from 'node:test';
import { ConfigError } from '../src/profiles.js';
import { validateOpen } from '../src/request.js';

const dir = os.tmpdir();

test('open requests follow the IDE rules', () => {
  const ok = validateOpen({ path: dir, prompt: '  ', args: ['--yolo'], env: { A: 'b' }, agent: 'codex' });
  assert.deepEqual(ok, { path: path.normalize(dir), agent: 'codex', args: ['--yolo'], env: { A: 'b' } });
  assert.equal(validateOpen({ path: dir, prompt: 'hi' }).prompt, 'hi');

  const bad: Parameters<typeof validateOpen>[0][] = [
    { path: '' },
    { path: 'relative/dir' },
    { path: path.join(dir, 'surely-absent-iat-dir') },
    { path: dir, prompt: 'x'.repeat(30_001) },
    { path: dir, prompt: 'a\0b' },
    { path: dir, args: Array(65).fill('a') },
    { path: dir, args: ['x'.repeat(30_001)] },
    { path: dir, args: ['a\0'] },
    { path: dir, env: { IDE_AGENT_TABS_ID: 'x' } },
    { path: dir, agent: ' ' },
    { path: dir, ide: '' },
    { path: dir, focus: 'yes' as unknown as boolean },
  ];
  for (const input of bad) assert.throws(() => validateOpen(input), ConfigError, JSON.stringify(input).slice(0, 80));
});

test('focus passes through only when the caller gives it', () => {
  assert.equal('focus' in validateOpen({ path: dir }), false);
  assert.equal(validateOpen({ path: dir, focus: true }).focus, true);
  assert.equal(validateOpen({ path: dir, focus: false }).focus, false);
});

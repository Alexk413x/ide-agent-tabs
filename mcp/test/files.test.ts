import assert from 'node:assert/strict';
import { existsSync, readFileSync, utimesSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import { LOCK_STALE_MS, LOCK_WAIT_MS, withFileLock } from '../src/files.js';
import { tempDir } from './tempDir.js';

const DEAD_PID = 2 ** 30;

test('a waiter outlasts the stale limit, so it can take over an orphaned lock', () => {
  assert.ok(LOCK_WAIT_MS > LOCK_STALE_MS);
});

test('a lock whose owner died is taken over at once', async () => {
  const file = path.join(tempDir('iat-lock-'), 'x');
  writeFileSync(`${file}.lock`, `${DEAD_PID} 0123456789abcdef`);
  const started = Date.now();
  assert.equal(await withFileLock(file, async () => 'ran', { timeoutMs: 2_000 }), 'ran');
  assert.ok(Date.now() - started < 1_000);
  assert.ok(!existsSync(`${file}.lock`));
});

test('a fresh lock held by a live process is waited on, not broken', async () => {
  const file = path.join(tempDir('iat-lock-'), 'x');
  writeFileSync(`${file}.lock`, `${process.pid} 0123456789abcdef`);
  await assert.rejects(withFileLock(file, async () => 'ran', { timeoutMs: 200 }), /timed out/);
  assert.equal(readFileSync(`${file}.lock`, 'utf8'), `${process.pid} 0123456789abcdef`);
});

test('a lock past the stale limit is broken even when its pid looks alive', async () => {
  const file = path.join(tempDir('iat-lock-'), 'x');
  writeFileSync(`${file}.lock`, `${process.pid} 0123456789abcdef`);
  const old = new Date(Date.now() - LOCK_STALE_MS - 1_000);
  utimesSync(`${file}.lock`, old, old);
  assert.equal(await withFileLock(file, async () => 'ran', { timeoutMs: 2_000 }), 'ran');
});

test('releasing leaves alone a lock that another owner holds by then', async () => {
  const file = path.join(tempDir('iat-lock-'), 'x');
  await withFileLock(file, async () => {
    writeFileSync(`${file}.lock`, `${process.pid} fedcba9876543210`);
  });
  assert.equal(readFileSync(`${file}.lock`, 'utf8'), `${process.pid} fedcba9876543210`);
});

test('the lock names its owner while held and is gone after', async () => {
  const file = path.join(tempDir('iat-lock-'), 'x');
  const held = await withFileLock(file, async () => readFileSync(`${file}.lock`, 'utf8'));
  assert.match(held, new RegExp(`^${process.pid} [0-9a-f]{16}$`));
  assert.ok(!existsSync(`${file}.lock`));
});

test('concurrent holders never overlap', async () => {
  const file = path.join(tempDir('iat-lock-'), 'x');
  let inside = 0;
  let most = 0;
  await Promise.all(
    Array.from({ length: 8 }, () =>
      withFileLock(file, async () => {
        inside++;
        most = Math.max(most, inside);
        await new Promise((r) => setTimeout(r, 5));
        inside--;
      }),
    ),
  );
  assert.equal(most, 1);
});

import assert from 'node:assert/strict';
import { spawn, type ChildProcess } from 'node:child_process';
import { existsSync, mkdirSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { test, type TestContext } from 'node:test';
import { fileURLToPath } from 'node:url';
import { query, SCHEMA_VERSION } from '../src/messaging/db.js';
import { claimBatch, MAX_SENT_PER_MINUTE, MAX_UNREAD, peekUnread, sendMessage, settleClaim, waitForMessage, CLAIM_TIMEOUT_MS } from '../src/messaging/store.js';
import { tempDir } from './tempDir.js';

const WORKER = fileURLToPath(new URL('./stressWorker.ts', import.meta.url));
const LONG = { timeout: 180_000 };
// The shared-server case sends every message in one burst, far above the 20-a-minute cap. On a Windows
// laptop it measured a 130 ms median send and a 200-300 ms event-loop p99; these bounds only catch a
// regression and leave room for slower CI runners.
const BURST_SEND_MS = 1_000;
const BURST_LOOP_DELAY_MS = 1_500;

interface Worker {
  child: ChildProcess;
  line(prefix: string): Promise<string>;
  go(): void;
  result<T>(): Promise<T>;
}

function worker(mode: string, args: Record<string, unknown>): Worker {
  const child = spawn(process.execPath, ['--import', 'tsx', WORKER, mode, JSON.stringify(args)], { stdio: ['pipe', 'pipe', 'inherit'] });
  const lines: string[] = [];
  const waiting: { prefix: string; resolve: (line: string) => void }[] = [];
  let buffer = '';
  const deliver = () => {
    for (let i = 0; i < waiting.length; i++) {
      const at = lines.findIndex((l) => l.startsWith(waiting[i]!.prefix));
      if (at === -1) continue;
      const [line] = lines.splice(at, 1);
      waiting.splice(i--, 1)[0]!.resolve(line!);
    }
  };
  child.stdout!.setEncoding('utf8');
  child.stdout!.on('data', (chunk: string) => {
    buffer += chunk;
    const parts = buffer.split('\n');
    buffer = parts.pop()!;
    lines.push(...parts.filter((p) => p !== ''));
    deliver();
  });
  const closed = new Promise<void>((resolve) => child.once('close', () => resolve()));
  const line = (prefix: string) =>
    new Promise<string>((resolve, reject) => {
      waiting.push({ prefix, resolve });
      deliver();
      void closed.then(() => reject(new Error(`${mode} worker exited before printing ${prefix}`)));
    });
  return {
    child,
    line,
    go: () => child.stdin!.write('go\n'),
    result: async <T>() => JSON.parse(await line('{')) as T,
  };
}

async function startTogether(workers: Worker[]): Promise<void> {
  await Promise.all(workers.map((w) => w.line('ready')));
  for (const w of workers) w.go();
}

const percentile = (values: number[], p: number) => {
  const sorted = [...values].sort((a, b) => a - b);
  return sorted[Math.min(sorted.length - 1, Math.floor((p / 100) * sorted.length))] ?? NaN;
};
const ms = (n: number) => `${n.toFixed(1)} ms`;
const report = (t: TestContext, label: string, values: number[]) =>
  t.diagnostic(`${label}: n ${values.length}, p50 ${ms(percentile(values, 50))}, p99 ${ms(percentile(values, 99))}, max ${ms(Math.max(...values))}`);

test('12 processes sending to each other and reading between sends lose nothing and read nothing twice', LONG, async (t) => {
  const home = tempDir('iat-stress-');
  const ids = Array.from({ length: 12 }, (_, i) => `p-${String(i).padStart(2, '0')}`);
  const workers = ids.map((me, i) => worker('mesh', { home, me, peers: ids.map((_, k) => ids[(i + 1 + k) % ids.length]!).slice(0, 11), count: 15, expected: 15 }));
  await startTogether(workers);
  const results = await Promise.all(workers.map((w) => w.result<{ sent: { id: string; to: string }[]; read: { id: string; to: string }[]; pairs: number[] }>()));
  const sent = results.flatMap((r) => r.sent);
  const read = results.flatMap((r, i) => r.read.map((m) => ({ ...m, by: ids[i]! })));
  assert.equal(sent.length, 12 * 15);
  assert.equal(read.length, sent.length);
  assert.equal(new Set(read.map((m) => m.id)).size, read.length, 'no message is read twice');
  const to = new Map(sent.map((m) => [m.id, m.to]));
  for (const m of read) assert.equal(to.get(m.id), m.by, 'each message is read by its recipient');
  assert.equal(await query(home, (db) => Number(db.prepare("SELECT count(*) AS n FROM messages WHERE state <> 'read'").get()!.n)), 0);
  report(t, 'send plus read', results.flatMap((r) => r.pairs));
});

test('six processes reading one mailbox while two send to it each take disjoint messages', LONG, async () => {
  const home = tempDir('iat-stress-');
  const stop = path.join(home, 'stop');
  const readers = Array.from({ length: 6 }, () => worker('readLoop', { home, target: 'tab-t', stop }));
  const senders = ['tab-s1', 'tab-s2'].map((from) => worker('send', { home, from, targets: ['tab-t'], texts: Array.from({ length: MAX_SENT_PER_MINUTE }, (_, i) => `${from} ${i}`) }));
  await startTogether([...readers, ...senders]);
  const sent = (await Promise.all(senders.map((w) => w.result<{ results: { id?: string; error?: string }[] }>()))).flatMap((r) => r.results);
  assert.deepEqual(sent.filter((r) => r.error), []);
  writeFileSync(stop, '');
  const reads = (await Promise.all(readers.map((w) => w.result<{ ids: string[] }>()))).map((r) => r.ids);
  const all = reads.flat();
  assert.equal(new Set(all).size, all.length, 'no two readers took one message');
  assert.deepEqual(new Set(all), new Set(sent.map((r) => r.id!)));
});

test('the rate limit holds across two processes that send as one session', LONG, async () => {
  const home = tempDir('iat-stress-');
  const texts = (p: string) => Array.from({ length: 15 }, (_, i) => `${p} ${i}`);
  const workers = ['a', 'b'].map((p) => worker('send', { home, from: 'tab-a', targets: ['tab-b', 'tab-c', 'tab-d'], texts: texts(p) }));
  await startTogether(workers);
  const results = (await Promise.all(workers.map((w) => w.result<{ results: { id?: string; error?: string }[] }>()))).flatMap((r) => r.results);
  assert.equal(results.filter((r) => r.id).length, MAX_SENT_PER_MINUTE);
  assert.equal(results.filter((r) => /20 messages in the last minute/.test(r.error ?? '')).length, 30 - MAX_SENT_PER_MINUTE);
  await assert.rejects(sendMessage(home, { from: { id: 'tab-a', agent: 'codex', path: '/' }, to: 'tab-b', text: 'now' }), /20 messages in the last minute/);
  await sendMessage(home, { from: { id: 'tab-a', agent: 'codex', path: '/' }, to: 'tab-b', text: 'later' }, Date.now() + 61_000);
});

test('four processes sending the same text to one recipient at once store it once', LONG, async () => {
  const home = tempDir('iat-stress-');
  const workers = Array.from({ length: 4 }, () => worker('send', { home, from: 'tab-a', targets: ['tab-b'], texts: ['the same request'] }));
  await startTogether(workers);
  const results = (await Promise.all(workers.map((w) => w.result<{ results: { id: string; duplicate?: boolean }[] }>()))).flatMap((r) => r.results);
  assert.equal(new Set(results.map((r) => r.id)).size, 1);
  assert.equal(results.filter((r) => r.duplicate).length, 3);
  assert.equal((await peekUnread(home, 'tab-b')).length, 1);
});

test('60 sends to one recipient from six processes at once stop at the unread cap', LONG, async () => {
  const home = tempDir('iat-stress-');
  const workers = Array.from({ length: 6 }, (_, i) => worker('send', { home, from: `tab-s${i}`, targets: ['tab-b'], texts: Array.from({ length: 10 }, (_, k) => `${i}.${k}`) }));
  await startTogether(workers);
  const results = (await Promise.all(workers.map((w) => w.result<{ results: { id?: string; error?: string }[] }>()))).flatMap((r) => r.results);
  assert.equal(results.filter((r) => r.id).length, MAX_UNREAD);
  assert.equal(results.filter((r) => /already has 50 unread messages/.test(r.error ?? '')).length, 10);
  assert.equal((await peekUnread(home, 'tab-b')).length, MAX_UNREAD);
});

test('the mod take and read_messages share a mailbox without taking one message twice', LONG, async () => {
  const home = tempDir('iat-stress-');
  const stop = path.join(home, 'stop');
  const sent: string[] = [];
  for (let i = 0; i < 40; i++) sent.push((await sendMessage(home, { from: { id: `s-${String(i).padStart(12, '0')}`, agent: 'codex', path: '/' }, to: 'tab-c', text: `m${i}` })).id);
  const claimer = worker('claimLoop', { home, target: 'tab-c', stop });
  const reader = worker('readLoop', { home, target: 'tab-c', stop });
  await startTogether([claimer, reader]);
  writeFileSync(stop, '');
  const [claimed, read] = await Promise.all([claimer.result<{ ids: string[] }>(), reader.result<{ ids: string[] }>()]);
  assert.deepEqual(claimed.ids.filter((id) => read.ids.includes(id)), []);
  assert.deepEqual(new Set([...claimed.ids, ...read.ids]), new Set(sent));

  await sendMessage(home, { from: { id: 'tab-a', agent: 'codex', path: '/' }, to: 'tab-c', text: 'released' });
  const now = Date.now();
  const held = await claimBatch(home, 'tab-c', 10, undefined, now);
  await settleClaim(home, 'tab-c', held.claim!, 'release', now);
  assert.deepEqual((await peekUnread(home, 'tab-c', now)).map((m) => m.text), ['released']);
  await claimBatch(home, 'tab-c', 10, undefined, now);
  assert.deepEqual(await peekUnread(home, 'tab-c', now + CLAIM_TIMEOUT_MS - 1), []);
  assert.deepEqual((await peekUnread(home, 'tab-c', now + CLAIM_TIMEOUT_MS)).map((m) => m.text), ['released']);
});

test('a process killed inside a write transaction leaves no row, and the next send goes through within a second', LONG, async () => {
  const home = tempDir('iat-stress-');
  await query(home, () => undefined);
  const holder = worker('holdLock', { home });
  await holder.line('locked');
  const exited = new Promise((r) => holder.child.once('close', r));
  holder.child.kill();
  await exited;
  const started = Date.now();
  await sendMessage(home, { from: { id: 'tab-a', agent: 'codex', path: '/' }, to: 'tab-b', text: 'after the crash' });
  assert.ok(Date.now() - started < 1_000, `took ${Date.now() - started} ms`);
  assert.deepEqual((await peekUnread(home, 'tab-b')).map((m) => m.text), ['after the crash']);
});

async function wakeLatencies(home: string, watch: boolean, rounds: number): Promise<number[]> {
  const tag = watch ? 'watched' : 'polled';
  const sender = worker('wakeSend', { home, from: 'tab-a', to: 'tab-w', rounds, delayMs: 200, tag });
  const latencies: number[] = [];
  for (let i = 0; i < rounds; i++) {
    await sender.line('ready');
    const waiting = waitForMessage(home, 'tab-w', {}, 10_000, undefined, { watch });
    await new Promise((r) => setTimeout(r, 50));
    sender.go();
    const message = await waiting;
    const returned = Date.now();
    const committed = Number((await sender.line('sent ')).slice(5));
    assert.equal(message?.text, `wake ${tag} ${i}`);
    latencies.push(returned - committed);
  }
  await sender.result();
  return latencies;
}

test('a waiter in one process wakes soon after a send from another, and within the poll interval with its watch off', LONG, async (t) => {
  const home = tempDir('iat-stress-');
  await query(home, () => undefined);
  const watched = await wakeLatencies(home, true, 10);
  report(t, 'wake latency, watch on', watched);
  assert.ok(Math.max(...watched) < 300, `watch on: ${watched.join(', ')} ms`);
  const polled = await wakeLatencies(home, false, 5);
  report(t, 'wake latency, watch off', polled);
  assert.ok(Math.max(...polled) < 1_500, `watch off: ${polled.join(', ')} ms`);
});

test('eight processes opening a fresh home at once migrate it once and delete the old mail folder', LONG, async () => {
  const home = tempDir('iat-stress-');
  mkdirSync(path.join(home, 'mail', 'tab-b', 'new'), { recursive: true });
  writeFileSync(path.join(home, 'mail', 'tab-b', 'new', '1-m-0123456789abcdef.json'), '{}');
  const workers = Array.from({ length: 8 }, () => worker('open', { home }));
  await startTogether(workers);
  const versions = await Promise.all(workers.map((w) => w.result<{ version: number }>()));
  assert.deepEqual(versions.map((v) => v.version), Array(8).fill(SCHEMA_VERSION));
  assert.ok(!existsSync(path.join(home, 'mail')));
  await sendMessage(home, { from: { id: 'tab-a', agent: 'codex', path: '/' }, to: 'tab-b', text: 'works' });
  assert.equal((await peekUnread(home, 'tab-b')).length, 1);
});

test('one process serving 32 sessions keeps its event loop responsive while 8 processes send to them', LONG, async (t) => {
  const home = tempDir('iat-stress-');
  const sessions = Array.from({ length: 32 }, (_, i) => `sh-${String(i).padStart(2, '0')}`);
  const internal = 5;
  const external = Array.from({ length: 8 }, (_, k) =>
    worker('send', { home, from: `ext-${k}`, targets: Array.from({ length: 20 }, (_, m) => sessions[(k * 20 + m) % 32]!), texts: Array.from({ length: 20 }, (_, m) => `ext ${k}.${m}`) }),
  );
  const server = worker('shared', { home, sessions, internal, expected: internal + 5 });
  await startTogether([server, ...external]);
  const sent = await Promise.all(external.map((w) => w.result<{ results: { id?: string; error?: string; ms: number }[] }>()));
  const shared = await server.result<{ received: Record<string, { id: string }[]>; sends: number[]; loopP99Ms: number; loopMaxMs: number }>();
  const externalSends = sent.flatMap((r) => r.results);
  assert.deepEqual(externalSends.filter((r) => r.error), []);
  const received = Object.values(shared.received).flat().map((m) => m.id);
  assert.equal(received.length, 32 * (internal + 5));
  assert.equal(new Set(received).size, received.length);
  for (const r of externalSends) assert.ok(received.includes(r.id!));
  report(t, 'shared-server sends', shared.sends);
  report(t, 'external sends', externalSends.map((r) => r.ms));
  t.diagnostic(`event loop delay: p99 ${ms(shared.loopP99Ms)}, max ${ms(shared.loopMaxMs)}`);
  assert.ok(percentile(shared.sends, 50) < BURST_SEND_MS, `median shared-server send ${percentile(shared.sends, 50)} ms`);
  assert.ok(percentile(externalSends.map((r) => r.ms), 50) < BURST_SEND_MS);
  assert.ok(shared.loopP99Ms < BURST_LOOP_DELAY_MS, `event loop p99 ${shared.loopP99Ms} ms`);
});

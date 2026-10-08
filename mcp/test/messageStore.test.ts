import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { existsSync, utimesSync, writeFileSync } from 'node:fs';
import { test } from 'node:test';
import { query } from '../src/messaging/db.js';
import {
  claimBatch,
  CLAIM_TIMEOUT_MS,
  cleanStore,
  DEDUPE_MS,
  hasUnread,
  KEEP_MS,
  logNative,
  mailTo,
  MAX_READ_CHARS,
  MAX_SENT_PER_MINUTE,
  MAX_TEXT_CHARS,
  MAX_UNREAD,
  newMessageId,
  peekUnread,
  putBack,
  sendMessage,
  setDelivery,
  settleClaim,
  storedFor,
  takeBatch,
  unreadSummary,
  waitForMessage,
  type Outgoing,
} from '../src/messaging/store.js';
import { wakePath } from '../src/messaging/wake.js';
import { tempDir } from './tempDir.js';

const T0 = Date.parse('2026-10-07T12:00:00Z');
const MINUTE = 60_000;

const out = (to: string, text: string, from = 'tab-a', over: Partial<Outgoing> = {}): Outgoing => ({
  from: { id: from, agent: 'codex', path: `/w/${from}` },
  to,
  text,
  ...over,
});

const texts = (messages: { text: string }[]) => messages.map((m) => m.text);

test('send refuses text over the cap and a recipient that is not a session id', async () => {
  const home = tempDir('iat-store-');
  await assert.rejects(sendMessage(home, out('tab-b', 'x'.repeat(MAX_TEXT_CHARS + 1)), T0), /exceeds 32000 characters/);
  await assert.rejects(sendMessage(home, out('../x', 'hi'), T0), /not a session id/);
  await sendMessage(home, out('tab-b', 'x'.repeat(MAX_TEXT_CHARS)), T0);
  assert.equal((await peekUnread(home, 'tab-b', T0))[0]!.text.length, MAX_TEXT_CHARS);
});

test('the rate limit counts a sender\'s last minute, and a refused send uses no slot', async () => {
  const home = tempDir('iat-store-');
  for (let i = 0; i < MAX_SENT_PER_MINUTE; i++) await sendMessage(home, out(`tab-${i % 3}`, `m${i}`), T0 + i);
  await assert.rejects(sendMessage(home, out('tab-b', 'one more'), T0 + MINUTE - 1), /20 messages in the last minute/);
  await sendMessage(home, out('tab-b', 'one more'), T0 + MINUTE);
  await sendMessage(home, out('tab-b', 'from z', 'tab-z'), T0 + 31);
});

test('the same text to the same recipient within DEDUPE_MS returns the first id', async () => {
  const home = tempDir('iat-store-');
  const first = await sendMessage(home, out('tab-b', 'review this'), T0);
  assert.deepEqual(await sendMessage(home, out('tab-b', 'review this'), T0 + DEDUPE_MS - 1), { id: first.id, duplicate: true });
  assert.notEqual((await sendMessage(home, out('tab-b', 'review this', 'tab-a', { replyTo: 'm-0000000000000001' }), T0 + 2)).id, first.id);
  assert.notEqual((await sendMessage(home, out('tab-c', 'review this'), T0 + 3)).id, first.id);
  const later = await sendMessage(home, out('tab-b', 'review this'), T0 + DEDUPE_MS);
  assert.notEqual(later.id, first.id);
  assert.equal(later.duplicate, undefined);
});

test('a recipient holds at most MAX_UNREAD unread messages; reading frees a slot', async () => {
  const home = tempDir('iat-store-');
  for (let i = 0; i < MAX_UNREAD; i++) await sendMessage(home, out('tab-b', `m${i}`, `s-${String(i).padStart(12, '0')}`), T0);
  await assert.rejects(sendMessage(home, out('tab-b', 'full'), T0), /tab-b already has 50 unread messages/);
  await takeBatch(home, 'tab-b', { count: 1 }, T0);
  await sendMessage(home, out('tab-b', 'fits'), T0);
});

test('a read takes at most the char cap, at least one message, and counts the rest', async () => {
  const home = tempDir('iat-store-');
  for (let i = 0; i < 3; i++) await sendMessage(home, out('tab-b', String(i).repeat(MAX_TEXT_CHARS)), T0 + i);
  const first = await takeBatch(home, 'tab-b', { chars: MAX_READ_CHARS }, T0 + 10);
  assert.deepEqual([first.messages.length, first.remaining, first.unreadable], [1, 2, 0]);
  assert.equal(first.messages[0]!.text[0], '0');
  assert.equal((await peekUnread(home, 'tab-b', T0 + 10)).length, 2);
});

test('a filtered take leaves the skipped messages unread, in order', async () => {
  const home = tempDir('iat-store-');
  const asked = await sendMessage(home, out('tab-b', 'question', 'tab-b-peer'), T0);
  await sendMessage(home, out('tab-a', 'unrelated', 'tab-c'), T0 + 1);
  await sendMessage(home, out('tab-a', 'answer', 'tab-b', { replyTo: asked.id }), T0 + 2);
  await sendMessage(home, out('tab-a', 'other answer', 'tab-c', { replyTo: asked.id }), T0 + 3);
  assert.ok(await hasUnread(home, 'tab-a', { from: 'tab-b' }, T0 + 4));
  assert.ok(!(await hasUnread(home, 'tab-a', { from: 'tab-z' }, T0 + 4)));
  assert.deepEqual(texts((await takeBatch(home, 'tab-a', { filter: { from: 'tab-b', replyTo: asked.id } }, T0 + 4)).messages), ['answer']);
  assert.deepEqual(texts((await takeBatch(home, 'tab-a', { filter: { replyTo: asked.id } }, T0 + 5)).messages), ['other answer']);
  assert.deepEqual(texts(await peekUnread(home, 'tab-a', T0 + 6)), ['unrelated']);
});

test('a claim holds messages until acked or released, and a stale claim returns them', async () => {
  const home = tempDir('iat-store-');
  await sendMessage(home, out('tab-c', 'one'), T0);
  await sendMessage(home, out('tab-c', 'two'), T0 + 1);
  const first = await claimBatch(home, 'tab-c', Infinity, MAX_READ_CHARS, T0 + 2);
  assert.match(first.claim!, /^c-[0-9a-f]{16}$/);
  assert.deepEqual(texts(first.messages), ['one', 'two']);
  assert.deepEqual(await peekUnread(home, 'tab-c', T0 + 3), []);
  assert.deepEqual((await takeBatch(home, 'tab-c', {}, T0 + 3)).messages, [], 'a read does not take held messages');
  assert.equal(await settleClaim(home, 'tab-c', first.claim!, 'release', T0 + 4), 2);
  assert.equal(await settleClaim(home, 'tab-c', first.claim!, 'release', T0 + 4), 0, 'a settled claim is gone');

  const second = await claimBatch(home, 'tab-c', Infinity, MAX_READ_CHARS, T0 + 5);
  assert.equal(await settleClaim(home, 'tab-c', second.claim!, 'ack', T0 + 6), 2);
  assert.deepEqual((await claimBatch(home, 'tab-c', Infinity, MAX_READ_CHARS, T0 + 7)).claim, null);
  assert.equal(await settleClaim(home, 'tab-c', 'not-a-claim', 'ack'), 0);

  await sendMessage(home, out('tab-c', 'three'), T0 + 8);
  const lost = await claimBatch(home, 'tab-c', Infinity, MAX_READ_CHARS, T0 + 9);
  assert.deepEqual(await peekUnread(home, 'tab-c', T0 + 9 + CLAIM_TIMEOUT_MS - 1), []);
  assert.deepEqual(texts(await peekUnread(home, 'tab-c', T0 + 9 + CLAIM_TIMEOUT_MS)), ['three'], 'a stale claim counts as unread');
  const again = await claimBatch(home, 'tab-c', Infinity, MAX_READ_CHARS, T0 + 9 + CLAIM_TIMEOUT_MS);
  assert.deepEqual(texts(again.messages), ['three']);
  assert.notEqual(again.claim, lost.claim);
  assert.equal(await settleClaim(home, 'tab-c', lost.claim!, 'ack', T0 + 9 + CLAIM_TIMEOUT_MS), 0, 'the old claim no longer settles');
  assert.deepEqual(texts(await peekUnread(home, 'tab-c', T0 - 2 * 60_000)), ['three'], 'a claim time far in the future counts as stale');
});

test('put back returns taken messages to unread', async () => {
  const home = tempDir('iat-store-');
  await sendMessage(home, out('tab-b', 'keep me'), T0);
  const batch = await takeBatch(home, 'tab-b', {}, T0);
  await putBack(home, 'tab-b', batch.ids, T0);
  assert.deepEqual(texts(await peekUnread(home, 'tab-b', T0)), ['keep me']);
});

test('the unread summary counts unread messages and names senders newest first', async () => {
  const home = tempDir('iat-store-');
  assert.deepEqual(await unreadSummary(home, 'tab-c', T0), { count: 0, senders: [] });
  await sendMessage(home, out('tab-c', '1', 'tab-a'), T0);
  await sendMessage(home, out('tab-c', '2', 'tab-b'), T0 + 1);
  await sendMessage(home, out('tab-c', '3', 'tab-a'), T0 + 2);
  await sendMessage(home, out('tab-x', 'other', 'tab-d'), T0 + 3);
  assert.deepEqual(await unreadSummary(home, 'tab-c', T0 + 4), { count: 3, senders: ['tab-a', 'tab-b'] });
});

test('history rows join a message with its delivery and state, and native log rows by owner or name', async () => {
  const home = tempDir('iat-store-');
  const sent = await sendMessage(home, out('tab-b', 'hello', 'tab-a', { toName: 'plugins-fa [6a3948]' }), T0);
  await setDelivery(home, sent.id, 'woken');
  await logNative(home, { id: newMessageId(), owner: 'tab-b', direction: 'sent', from: { id: 'tab-b', name: 'plugins-fa [6a3948]' }, to: { name: 'docs-9b [11aa22]' }, text: 'native', sentAt: new Date(T0 + 1).toISOString() });
  const [mail, native] = await storedFor(home, { id: 'tab-b', names: [] });
  assert.deepEqual(
    [mail!.id, mail!.route, mail!.delivery, mail!.state, mail!.from.id, mail!.to.id, mail!.to.name],
    [sent.id, 'agent-tabs', 'woken', 'unread', 'tab-a', 'tab-b', 'plugins-fa [6a3948]'],
  );
  assert.deepEqual([native!.route, native!.owner, native!.direction, native!.to.name], ['native', 'tab-b', 'sent', 'docs-9b [11aa22]']);
  assert.deepEqual((await storedFor(home, { names: ['docs-9b [11aa22]'] })).map((m) => m.text), ['native']);
  assert.deepEqual((await storedFor(home, { names: ['nobody'] })).length, 0);
});

test('mail to a session lists its unread and read messages for the handoff check', async () => {
  const home = tempDir('iat-store-');
  const ask = await sendMessage(home, out('tab-new', 'taking over', 'tab-old'), T0);
  await sendMessage(home, out('tab-new', 'stopped', 'tab-old', { replyTo: ask.id }), T0 + 1);
  await takeBatch(home, 'tab-new', { count: 1 }, T0 + 2);
  assert.deepEqual((await mailTo(home, 'tab-new')).map((m) => [m.text, m.replyTo]), [
    ['taking over', undefined],
    ['stopped', ask.id],
  ]);
});

test('cleanup keeps 7 days: read and native rows age out, unread mail ages out only for a session that is gone', async () => {
  const home = tempDir('iat-store-');
  const old = T0 - KEEP_MS - 1;
  await sendMessage(home, out('live', 'old read'), old);
  await takeBatch(home, 'live', {}, old);
  await sendMessage(home, out('live', 'old unread'), old + 1);
  await sendMessage(home, out('gone', 'old unread', 'tab-q'), old);
  await sendMessage(home, out('gone-recent', 'new unread', 'tab-q'), T0);
  await logNative(home, { id: newMessageId(), owner: 'live', direction: 'received', from: { name: 'p' }, to: { id: 'live' }, text: 'old native', sentAt: new Date(old).toISOString() });
  await logNative(home, { id: newMessageId(), owner: 'live', direction: 'received', from: { name: 'p' }, to: { id: 'live' }, text: 'new native', sentAt: new Date(T0).toISOString() });
  writeFileSync(wakePath(home, 'gone'), '1');
  utimesSync(wakePath(home, 'gone'), new Date(old), new Date(old));
  writeFileSync(wakePath(home, 'live'), '1');
  utimesSync(wakePath(home, 'live'), new Date(old), new Date(old));

  await cleanStore(home, new Set(['live']), T0);
  const left = await query(home, (db) => db.prepare('SELECT to_id, text FROM messages ORDER BY seq').all().map((r) => `${r.to_id}: ${r.text}`));
  assert.deepEqual(left, ['live: old unread', 'gone-recent: new unread', 'live: new native']);
  assert.ok(!existsSync(wakePath(home, 'gone')));
  assert.ok(existsSync(wakePath(home, 'live')));
});

test('a waiter wakes on a send from this process at once, and on a send from another process through the wake file', async () => {
  const home = tempDir('iat-store-');
  const waiting = waitForMessage(home, 'tab-b', {}, 5_000, undefined, { pollMs: 60_000 });
  setTimeout(() => void sendMessage(home, out('tab-b', 'local')), 50);
  const started = Date.now();
  assert.equal((await waiting)?.text, 'local');
  assert.ok(Date.now() - started < 1_000);

  const script = `const { sendMessage } = await import(${JSON.stringify(new URL('../src/messaging/store.ts', import.meta.url).href)});
await new Promise((r) => setTimeout(r, 300));
await sendMessage(${JSON.stringify(home)}, { from: { id: 'tab-z', agent: 'codex', path: '/z' }, to: 'tab-b', text: 'remote' });`;
  const closed = new Promise((r) => spawn(process.execPath, ['--import', 'tsx', '--input-type=module', '-e', script], { stdio: 'inherit' }).once('close', r));
  const remote = await waitForMessage(home, 'tab-b', {}, 20_000, undefined, { pollMs: 60_000 });
  await closed;
  assert.equal(remote?.text, 'remote');
});

test('a waiter whose watch misses still finds the message by polling', async () => {
  const home = tempDir('iat-store-');
  await query(home, () => undefined);
  const script = `const { sendMessage } = await import(${JSON.stringify(new URL('../src/messaging/store.ts', import.meta.url).href)});
await sendMessage(${JSON.stringify(home)}, { from: { id: 'tab-z', agent: 'codex', path: '/z' }, to: 'tab-b', text: 'polled' });`;
  const closed = new Promise((r) => spawn(process.execPath, ['--import', 'tsx', '--input-type=module', '-e', script], { stdio: 'inherit' }).once('close', r));
  const got = await waitForMessage(home, 'tab-b', {}, 20_000, undefined, { watch: false, pollMs: 100 });
  await closed;
  assert.equal(got?.text, 'polled');
});

test('a wait cancelled after it takes a message puts the message back, and one that times out returns nothing', async () => {
  const home = tempDir('iat-store-');
  await sendMessage(home, out('tab-b', 'keep me'));
  const cancel = new AbortController();
  cancel.abort();
  assert.equal(await waitForMessage(home, 'tab-b', {}, 1_000, cancel.signal), undefined);
  assert.deepEqual(texts(await peekUnread(home, 'tab-b')), ['keep me']);
  const started = Date.now();
  assert.equal(await waitForMessage(home, 'tab-b', { from: 'tab-z' }, 300), undefined);
  assert.ok(Date.now() - started >= 250);
});

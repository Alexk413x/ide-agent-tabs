import assert from 'node:assert/strict';
import { writeFileSync } from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js';
import { HISTORY_REPLY_CHARS, PIECE_CHARS, PREVIEW_CHARS, textPiece } from '../src/messaging/history.js';
import { cleanStore, KEEP_MS, MAX_TEXT_CHARS } from '../src/messaging/store.js';
import { Messaging, type Hosts } from '../src/messaging/messaging.js';
import { resolveSettings } from '../src/profiles.js';
import { createServer, MOD_TOOL } from '../src/server.js';
import { Service } from '../src/service.js';
import { unread } from './mail.js';
import { tempDir } from './tempDir.js';

const hosts: Hosts = { findHost: async () => undefined, typeInto: async () => ({ ok: true }) };

function session(home: string, id: string, agent: string, pid: number, now?: () => number) {
  return new Messaging({ home, env: { IDE_AGENT_TABS_ID: id, IDE_AGENT_TABS_AGENT: agent }, pid, cwd: `/w/${id}`, hosts, isAlive: () => true, ...(now ? { now } : {}) });
}

const unreadIds = async (home: string, id: string) => (await unread(home, id)).map((m) => m.id);

test('every send is in the sender\'s history with its delivery', async () => {
  const home = tempDir('iat-hist-');
  const a = session(home, 'tab-a', 'codex', 1);
  const b = session(home, 'tab-b', 'claude', 2);
  await a.start();
  await b.start();
  try {
    const sent = await a.send({ to: 'tab-b', text: 'review x.ts' });
    const [record] = (await a.modHistory({ id: 'tab-a', names: [] })).messages;
    assert.deepEqual(
      { id: record!.id, route: record!.route, from: record!.from.id, to: record!.to.id, text: record!.text, delivery: record!.delivery, direction: record!.direction },
      { id: sent.id, route: 'agent-tabs', from: 'tab-a', to: 'tab-b', text: 'review x.ts', delivery: 'queued', direction: 'sent' },
    );
  } finally {
    a.stopFollowUps();
    a.stopSync();
    b.stopSync();
  }
});

test('history merges sent, received and native traffic oldest first, and marks nothing read', async () => {
  const home = tempDir('iat-hist-');
  let clock = Date.now() - 10 * 60_000;
  const now = () => clock;
  const a = session(home, 'tab-a', 'codex', 1, now);
  const b = session(home, 'tab-b', 'claude', 2, now);
  await a.start();
  await b.start();
  try {
    await b.modPresence({ driver: true, nativeName: 'plugins-fa [6a3948]', state: 'busy' });
    await b.modLog({ direction: 'received', peer: 'docs-9b [11aa22]', text: 'native hello', at: clock - 60_000 });
    const first = await a.send({ to: 'tab-b', text: 'from codex' });
    clock += 60_000;
    const reply = await b.send({ to: 'tab-a', text: 'from claude', replyTo: first.id });
    clock += 60_000;
    await b.modLog({ direction: 'sent', peer: 'docs-9b [11aa22]', text: 'native reply', delivery: 'delivered' });

    const unreadB = await unreadIds(home, 'tab-b');
    const unreadA = await unreadIds(home, 'tab-a');
    const { messages } = await b.modHistory({ id: 'tab-b', names: ['plugins-fa [6a3948]'] });
    assert.deepEqual(
      messages.map((m) => [m.direction, m.peer.id ?? m.peer.name, m.text, m.route]),
      [
        ['received', 'docs-9b [11aa22]', 'native hello', 'native'],
        ['received', 'tab-a', 'from codex', 'agent-tabs'],
        ['sent', 'tab-a', 'from claude', 'agent-tabs'],
        ['sent', 'docs-9b [11aa22]', 'native reply', 'native'],
      ],
    );
    assert.equal(messages[1]!.status, 'unread');
    assert.equal(messages[2]!.replyTo, first.id);
    assert.deepEqual([messages[2]!.delivery, messages[2]!.status], ['queued', 'unread'], 'the sent log knows the delivery, the mailbox the status');
    assert.equal(messages[3]!.delivery, 'delivered');
    assert.equal(new Set(messages.map((m) => m.id)).size, 4, 'a message in a sent log and a mailbox appears once');
    assert.ok(messages.some((m) => m.id === reply.id));

    const fromCodex = (await a.modHistory({ id: 'tab-a', names: [] })).messages;
    assert.deepEqual(fromCodex.map((m) => [m.direction, m.text]), [
      ['sent', 'from codex'],
      ['received', 'from claude'],
    ]);
    const nativePeer = (await b.modHistory({ names: ['docs-9b [11aa22]'] })).messages;
    assert.deepEqual(nativePeer.map((m) => [m.direction, m.text]), [
      ['sent', 'native hello'],
      ['received', 'native reply'],
    ], 'a native peer without Agent Tabs shows the traffic logged by sessions that talked to it');

    assert.deepEqual(await unreadIds(home, 'tab-b'), unreadB, 'history leaves unread messages unread');
    assert.deepEqual(await unreadIds(home, 'tab-a'), unreadA);
  } finally {
    a.stopFollowUps();
    b.stopFollowUps();
    a.stopSync();
    b.stopSync();
  }
});

test('log refuses a bad peer or direction, and history needs a session or a name', async () => {
  const home = tempDir('iat-hist-');
  const b = session(home, 'tab-b', 'claude', 2);
  await b.start();
  await assert.rejects(b.modLog({ direction: 'sent', peer: 'a\nb', text: 'x' }), /peer must be one printable line/);
  await assert.rejects(b.modLog({ direction: 'sideways' as never, peer: 'p', text: 'x' }), /direction/);
  await assert.rejects(b.modHistory({ names: [] }), /needs session or names/);
  await assert.rejects(b.modHistory({ id: '../x', names: [] }), /not a session id/);
  b.stopSync();
});

test('cleanup drops logged native traffic after 7 days, like read mail', async () => {
  const home = tempDir('iat-hist-');
  const b = session(home, 'tab-b', 'claude', 2);
  await b.start();
  const old = Date.now() - KEEP_MS - 60_000;
  await b.modLog({ direction: 'sent', peer: 'p', text: 'old sent', at: old });
  await b.modLog({ direction: 'received', peer: 'p', text: 'old received', at: old });
  await b.modLog({ direction: 'sent', peer: 'p', text: 'new' });
  await cleanStore(home, new Set(['tab-b']));
  assert.deepEqual((await b.modHistory({ id: 'tab-b', names: [] })).messages.map((m) => m.text), ['new']);
  b.stopSync();
});

test('claudeMod comes from config.json: on by default, off when set, a warning otherwise', async () => {
  assert.equal(resolveSettings(undefined, undefined).claudeMod, 'on');
  assert.equal(resolveSettings(undefined, JSON.stringify({ claudeMod: 'off' })).claudeMod, 'off');
  const bad = resolveSettings(undefined, JSON.stringify({ claudeMod: false }));
  assert.equal(bad.claudeMod, 'on');
  assert.match(bad.warnings.join(' '), /claudeMod/);

  const home = tempDir('iat-hist-');
  writeFileSync(path.join(home, 'config.json'), JSON.stringify({ claudeMod: 'off' }));
  const service = new Service({ home, scriptsDir: home, platform: 'linux', env: { PATH: '' }, callIde: async () => ({}), drivers: [] });
  const messaging = new Messaging({ home, env: { IDE_AGENT_TABS_ID: 'tab-c' }, pid: 3, cwd: '/w', hosts: service, isAlive: () => true });
  await messaging.start();
  const [clientSide, serverSide] = InMemoryTransport.createLinkedPair();
  await createServer(service, undefined, messaging).connect(serverSide);
  const client = new Client({ name: 'claude-code', version: '1.0.0' });
  await client.connect(clientSide);
  try {
    const result = (await client.callTool({ name: MOD_TOOL, arguments: { op: 'settings' } })) as { content: { text: string }[] };
    assert.deepEqual(JSON.parse(result.content[0]!.text), { claudeMod: 'off' });
  } finally {
    await client.close();
    messaging.stopSync();
  }
});

test('counts match the history each session shows, and grow with new traffic', async () => {
  const home = tempDir('iat-hist-');
  const a = session(home, 'tab-a', 'codex', 1);
  const b = session(home, 'tab-b', 'claude', 2);
  await a.start();
  await b.start();
  try {
    await b.modPresence({ driver: true, nativeName: 'plugins-fa [6a3948]', state: 'busy' });
    await a.send({ to: 'tab-b', text: 'one' });
    await b.send({ to: 'tab-a', text: 'two' });
    await b.modLog({ direction: 'sent', peer: 'docs-9b [11aa22]', text: 'native', delivery: 'delivered' });
    const whos = [{ id: 'tab-a', names: [] }, { id: 'tab-b', names: ['plugins-fa [6a3948]'] }, { names: ['docs-9b [11aa22]'] }, { names: [] }];
    const { counts } = await b.modCounts(whos);
    const shown = await Promise.all(whos.slice(0, 3).map(async (who) => (await b.modHistory(who)).total));
    assert.deepEqual(counts, [...shown, null]);
    assert.deepEqual(counts, [2, 3, 1, null]);

    await a.send({ to: 'tab-b', text: 'three' });
    assert.deepEqual((await b.modCounts(whos)).counts, [3, 4, 1, null]);
  } finally {
    a.stopFollowUps();
    b.stopFollowUps();
    a.stopSync();
    b.stopSync();
  }
});

test('a long history fits one MCP reply: its full total, text previews of the newest messages, and each message whole on request', async () => {
  const home = tempDir('iat-hist-');
  let clock = Date.now() - 60 * 60_000;
  const a = session(home, 'tab-a', 'codex', 1, () => clock);
  const b = session(home, 'tab-b', 'claude', 2, () => clock);
  await a.start();
  await b.start();
  try {
    for (let i = 0; i < 200; i++) {
      clock += 1_000;
      await b.modLog({ direction: i % 2 ? 'sent' : 'received', peer: 'docs-9b [11aa22]', text: `${i} ${'y'.repeat(3_000)}`, at: clock });
    }
    const who = { id: 'tab-b', names: [] };
    const reply = await b.modHistory(who);
    assert.ok(JSON.stringify(reply).length < HISTORY_REPLY_CHARS, 'the reply stays under the MCP output limit');
    assert.equal(reply.total, 200);
    assert.deepEqual((await b.modCounts([who])).counts, [200], 'the count is the total the reply reports');
    assert.ok(reply.messages.length > 0 && reply.messages.length < 200);
    assert.equal(reply.messages.at(-1)!.text.split(' ')[0], '199', 'the newest messages are the ones kept');
    assert.ok(reply.messages.every((m) => m.text.length === PREVIEW_CHARS && m.textLength > PREVIEW_CHARS));
    const whole = await b.modMessage(who, reply.messages[0]!.id);
    assert.equal(whole.text?.length, reply.messages[0]!.textLength);
    assert.deepEqual([whole.offset, whole.total, whole.message?.text], [0, reply.messages[0]!.textLength, ''], 'the record carries no text of its own');
    assert.deepEqual(await b.modMessage(who, 'm-0000000000000000'), { message: null });
  } finally {
    a.stopFollowUps();
    b.stopFollowUps();
    a.stopSync();
    b.stopSync();
  }
});

test('a message whose JSON outgrows one reply comes back in pieces that each stay under the MCP output limit', async () => {
  const home = tempDir('iat-hist-');
  const b = session(home, 'tab-b', 'claude', 2);
  await b.start();
  try {
    const text = Array.from({ length: MAX_TEXT_CHARS }, (_, i) => (i % 7 === 0 ? '"' : i % 5 === 0 ? String.fromCharCode(10) : String.fromCharCode(97 + (i % 26)))).join('');
    const { id } = await b.modLog({ direction: 'sent', peer: 'docs-9b [11aa22]', text });
    const who = { id: 'tab-b', names: [] };
    let offset = 0;
    let joined = '';
    let pieces = 0;
    while (offset < text.length) {
      const reply = await b.modMessage(who, id, offset);
      assert.ok(JSON.stringify(reply).length < HISTORY_REPLY_CHARS, `piece ${pieces} fits`);
      assert.equal(reply.offset, offset);
      assert.equal(reply.total, MAX_TEXT_CHARS);
      joined += reply.text!;
      offset += reply.text!.length;
      pieces++;
    }
    assert.equal(joined, text);
    assert.ok(pieces >= 2);
  } finally {
    b.stopSync();
  }
});

test('a piece never splits a surrogate pair and shrinks until its JSON fits', () => {
  const emoji = '😀'.repeat(10);
  const piece = textPiece(emoji, 0, 9);
  assert.equal(piece.length % 2, 0);
  assert.ok(JSON.stringify(textPiece('"'.repeat(100), 0, 50)).length <= 50);
  assert.equal(textPiece('abc', 3), '');
  assert.equal(PIECE_CHARS, 30_000);
});

test('history pages older batches by a before cursor, and the totals add up', async () => {
  const home = tempDir('iat-hist-');
  let clock = Date.now() - 60 * 60_000;
  const b = session(home, 'tab-b', 'claude', 2, () => clock);
  await b.start();
  try {
    for (let i = 0; i < 300; i++) {
      clock += 1_000;
      await b.modLog({ direction: 'sent', peer: 'docs-9b [11aa22]', text: `${i} ${'z'.repeat(600)}`, at: clock });
    }
    const who = { id: 'tab-b', names: [] };
    const seen: string[] = [];
    let before: string | undefined;
    let batches = 0;
    for (;;) {
      const reply = await b.modHistory(who, before);
      assert.equal(reply.total, 300);
      seen.unshift(...reply.messages.map((m) => m.text.split(' ')[0]!));
      batches++;
      assert.equal(reply.older, 300 - seen.length);
      if (reply.older === 0) break;
      before = reply.messages[0]!.id;
    }
    assert.ok(batches >= 3);
    assert.deepEqual(seen, Array.from({ length: 300 }, (_, i) => String(i)));
  } finally {
    b.stopSync();
  }
});

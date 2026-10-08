import assert from 'node:assert/strict';
import { existsSync, mkdirSync, readFileSync, rmSync, utimesSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import { MAX_READ_CHARS, MAX_SENT_PER_MINUTE, MAX_TEXT_CHARS, MAX_UNREAD, waitForMessage } from '../src/messaging/store.js';
import { AGY_MAX_WAIT_S, Messaging, type Hosts } from '../src/messaging/messaging.js';
import { runHook } from '../src/messaging/hook.js';
import { unreadReminder, wakeLine } from '../src/messaging/notice.js';
import {
  agentFromClient,
  BUSY_STALE_MS,
  effectiveState,
  liveSessions,
  PRESENCE_BEATS_MISSED,
  presencePath,
  readPresence,
  updatePresence,
  WAKE_TIMEOUT_MS,
  type SessionState,
} from '../src/messaging/sessions.js';
import { deliverTo, newMessageId, readBy, take, unread, type Message } from './mail.js';
import { tempDir } from './tempDir.js';

const DAY = 24 * 60 * 60 * 1000;

let texts = 0;

function message(to: string, over: Partial<Message> = {}): Message {
  return { id: newMessageId(), from: { id: 'tab-a', agent: 'codex', path: '/w' }, to, text: `hello ${texts++}`, sentAt: new Date().toISOString(), ...over };
}

const fromMany = (i: number) => ({ from: { id: `s-${String(i).padStart(12, '0')}`, agent: 'codex', path: '/w' } });

interface Typed {
  id: string;
  host: string;
  text: string;
}

function hosts(typed: Typed[], ok = true, host: string | undefined = 'fake-term'): Hosts {
  return {
    findHost: async () => host,
    typeInto: async (id, h, text) => {
      typed.push({ id, host: h, text });
      return ok ? { ok: true } : { ok: false, reason: 'no input here' };
    },
  };
}

function session(home: string, id: string | undefined, pid: number, over: Partial<ConstructorParameters<typeof Messaging>[0]> = {}) {
  const env: NodeJS.ProcessEnv = id ? { IDE_AGENT_TABS_ID: id, IDE_AGENT_TABS_AGENT: 'codex' } : {};
  return new Messaging({ home, env, pid, cwd: `/w/${id ?? 'x'}`, hosts: hosts([]), isAlive: () => true, ...over });
}

test('maps MCP client names to agent names', () => {
  assert.equal(agentFromClient('claude-code'), 'claude');
  assert.equal(agentFromClient('codex-mcp-client'), 'codex');
  assert.equal(agentFromClient('gemini-cli-mcp-client'), 'gemini');
  assert.equal(agentFromClient('github-copilot-cli'), 'copilot');
  assert.equal(agentFromClient('opencode'), 'opencode');
  assert.equal(agentFromClient('antigravity-client'), 'agy');
  assert.equal(agentFromClient('grok-shell-ide-agent-tabs'), 'grok');
  assert.equal(agentFromClient('qwen-cli-mcp-client-ide-agent-tabs'), 'qwen');
  assert.equal(agentFromClient('goose-cli'), 'goose');
  assert.equal(agentFromClient('pi'), 'pi');
  assert.equal(agentFromClient('pipeline'), 'pipeline', 'pi matches only as the whole name');
  assert.equal(agentFromClient('My Agent!'), 'MyAgent');
  assert.equal(agentFromClient(undefined), 'unknown');
});

test('the wake line and the reminder hold only sanitized names and short ids', () => {
  assert.equal(wakeLine('codex', '0123456789abcdef'), 'Agent Tabs: new message from codex 01234567. Call read_messages.');
  assert.equal(wakeLine('x; rm -rf ~\r\n', 's-00ff00ff00ff'), 'Agent Tabs: new message from xrm-rf s-00ff00. Call read_messages.');
  assert.equal(unreadReminder([]), undefined);
  const two = [message('b'), message('b', { from: { id: 's-1234567890', agent: 'gemini', path: '/' } })];
  assert.equal(unreadReminder(two), 'Agent Tabs: 2 unread messages from codex tab-a, gemini s-123456. read_messages returns them.');
});

test('a message arrives whole, counts as read once taken, and nobody reads it twice', async () => {
  const home = tempDir('iat-mail-');
  const first = await deliverTo(home, message('tab-b', { text: 'x'.repeat(32_000) }));
  await deliverTo(home, message('tab-b'));
  assert.equal((await unread(home, 'tab-b')).length, 2);
  const [a, b] = await Promise.all([take(home, 'tab-b'), take(home, 'tab-b')]);
  assert.equal(a!.length + b!.length, 2);
  assert.equal([...a!, ...b!].find((m) => m.id === first)!.text.length, 32_000);
  assert.deepEqual(await unread(home, 'tab-b'), []);
  assert.equal((await readBy(home, 'tab-b')).length, 2);
});

test('refuses text over 32,000 characters and a full mailbox', async () => {
  const home = tempDir('iat-mail-');
  await assert.rejects(deliverTo(home, message('tab-b', { text: 'x'.repeat(32_001) })), /32000/);
  for (let i = 0; i < MAX_UNREAD; i++) await deliverTo(home, message('tab-b', fromMany(i)));
  await assert.rejects(deliverTo(home, message('tab-b')), /50 unread/);
  await take(home, 'tab-b', {}, 1);
  await deliverTo(home, message('tab-b'));
  await assert.rejects(deliverTo(home, message('../x')), /not a session id/);
});

test('presence files: written at start, stale ones ignored and removed, and deleted by their own server only', async () => {
  const home = tempDir('iat-pres-');
  const alive = new Set([100, 200]);
  const isAlive = (pid: number) => alive.has(pid);
  const a = session(home, 'tab-a', 100, { isAlive });
  await a.start();
  const presence = JSON.parse(readFileSync(presencePath(home, 'tab-a'), 'utf8'));
  assert.deepEqual({ ...presence, startedAt: undefined }, { id: 'tab-a', agent: 'codex', path: '/w/tab-a', pid: 100, startedAt: undefined, state: 'unknown', beatMs: 60_000, mail: 2 });

  const child = session(home, 'tab-a', 200, { isAlive, randomId: () => 's-child0000001' });
  await child.start();
  assert.equal(child.id, 's-child0000001', 'a second live server with the same tab id gets its own id');

  const loose = session(home, undefined, 200, { isAlive, randomId: () => 's-loose0000001' });
  await loose.start();
  assert.equal(loose.id, 's-loose0000001');
  await loose.setClient('gemini-cli-mcp-client');
  assert.equal((await readPresence(home, loose.id))!.agent, 'gemini');
  await a.setClient('gemini-cli-mcp-client');
  assert.equal((await readPresence(home, 'tab-a'))!.agent, 'codex', 'IDE_AGENT_TABS_AGENT wins over the client name');

  writeFileSync(presencePath(home, 'dead'), JSON.stringify({ id: 'dead', agent: 'x', path: '/', pid: 999, startedAt: 't', state: 'idle' }));
  writeFileSync(presencePath(home, 'stub'), JSON.stringify({ id: 'stub', state: 'idle' }));
  assert.deepEqual((await liveSessions(home, isAlive)).map((s) => s.id).sort(), ['s-child0000001', 's-loose0000001', 'tab-a']);
  assert.ok(!existsSync(presencePath(home, 'dead')));
  assert.ok(existsSync(presencePath(home, 'stub')), 'a fresh stub from a hook waits for its server');
  const stubs = tempDir('iat-pres-');
  mkdirSync(path.join(stubs, 'sessions'));
  writeFileSync(presencePath(stubs, 'stub'), JSON.stringify({ id: 'stub', state: 'idle' }));
  await liveSessions(stubs, isAlive, Date.now() + 2 * 60 * 60 * 1000);
  assert.ok(!existsSync(presencePath(stubs, 'stub')), 'a stub whose server never came is removed after an hour');

  child.stopSync();
  assert.ok(existsSync(presencePath(home, 'tab-a')), "a server never deletes another server's presence");
  a.stopSync();
  assert.ok(!existsSync(presencePath(home, 'tab-a')));
});

test('a server keeps the state a hook wrote before it started', async () => {
  const home = tempDir('iat-pres-');
  mkdirSync(path.join(home, 'sessions'));
  writeFileSync(presencePath(home, 'tab-a'), JSON.stringify({ id: 'tab-a', state: 'idle', stateAt: 'then', nudges: 2 }));
  await session(home, 'tab-a', 100).start();
  const p = (await readPresence(home, 'tab-a'))!;
  assert.deepEqual([p.state, p.stateAt, p.nudges, p.pid], ['idle', 'then', 2, 100]);
});

const THREAD = '019a2b3c-4d5e-7f60-8a9b-0c1d2e3f4a5b';

test('a Codex session keeps the tab id of an open tab and records its thread', async () => {
  const home = tempDir('iat-thread-');
  const a = session(home, 'tab-a', 100, { hosts: hosts([], true, 'jetbrains-1') });
  await a.start();
  await Promise.all([a.noteThread(THREAD), a.noteThread(THREAD)]);
  assert.equal(a.id, 'tab-a');
  const p = (await readPresence(home, 'tab-a'))!;
  assert.deepEqual([p.threadId, p.host, p.pid], [THREAD, 'jetbrains-1', 100]);
});

test('a Codex session whose tab id names no open tab becomes codex-<thread>', async () => {
  const home = tempDir('iat-thread-');
  const stale = session(home, 'tab-gone', 100, { hosts: { findHost: async () => undefined, typeInto: async () => ({ ok: true }) } });
  await stale.start();
  await setState(home, 'tab-gone', 'busy');
  await stale.noteThread('not a thread id');
  assert.equal(stale.id, 'tab-gone');
  await stale.noteThread(THREAD);
  assert.equal(stale.id, `codex-${THREAD}`);
  assert.ok(!existsSync(presencePath(home, 'tab-gone')));
  const p = (await readPresence(home, stale.id))!;
  assert.deepEqual([p.id, p.threadId, p.pid, p.state, p.path], [`codex-${THREAD}`, THREAD, 100, 'busy', '/w/tab-gone']);
  await stale.noteThread(THREAD);
  assert.equal(stale.id, `codex-${THREAD}`);
  stale.stopSync();
  assert.ok(!existsSync(presencePath(home, stale.id)));
});

test('a session without a tab id takes codex-<thread> unless another live server holds it', async () => {
  const home = tempDir('iat-thread-');
  const first = session(home, undefined, 100, { randomId: () => 's-first000001' });
  await first.start();
  await first.noteThread(THREAD);
  assert.equal(first.id, `codex-${THREAD}`);
  assert.ok(!existsSync(presencePath(home, 's-first000001')));

  const second = session(home, undefined, 200, { randomId: () => 's-second00001' });
  await second.start();
  await second.noteThread(THREAD);
  assert.equal(second.id, 's-second00001');
  assert.equal((await readPresence(home, 's-second00001'))!.threadId, THREAD);
  assert.equal((await readPresence(home, `codex-${THREAD}`))!.pid, 100);
});

async function until(done: () => boolean, ms: number): Promise<void> {
  const end = Date.now() + ms;
  while (!done() && Date.now() < end) await new Promise((r) => setTimeout(r, 20));
}

async function pair(typed: Typed[], ok = true) {
  const home = tempDir('iat-pair-');
  const a = new Messaging({ home, env: { IDE_AGENT_TABS_ID: 'tab-a', IDE_AGENT_TABS_AGENT: 'codex' }, pid: 1, cwd: '/a', hosts: hosts(typed, ok), isAlive: () => true });
  const b = new Messaging({ home, env: { IDE_AGENT_TABS_ID: 'tab-b', IDE_AGENT_TABS_AGENT: 'claude' }, pid: 2, cwd: '/b', hosts: hosts([]), isAlive: () => true });
  await a.start();
  await b.start();
  return { home, a, b };
}

async function setState(home: string, id: string, state: string) {
  await updatePresence(home, id, (p) => ({ ...p!, state: state as SessionState }));
}

test('a send to a session from a build before the message store fails with the restart advice and stores nothing', async () => {
  const { home, a } = await pair([]);
  await updatePresence(home, 'tab-b', (p) => {
    const { mail: _, ...old } = p!;
    return old;
  });
  await assert.rejects(a.send({ to: 'tab-b', text: 'hi' }), /tab-b runs an older Agent Tabs; restart that session to message it/);
  assert.deepEqual(await unread(home, 'tab-b'), []);
  assert.ok(!existsSync(path.join(home, 'mail')));
  a.stopFollowUps();
});

test('send wakes an idle session with the fixed line, and only queues for any other state', async () => {
  const typed: Typed[] = [];
  const { home, a } = await pair(typed);
  assert.equal((await a.send({ to: 'tab-b', text: 'secret; $(rm -rf /)' })).delivery, 'queued', 'state unknown');
  await setState(home, 'tab-b', 'busy');
  assert.equal((await a.send({ to: 'tab-b', text: 'x' })).delivery, 'queued');
  await setState(home, 'tab-b', 'idle');
  const woken = await a.send({ to: 'tab-b', text: 'secret; `rm -rf /`' });
  assert.equal(woken.delivery, 'woken');
  assert.deepEqual(typed, [{ id: 'tab-b', host: 'fake-term', text: 'Agent Tabs: new message from codex tab-a. Call read_messages.' }]);
  const after = (await readPresence(home, 'tab-b'))!;
  assert.equal(after.state, 'waking', 'a woken session is marked waking so a second message does not type again');
  assert.equal(after.host, 'fake-term');
  assert.equal((await a.send({ to: 'tab-b', text: 'y' })).delivery, 'queued');
  assert.equal(typed.length, 1);
});

test('a queued send keeps retrying the wake-up until the message is read', async () => {
  const typed: Typed[] = [];
  const home = tempDir('iat-pair-');
  const a = new Messaging({ home, env: { IDE_AGENT_TABS_ID: 'tab-a', IDE_AGENT_TABS_AGENT: 'codex' }, pid: 1, cwd: '/a', hosts: hosts(typed), isAlive: () => true, rewakeEveryMs: 20 });
  const b = new Messaging({ home, env: { IDE_AGENT_TABS_ID: 'tab-b', IDE_AGENT_TABS_AGENT: 'claude' }, pid: 2, cwd: '/b', hosts: hosts([]), isAlive: () => true });
  await a.start();
  await b.start();
  const pause = (ms: number) => new Promise((r) => setTimeout(r, ms));
  try {
    assert.equal((await a.send({ to: 'tab-b', text: 'x' })).delivery, 'queued', 'state unknown');
    await pause(80);
    assert.equal(typed.length, 0, 'no wake line while the state allows none');
    await setState(home, 'tab-b', 'idle');
    await until(() => typed.length > 0, 3_000);
    assert.equal(typed.length, 1, 'the retry types once the session is idle');
    assert.equal((await readPresence(home, 'tab-b'))!.state, 'waking');
    await b.read();
    await setState(home, 'tab-b', 'idle');
    await pause(150);
    assert.equal(typed.length, 1, 'a read message ends the retries');
  } finally {
    a.stopFollowUps();
  }
});

test('a failed wake-up leaves the message queued and the session idle', async () => {
  const typed: Typed[] = [];
  const { home, a } = await pair(typed, false);
  await setState(home, 'tab-b', 'idle');
  const r = await a.send({ to: 'tab-b', text: 'x' });
  assert.equal(r.delivery, 'queued');
  assert.match(r.note!, /no input here/);
  assert.equal((await readPresence(home, 'tab-b'))!.state, 'idle');
});

test('a thrown wake-up leaves the message queued and the session idle', async () => {
  const home = tempDir('iat-pair-');
  const a = new Messaging({
    home,
    env: { IDE_AGENT_TABS_ID: 'tab-a', IDE_AGENT_TABS_AGENT: 'codex' },
    pid: 1,
    cwd: '/a',
    hosts: {
      findHost: async () => 'fake-term',
      typeInto: async () => {
        throw new Error('no input here');
      },
    },
    isAlive: () => true,
  });
  const b = new Messaging({ home, env: { IDE_AGENT_TABS_ID: 'tab-b', IDE_AGENT_TABS_AGENT: 'claude' }, pid: 2, cwd: '/b', hosts: hosts([]), isAlive: () => true });
  await a.start();
  await b.start();
  await setState(home, 'tab-b', 'idle');
  const result = await a.send({ to: 'tab-b', text: 'x' });
  assert.equal(result.delivery, 'queued');
  assert.match(result.note!, /no input here/);
  assert.equal((await readPresence(home, 'tab-b'))!.state, 'idle');
});

test('send refuses bad targets and ids; read marks read and wraps the text as untrusted', async () => {
  const { home, a, b } = await pair([]);
  await assert.rejects(a.send({ to: 'tab-a', text: 'x' }), /this session/);
  await assert.rejects(a.send({ to: 'nobody', text: 'x' }), /no live session/);
  await assert.rejects(a.send({ to: '../etc', text: 'x' }), /not a session id/);
  await assert.rejects(a.send({ to: 'tab-b', text: ' ' }), /empty/);
  await assert.rejects(a.send({ to: 'tab-b', text: 'x', replyTo: 'nope' }), /message id/);
  const sent = await a.send({ to: 'tab-b', text: 'please review' });
  await updatePresence(home, 'tab-b', (p) => ({ ...p!, nudges: 2 }));
  const read = await b.read();
  assert.equal((await readPresence(home, 'tab-b'))!.nudges, 0, 'reading resets the turn-end count');
  assert.match(read.notice!, /not from your user/);
  assert.deepEqual(read.messages.map((m) => [m.id, m.text, m.from]), [[sent.id, 'please review', { id: 'tab-a', agent: 'codex', path: '/a' }]]);
  assert.deepEqual(await b.read(), { messages: [] });
});

test('wait_for_message returns at once when a message waits, filters, and times out', async () => {
  const { home, a, b } = await pair([]);
  const first = await a.send({ to: 'tab-b', text: 'one' });
  const started = Date.now();
  const got = await b.wait({ timeout: 30 });
  assert.equal(got.message!.id, first.id);
  assert.ok(Date.now() - started < 1_000);

  const late = b.wait({ timeout: 10, replyTo: first.id });
  await a.send({ to: 'tab-b', text: 'unrelated' });
  setTimeout(() => void a.send({ to: 'tab-b', text: 'answer', replyTo: first.id }), 200);
  const answer = await late;
  assert.equal(answer.message!.text, 'answer');
  assert.equal((await b.read()).messages[0]!.text, 'unrelated', 'a message the filter skipped stays unread');

  const t0 = Date.now();
  assert.deepEqual(await b.wait({ timeout: 1 }), { message: null, timedOut: true, waitedSeconds: 1 });
  assert.ok(Date.now() - t0 >= 900);

  const controller = new AbortController();
  setTimeout(() => controller.abort(), 100);
  const t1 = Date.now();
  assert.equal((await b.wait({ timeout: 60 }, controller.signal)).message, null);
  assert.ok(Date.now() - t1 < 2_000);

  assert.equal(await waitForMessage(home, 'tab-b', { from: 'tab-z' }, 0), undefined);
});

test('a waking session counts as idle again once the wake line had time to start a turn', () => {
  const at = Date.parse('2026-09-29T06:00:00Z');
  const waking = { state: 'waking' as const, stateAt: new Date(at).toISOString() };
  assert.equal(effectiveState(waking, at + WAKE_TIMEOUT_MS - 1), 'waking');
  assert.equal(effectiveState(waking, at + WAKE_TIMEOUT_MS), 'idle');
  assert.equal(effectiveState(waking, at - 1), 'idle');
  assert.equal(effectiveState({ state: 'waking' }, at), 'idle');
  const busy = { state: 'busy' as const, stateAt: new Date(at).toISOString() };
  assert.equal(effectiveState(busy, at + BUSY_STALE_MS - 1), 'busy');
  assert.equal(effectiveState(busy, at + BUSY_STALE_MS), 'idle', 'a turn with no hook for BUSY_STALE_MS was interrupted');
  assert.equal(effectiveState({ state: 'permission', stateAt: busy.stateAt }, at + 10 * BUSY_STALE_MS), 'permission', 'a wake line never answers a permission prompt');
  assert.equal(effectiveState({}, at), 'unknown');
});

test('one read returns at most MAX_READ_CHARS of text and leaves the rest unread', async () => {
  const { home, a, b } = await pair([]);
  for (let i = 0; i < 3; i++) await a.send({ to: 'tab-b', text: String(i).repeat(MAX_TEXT_CHARS) });
  a.stopFollowUps();
  const first = await b.read();
  assert.equal(first.messages.length, Math.max(1, Math.floor(MAX_READ_CHARS / MAX_TEXT_CHARS)));
  assert.equal(first.remaining, 3 - first.messages.length);
  assert.equal((await unread(home, 'tab-b')).length, first.remaining);
  const rest: string[] = [];
  for (let r = await b.read(); r.messages.length; r = await b.read()) rest.push(...r.messages.map((m) => m.text[0]!));
  assert.deepEqual([...first.messages.map((m) => m.text[0]!), ...rest], ['0', '1', '2']);
});

test('a read cancelled before it answers leaves the messages unread', async () => {
  const { home, a, b } = await pair([]);
  await a.send({ to: 'tab-b', text: 'keep me' });
  a.stopFollowUps();
  const cancel = new AbortController();
  cancel.abort();
  await assert.rejects(b.read(cancel.signal), /cancelled/);
  assert.deepEqual((await unread(home, 'tab-b')).map((m) => m.text), ['keep me']);
  assert.equal((await b.read()).messages[0]!.text, 'keep me');
});

test('a wait cancelled after it takes a message puts the message back', async () => {
  const home = tempDir('iat-mail-');
  await deliverTo(home, message('tab-b', { text: 'keep me' }));
  const cancel = new AbortController();
  cancel.abort();
  assert.equal(await waitForMessage(home, 'tab-b', {}, 1_000, cancel.signal), undefined);
  assert.deepEqual((await unread(home, 'tab-b')).map((m) => m.text), ['keep me']);
});

test('a restarted server forgets the busy state and nudges its dead predecessor left behind', async () => {
  const home = tempDir('iat-restart-');
  const old = new Date(Date.now() - 5 * 60_000).toISOString();
  mkdirSync(path.join(home, 'sessions'));
  writeFileSync(
    presencePath(home, 'tab-b'),
    JSON.stringify({ id: 'tab-b', agent: 'claude', path: '/b', pid: 7, startedAt: old, state: 'busy', stateAt: old, nudges: 3, owner: 'gone' }),
  );
  const b = session(home, 'tab-b', 8, { isAlive: (pid) => pid !== 7 });
  await b.start();
  const p = (await readPresence(home, 'tab-b'))!;
  assert.equal(p.state, 'unknown');
  assert.equal(p.nudges, undefined);
  assert.equal(p.owner, undefined);
});

test('a restarted server keeps a state its own agent set just before it started', async () => {
  const home = tempDir('iat-restart-');
  const now = new Date().toISOString();
  mkdirSync(path.join(home, 'sessions'));
  writeFileSync(presencePath(home, 'tab-b'), JSON.stringify({ id: 'tab-b', agent: 'claude', path: '/b', pid: 7, startedAt: now, state: 'busy', stateAt: now }));
  const b = session(home, 'tab-b', 8, { isAlive: (pid) => pid !== 7 });
  await b.start();
  assert.equal((await readPresence(home, 'tab-b'))!.state, 'busy');
});

test('a wake that fails on a cached host finds the host again and types there', async () => {
  const typed: Typed[] = [];
  const home = tempDir('iat-pair-');
  const moving: Hosts = {
    findHost: async () => 'new-ide',
    typeInto: async (id, host, text) => {
      if (host !== 'new-ide') return { ok: false, reason: `no running IDE or terminal with id ${host}` };
      typed.push({ id, host, text });
      return { ok: true };
    },
  };
  const a = session(home, 'tab-a', 1, { hosts: moving });
  const b = session(home, 'tab-b', 2);
  await a.start();
  await b.start();
  await updatePresence(home, 'tab-b', (p) => ({ ...p!, host: 'old-ide', state: 'idle', stateAt: new Date(Date.now() - 10_000).toISOString() }));
  const sent = await a.send({ to: 'tab-b', text: 'x' });
  a.stopFollowUps();
  assert.equal(sent.delivery, 'woken');
  assert.deepEqual(typed.map((t) => t.host), ['new-ide']);
  assert.equal((await readPresence(home, 'tab-b'))!.host, 'new-ide');
});

test('a send that fails to deliver gives its rate slot back', async () => {
  const { home, a } = await pair([]);
  for (let i = 0; i < MAX_UNREAD; i++) await deliverTo(home, message('tab-b', fromMany(i)));
  for (let i = 0; i < MAX_SENT_PER_MINUTE + 1; i++) await assert.rejects(a.send({ to: 'tab-b', text: `x${i}` }), /50 unread/);
  a.stopFollowUps();
});

test('the same message sent again within DEDUPE_MS is not delivered twice', async () => {
  const { home, a } = await pair([]);
  const first = await a.send({ to: 'tab-b', text: 'review this' });
  const again = await a.send({ to: 'tab-b', text: 'review this' });
  const other = await a.send({ to: 'tab-b', text: 'review that' });
  a.stopFollowUps();
  assert.equal(again.id, first.id);
  assert.equal(again.duplicate, true);
  assert.notEqual(other.id, first.id);
  assert.equal((await unread(home, 'tab-b')).length, 2);
});

test('a presence whose heartbeat stopped counts as gone even if its pid is reused', async () => {
  const home = tempDir('iat-beat-');
  mkdirSync(path.join(home, 'sessions'));
  const file = presencePath(home, 'tab-b');
  const now = new Date().toISOString();
  writeFileSync(file, JSON.stringify({ id: 'tab-b', agent: 'claude', path: '/b', pid: 7, startedAt: now, state: 'idle', stateAt: now, beatMs: 1_000 }));
  assert.equal((await liveSessions(home, () => true)).length, 1);
  const old = new Date(Date.now() - PRESENCE_BEATS_MISSED * 1_000 - 1_000);
  utimesSync(file, old, old);
  assert.equal((await liveSessions(home, () => true)).length, 0);
  assert.ok(!existsSync(file));
});

test('a presence from a server without a heartbeat still lives by its pid', async () => {
  const home = tempDir('iat-beat-');
  mkdirSync(path.join(home, 'sessions'));
  const file = presencePath(home, 'tab-b');
  const now = new Date().toISOString();
  writeFileSync(file, JSON.stringify({ id: 'tab-b', agent: 'claude', path: '/b', pid: 7, startedAt: now, state: 'idle', stateAt: now }));
  const old = new Date(Date.now() - DAY);
  utimesSync(file, old, old);
  assert.equal((await liveSessions(home, () => true)).length, 1);
});

test('a server keeps its presence fresh and writes it again if it goes missing', async () => {
  const home = tempDir('iat-beat-');
  const b = session(home, 'tab-b', 2, { heartbeatMs: 20 });
  await b.start();
  const file = presencePath(home, 'tab-b');
  assert.equal((await readPresence(home, 'tab-b'))!.beatMs, 20);
  rmSync(file);
  await new Promise((r) => setTimeout(r, 120));
  b.stopHeartbeat();
  assert.ok(existsSync(file));
});

test('a tab still starting is not typed into, and the follow-up wakes it once it is up', async () => {
  const typed: Typed[] = [];
  const home = tempDir('iat-pair-');
  const a = session(home, 'tab-a', 1, { hosts: hosts(typed), rewakeEveryMs: 30 });
  const b = session(home, 'tab-b', 2);
  await a.start();
  await b.start();
  await updatePresence(home, 'tab-b', (p) => ({ ...p!, host: 'fake-term', state: 'idle', stateAt: new Date(Date.now() + 1_000).toISOString() }));
  try {
    assert.equal((await a.send({ to: 'tab-b', text: 'hi' })).delivery, 'queued');
    assert.equal(typed.length, 0);
    await until(() => typed.length > 0, 10_000);
    assert.equal(typed.length, 1);
  } finally {
    a.stopFollowUps();
  }
});

test('an Antigravity CLI session waits at most AGY_MAX_WAIT_S, inside its 3-minute tool limit', async () => {
  const home = tempDir('iat-agy-');
  const env = { IDE_AGENT_TABS_ID: 'tab-agy', IDE_AGENT_TABS_AGENT: 'agy' };
  const agy = new Messaging({ home, env, pid: 3, cwd: '/a', hosts: hosts([]), isAlive: () => true });
  await agy.start();
  const cancel = new AbortController();
  cancel.abort();
  assert.equal((await agy.wait({ timeout: 600 }, cancel.signal)).waitedSeconds, AGY_MAX_WAIT_S);
  assert.ok(AGY_MAX_WAIT_S < 180);
  agy.stopHeartbeat();
});

test('a session served over HTTP waits at most its cap, and writes its agent pid and start time into presence', async () => {
  const home = tempDir('iat-cap-');
  const env = { IDE_AGENT_TABS_ID: 'tab-http', IDE_AGENT_TABS_AGENT: 'claude' };
  const session = new Messaging({ home, env, pid: 4, pidStart: 1_700_000_000_000, cwd: '/a', hosts: hosts([]), isAlive: () => true, maxWaitS: 240 });
  await session.start();
  const cancel = new AbortController();
  cancel.abort();
  assert.equal((await session.wait({ timeout: 600 }, cancel.signal)).waitedSeconds, 240);
  const presence = await readPresence(home, 'tab-http');
  assert.equal(presence?.pid, 4);
  assert.equal(presence?.pidStart, 1_700_000_000_000);
  session.stopHeartbeat();
});

test('a Claude tab whose turn just ended is not typed into until its prompt has sat idle', async () => {
  const typed: Typed[] = [];
  const home = tempDir('iat-pair-');
  const a = session(home, 'tab-a', 1, { hosts: hosts(typed), rewakeEveryMs: 20 });
  const b = session(home, 'tab-b', 2, { env: { IDE_AGENT_TABS_ID: 'tab-b', IDE_AGENT_TABS_AGENT: 'claude' } });
  await a.start();
  await b.start();
  const at = Date.now() - 10_000;
  await runHook({ cli: 'claude', event: 'Stop', input: {}, home, sessionId: 'tab-b', now: at });
  try {
    assert.equal((await a.send({ to: 'tab-b', text: 'hi' })).delivery, 'queued', 'the user may be typing a new prompt');
    await new Promise((r) => setTimeout(r, 100));
    assert.equal(typed.length, 0);
    await runHook({ cli: 'claude', event: 'Notification', input: { notification_type: 'idle_prompt' }, home, sessionId: 'tab-b', now: at });
    await until(() => typed.length > 0, 3_000);
    assert.equal(typed.length, 1, 'the follow-up wakes it once the prompt is idle');
  } finally {
    a.stopFollowUps();
  }
});

test('a CLI without an input-idle signal is woken as soon as its turn ends', async () => {
  const typed: Typed[] = [];
  const home = tempDir('iat-pair-');
  const a = session(home, 'tab-a', 1, { hosts: hosts(typed) });
  await a.start();
  await session(home, 'tab-b', 2).start();
  await runHook({ cli: 'codex', event: 'Stop', input: {}, home, sessionId: 'tab-b', now: Date.now() - 10_000 });
  assert.equal((await a.send({ to: 'tab-b', text: 'hi' })).delivery, 'woken');
  a.stopFollowUps();
});

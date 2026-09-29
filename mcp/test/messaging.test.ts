import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { existsSync, mkdirSync, readdirSync, readFileSync, utimesSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import {
  cleanMail,
  deliver,
  KEEP_MS,
  mailboxDir,
  MAX_SENT_PER_MINUTE,
  MAX_UNREAD,
  newMessageId,
  reserveSend,
  takeMessages,
  waitForMessage,
  type Message,
} from '../src/messaging/mailbox.js';
import { Messaging, type Hosts } from '../src/messaging/messaging.js';
import { unreadReminder, wakeLine } from '../src/messaging/notice.js';
import { agentFromClient, effectiveState, liveSessions, presencePath, readPresence, WAKE_TIMEOUT_MS } from '../src/messaging/sessions.js';
import { tempDir } from './tempDir.js';

const DAY = 24 * 60 * 60 * 1000;

function message(to: string, over: Partial<Message> = {}): Message {
  return { id: newMessageId(), from: { id: 'tab-a', agent: 'codex', path: '/w' }, to, text: 'hello', sentAt: new Date().toISOString(), ...over };
}

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
  assert.equal(agentFromClient('My Agent!'), 'MyAgent');
  assert.equal(agentFromClient(undefined), 'unknown');
});

test('the wake line and the reminder hold only sanitized names and short ids', () => {
  assert.equal(wakeLine('codex', '0123456789abcdef'), 'Agent Tabs: new message from codex 01234567. Call read_messages.');
  assert.equal(wakeLine('x; rm -rf ~\r\n', 's-00ff00ff00ff'), 'Agent Tabs: new message from xrm-rf s-00ff00. Call read_messages.');
  assert.equal(unreadReminder([]), undefined);
  const two = [message('b'), message('b', { from: { id: 's-1234567890', agent: 'gemini', path: '/' } })];
  assert.equal(unreadReminder(two), 'Agent Tabs: 2 unread messages from codex tab-a, gemini s-123456; call read_messages.');
});

test('a message lands in new/ whole, moves to cur/ when read, and nobody reads it twice', async () => {
  const home = tempDir('iat-mail-');
  const first = message('tab-b', { text: 'x'.repeat(32_000) });
  await deliver(home, first);
  await deliver(home, message('tab-b'));
  const box = mailboxDir(home, 'tab-b');
  assert.deepEqual(readdirSync(path.join(box, 'tmp')), []);
  assert.equal(readdirSync(path.join(box, 'new')).length, 2);
  const [a, b] = await Promise.all([takeMessages(home, 'tab-b'), takeMessages(home, 'tab-b')]);
  assert.equal(a!.length + b!.length, 2);
  assert.equal([...a!, ...b!].find((m) => m.id === first.id)!.text.length, 32_000);
  assert.deepEqual(readdirSync(path.join(box, 'new')), []);
  assert.equal(readdirSync(path.join(box, 'cur')).length, 2);
});

test('refuses text over 32,000 characters and a full mailbox', async () => {
  const home = tempDir('iat-mail-');
  await assert.rejects(deliver(home, message('tab-b', { text: 'x'.repeat(32_001) })), /32000/);
  for (let i = 0; i < MAX_UNREAD; i++) await deliver(home, message('tab-b'));
  await assert.rejects(deliver(home, message('tab-b')), /50 unread/);
  await takeMessages(home, 'tab-b', {}, 1);
  await deliver(home, message('tab-b'));
  await assert.rejects(deliver(home, message('../x')), /not a session id/);
});

test('the send rate limit holds across two processes that share a home', async () => {
  const home = tempDir('iat-rate-');
  const script = `import { reserveSend } from ${JSON.stringify(new URL('../src/messaging/mailbox.ts', import.meta.url).href)};
let ok = 0;
for (let i = 0; i < 15; i++) { try { await reserveSend(${JSON.stringify(home)}, 'tab-a'); ok++; } catch {} }
process.stdout.write(String(ok));`;
  const runOne = () =>
    new Promise<number>((resolve, reject) => {
      const child = spawn(process.execPath, ['--import', 'tsx', '--input-type=module', '-e', script], { stdio: ['ignore', 'pipe', 'inherit'] });
      let out = '';
      child.stdout.on('data', (d) => (out += d));
      child.on('error', reject);
      child.on('close', () => resolve(Number(out)));
    });
  const [a, b] = await Promise.all([runOne(), runOne()]);
  assert.equal(a + b, MAX_SENT_PER_MINUTE);
  await assert.rejects(reserveSend(home, 'tab-a'), /20 messages in the last minute/);
  await reserveSend(home, 'tab-a', Date.now() + 61_000);
});

test('cleanup deletes read messages and stale mailboxes after 7 days', async () => {
  const home = tempDir('iat-clean-');
  await deliver(home, message('live'));
  await deliver(home, message('live'));
  await takeMessages(home, 'live', {}, 1);
  await deliver(home, message('gone'));
  const now = Date.now();
  const old = new Date(now - KEEP_MS - DAY);
  const cur = path.join(mailboxDir(home, 'live'), 'cur');
  for (const name of readdirSync(cur)) utimesSync(path.join(cur, name), old, old);
  writeFileSync(path.join(mailboxDir(home, 'live'), 'tmp', 'left.json'), '{');
  utimesSync(path.join(mailboxDir(home, 'live'), 'tmp', 'left.json'), old, old);
  const gone = mailboxDir(home, 'gone');
  for (const dir of [path.join(gone, 'new'), path.join(gone, 'tmp'), path.join(gone, 'cur'), gone]) {
    for (const name of readdirSync(dir)) utimesSync(path.join(dir, name), old, old);
    utimesSync(dir, old, old);
  }
  mkdirSync(mailboxDir(home, 'fresh'), { recursive: true });

  await cleanMail(home, new Set(['live']), now);
  assert.deepEqual(readdirSync(cur), []);
  assert.deepEqual(readdirSync(path.join(mailboxDir(home, 'live'), 'tmp')), []);
  assert.equal(readdirSync(path.join(mailboxDir(home, 'live'), 'new')).length, 1);
  assert.ok(!existsSync(gone));
  assert.ok(existsSync(mailboxDir(home, 'fresh')));
});

test('presence files: written at start, stale ones ignored and removed, and deleted by their own server only', async () => {
  const home = tempDir('iat-pres-');
  const alive = new Set([100, 200]);
  const isAlive = (pid: number) => alive.has(pid);
  const a = session(home, 'tab-a', 100, { isAlive });
  await a.start();
  const presence = JSON.parse(readFileSync(presencePath(home, 'tab-a'), 'utf8'));
  assert.deepEqual({ ...presence, startedAt: undefined }, { id: 'tab-a', agent: 'codex', path: '/w/tab-a', pid: 100, startedAt: undefined, state: 'unknown' });

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
  await liveSessions(home, isAlive, Date.now() + 2 * 60 * 60 * 1000);
  assert.ok(!existsSync(presencePath(home, 'stub')));

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

async function pair(typed: Typed[], ok = true) {
  const home = tempDir('iat-pair-');
  const a = new Messaging({ home, env: { IDE_AGENT_TABS_ID: 'tab-a', IDE_AGENT_TABS_AGENT: 'codex' }, pid: 1, cwd: '/a', hosts: hosts(typed, ok), isAlive: () => true });
  const b = new Messaging({ home, env: { IDE_AGENT_TABS_ID: 'tab-b', IDE_AGENT_TABS_AGENT: 'claude' }, pid: 2, cwd: '/b', hosts: hosts([]), isAlive: () => true });
  await a.start();
  await b.start();
  return { home, a, b };
}

async function setState(home: string, id: string, state: string) {
  const file = presencePath(home, id);
  writeFileSync(file, JSON.stringify({ ...JSON.parse(readFileSync(file, 'utf8')), state }));
}

test('send wakes an idle session with the fixed line, and only queues for any other state', async () => {
  const typed: Typed[] = [];
  const { home, a } = await pair(typed);
  assert.equal((await a.send({ to: 'tab-b', text: 'secret; $(rm -rf /)' })).delivery, 'queued', 'state unknown');
  await setState(home, 'tab-b', 'busy');
  assert.equal((await a.send({ to: 'tab-b', text: 'x' })).delivery, 'queued');
  await setState(home, 'tab-b', 'idle');
  const woken = await a.send({ to: 'tab-b', text: 'secret; $(rm -rf /)' });
  assert.equal(woken.delivery, 'woken');
  assert.deepEqual(typed, [{ id: 'tab-b', host: 'fake-term', text: 'Agent Tabs: new message from codex tab-a. Call read_messages.' }]);
  const after = (await readPresence(home, 'tab-b'))!;
  assert.equal(after.state, 'waking', 'a woken session is marked waking so a second message does not type again');
  assert.equal(after.host, 'fake-term');
  assert.equal((await a.send({ to: 'tab-b', text: 'x' })).delivery, 'queued');
  assert.equal(typed.length, 1);
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
  const file = presencePath(home, 'tab-b');
  writeFileSync(file, JSON.stringify({ ...JSON.parse(readFileSync(file, 'utf8')), nudges: 2 }));
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
  assert.equal(effectiveState({ state: 'busy', stateAt: new Date(at).toISOString() }, at + 10 * WAKE_TIMEOUT_MS), 'busy');
  assert.equal(effectiveState({}, at), 'unknown');
});

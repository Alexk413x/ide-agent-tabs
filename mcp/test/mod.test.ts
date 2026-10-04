import assert from 'node:assert/strict';
import { readdirSync, utimesSync } from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js';
import { CLAIM_TIMEOUT_MS, deliver, mailboxDir, newMessageId, peekUnread, type Message } from '../src/messaging/mailbox.js';
import { runHook } from '../src/messaging/hook.js';
import { Messaging, MOD_DELIVERY_NOTE, type Hosts } from '../src/messaging/messaging.js';
import { isModDriven, MOD_STALE_MS, readPresence, updatePresence } from '../src/messaging/sessions.js';
import { createServer, MOD_TOOL } from '../src/server.js';
import { Service } from '../src/service.js';
import { tempDir } from './tempDir.js';

function message(to: string, text = 'hello'): Message {
  return { id: newMessageId(), from: { id: 'codex-1a2b', agent: 'codex', path: '/w' }, to, text, sentAt: new Date().toISOString() };
}

function fakeHosts(typed: string[] = [], labels: Record<string, string> = {}, found: string | null = 'fake-term'): Hosts {
  return {
    findHost: async () => found ?? undefined,
    typeInto: async (id) => {
      typed.push(id);
      return { ok: true };
    },
    describeHost: async (host) => labels[host],
  };
}

function session(home: string, id: string, agent: string, pid: number, hosts = fakeHosts(), now?: () => number) {
  return new Messaging({ home, env: { IDE_AGENT_TABS_ID: id, IDE_AGENT_TABS_AGENT: agent }, pid, cwd: `/w/${id}`, hosts, isAlive: () => true, ...(now ? { now } : {}) });
}

async function idle(home: string, id: string, at = Date.now() - 10_000) {
  await updatePresence(home, id, (p) => (p ? { ...p, state: 'idle', stateAt: new Date(at).toISOString(), inputIdle: true } : p));
}

test('the mod claims a tab, reports its state and native name, and gets its mailbox path', async () => {
  const home = tempDir('iat-mod-');
  const claude = session(home, 'tab-c', 'claude', 1);
  await claude.start();
  const reply = await claude.modPresence({ driver: true, nativeName: 'plugins-fa [6a3948]', state: 'busy' });
  assert.deepEqual(reply, { id: 'tab-c', tab: true, driver: true, mailbox: path.join(mailboxDir(home, 'tab-c'), 'new') });
  const p = (await readPresence(home, 'tab-c'))!;
  assert.equal(p.driver, 'mod');
  assert.equal(p.nativeName, 'plugins-fa [6a3948]');
  assert.equal(p.state, 'busy');
  assert.ok(isModDriven(p, Date.now()));
  await claude.modPresence({ driver: false });
  const released = (await readPresence(home, 'tab-c'))!;
  assert.equal(released.driver, undefined);
  assert.equal(released.modBeat, undefined);
  claude.stopSync();
});

test('a session outside a tab never claims the driver', async () => {
  const home = tempDir('iat-mod-');
  const loose = new Messaging({ home, env: {}, pid: 1, cwd: '/w', hosts: fakeHosts(), isAlive: () => true, randomId: () => 's-000000000001' });
  await loose.start();
  const reply = await loose.modPresence({ driver: true, state: 'idle' });
  assert.equal(reply.driver, false);
  assert.equal((await readPresence(home, 's-000000000001'))!.driver, undefined);
  loose.stopSync();
});

test('a mod-driven recipient is never typed into; a stale mod beat falls back to the wake line', async () => {
  const home = tempDir('iat-mod-');
  const typed: string[] = [];
  const codex = session(home, 'tab-x', 'codex', 1, fakeHosts(typed));
  const claude = session(home, 'tab-c', 'claude', 2);
  await codex.start();
  await claude.start();
  try {
    await claude.modPresence({ driver: true, state: 'idle' });
    await idle(home, 'tab-c');
    const sent = await codex.send({ to: 'tab-c', text: 'hi' });
    assert.deepEqual([sent.delivery, sent.note], ['queued', MOD_DELIVERY_NOTE]);
    assert.deepEqual(typed, []);
    await updatePresence(home, 'tab-c', (p) => (p ? { ...p, modBeat: Date.now() - MOD_STALE_MS - 1 } : p));
    await idle(home, 'tab-c');
    assert.equal((await codex.send({ to: 'tab-c', text: 'again' })).delivery, 'woken');
    assert.deepEqual(typed, ['tab-c']);
  } finally {
    codex.stopFollowUps();
    codex.stopSync();
    claude.stopSync();
  }
});

test('the classic hooks do nothing for a mod-driven session and resume with no driver or a stale beat', async () => {
  const home = tempDir('iat-mod-');
  const claude = session(home, 'tab-c', 'claude', 1);
  await claude.start();
  await claude.modPresence({ driver: true, state: 'idle' });
  await deliver(home, message('tab-c'));
  assert.equal(await runHook({ cli: 'claude', event: 'Stop', input: {}, home, sessionId: 'tab-c' }), undefined, 'the mod delivers; the Stop hook keeps no turn open');
  assert.equal(await runHook({ cli: 'claude', event: 'UserPromptSubmit', input: {}, home, sessionId: 'tab-c' }), undefined);
  assert.equal((await readPresence(home, 'tab-c'))!.state, 'idle', 'the hook left the state to the mod');

  await updatePresence(home, 'tab-c', (p) => (p ? { ...p, modBeat: Date.now() - MOD_STALE_MS - 1 } : p));
  const stale = await runHook({ cli: 'claude', event: 'Stop', input: {}, home, sessionId: 'tab-c' });
  assert.match(JSON.stringify(stale), /Agent Tabs kept this turn open/);

  await claude.modPresence({ driver: false });
  await runHook({ cli: 'claude', event: 'UserPromptSubmit', input: {}, home, sessionId: 'tab-c' });
  assert.equal((await readPresence(home, 'tab-c'))!.state, 'busy', 'with no driver the hooks set the state as in 0.6.0');
  claude.stopSync();
});

test('a server that replaces a dead one drops the driver its mod left', async () => {
  const home = tempDir('iat-mod-');
  let alive = true;
  const first = new Messaging({ home, env: { IDE_AGENT_TABS_ID: 'tab-c', IDE_AGENT_TABS_AGENT: 'claude' }, pid: 1, cwd: '/w', hosts: fakeHosts(), isAlive: (pid) => pid !== 1 || alive });
  await first.start();
  await first.modPresence({ driver: true, nativeName: 'old [1]' });
  first.stopHeartbeat();
  alive = false;
  const second = new Messaging({ home, env: { IDE_AGENT_TABS_ID: 'tab-c', IDE_AGENT_TABS_AGENT: 'claude' }, pid: 2, cwd: '/w', hosts: fakeHosts(), isAlive: (pid) => pid !== 1 || alive });
  await second.start();
  const p = (await readPresence(home, 'tab-c'))!;
  assert.deepEqual([p.pid, p.driver, p.nativeName], [2, undefined, undefined]);
  second.stopSync();
});

test('take claims mail at least once: release and a stale claim return it, ack marks it read', async () => {
  const home = tempDir('iat-mod-');
  const claude = session(home, 'tab-c', 'claude', 1);
  await claude.start();
  await deliver(home, message('tab-c', 'one'));
  await deliver(home, message('tab-c', 'two'));

  const first = await claude.modTake(1);
  assert.equal(first.messages.length, 1);
  assert.equal(first.remaining, 1);
  assert.match('notice' in first ? first.notice : '', /not from your user/);
  assert.equal((await peekUnread(home, 'tab-c')).length, 1, 'a claimed message is no longer unread');
  assert.deepEqual(await claude.modSettle(first.claim!, 'release'), { claim: first.claim, released: 1 });
  assert.equal((await peekUnread(home, 'tab-c')).length, 2);

  const both = await claude.modTake();
  assert.deepEqual(both.messages.map((m) => m.text), ['one', 'two']);
  assert.deepEqual(await claude.modSettle(both.claim!, 'ack'), { claim: both.claim, read: 2 });
  assert.equal(readdirSync(path.join(mailboxDir(home, 'tab-c'), 'cur')).length, 2);
  await assert.rejects(claude.modSettle(both.claim!, 'ack'), /no open claim/);
  assert.deepEqual(await claude.modTake(), { claim: null, messages: [] });

  await deliver(home, message('tab-c', 'three'));
  const lost = await claude.modTake();
  const held = path.join(mailboxDir(home, 'tab-c'), 'held');
  const old = new Date(Date.now() - CLAIM_TIMEOUT_MS - 1000);
  for (const name of readdirSync(held)) utimesSync(path.join(held, name), old, old);
  const again = await claude.modTake();
  assert.deepEqual(again.messages.map((m) => m.text), ['three'], 'an unsettled claim returns to unread after the timeout');
  assert.notEqual(again.claim, lost.claim);
  assert.equal(readdirSync(held).length, 1);

  for (const name of readdirSync(held)) utimesSync(path.join(held, name), old, old);
  assert.equal((await peekUnread(home, 'tab-c')).length, 1, 'with the mod gone, the classic hooks see the message again');
  assert.deepEqual((await claude.read()).messages.map((m) => m.text), ['three']);
  claude.stopSync();
});

test('list_sessions rows carry name, route, tab, host and via, in the agent order', async () => {
  const home = tempDir('iat-mod-');
  const labels = { 'jetbrains-1': 'IntelliJ IDEA', 'windows-terminal': 'Windows Terminal' };
  const made = [
    session(home, 'tab-g', 'gemini', 1, fakeHosts([], labels, null)),
    session(home, 'tab-x', 'codex', 2, fakeHosts([], labels, null)),
    session(home, 'tab-c', 'claude', 3, fakeHosts([], labels, null)),
    session(home, 'tab-z', 'zed-agent', 4, fakeHosts([], labels, null)),
    session(home, 'tab-a', 'agy', 5, fakeHosts([], labels, null)),
  ];
  await updatePresence(home, 'tab-c', () => ({ id: 'tab-c', host: 'jetbrains-1', project: 'Plugins', via: 'direct' }));
  await updatePresence(home, 'tab-x', () => ({ id: 'tab-x', host: 'windows-terminal', via: 'ori' }));
  for (const m of made) await m.start();
  await made[2]!.modPresence({ driver: true, nativeName: 'plugins-fa [6a3948]', state: 'idle' });
  const { sessions } = await made[0]!.listSessions();
  assert.deepEqual(sessions.map((s) => s.agent), ['claude', 'codex', 'agy', 'gemini', 'zed-agent']);
  const [claudeRow, codexRow] = sessions;
  assert.deepEqual(
    [claudeRow!.name, claudeRow!.id, claudeRow!.route, claudeRow!.tab, claudeRow!.host, claudeRow!.ide, claudeRow!.via, claudeRow!.state],
    ['plugins-fa [6a3948]', 'tab-c', 'native', 'tab-c', 'IntelliJ IDEA (Plugins)', 'jetbrains-1', 'direct', 'idle'],
  );
  assert.deepEqual([codexRow!.name, codexRow!.route, codexRow!.host, codexRow!.via], ['tab-x', 'agent-tabs', 'Windows Terminal', 'ori']);
  assert.equal(sessions.find((s) => s.agent === 'gemini')!.self, true);

  await updatePresence(home, 'tab-c', (p) => (p ? { ...p, modBeat: Date.now() - MOD_STALE_MS - 1 } : p));
  const stale = (await made[0]!.listSessions()).sessions[0]!;
  assert.deepEqual([stale.name, stale.route], ['tab-c', 'agent-tabs'], 'a stopped mod is reached through Agent Tabs again');
  for (const m of made) m.stopSync();
});

async function connect(home: string, id: string, clientName: string, pid: number) {
  const service = new Service({ home, scriptsDir: home, platform: 'linux', env: { PATH: '' }, callIde: async () => ({}), drivers: [] });
  const messaging = new Messaging({ home, env: { IDE_AGENT_TABS_ID: id }, pid, cwd: `/work/${id}`, hosts: service, isAlive: () => true });
  await messaging.start();
  const [clientSide, serverSide] = InMemoryTransport.createLinkedPair();
  await createServer(service, undefined, messaging).connect(serverSide);
  const client = new Client({ name: clientName, version: '1.0.0' });
  await client.connect(clientSide);
  const call = async (args: Record<string, unknown>) => {
    const result = (await client.callTool({ name: MOD_TOOL, arguments: args })) as { isError?: boolean; content: { text: string }[] };
    const text = result.content[0]!.text;
    return { isError: result.isError === true, text, json: result.isError ? undefined : JSON.parse(text) };
  };
  return { client, call, messaging };
}

test('agent_tabs_mod is offered to Claude Code only, and its ops run over MCP', async () => {
  const home = tempDir('iat-mod-srv-');
  const codex = await connect(home, 'tab-x', 'codex-mcp-client', 101);
  const claude = await connect(home, 'tab-c', 'claude-code', 102);
  try {
    for (let i = 0; i < 50 && (await codex.client.listTools()).tools.some((t) => t.name === MOD_TOOL); i++) await new Promise((r) => setTimeout(r, 20));
    assert.ok(!(await codex.client.listTools()).tools.some((t) => t.name === MOD_TOOL));
    assert.ok((await claude.client.listTools()).tools.some((t) => t.name === MOD_TOOL));

    const presence = await claude.call({ op: 'presence', driver: true, nativeName: 'plugins-fa [6a3948]', state: 'idle' });
    assert.equal(presence.json.driver, true);
    const sent = await claude.call({ op: 'send', to: 'tab-x', text: 'from the mod' });
    assert.equal(sent.isError, false);
    assert.equal((await peekUnread(home, 'tab-x'))[0]!.from.id, 'tab-c');
    assert.match((await claude.call({ op: 'send', to: 'tab-x' })).text, /send needs text/);
    assert.match((await claude.call({ op: 'send', to: 'tab-c', text: 'me' })).text, /to is this session/);

    await deliver(home, message('tab-c', 'for claude'));
    const taken = await claude.call({ op: 'take', max: 5 });
    assert.equal(taken.json.messages[0].text, 'for claude');
    assert.deepEqual((await claude.call({ op: 'ack', claim: taken.json.claim })).json, { claim: taken.json.claim, read: 1 });
    assert.match((await claude.call({ op: 'release', claim: 'c-0000' })).text, /no open claim/);
    const rows = (await claude.call({ op: 'sessions' })).json.sessions;
    assert.deepEqual(rows.map((r: { name: string }) => r.name), ['plugins-fa [6a3948]', 'tab-x']);
  } finally {
    codex.messaging.stopFollowUps();
    claude.messaging.stopFollowUps();
    await codex.client.close();
    await claude.client.close();
    codex.messaging.stopSync();
    claude.messaging.stopSync();
  }
});

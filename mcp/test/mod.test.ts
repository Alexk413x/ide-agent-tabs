import assert from 'node:assert/strict';
import { mkdirSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js';
import { CLAIM_TIMEOUT_MS, MAX_READ_CHARS, MAX_TEXT_CHARS } from '../src/messaging/store.js';
import { runHook } from '../src/messaging/hook.js';
import { parseCodexConfig } from '../src/messaging/codexConfig.js';
import { folderSlug, Messaging, MOD_DELIVERY_NOTE, sessionNames, shortNames, type Hosts } from '../src/messaging/messaging.js';
import { isModDriven, MOD_STALE_MS, readPresence, updatePresence } from '../src/messaging/sessions.js';
import { createServer, MOD_TOOL } from '../src/server.js';
import { Service } from '../src/service.js';
import { deliverTo, newMessageId, readBy, unread, type Message } from './mail.js';
import { tempDir } from './tempDir.js';

let texts = 0;

function message(to: string, text = `hello ${texts++}`): Message {
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

test('the mod claims a tab and reports its state and native name', async () => {
  const home = tempDir('iat-mod-');
  const claude = session(home, 'tab-c', 'claude', 1);
  await claude.start();
  const reply = await claude.modPresence({ driver: true, nativeName: 'plugins-fa [6a3948]', state: 'busy' });
  assert.deepEqual(reply, { id: 'tab-c', tab: true, driver: true });
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
  await deliverTo(home, message('tab-c'));
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
  let clock = Date.now();
  const claude = session(home, 'tab-c', 'claude', 1, fakeHosts(), () => clock);
  await claude.start();
  await deliverTo(home, message('tab-c', 'one'));
  await deliverTo(home, message('tab-c', 'two'));

  const first = await claude.modTake();
  assert.equal(first.messages.length, 2);
  assert.match('notice' in first ? first.notice : '', /not from your user/);
  assert.equal((await unread(home, 'tab-c')).length, 0, 'a claimed message is no longer unread');
  assert.deepEqual(await claude.modSettle(first.claim!, 'release'), { claim: first.claim, released: 2 });
  assert.equal((await unread(home, 'tab-c')).length, 2);

  const both = await claude.modTake();
  assert.deepEqual(both.messages.map((m) => m.text), ['one', 'two']);
  assert.deepEqual(await claude.modSettle(both.claim!, 'ack'), { claim: both.claim, read: 2 });
  assert.equal((await readBy(home, 'tab-c')).length, 2);
  await assert.rejects(claude.modSettle(both.claim!, 'ack'), /no open claim/);
  assert.deepEqual(await claude.modTake(), { claim: null, messages: [] });

  await deliverTo(home, message('tab-c', 'three'));
  const lost = await claude.modTake();
  clock += CLAIM_TIMEOUT_MS + 1000;
  const again = await claude.modTake();
  assert.deepEqual(again.messages.map((m) => m.text), ['three'], 'an unsettled claim returns to unread after the timeout');
  assert.notEqual(again.claim, lost.claim);
  assert.deepEqual(await unread(home, 'tab-c', clock), [], 'the new claim holds it');

  clock += CLAIM_TIMEOUT_MS + 1000;
  assert.equal((await unread(home, 'tab-c', clock)).length, 1, 'with the mod gone, the classic hooks see the message again');
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
  await made[2]!.modPresence({ driver: true, nativeName: 'plugins-fa [6a3948]', state: 'idle', model: 'opus', effort: 'high' });
  const { sessions } = await made[0]!.listSessions();
  assert.deepEqual(sessions.map((s) => s.agent), ['claude', 'codex', 'agy', 'gemini', 'zed-agent']);
  const [claudeRow, codexRow] = sessions;
  assert.deepEqual(
    [claudeRow!.name, claudeRow!.id, claudeRow!.route, claudeRow!.tab, claudeRow!.host, claudeRow!.ide, claudeRow!.via, claudeRow!.state],
    ['plugins-fa [6a3948]', 'tab-c', 'native', 'tab-c', 'IntelliJ IDEA (Plugins)', 'jetbrains-1', 'direct', 'idle'],
  );
  assert.deepEqual([codexRow!.name, codexRow!.route, codexRow!.host, codexRow!.via], ['tab-x-ab', 'agent-tabs', 'Windows Terminal', 'ori']);
  assert.deepEqual(
    [codexRow!.shortName, codexRow!.session, codexRow!.harness, codexRow!.where, codexRow!.folder, codexRow!.model, codexRow!.effort],
    ['tab-x-ab', 'tab-x', 'Codex via OpenRouter', 'Windows Terminal', '/w/tab-x', null, null],
  );
  assert.deepEqual([claudeRow!.shortName, claudeRow!.legacyName, claudeRow!.harness, claudeRow!.where, claudeRow!.model, claudeRow!.effort], ['plugins-fa [6a3948]', 'claude-tabc', 'Claude Code', 'IntelliJ IDEA', 'opus', 'high']);
  assert.equal(sessions.find((s) => s.agent === 'gemini')!.self, true);

  await updatePresence(home, 'tab-c', (p) => (p ? { ...p, modBeat: Date.now() - MOD_STALE_MS - 1 } : p));
  const stale = (await made[0]!.listSessions()).sessions[0]!;
  assert.deepEqual([stale.name, stale.route], ['plugins-fa [6a3948]', 'agent-tabs'], 'a stopped mod is reached through Agent Tabs again');
  assert.equal(stale.nativeName, 'plugins-fa [6a3948]', 'the native name stays so a listing can merge the row');
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
    assert.equal((await unread(home, 'tab-x'))[0]!.from.id, 'tab-c');
    assert.match((await claude.call({ op: 'send', to: 'tab-x' })).text, /send needs text/);
    assert.match((await claude.call({ op: 'send', to: 'tab-c', text: 'me' })).text, /to is this session/);

    await deliverTo(home, message('tab-c', 'for claude'));
    assert.deepEqual((await claude.call({ op: 'unread' })).json, { count: 1, senders: ['codex-1a2b'] });
    const taken = await claude.call({ op: 'take' });
    assert.equal(taken.json.messages[0].text, 'for claude');
    assert.deepEqual((await claude.call({ op: 'ack', claim: taken.json.claim })).json, { claim: taken.json.claim, read: 1 });
    assert.deepEqual((await claude.call({ op: 'unread' })).json, { count: 0, senders: [] });
    assert.match((await claude.call({ op: 'release', claim: 'c-0000' })).text, /no open claim/);
    const rows = (await claude.call({ op: 'sessions' })).json.sessions;
    assert.deepEqual(rows.map((r: { name: string }) => r.name), ['plugins-fa [6a3948]', 'tab-x-ab']);
  } finally {
    codex.messaging.stopFollowUps();
    claude.messaging.stopFollowUps();
    await codex.client.close();
    await claude.client.close();
    codex.messaging.stopSync();
    claude.messaging.stopSync();
  }
});

test('a mod presence call that beats the client name still records the session as Claude', async () => {
  const home = tempDir('iat-mod-race-');
  const m = new Messaging({ home, env: { IDE_AGENT_TABS_ID: 'tab-r' }, pid: 7, cwd: '/r', hosts: { findHost: async () => undefined, typeInto: async () => ({ ok: true }) }, isAlive: () => true });
  await m.start();
  try {
    assert.equal((await readPresence(home, 'tab-r'))!.agent, 'unknown');
    await m.modPresence({ driver: true, nativeName: 'plugins-fa [6a3948]', state: 'idle' });
    assert.equal((await readPresence(home, 'tab-r'))!.agent, 'claude');
  } finally {
    m.stopHeartbeat();
    m.stopSync();
  }
});

test('one take claims every waiting message up to MAX_READ_CHARS, at least one, and leaves the rest', async () => {
  const home = tempDir('iat-mod-take-');
  const m = new Messaging({ home, env: { IDE_AGENT_TABS_ID: 'tab-t', IDE_AGENT_TABS_AGENT: 'claude' }, pid: 9, cwd: '/t', hosts: { findHost: async () => undefined, typeInto: async () => ({ ok: true }) }, isAlive: () => true });
  await m.start();
  try {
    for (let i = 0; i < 12; i++) await deliverTo(home, message('tab-t', `short ${i}`));
    const all = await m.modTake();
    assert.equal(all.messages.length, 12, 'no count limit');
    for (let i = 0; i < 3; i++) await deliverTo(home, message('tab-t', String(i).repeat(MAX_TEXT_CHARS)));
    const capped = await m.modTake();
    assert.equal(capped.messages.length, Math.max(1, Math.floor(MAX_READ_CHARS / MAX_TEXT_CHARS)));
    assert.equal(capped.remaining, 3 - capped.messages.length);
  } finally {
    m.stopHeartbeat();
    m.stopSync();
  }
});

test('short names take four id characters after the agent, more only when two sessions of one agent would collide', () => {
  const names = shortNames([
    { id: 'f99f0a1b-2222-4333-8444-555566667777', agent: 'codex' },
    { id: 'codex-019a2b3c-dead-beef', agent: 'codex' },
    { id: 'codex-019a2b9f-dead-beef', agent: 'codex' },
    { id: 's-019a2b3c4d5e', agent: 'claude' },
    { id: '0bad', agent: 'agy' },
  ]);
  assert.deepEqual([...names.values()], ['codex-f99f', 'codex-019a2b3', 'codex-019a2b9', 'claude-019a', 'agy-0bad']);
});

test('send_message takes a short name as well as the full id', async () => {
  const home = tempDir('iat-mod-');
  const sender = session(home, 'tab-s', 'claude', 1);
  const codex = session(home, 'f99f0a1b-2222-4333-8444-555566667777', 'codex', 2);
  await sender.start();
  await codex.start();
  const byName = await sender.send({ to: 'codex-f99f', text: 'by name' });
  assert.equal(byName.to, 'f99f0a1b-2222-4333-8444-555566667777');
  const byId = await sender.send({ to: 'f99f0a1b-2222-4333-8444-555566667777', text: 'by id' });
  assert.equal(byId.to, 'f99f0a1b-2222-4333-8444-555566667777');
  assert.deepEqual((await unread(home, 'f99f0a1b-2222-4333-8444-555566667777')).map((m) => m.text).sort(), ['by id', 'by name']);
  await assert.rejects(sender.send({ to: 'codex-0000', text: 'x' }), /no live session with id or name codex-0000/);
  await assert.rejects(codex.send({ to: 'codex-f99f', text: 'x' }), /to is this session/);
  for (const m of [sender, codex]) {
    m.stopFollowUps();
    m.stopSync();
  }
});

test('the mod records the model and effort; bad values are refused', async () => {
  const home = tempDir('iat-mod-');
  const claude = session(home, 'tab-c', 'claude', 1);
  await claude.start();
  await claude.modPresence({ model: 'claude-opus-5-5', effort: 'xhigh' });
  const p = (await readPresence(home, 'tab-c'))!;
  assert.deepEqual([p.model, p.effort], ['claude-opus-5-5', 'xhigh']);
  await assert.rejects(claude.modPresence({ model: 'a\nb' }), /model must be one printable line/);
  await assert.rejects(claude.modPresence({ effort: 'very high' }), /effort must be/);
  claude.stopSync();
});

test("a Codex session's server reads its model and effort from config.toml, honouring CODEX_HOME and the profile", async () => {
  assert.deepEqual(parseCodexConfig('model = "gpt-5.5"\nmodel_reasoning_effort = "high" # comment\n[profiles.fast]\nmodel = "x"\n'), { model: 'gpt-5.5', effort: 'high' });
  assert.deepEqual(parseCodexConfig("profile = 'fast'\nmodel = 'gpt-5.5'\n[profiles.fast]\nmodel_reasoning_effort = 'low'\n"), { model: 'gpt-5.5', effort: 'low' });
  assert.deepEqual(parseCodexConfig('[tools]\nmodel = "nested"\n'), {});

  const home = tempDir('iat-mod-');
  const codexHome = tempDir('iat-codex-');
  mkdirSync(codexHome, { recursive: true });
  writeFileSync(path.join(codexHome, 'config.toml'), 'model = "gpt-5.5"\nmodel_reasoning_effort = "medium"\n');
  const env = { IDE_AGENT_TABS_ID: 'tab-x', IDE_AGENT_TABS_AGENT: 'codex', CODEX_HOME: codexHome };
  const codex = new Messaging({ home, env, pid: 1, cwd: '/w', hosts: fakeHosts(), isAlive: () => true });
  await codex.start();
  assert.deepEqual([(await readPresence(home, 'tab-x'))?.model, (await readPresence(home, 'tab-x'))?.effort], ['gpt-5.5', 'medium']);
  codex.stopSync();

  await updatePresence(home, 'tab-y', () => ({ id: 'tab-y', model: 'gpt-5.5-codex' }));
  const opened = new Messaging({ home, env: { ...env, IDE_AGENT_TABS_ID: 'tab-y' }, pid: 2, cwd: '/w', hosts: fakeHosts(), isAlive: () => true });
  await opened.start();
  assert.deepEqual([(await readPresence(home, 'tab-y'))?.model, (await readPresence(home, 'tab-y'))?.effort], ['gpt-5.5-codex', 'medium'], 'the open_tab model wins over the config');
  opened.stopSync();

  const claude = new Messaging({ home, env: { ...env, IDE_AGENT_TABS_ID: 'tab-z', IDE_AGENT_TABS_AGENT: 'claude' }, pid: 3, cwd: '/w', hosts: fakeHosts(), isAlive: () => true });
  await claude.start();
  assert.equal((await readPresence(home, 'tab-z'))?.model, undefined, 'only a Codex session reads the Codex config');
  claude.stopSync();
});

test('where shows the stored IDE product when the endpoint is gone, refreshes it when the tab is re-adopted, and is null when unknown', async () => {
  const home = tempDir('iat-mod-');
  const quiet: Hosts = { findHost: async () => undefined, typeInto: async () => ({ ok: true }), describeHost: async () => undefined };
  const kept = session(home, 'tab-v', 'codex', 1, quiet);
  const bare = session(home, 'tab-u', 'codex', 2, quiet);
  await kept.start();
  await bare.start();
  await updatePresence(home, 'tab-v', (p) => (p ? { ...p, host: 'vscode-1-old', product: 'Visual Studio Code', project: 'proj' } : p));
  await updatePresence(home, 'tab-u', (p) => (p ? { ...p, host: 'vscode-2-gone' } : p));
  const row = async (id: string) => (await kept.listSessions()).sessions.find((s) => s.id === id)!;
  assert.deepEqual([(await row('tab-v')).where, (await row('tab-v')).host], ['Visual Studio Code', 'Visual Studio Code (proj)']);
  assert.deepEqual([(await row('tab-u')).where, (await row('tab-u')).host], [null, null], 'a raw host id is never shown');

  const typed: string[] = [];
  const readopt: Hosts = {
    findHost: async () => 'vscode-3-new',
    typeInto: async (_id, host) => {
      typed.push(host);
      return host === 'vscode-1-old' ? { ok: false, reason: 'gone' } : { ok: true };
    },
    describeHost: async (host) => (host === 'vscode-3-new' ? 'Cursor' : undefined),
  };
  const sender = new Messaging({ home, env: {}, pid: 3, cwd: '/w', hosts: readopt, isAlive: () => true, randomId: () => 's-000000000003' });
  await sender.start();
  await idle(home, 'tab-v');
  assert.equal((await sender.send({ to: 'tab-v', text: 'wake' })).delivery, 'woken');
  assert.deepEqual(typed, ['vscode-1-old', 'vscode-3-new']);
  const after = (await readPresence(home, 'tab-v'))!;
  assert.deepEqual([after.host, after.product], ['vscode-3-new', 'Cursor']);
  for (const m of [kept, bare, sender]) {
    m.stopFollowUps();
    m.stopSync();
  }
});

test('names follow the native style: the folder slug and two id hex characters, longer only on a collision', () => {
  assert.equal(folderSlug('C:\\Users\\me\\Projects\\Plugins'), 'plugins');
  assert.equal(folderSlug('/home/me/The Index (old)/'), 'the-index--old');
  assert.equal(folderSlug('/home/me/a-very-long-folder-name-that-goes-on'), 'a-very-long-folder-name');
  assert.equal(folderSlug('/'), 'session');
  const names = sessionNames([
    { id: 'tab-1', agent: 'claude', path: '/p/plugins', nativeName: 'plugins-82' },
    { id: 's-82aa00000000', agent: 'codex', path: '/q/Plugins' },
    { id: 'codex-c66c0000-dead', agent: 'codex', path: '/p/the-index' },
    { id: 'c6d70000-1111', agent: 'agy', path: '/p/the-index' },
    { id: '45f20000-2222', agent: 'claude', path: '/p/calc' },
  ]);
  assert.deepEqual([...names.values()], ['plugins-82', 'plugins-82a', 'the-index-c66', 'the-index-c6d', 'calc-45']);
  const again = sessionNames([{ id: '45f20000-2222', agent: 'claude', path: '/p/calc' }]);
  assert.equal(again.get('45f20000-2222'), 'calc-45', 'a name depends only on the id and folder when nothing collides');
});

test('send_message takes the native-style name, the legacy short name and the full id', async () => {
  const home = tempDir('iat-mod-');
  const sender = session(home, 'tab-s', 'claude', 1);
  const codex = session(home, 'f99f0a1b-2222-4333-8444-555566667777', 'codex', 2);
  await sender.start();
  await codex.start();
  const row = (await sender.listSessions()).sessions.find((s) => s.agent === 'codex')!;
  assert.deepEqual([row.name, row.shortName, row.legacyName], ['f99f0a1b-2222-4333-8444-f9', 'f99f0a1b-2222-4333-8444-f9', 'codex-f99f']);
  for (const to of [row.name, row.legacyName, row.id]) assert.equal((await sender.send({ to, text: `to ${to}` })).to, row.id);
  for (const m of [sender, codex]) {
    m.stopFollowUps();
    m.stopSync();
  }
});

test('the mod records the agent type and its color, lists them, and refuses a color outside the palette', async () => {
  const home = tempDir('iat-mod-');
  const claude = session(home, 'tab-c', 'claude', 1);
  await claude.start();
  await claude.modPresence({ driver: true, agentType: 'reviewer', agentColor: 'purple' });
  const row = (await claude.listSessions()).sessions[0]!;
  assert.deepEqual([row.agentType, row.agentColor], ['reviewer', 'purple']);
  await assert.rejects(claude.modPresence({ agentColor: 'chartreuse' }), /agentColor must be one of red, blue/);
  await assert.rejects(claude.modPresence({ agentType: 'two words' }), /agentType must be/);
  const plain = session(home, 'tab-d', 'codex', 2);
  await plain.start();
  const other = (await claude.listSessions()).sessions.find((s) => s.id === 'tab-d')!;
  assert.deepEqual([other.agentType, other.agentColor], [null, null]);
  for (const m of [claude, plain]) m.stopSync();
});

import assert from 'node:assert/strict';
import { existsSync, readFileSync, statSync, writeFileSync } from 'node:fs';
import { test } from 'node:test';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js';
import { HANDOFF_TIMEOUT_MS, handoffPath, Handoffs, type HandoffDeps, type HandoffOpen, type HandoffRecord } from '../src/handoff.js';
import { Messaging, type Hosts } from '../src/messaging/messaging.js';
import { readPresence } from '../src/messaging/sessions.js';
import { createServer } from '../src/server.js';
import { Service } from '../src/service.js';
import { tempDir } from './tempDir.js';

const HANDOFF_ID = 'h-0123456789ab';
const OLD = 'tab-old-1';
const NEW = 'tab-new-2';

const fakeHosts: Hosts = {
  findHost: async (id) => (id === OLD || id === NEW ? 'fake-term' : undefined),
  typeInto: async () => ({ ok: true }),
};

function session(home: string, id: string | undefined, pid: number) {
  const env: NodeJS.ProcessEnv = id ? { IDE_AGENT_TABS_ID: id, IDE_AGENT_TABS_AGENT: 'claude' } : {};
  return new Messaging({ home, env, pid, cwd: `/w/${id ?? 'x'}`, hosts: fakeHosts, isAlive: () => true, rewakeEveryMs: 60_000 });
}

function handoffs(home: string, old: Messaging, over: Partial<HandoffDeps> = {}) {
  const opened: HandoffOpen[] = [];
  const h = new Handoffs({
    home,
    env: { IDE_AGENT_TABS_ID: old.id },
    sessionId: () => old.id,
    openTab: async (input) => {
      opened.push(input);
      return { id: NEW, ide: 'fake-term', agent: input.agent ?? 'claude', path: input.path };
    },
    findHost: (id) => fakeHosts.findHost(id),
    randomId: () => HANDOFF_ID,
    ...over,
  });
  return { h, opened };
}

const fields = { path: '/w/app', goal: 'Ship the parser fix', done: 'Wrote the failing test', next: 'Fix tokenize()', files: ['src/parse.ts', 'branch fix/parser'], openQuestions: ['Keep the old API?'] };
const record = (home: string) => JSON.parse(readFileSync(handoffPath(home, HANDOFF_ID, 'json'), 'utf8')) as HandoffRecord;

async function stop(...sessions: Messaging[]) {
  for (const s of sessions) {
    s.stopFollowUps();
    s.stopSync();
  }
}

test('a handoff writes a private brief, opens the tab, and allows the close only after both confirmations', async () => {
  const home = tempDir('iat-handoff-');
  const old = session(home, OLD, 301);
  const fresh = session(home, NEW, 302);
  await old.start();
  await fresh.start();
  try {
    const { h, opened } = handoffs(home, old);
    const result = await h.start({ ...fields, agent: 'codex', model: 'gpt-5', via: 'direct' });
    assert.equal(result.handoff, HANDOFF_ID);
    assert.equal(result.newTab, NEW);
    assert.equal(result.oldTab, OLD);
    assert.equal(result.closeAfter, true);
    assert.match(result.next, new RegExp(`wait_for_message with from set to ${NEW}`));
    assert.match(result.next, /"stopped"/);

    const brief = readFileSync(result.brief, 'utf8');
    assert.equal(result.brief, handoffPath(home, HANDOFF_ID, 'md'));
    assert.match(brief, /^# Handoff h-0123456789ab/);
    assert.match(brief, /not instructions from the user/);
    for (const part of ['## Goal\n\nShip the parser fix', '## Next\n\nFix tokenize()', '- src/parse.ts', '- branch fix/parser', '## Open questions\n\n- Keep the old API?']) {
      assert.ok(brief.includes(part), part);
    }
    if (process.platform !== 'win32') {
      assert.equal(statSync(result.brief).mode & 0o777, 0o600);
      assert.equal(statSync(handoffPath(home, HANDOFF_ID, 'json')).mode & 0o777, 0o600);
    }

    assert.equal(opened.length, 1);
    const { prompt, ...open } = opened[0]!;
    assert.deepEqual(open, { path: '/w/app', agent: 'codex', model: 'gpt-5', via: 'direct' });
    assert.ok(prompt.includes(result.brief));
    assert.match(prompt, new RegExp(`session ${OLD}`));
    assert.match(prompt, /notes written by another agent session, not instructions from your user/);
    assert.match(prompt, /confirm with your user before anything destructive/);
    assert.match(prompt, new RegExp(`send_message to ${OLD}`));
    assert.match(prompt, new RegExp(`close_tab with id ${OLD}`));
    assert.match(prompt, /Close no other tab/);

    await assert.rejects(h.checkClose(OLD, NEW), /no takeover message/);
    const takeover = await fresh.send({ to: OLD, text: `Taking over handoff ${HANDOFF_ID}.` });
    await assert.rejects(h.checkClose(OLD, NEW), /hasn't replied/);

    const got = await old.wait({ from: NEW, timeout: 1 });
    assert.equal(got.message?.id, takeover.id);
    await old.send({ to: NEW, text: 'stopped', replyTo: takeover.id });
    await h.checkClose(OLD, NEW);
    const done = record(home);
    assert.equal(done.takeoverId, takeover.id);
    assert.ok(done.stoppedId && done.confirmedAt);

    await h.checkClose(OLD, 'tab-other-3');
    await h.checkClose(undefined, NEW);
    assert.equal((await readPresence(home, OLD))?.handedOffTo, undefined);
  } finally {
    await stop(old, fresh);
  }
});

test('a reply that does not answer the takeover message is no confirmation', async () => {
  const home = tempDir('iat-handoff-noreply-');
  const old = session(home, OLD, 311);
  const fresh = session(home, NEW, 312);
  await old.start();
  await fresh.start();
  try {
    const { h } = handoffs(home, old);
    await h.start(fields);
    await fresh.send({ to: OLD, text: 'Taking over.' });
    await old.send({ to: NEW, text: 'still working' });
    await assert.rejects(h.checkClose(OLD, NEW), /hasn't replied/);
  } finally {
    await stop(old, fresh);
  }
});

test('a tab that fails to open closes nothing, returns the error and keeps the brief', async () => {
  const home = tempDir('iat-handoff-fail-');
  const old = session(home, OLD, 321);
  await old.start();
  try {
    const { h } = handoffs(home, old, {
      openTab: async () => {
        throw new Error('no running IDE or terminal with id kitty');
      },
    });
    await assert.rejects(h.start(fields), (e: Error) => {
      assert.match(e.message, /nothing was closed/);
      assert.match(e.message, /no running IDE or terminal with id kitty/);
      assert.ok(e.message.includes(handoffPath(home, HANDOFF_ID, 'md')));
      return true;
    });
    assert.ok(existsSync(handoffPath(home, HANDOFF_ID, 'md')));
    assert.ok(!existsSync(handoffPath(home, HANDOFF_ID, 'json')));
    assert.equal((await readPresence(home, OLD))?.handedOffTo, undefined);
  } finally {
    await stop(old);
  }
});

test('a takeover after the confirmation timeout leaves the old tab open', async () => {
  const home = tempDir('iat-handoff-late-');
  const old = session(home, OLD, 331);
  const fresh = session(home, NEW, 332);
  await old.start();
  await fresh.start();
  try {
    const { h } = handoffs(home, old, { now: () => Date.now() - HANDOFF_TIMEOUT_MS - 60_000 });
    const result = await h.start(fields);
    assert.match(result.next, /this tab stays open/);
    const takeover = await fresh.send({ to: OLD, text: 'Taking over.' });
    await old.send({ to: NEW, text: 'stopped', replyTo: takeover.id });
    await assert.rejects(h.checkClose(OLD, NEW), /no takeover message from this session reached .* by /);
    assert.equal(record(home).confirmedAt, undefined);
  } finally {
    await stop(old, fresh);
  }
});

test('with closeAfterHandoff false the old tab stays open and shows as handed off', async () => {
  const home = tempDir('iat-handoff-keep-');
  writeFileSync(`${home}/config.json`, JSON.stringify({ closeAfterHandoff: false }));
  const old = session(home, OLD, 341);
  const fresh = session(home, NEW, 342);
  await old.start();
  await fresh.start();
  try {
    const { h, opened } = handoffs(home, old);
    const result = await h.start(fields);
    assert.equal(result.closeAfter, false);
    assert.match(result.next, /stays open, marked as handed off/);
    assert.match(opened[0]!.prompt, new RegExp(`Don't close the old session's tab ${OLD}`));
    assert.doesNotMatch(opened[0]!.prompt, /call close_tab/);

    assert.equal((await readPresence(home, OLD))?.handedOffTo, NEW);
    const listed = (await fresh.listSessions()).sessions.find((s) => s.id === OLD) as { handedOffTo?: string };
    assert.equal(listed.handedOffTo, NEW);

    const takeover = await fresh.send({ to: OLD, text: 'Taking over.' });
    await old.send({ to: NEW, text: 'stopped', replyTo: takeover.id });
    await assert.rejects(h.checkClose(OLD, NEW), /closeAfterHandoff is off/);
  } finally {
    await stop(old, fresh);
  }
});

test('a session outside a tab hands off but closes nothing', async () => {
  const home = tempDir('iat-handoff-notab-');
  const old = session(home, undefined, 351);
  await old.start();
  try {
    const { h, opened } = handoffs(home, old, { env: {} });
    const result = await h.start({ path: '/w/app', brief: '## Goal\n\nKeep going.' });
    assert.equal(result.oldTab, null);
    assert.equal(result.closeAfter, false);
    assert.match(opened[0]!.prompt, /isn't in an Agent Tabs tab, so close no tab/);
    assert.match(readFileSync(result.brief, 'utf8'), /## Goal\n\nKeep going\./);
    assert.equal((await readPresence(home, old.id))?.handedOffTo, NEW);
  } finally {
    await stop(old);
  }
});

test('a handoff needs a brief, and a bad closeAfterHandoff value warns and closes', async () => {
  const home = tempDir('iat-handoff-input-');
  writeFileSync(`${home}/config.json`, JSON.stringify({ closeAfterHandoff: 'no' }));
  const old = session(home, OLD, 361);
  await old.start();
  try {
    const { h, opened } = handoffs(home, old);
    await assert.rejects(h.start({ path: '/w/app', done: 'everything' }), /give a brief/);
    assert.equal(opened.length, 0);
    const result = await h.start(fields);
    assert.equal(result.closeAfter, true);
    assert.match(result.warning ?? '', /Ignoring closeAfterHandoff/);
  } finally {
    await stop(old);
  }
});

test('the handoff tool is listed with messaging, and close_tab refuses an unconfirmed handoff', async () => {
  const home = tempDir('iat-handoff-srv-');
  const old = session(home, OLD, 371);
  const fresh = session(home, NEW, 372);
  await old.start();
  await fresh.start();
  const service = new Service({ home, scriptsDir: home, platform: 'linux', env: { PATH: '' }, callIde: async () => ({}), drivers: [] });
  const [clientSide, serverSide] = InMemoryTransport.createLinkedPair();
  const failing = new Handoffs({ home, env: {}, sessionId: () => NEW, openTab: (i) => service.openTab(i), findHost: async () => undefined });
  await createServer(service, undefined, fresh, failing).connect(serverSide);
  const client = new Client({ name: 'claude-code', version: '1.0.0' });
  await client.connect(clientSide);
  const call = async (name: string, args: Record<string, unknown> = {}) =>
    (await client.callTool({ name, arguments: args })) as { isError?: boolean; content: { text: string }[] };
  try {
    assert.ok((await client.listTools()).tools.some((t) => t.name === 'handoff'));
    const failed = await call('handoff', { path: home, goal: 'x' });
    assert.ok(failed.isError);
    assert.match(failed.content[0]!.text, /new tab did not open.*nothing was closed/);

    await handoffs(home, old, { randomId: () => 'h-00000000000a' }).h.start(fields);
    const refused = await call('close_tab', { id: OLD });
    assert.ok(refused.isError);
    assert.match(refused.content[0]!.text, /no takeover message/);
  } finally {
    await client.close();
    await stop(old, fresh);
  }
});

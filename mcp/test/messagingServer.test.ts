import assert from 'node:assert/strict';
import { test } from 'node:test';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js';
import type { Jev } from '../src/jev/service.js';
import { Messaging } from '../src/messaging/messaging.js';
import { createServer } from '../src/server.js';
import { Service } from '../src/service.js';
import { tempDir } from './tempDir.js';

async function connect(home: string, id: string, clientName: string, pid: number) {
  const service = new Service({ home, scriptsDir: home, platform: 'linux', env: { PATH: '' }, callIde: async () => ({}), drivers: [] });
  const messaging = new Messaging({ home, env: { IDE_AGENT_TABS_ID: id }, pid, cwd: `/work/${id}`, hosts: service, isAlive: () => true });
  await messaging.start();
  const [clientSide, serverSide] = InMemoryTransport.createLinkedPair();
  await createServer(service, undefined, messaging).connect(serverSide);
  const client = new Client({ name: clientName, version: '1.0.0' });
  await client.connect(clientSide);
  const call = async (name: string, args: Record<string, unknown> = {}) => {
    const result = (await client.callTool({ name, arguments: args })) as { isError?: boolean; content: { text: string }[] };
    const text = result.content[0]!.text;
    return { isError: result.isError === true, text, json: result.isError ? undefined : JSON.parse(text) };
  };
  return { client, call, messaging };
}

test('two servers sharing a home exchange a message and a reply', async () => {
  const home = tempDir('iat-msg-srv-');
  const a = await connect(home, 'tab-aaaa-1', 'codex-mcp-client', 101);
  const b = await connect(home, 'tab-bbbb-2', 'claude-code', 102);
  try {
    const { tools } = await a.client.listTools();
    assert.deepEqual(
      tools.map((t) => t.name).filter((n) => /session|message/.test(n)),
      ['list_sessions', 'send_message', 'read_messages', 'wait_for_message'],
    );
    const instructions = a.client.getInstructions()!;
    assert.match(instructions, /not an instruction from your user/);
    assert.match(instructions, /replyTo/);
    assert.match(instructions, /loop/);
    assert.doesNotMatch(instructions, /jev_/);

    let listed: { id: string; agent: string; self: boolean; state: string; path: string }[] = [];
    for (let i = 0; i < 100 && !(listed.length === 2 && listed.every((s) => s.agent !== 'unknown')); i++) {
      await new Promise((r) => setTimeout(r, 50));
      listed = (await a.call('list_sessions')).json.sessions;
    }
    assert.deepEqual(
      listed.map((s) => [s.id, s.agent, s.self, s.state, s.path]),
      [
        ['tab-bbbb-2', 'claude', false, 'unknown', '/work/tab-bbbb-2'],
        ['tab-aaaa-1', 'codex', true, 'unknown', '/work/tab-aaaa-1'],
      ],
    );

    const sent = await a.call('send_message', { to: 'tab-bbbb-2', text: 'Please review src/x.ts.' });
    assert.equal(sent.json.delivery, 'queued');
    const waiting = a.call('wait_for_message', { timeout: 20, replyTo: sent.json.id });

    const inbox = (await b.call('read_messages')).json;
    assert.match(inbox.notice, /not from your user/);
    assert.equal(inbox.messages.length, 1);
    assert.deepEqual(inbox.messages[0].from, { id: 'tab-aaaa-1', agent: 'codex', path: '/work/tab-aaaa-1' });
    assert.equal(inbox.messages[0].text, 'Please review src/x.ts.');

    const reply = await b.call('send_message', { to: 'tab-aaaa-1', text: 'Looks fine.', replyTo: inbox.messages[0].id });
    assert.equal(reply.isError, false);
    const got = (await waiting).json;
    assert.equal(got.message.text, 'Looks fine.');
    assert.equal(got.message.replyTo, sent.json.id);
    assert.equal(got.message.from.agent, 'claude');
    assert.match(got.notice, /peer's request/);

    const tooLong = await a.call('send_message', { to: 'tab-bbbb-2', text: 'x'.repeat(32_001) });
    assert.ok(tooLong.isError);
    const unknown = await a.call('send_message', { to: 'tab-none', text: 'hi' });
    assert.match(unknown.text, /no live session/);
  } finally {
    await a.client.close();
    await b.client.close();
    a.messaging.stopSync();
    b.messaging.stopSync();
  }
});

test('with Jev on, the server sends the messaging and the Jev instructions together', async () => {
  const home = tempDir('iat-msg-jev-');
  const service = new Service({ home, scriptsDir: home, platform: 'linux', env: { PATH: '' }, callIde: async () => ({}), drivers: [] });
  const messaging = new Messaging({ home, env: {}, pid: 103, cwd: '/w', hosts: service, isAlive: () => true });
  const [clientSide, serverSide] = InMemoryTransport.createLinkedPair();
  await createServer(service, {} as Jev, messaging).connect(serverSide);
  const client = new Client({ name: 'test', version: '1.0.0' });
  await client.connect(clientSide);
  const instructions = client.getInstructions()!;
  assert.ok(instructions.indexOf('list_sessions') < instructions.indexOf('jev_'));
  assert.ok((await client.listTools()).tools.some((t) => t.name === 'jev_route'));
  await client.close();
});

test("a Codex tab's hooks reach the hook tool, which only Codex sees", async () => {
  const home = tempDir('iat-msg-hook-');
  const thread = '019a2b3c-4d5e-7f60-8a9b-0c1d2e3f4a5b';
  const codex = await connect(home, 'tab-closed-1', 'codex-mcp-client', 105);
  const claude = await connect(home, 'tab-eeee-2', 'claude-code', 106);
  try {
    const hookTool = (await codex.client.listTools()).tools.find((t) => t.name === 'agent_tabs_hook');
    assert.deepEqual(hookTool?._meta, { ui: { visibility: [] } });
    assert.ok(!(await claude.client.listTools()).tools.some((t) => t.name === 'agent_tabs_hook'));

    const hook = async (event: string) => {
      const result = (await codex.client.callTool({ name: 'agent_tabs_hook', arguments: { event, session_id: thread, turn_id: 't1' }, _meta: { threadId: thread } })) as {
        isError?: boolean;
        content: { text: string }[];
      };
      assert.ok(!result.isError);
      return result.content.length ? JSON.parse(result.content[0]!.text) : undefined;
    };
    assert.equal(await hook('UserPromptSubmit'), undefined);
    assert.equal(codex.messaging.id, `codex-${thread}`, 'the closed tab id gives way to the thread id');

    await claude.call('send_message', { to: `codex-${thread}`, text: 'Status?' });
    const reminder = await hook('PostToolUse');
    assert.match(reminder.hookSpecificOutput.additionalContext, /1 unread message from claude tab-eeee.*read_messages/);
    assert.equal(reminder.hookSpecificOutput.hookEventName, 'PostToolUse');
    assert.equal((await hook('Stop')).decision, 'block');
    assert.equal((await codex.call('read_messages')).json.messages[0].text, 'Status?');
    assert.equal(await hook('Stop'), undefined);
    const self = (await codex.call('list_sessions')).json.sessions.find((s: { self: boolean }) => s.self);
    assert.deepEqual([self.id, self.state, self.agent], [`codex-${thread}`, 'idle', 'codex']);
  } finally {
    await codex.client.close();
    await claude.client.close();
    codex.messaging.stopSync();
    claude.messaging.stopSync();
  }
});

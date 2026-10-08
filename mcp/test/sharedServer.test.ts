import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { test, type TestContext } from 'node:test';
import { closeAllDbs } from '../src/messaging/db.js';
import { readPresence, updatePresence } from '../src/messaging/sessions.js';
import { CATALOG } from '../src/shared/catalog.generated.js';
import { Engine } from '../src/shared/engine.js';
import { Front, type Identity } from '../src/shared/front.js';
import { derivedId, Hub, rootPath, sessionKey } from '../src/shared/hub.js';
import { ensureToken, newToken } from '../src/shared/state.js';
import { freePort, McpHttp, PROTOCOL, request } from './httpClient.js';
import { tempDir } from './tempDir.js';

async function server(t: TestContext, options: { alive?: (pid: number) => boolean; bind?: Hub['call'] } = {}) {
  const home = tempDir('iat-shared-');
  const port = await freePort();
  const token = await ensureToken(home);
  const shutdownToken = newToken();
  const engine = new Engine({ home, scriptsDir: home, env: { PATH: process.env.PATH ?? '' }, log: () => undefined, detect: false });
  const hub = new Hub({ bind: (binding) => engine.bind(binding), ...(options.alive ? { isAlive: options.alive } : {}) });
  let shutdowns = 0;
  const front = new Front({
    port,
    token,
    shutdownToken,
    serverInfo: { name: 'ide-agent-tabs', version: 'test' },
    health: () => ({ service: 'ide-agent-tabs', version: 'test', pid: process.pid, port }),
    catalog: async () => ({ tools: CATALOG.tools, instructions: CATALOG.instructions.plain }),
    call: options.bind ?? ((identity, params, signal) => hub.call(identity, params, signal)),
    end: (pid) => hub.endPid(pid),
    onRequest: () => undefined,
    onShutdown: () => shutdowns++,
  });
  await front.listen();
  t.after(async () => {
    await front.close();
    await hub.close();
    await closeAllDbs();
  });
  return { home, port, token, shutdownToken, hub, front, shutdowns: () => shutdowns };
}

const pidHeaders = (pid: number, start: number, extra: Record<string, string> = {}) => ({
  'x-agent-tabs-client': `c${pid}x${start}`,
  'x-agent-tabs-pid': String(pid),
  'x-agent-tabs-pid-start': String(start),
  ...extra,
});

test('the server refuses a foreign Host or Origin and a missing token with 403, and answers /health without one', async (t) => {
  const s = await server(t);
  assert.equal((await request(s.port, '/health', { method: 'GET' })).json.service, 'ide-agent-tabs');
  assert.equal((await request(s.port, '/health', { method: 'GET', headers: { host: `evil.example:${s.port}` } })).status, 403);
  assert.equal((await request(s.port, '/health', { method: 'GET', headers: { origin: 'http://evil.example' } })).status, 403);
  const anonymous = new McpHttp(s.port, 'wrong');
  assert.equal((await anonymous.rpc('server/discover')).status, 403);
  assert.equal((await request(s.port, '/shutdown', { headers: { authorization: `Bearer ${s.token}` } })).status, 403);
  assert.equal((await request(s.port, '/shutdown', { headers: { authorization: `Bearer ${s.shutdownToken}` } })).status, 200);
  assert.equal(s.shutdowns(), 1);
});

test('MCP 2026-07-28 discovery and tool listing answer from the catalog, and older protocols are refused', async (t) => {
  const s = await server(t);
  const client = new McpHttp(s.port, s.token);
  const discovered = await client.rpc('server/discover');
  assert.equal(discovered.status, 200);
  assert.deepEqual(discovered.json.result.supportedVersions, [PROTOCOL]);
  assert.match(discovered.json.result.instructions, /list_sessions/);
  const listed = (await client.rpc('tools/list')).json.result.tools.map((t: { name: string }) => t.name);
  assert.ok(listed.includes('send_message') && listed.includes('agent_tabs_mod') && !listed.includes('agent_tabs_hook'));
  assert.deepEqual((await client.rpc('resources/list')).json.result.resources, []);
  const classic = await client.rpc('initialize', {}, { 'mcp-protocol-version': '2025-11-25' });
  assert.equal(classic.status, 400);
  assert.equal(classic.json.error.code, -32022);
  const mismatched = await client.rpc('tools/list', {}, { 'mcp-method': 'tools/call' });
  assert.equal(mismatched.json.error.code, -32020);
  assert.equal((await request(s.port, '/mcp', { body: '{', headers: { authorization: `Bearer ${s.token}`, accept: 'application/json', 'mcp-protocol-version': PROTOCOL } })).json.error.code, -32700);
});

test("a session's first tool call asks for its roots, and the session keeps that folder, its agent pid and start time", async (t) => {
  const s = await server(t);
  const cwd = tempDir('iat-shared-cwd-');
  const client = new McpHttp(s.port, s.token, pidHeaders(process.pid, 1234), cwd);
  const first = await client.rpc('tools/call', { name: 'list_sessions', arguments: {} });
  assert.equal(first.json.result.resultType, 'input_required');
  assert.equal(Object.values(first.json.result.inputRequests as Record<string, { method: string }>)[0]!.method, 'roots/list');
  const listed = (await client.call('list_sessions')).json;
  const self = listed.sessions.find((x: { self: boolean }) => x.self);
  assert.equal(self.id, derivedId(`pid:${process.pid}:1234`));
  assert.equal(path.resolve(self.path), path.resolve(cwd));
  const presence = await readPresence(s.home, self.id);
  assert.equal(presence?.pid, process.pid);
  assert.equal(presence?.pidStart, 1234);
  const again = await client.rpc('tools/call', { name: 'list_sessions', arguments: {} });
  assert.equal(again.json.result.resultType, 'complete');
});

test('two agent processes get two sessions that message each other, and one process reconnecting keeps its session', async (t) => {
  const s = await server(t);
  const a = new McpHttp(s.port, s.token, pidHeaders(process.pid, 1));
  const b = new McpHttp(s.port, s.token, pidHeaders(process.pid, 2));
  const idA = (await a.call('list_sessions')).json.sessions.find((x: { self: boolean }) => x.self).id;
  const idB = (await b.call('list_sessions')).json.sessions.find((x: { self: boolean }) => x.self).id;
  assert.notEqual(idA, idB);
  const sent = (await a.call('send_message', { to: idB, text: 'hello from a' })).json;
  assert.equal(sent.to, idB);
  const read = (await b.call('read_messages')).json;
  assert.deepEqual(read.messages.map((m: { text: string; from: { id: string } }) => [m.text, m.from.id]), [['hello from a', idA]]);
  const reconnected = new McpHttp(s.port, s.token, { ...pidHeaders(process.pid, 1), 'x-agent-tabs-client': 'another-connect' });
  assert.equal((await reconnected.call('list_sessions')).json.sessions.find((x: { self: boolean }) => x.self).id, idA);
  assert.equal(s.hub.size, 2);
});

test('a tab id binds only when no other live process holds it', async (t) => {
  const s = await server(t);
  await updatePresence(s.home, 'tab-held', () => ({ id: 'tab-held', agent: 'claude', path: '/x', pid: process.ppid, startedAt: new Date().toISOString(), state: 'idle' }));
  const claim = new McpHttp(s.port, s.token, pidHeaders(process.pid, 7, { 'x-agent-tabs-tab': 'tab-held' }));
  const self = (await claim.call('list_sessions')).json.sessions.find((x: { self: boolean }) => x.self);
  assert.equal(self.id, derivedId(`pid:${process.pid}:7`));
  const free = new McpHttp(s.port, s.token, pidHeaders(process.pid, 8, { 'x-agent-tabs-tab': 'tab-free' }));
  assert.equal((await free.call('list_sessions')).json.sessions.find((x: { self: boolean }) => x.self).id, 'tab-free');
});

test('a session ends when its agent process exits or its SessionEnd hook reports it', async (t) => {
  let dead = new Set<number>();
  const s = await server(t, { alive: (pid) => !dead.has(pid) });
  const a = new McpHttp(s.port, s.token, pidHeaders(process.pid, 11));
  const b = new McpHttp(s.port, s.token, pidHeaders(process.ppid, 12));
  const idA = (await a.call('list_sessions')).json.sessions.find((x: { self: boolean }) => x.self).id;
  const idB = (await b.call('list_sessions')).json.sessions.find((x: { self: boolean }) => x.self).id;
  dead = new Set([process.pid]);
  assert.equal(await s.hub.sweep(), 1);
  assert.equal(await readPresence(s.home, idA), undefined);
  const ended = await request(s.port, '/end', { body: JSON.stringify({ pid: process.ppid }), headers: { authorization: `Bearer ${s.token}`, 'content-type': 'application/json' } });
  assert.deepEqual(ended.json, { ended: 1 });
  assert.equal(await readPresence(s.home, idB), undefined);
  assert.equal(s.hub.size, 0);
});

test('a dropped HTTP request cancels its tool call', async (t) => {
  let aborted = false;
  const s = await server(t, {
    bind: async (_identity: Identity, _params, signal) => {
      await new Promise<void>((resolve) => signal.addEventListener('abort', () => resolve(), { once: true }));
      aborted = true;
      return {};
    },
  });
  const controller = new AbortController();
  const body = JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'tools/call', params: { name: 'wait_for_message', arguments: {}, _meta: { 'io.modelcontextprotocol/protocolVersion': PROTOCOL, 'io.modelcontextprotocol/clientCapabilities': {} } } });
  const pending = fetch(`http://127.0.0.1:${s.port}/mcp`, {
    method: 'POST',
    body,
    signal: controller.signal,
    headers: { authorization: `Bearer ${s.token}`, accept: 'application/json', 'content-type': 'application/json', 'mcp-protocol-version': PROTOCOL, 'mcp-method': 'tools/call', 'mcp-name': 'wait_for_message' },
  }).catch(() => undefined);
  await new Promise((r) => setTimeout(r, 200));
  controller.abort();
  await pending;
  for (let i = 0; i < 50 && !aborted; i++) await new Promise((r) => setTimeout(r, 20));
  assert.ok(aborted);
});

test('session keys come from the agent pid and start time, else the client id, and roots must be file URLs', () => {
  assert.equal(sessionKey({ pid: 5, pidStart: 9, client: 'x' }), 'pid:5:9');
  assert.equal(sessionKey({ client: 'abc' }), 'client:abc');
  assert.equal(sessionKey({}), undefined);
  assert.match(derivedId('pid:5:9'), /^s-[0-9a-f]{12}$/);
  const root = process.platform === 'win32' ? 'file:///C:/work/repo' : 'file:///work/repo';
  assert.equal(rootPath({ 'agent-tabs-roots': { roots: [{ uri: 'https://x' }, { uri: root }] } }), process.platform === 'win32' ? 'C:\\work\\repo' : '/work/repo');
  assert.equal(rootPath({ 'agent-tabs-roots': { roots: [{ uri: 'file://server/share' }] } }), undefined);
  assert.equal(rootPath(undefined), undefined);
});

test('the generated catalog matches the tool registrations', async () => {
  const { catalogText, CATALOG_FILE } = await import('../scripts/write-catalog.js');
  assert.equal(readFileSync(CATALOG_FILE, 'utf8').replace(/\r\n/g, '\n'), await catalogText(), 'run node --import tsx scripts/write-catalog.ts');
});

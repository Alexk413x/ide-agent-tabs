import assert from 'node:assert/strict';
import { rmSync } from 'node:fs';
import http from 'node:http';
import { test, type TestContext } from 'node:test';
import { fileURLToPath } from 'node:url';
import { helperHeaders } from '../src/shared/headers.js';
import { ensureServer, probe, stopServer } from '../src/shared/client.js';
import { Front } from '../src/shared/front.js';
import { claimPort } from '../src/shared/handover.js';
import { newToken, statePath, writeState } from '../src/shared/state.js';
import { PACKAGE_VERSION } from '../src/version.js';
import { freePort, McpHttp } from './httpClient.js';
import { tempDir } from './tempDir.js';

const LAUNCHER = fileURLToPath(new URL('./serverLauncher.mjs', import.meta.url));
const LONG = { timeout: 60_000 };
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

async function started(t: TestContext) {
  const home = tempDir('iat-srv-');
  const port = await freePort();
  const ensured = await ensureServer({ script: LAUNCHER, port, home, version: PACKAGE_VERSION, waitMs: 20_000 });
  t.after(async () => {
    await stopServer(home, port).catch(() => undefined);
  });
  return { home, port, ensured };
}

test('the first caller starts the server and gets its token, and later callers reuse it', LONG, async (t) => {
  const { home, port, ensured } = await started(t);
  assert.equal(ensured.problem, undefined);
  assert.equal(ensured.started, true);
  assert.equal(ensured.health?.version, PACKAGE_VERSION);
  assert.match(ensured.token ?? '', /^[0-9a-f]{64}$/);
  const again = await ensureServer({ script: LAUNCHER, port, home, version: PACKAGE_VERSION });
  assert.equal(again.started, false);
  assert.equal(again.health?.pid, ensured.health?.pid);
  const discovered = await new McpHttp(port, ensured.token!).rpc('server/discover');
  assert.equal(discovered.status, 200);
});

test('the headers helper sends the token, a client id and the pid of the process above its shell', LONG, async (t) => {
  const { home, port, ensured } = await started(t);
  const headers = await helperHeaders({ IDE_AGENT_TABS_HOME: home, CLAUDE_CODE_MCP_SERVER_URL: `http://127.0.0.1:${port}/mcp`, IDE_AGENT_TABS_ID: 'tab-x1', IDE_AGENT_TABS_AGENT: 'claude' }, tempDir('iat-root-'), 60_000);
  assert.equal(headers.Authorization, `Bearer ${ensured.token}`);
  assert.match(headers['X-Agent-Tabs-Client']!, /^[0-9a-f]{24}$/);
  assert.equal(headers['X-Agent-Tabs-Tab'], 'tab-x1');
  assert.equal(headers['X-Agent-Tabs-Agent'], 'claude');
  assert.match(headers['X-Agent-Tabs-Pid'] ?? '', /^\d+$/);
  const unset = await helperHeaders({ IDE_AGENT_TABS_HOME: home, CLAUDE_CODE_MCP_SERVER_URL: `http://127.0.0.1:${port}/mcp`, IDE_AGENT_TABS_ID: '${IDE_AGENT_TABS_ID}', IDE_AGENT_TABS_AGENT: 'bad name' }, tempDir('iat-root-'));
  assert.equal(unset['X-Agent-Tabs-Tab'], undefined);
  assert.equal(unset['X-Agent-Tabs-Agent'], undefined);
});

test('server stop asks the server named in the state file to exit', LONG, async (t) => {
  const { home, port } = await started(t);
  const stopped = await stopServer(home, port);
  assert.equal(stopped.stopped, true);
  assert.equal((await probe(port)).kind, 'free');
  assert.match((await stopServer(home, port)).problem ?? '', /nothing listens/);
});

test('a server whose state file is gone exits within 5 seconds', LONG, async (t) => {
  const { home, port } = await started(t);
  rmSync(statePath(home, port));
  const deadline = Date.now() + 8_000;
  while ((await probe(port)).kind !== 'free' && Date.now() < deadline) await sleep(100);
  assert.equal((await probe(port)).kind, 'free');
});

test('a caller never sends the token to a port that another program holds', LONG, async (t) => {
  const home = tempDir('iat-srv-');
  const port = await freePort();
  const other = http.createServer((_req, res) => res.end('hello'));
  await new Promise<void>((r) => other.listen(port, '127.0.0.1', () => r()));
  t.after(() => other.close());
  const ensured = await ensureServer({ script: LAUNCHER, port, home, version: PACKAGE_VERSION, waitMs: 500 });
  assert.equal(ensured.token, undefined);
  assert.match(ensured.problem ?? '', /another program; set the Agent Tabs server_port option/);
});

function oldServer(port: number, version: string) {
  const front = new Front({
    port,
    token: newToken(),
    shutdownToken: 'old-shutdown-token',
    serverInfo: { name: 'ide-agent-tabs', version },
    health: () => ({ service: 'ide-agent-tabs', version, pid: process.pid, port }),
    catalog: async () => ({ tools: [], instructions: '' }),
    call: async () => ({}),
    end: async () => 0,
    onRequest: () => undefined,
    onShutdown: () => void front.close(),
  });
  return front;
}

test('a newer build takes the port from an older one it verifies, and leaves a newer or unverified one alone', LONG, async (t) => {
  const home = tempDir('iat-srv-');
  const port = await freePort();
  const old = oldServer(port, '0.0.1');
  await old.listen();
  t.after(() => old.close());
  const log = () => undefined;
  const contender = (version: string) => oldServer(port, version);

  await writeState(home, { pid: process.pid + 1, port, version: '0.0.1', startedAt: '', shutdownToken: 'old-shutdown-token' });
  assert.equal(await claimPort({ front: contender('9.9.9'), port, home, version: '9.9.9', log, waitMs: 500 }), false);
  await writeState(home, { pid: process.pid, port, version: '0.0.1', startedAt: '', shutdownToken: 'old-shutdown-token' });
  assert.equal(await claimPort({ front: contender('0.0.1'), port, home, version: '0.0.1', log, waitMs: 500 }), false);
  const newer = contender('9.9.9');
  t.after(() => newer.close());
  assert.equal(await claimPort({ front: newer, port, home, version: '9.9.9', log }), true);
  const now = await probe(port);
  assert.equal(now.kind === 'ours' && now.health.version, '9.9.9');
});

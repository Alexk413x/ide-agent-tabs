import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { existsSync, mkdirSync, readdirSync, readFileSync, writeFileSync } from 'node:fs';
import http from 'node:http';
import type { AddressInfo } from 'node:net';
import path from 'node:path';
import { after, before, test } from 'node:test';
import { tempDir } from './tempDir.js';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js';
import { ideCaller } from '../src/ideClient.js';
import { createServer, SERVER_VERSION } from '../src/server.js';
import { readPresence } from '../src/messaging/sessions.js';
import { FRESH_TAB_START_MS, Service } from '../src/service.js';
import type { LaunchSpec } from '../src/spec.js';
import type { OpenOptions, TerminalDriver, TerminalTab } from '../src/terminals/types.js';

const TOKEN = 'f'.repeat(64);
let ideToken = TOKEN;
const home = tempDir('iat-it-');
const project = tempDir('iat-proj-');
const outside = tempDir('iat-out-');
const endpoints = path.join(home, 'endpoints');
mkdirSync(endpoints);

interface Seen {
  route: string;
  body: Record<string, unknown>;
  headers: http.IncomingHttpHeaders;
}
const seen: Seen[] = [];
const ideTabs: Record<string, unknown>[] = [];
let ideServer: http.Server;

function fakeIde(req: http.IncomingMessage, res: http.ServerResponse) {
  let data = '';
  req.on('data', (d) => (data += d));
  req.on('end', () => {
    const reply = (status: number, body: object) => {
      res.writeHead(status, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify(body));
    };
    if (req.headers.authorization !== `Bearer ${ideToken}`) return reply(401, { ok: false, error: 'missing or wrong token' });
    if (req.headers['content-type'] !== 'application/json') return reply(415, { ok: false, error: 'Content-Type must be application/json' });
    if (req.headers.origin || /Mozilla/.test(req.headers['user-agent'] ?? '')) return reply(403, { ok: false, error: 'browser' });
    const route = req.url!.replace('/ide-agent-tabs/', '');
    const body = JSON.parse(data || '{}');
    seen.push({ route, body, headers: req.headers });
    switch (route) {
      case 'info':
        return reply(200, { ok: true, ide: 'jetbrains', product: 'Fake Studio', version: '1.0', pid: process.pid, projects: [{ name: 'proj', path: project, focused: true }] });
      case 'open': {
        if (body.agent === 'nope') return reply(400, { ok: false, error: 'unknown agent: nope' });
        if (body.agent === 'dialog') return reply(503, { ok: false, error: 'the IDE did not respond in 10 s, likely a modal dialog; nothing was done' });
        if (body.agent === 'no-project') return reply(409, { ok: false, error: 'no open project to host the tab' });
        if (body.agent === 'rotate' && ideToken === TOKEN) {
          ideToken = 'e'.repeat(64);
          const file = path.join(endpoints, `jetbrains-${process.pid}.json`);
          writeFileSync(file, readFileSync(file, 'utf8').replace(TOKEN, ideToken));
          return reply(401, { ok: false, error: 'missing or wrong token' });
        }
        const tab = { id: `ide-tab-${ideTabs.length + 1}`, agent: body.agent ?? 'claude', project: 'proj', path: body.path };
        ideTabs.push(tab);
        return reply(200, { ok: true, ...tab });
      }
      case 'list':
        return reply(200, { ok: true, tabs: ideTabs });
      case 'close': {
        const i = ideTabs.findIndex((t) => t.id === body.id);
        if (i < 0) return reply(404, { ok: false, error: `no open agent tab with id ${body.id}` });
        ideTabs.splice(i, 1);
        return reply(200, { ok: true, id: body.id });
      }
      default:
        return reply(404, { ok: false, error: 'no route' });
    }
  });
}

const opened: { spec: LaunchSpec; title: string }[] = [];
const openOptions: (OpenOptions | undefined)[] = [];
const closed: string[] = [];
const liveTerminalTabs = new Set<string>();
const fakeTerminal: TerminalDriver = {
  name: 'fake-term',
  label: 'Fake Terminal',
  capabilities: { open: 'tab', list: 'yes', close: 'yes' },
  available: async () => true,
  open: async (_ctx, spec, title, options) => {
    opened.push({ spec, title });
    openOptions.push(options);
    liveTerminalTabs.add(spec.id);
    return { id: spec.id, terminal: 'fake-term', agent: spec.agent, path: spec.cwd, createdAt: Date.now(), terminalId: `t-${spec.id}` };
  },
  alive: async (_ctx, tabs: TerminalTab[]) => new Set(tabs.filter((t) => liveTerminalTabs.has(t.id)).map((t) => t.id)),
  close: async (_ctx, tab) => {
    closed.push(tab.id);
    liveTerminalTabs.delete(tab.id);
  },
};

let ids = 0;
const env: NodeJS.ProcessEnv = { PATH: '' };
let client: Client;
let service: Service;

async function call(name: string, args: Record<string, unknown> = {}) {
  const result = (await client.callTool({ name, arguments: args })) as { isError?: boolean; content: { text: string }[] };
  const text = result.content[0]!.text;
  return { isError: result.isError === true, text, json: result.isError ? undefined : JSON.parse(text) };
}

function writeEndpoint(name: string, body: object) {
  writeFileSync(path.join(endpoints, name), JSON.stringify(body));
}

before(async () => {
  ideServer = http.createServer(fakeIde);
  await new Promise<void>((r) => ideServer.listen(0, '127.0.0.1', r));
  const port = (ideServer.address() as AddressInfo).port;
  const base = { protocol: 1, ide: 'jetbrains', product: 'Fake Studio', version: '1.0', url: `http://127.0.0.1:${port}/ide-agent-tabs`, token: TOKEN };
  writeEndpoint(`jetbrains-${process.pid}.json`, { ...base, pid: process.pid });
  const dead = spawnSync(process.execPath, ['-e', '']).pid!;
  writeEndpoint(`jetbrains-${dead}.json`, { ...base, pid: dead });
  writeEndpoint('future-1.json', { ...base, protocol: 2, pid: process.pid });

  service = new Service({
    home,
    scriptsDir: path.join(home, 'scripts'),
    platform: 'linux',
    env,
    callIde: ideCaller(5_000),
    drivers: [fakeTerminal],
    newId: () => `term-tab-${++ids}`,
    selfCloseDelayMs: 10,
  });
  const [clientSide, serverSide] = InMemoryTransport.createLinkedPair();
  await createServer(service).connect(serverSide);
  client = new Client({ name: 'test', version: '1.0.0' });
  await client.connect(clientSide);
});

after(async () => {
  await client.close();
  await new Promise((r) => ideServer.close(r));
});

test('the server offers exactly the four tab tools, each with a description', async () => {
  const { tools } = await client.listTools();
  assert.deepEqual(tools.map((t) => t.name).sort(), ['close_tab', 'list_agents', 'list_tabs', 'open_tab']);
  for (const t of tools) assert.ok((t.description ?? '').length > 20, t.name);
  const open = tools.find((t) => t.name === 'open_tab')!;
  assert.deepEqual((open.inputSchema as { required?: string[] }).required, ['path']);
});

test('list-ides reads the registry, drops dead entries and shows terminals', async () => {
  const json = await service.listIdes();
  assert.equal(json.ides.length, 1);
  assert.deepEqual(
    json.ides[0],
    { id: `jetbrains-${process.pid}`, ide: 'jetbrains', product: 'Fake Studio', version: '1.0', projects: [{ name: 'proj', path: project, focused: true }] },
  );
  assert.deepEqual(json.terminals, [{ id: 'fake-term', name: 'Fake Terminal', capabilities: { open: 'tab', list: 'yes', close: 'yes' }, preferred: false }]);
  assert.deepEqual(json.shells, []);
  const detected = JSON.parse(readFileSync(path.join(home, 'detected.json'), 'utf8'));
  assert.equal(detected.version, 1);
  assert.equal(detected.platform, 'linux');
  assert.deepEqual(detected.terminals, [{ id: 'fake-term', name: 'Fake Terminal' }]);
  assert.deepEqual(detected.shells, []);
  assert.ok(Date.parse(detected.detectedAt) > 0);
  assert.equal(readdirSync(endpoints).length, 2, 'the dead entry is deleted and the future one kept');
  const info = seen.find((s) => s.route === 'info')!;
  assert.equal(info.headers['content-type'], 'application/json');
  assert.equal(info.headers['user-agent'], `ide-agent-tabs-mcp/${SERVER_VERSION}`);
});

test('list_agents reports profiles, installed state and the default, and warns on a bad agents file', async () => {
  writeFileSync(path.join(home, 'agents.json'), JSON.stringify({ probe: { label: 'Probe', command: process.execPath, args: ['-e', ''], promptFlag: '--prompt' } }));
  writeFileSync(path.join(home, 'config.json'), JSON.stringify({ defaultAgent: 'probe' }));
  const { json } = await call('list_agents');
  assert.equal(json.default, 'probe');
  assert.deepEqual(json.agents.map((a: { name: string }) => a.name), ['claude', 'codex', 'agy', 'copilot', 'gemini', 'grok', 'pi', 'hermes', 'opencode', 'qwen', 'goose', 'codex-local', 'probe']);
  assert.equal(json.agents.at(-1).installed, true);
  assert.equal(json.agents[0].installed, false);

  writeFileSync(path.join(home, 'agents.json'), '{"x": {"command": ""}}');
  const bad = await call('list_agents');
  assert.equal(bad.json.default, 'claude');
  assert.match(bad.json.warnings[0], /needs a command/);
});

test('open_tab routes a path inside an open project to that IDE and forwards the request', async () => {
  const { json } = await call('open_tab', { path: path.join(project), agent: 'codex', prompt: 'hi "there"', args: ['--yolo'], env: { A: 'b' } });
  assert.equal(json.ide, `jetbrains-${process.pid}`);
  assert.equal(json.id, 'ide-tab-1');
  assert.equal(json.agent, 'codex');
  assert.match(json.reason, /open project proj contains the path/);
  assert.deepEqual(seen.at(-1)!.body, { path: path.normalize(project), agent: 'codex', prompt: 'hi "there"', args: ['--yolo'], env: { A: 'b' }, focus: false });
});

test('a tab opened without a prompt counts as idle once it has had time to start, so a message wakes it', async () => {
  const prompted = await call('open_tab', { path: project, prompt: 'go', model: 'claude-opus-5-5' });
  assert.deepEqual(
    await readPresence(home, prompted.json.id),
    { id: prompted.json.id, via: 'direct', project: 'proj', model: 'claude-opus-5-5', product: prompted.json.product },
    'a tab with a prompt starts busy and reports its state itself; the model and IDE product it opened with are kept for list_sessions',
  );
  const before = Date.now();
  const fresh = await call('open_tab', { path: project });
  const presence = (await readPresence(home, fresh.json.id))!;
  assert.equal(presence.state, 'idle');
  assert.equal(presence.host, fresh.json.ide);
  assert.equal(presence.model, undefined);
  assert.equal(typeof presence.product, 'string');
  assert.ok(Date.parse(presence.stateAt!) >= before + FRESH_TAB_START_MS);
  for (const tab of [prompted, fresh]) assert.ok(!(await call('close_tab', { id: tab.json.id })).isError);
});

test('open_tab falls back to the most recently started IDE and adds the next step to IDE errors', async () => {
  const ok = await call('open_tab', { path: outside });
  assert.match(ok.json.reason, /most recently started IDE/);
  const bad = await call('open_tab', { path: outside, agent: 'nope' });
  assert.ok(bad.isError);
  assert.match(bad.text, /answered HTTP 400: unknown agent: nope\. Call list_agents/);
  const dialog = await call('open_tab', { path: outside, agent: 'dialog' });
  assert.match(dialog.text, /answered HTTP 503: .*modal dialog.*\. A modal dialog is likely open in Fake Studio\. Ask the user to close it/);
  const noProject = await call('open_tab', { path: outside, agent: 'no-project' });
  assert.match(noProject.text, /answered HTTP 409: no open project to host the tab\. .*pass ide set to a terminal id such as windows-terminal/);
});

test('open_tab rereads the registry and retries once when the IDE rotated its token', async () => {
  const file = path.join(endpoints, `jetbrains-${process.pid}.json`);
  const saved = readFileSync(file, 'utf8');
  try {
    const rotated = await call('open_tab', { path: outside, agent: 'rotate' });
    assert.equal(rotated.json.agent, 'rotate');
    assert.equal(seen.filter((s) => s.body.agent === 'rotate').length, 2);
    ideTabs.pop();

    writeFileSync(file, saved.replace(TOKEN, 'd'.repeat(64)));
    const stale = await call('open_tab', { path: outside, agent: 'codex' });
    assert.match(stale.text, /answered HTTP 401: missing or wrong token\. Fake Studio refused the token in jetbrains-\d+, so that endpoint is stale\. Run the Agent Tabs command line's list-ides/);
  } finally {
    ideToken = TOKEN;
    writeFileSync(file, saved);
  }
});

test('open_tab refuses bad input before routing', async () => {
  for (const args of [
    { path: 'relative' },
    { path: path.join(outside, 'missing') },
    { path: outside, env: { IDE_AGENT_TABS_ID: 'x' } },
    { path: outside, ide: 'jetbrains-1' },
  ]) {
    const r = await call('open_tab', args);
    assert.ok(r.isError, JSON.stringify(args));
  }
  const tooLong = await call('open_tab', { path: outside, prompt: 'x'.repeat(30_001) });
  assert.ok(tooLong.isError);
});

test('open_tab with ide naming a terminal opens there with the resolved profile', async () => {
  writeFileSync(path.join(home, 'agents.json'), JSON.stringify({ probe: { label: 'Probe; X', command: 'probe-cli', args: ['--p'], promptFlag: '--prompt', env: { P: '1' } } }));
  const { json } = await call('open_tab', { path: outside, agent: 'probe', prompt: 'go', args: ['--x'], env: { C: '2' }, ide: 'fake-term' });
  assert.deepEqual(json, { id: 'term-tab-1', ide: 'fake-term', product: 'Fake Terminal', agent: 'probe', path: path.normalize(outside), reason: 'named by ide' });
  assert.deepEqual(opened.at(-1), {
    title: 'Probe; X',
    spec: { id: 'term-tab-1', agent: 'probe', cwd: path.normalize(outside), command: 'probe-cli', args: ['--p', '--x', '--prompt'], prompt: 'go', env: { P: '1', C: '2' } },
  });
  const stored = JSON.parse(readFileSync(path.join(home, 'terminal-tabs.json'), 'utf8'));
  assert.deepEqual(stored.tabs.map((t: TerminalTab) => t.id), ['term-tab-1']);
  const unknown = await call('open_tab', { path: outside, agent: 'absent', ide: 'fake-term' });
  assert.match(unknown.text, /unknown agent: absent/);
});

test('list_tabs spans IDEs and terminals and prunes terminal tabs that are gone', async () => {
  const all = await call('list_tabs');
  assert.deepEqual(all.json.tabs.map((t: { id: string; ide: string }) => `${t.ide}:${t.id}`).sort(), [
    'fake-term:term-tab-1',
    `jetbrains-${process.pid}:ide-tab-1`,
    `jetbrains-${process.pid}:ide-tab-2`,
  ]);
  const onlyTerm = await call('list_tabs', { ide: 'fake-term' });
  assert.deepEqual(onlyTerm.json.tabs.map((t: { id: string }) => t.id), ['term-tab-1']);
  const onlyIde = await call('list_tabs', { ide: `jetbrains-${process.pid}` });
  assert.equal(onlyIde.json.tabs.length, 2);
  assert.ok((await call('list_tabs', { ide: 'nope-1' })).isError);

  liveTerminalTabs.delete('term-tab-1');
  assert.deepEqual((await call('list_tabs', { ide: 'fake-term' })).json.tabs, []);
  assert.deepEqual(JSON.parse(readFileSync(path.join(home, 'terminal-tabs.json'), 'utf8')).tabs, []);
});

test('close_tab closes IDE and terminal tabs by id', async () => {
  const closedIde = await call('close_tab', { id: 'ide-tab-1' });
  assert.deepEqual(closedIde.json, { id: 'ide-tab-1', ide: `jetbrains-${process.pid}`, closed: true });
  assert.deepEqual(seen.at(-1)!.body, { id: 'ide-tab-1' });
  const missing = await call('close_tab', { id: 'ide-tab-1' });
  assert.match(missing.text, /no open agent tab with id ide-tab-1/);

  const term = await call('open_tab', { path: outside, ide: 'fake-term' });
  const closedTerm = await call('close_tab', { id: term.json.id });
  assert.deepEqual(closedTerm.json, { id: term.json.id, ide: 'fake-term', closed: true });
  assert.deepEqual(closed, [term.json.id]);
});

test('close_tab with no id closes the caller\'s own tab from IDE_AGENT_TABS_ID', async () => {
  assert.match((await call('close_tab')).text, /IDE_AGENT_TABS_ID is not set/);
  env.IDE_AGENT_TABS_ID = 'ide-tab-2';
  assert.deepEqual((await call('close_tab')).json, { id: 'ide-tab-2', ide: `jetbrains-${process.pid}`, closed: true });

  const term = await call('open_tab', { path: outside, ide: 'fake-term' });
  env.IDE_AGENT_TABS_ID = term.json.id;
  assert.deepEqual((await call('close_tab')).json, { id: term.json.id, ide: 'fake-term', closing: true });
  await new Promise((r) => setTimeout(r, 200));
  assert.equal(closed.at(-1), term.json.id);
  delete env.IDE_AGENT_TABS_ID;
});

test("tabRouting caller opens a terminal caller's tab in its own terminal window, even when an IDE has the project", async () => {
  const caller = await call('open_tab', { path: outside, ide: 'fake-term' });
  assert.deepEqual(openOptions.at(-1), { window: 'last', focus: false });
  env.IDE_AGENT_TABS_ID = caller.json.id;
  try {
    const byProject = await call('open_tab', { path: project, prompt: 'p' });
    assert.equal(byProject.json.ide, `jetbrains-${process.pid}`, 'the default keeps project routing');

    writeFileSync(path.join(home, 'config.json'), JSON.stringify({ tabRouting: 'caller', terminalWindow: 'dedicated' }));
    const byCaller = await call('open_tab', { path: project, prompt: 'c' });
    assert.equal(byCaller.json.ide, 'fake-term');
    assert.equal(byCaller.json.reason, "tabRouting is caller; the caller's terminal window");
    assert.equal(openOptions.at(-1)?.window, 'dedicated');
    assert.equal(openOptions.at(-1)?.near?.id, caller.json.id);

    const named = await call('open_tab', { path: project, prompt: 'n', ide: `jetbrains-${process.pid}` });
    assert.equal(named.json.ide, `jetbrains-${process.pid}`, 'a named ide wins over tabRouting');
    for (const id of [byProject.json.id, byCaller.json.id, named.json.id]) assert.ok(!(await call('close_tab', { id })).isError);
  } finally {
    delete env.IDE_AGENT_TABS_ID;
    writeFileSync(path.join(home, 'config.json'), '{}');
    await call('close_tab', { id: caller.json.id });
  }
});

test('with no IDE running, open_tab uses the preferred terminal, or explains why it cannot', async () => {
  const live = path.join(endpoints, `jetbrains-${process.pid}.json`);
  const saved = readFileSync(live, 'utf8');
  writeFileSync(live, saved.replace(`"protocol":1`, `"protocol":7`));
  try {
    writeFileSync(path.join(home, 'config.json'), JSON.stringify({ terminal: 'fake-term' }));
    const viaConfig = await call('open_tab', { path: outside });
    assert.equal(viaConfig.json.ide, 'fake-term');
    assert.match(viaConfig.json.reason, /preferred terminal from config.json/);

    writeFileSync(path.join(home, 'config.json'), JSON.stringify({ terminal: 'kitty' }));
    assert.match((await call('open_tab', { path: outside })).text, /names terminal "kitty"/);

    writeFileSync(path.join(home, 'config.json'), '{}');
    assert.match((await call('open_tab', { path: outside })).text, /no supported terminal is available/);
  } finally {
    writeFileSync(live, saved);
  }
  assert.ok(existsSync(live));
});

test('the server reports the version of its package and of the Claude Code plugin', () => {
  const version = (file: string) => JSON.parse(readFileSync(path.join(import.meta.dirname, file), 'utf8')).version;
  assert.equal(SERVER_VERSION, version('../package.json'));
  assert.equal(SERVER_VERSION, version('../../claude-plugin/.claude-plugin/plugin.json'));
});

test('open_tab passes a model and via to the IDE, and builds the model flag into a terminal launch', async () => {
  await call('open_tab', { path: project, agent: 'codex', model: 'gpt-5.5', via: 'direct' });
  assert.deepEqual(seen.at(-1)!.body, { path: path.normalize(project), agent: 'codex', model: 'gpt-5.5', via: 'direct', focus: false });
  const term = await call('open_tab', { path: project, agent: 'claude', model: 'opus', ide: 'fake-term' });
  assert.ok(!term.isError, term.text);
  assert.deepEqual(opened.at(-1)!.spec.args.slice(0, 2), ['--model', 'opus']);
  const noOri = await call('open_tab', { path: project, agent: 'claude', via: 'ori', ide: 'fake-term' });
  assert.ok(noOri.isError);
  assert.match(noOri.text, /claude can't launch through Ori: Ori isn't installed/);
  const badModel = await call('open_tab', { path: project, model: 'two words' });
  assert.ok(badModel.isError);
  for (const id of [term.json.id]) await call('close_tab', { id });
});

test('open_tab focus: auto follows the call, always and never decide alone, for IDE and terminal tabs', async () => {
  const cases: [string | undefined, boolean | undefined, boolean][] = [
    [undefined, undefined, false],
    ['auto', undefined, false],
    ['auto', true, true],
    ['auto', false, false],
    ['always', undefined, true],
    ['always', true, true],
    ['always', false, true],
    ['never', undefined, false],
    ['never', true, false],
    ['never', false, false],
  ];
  const opened: string[] = [];
  try {
    for (const [setting, focus, expected] of cases) {
      writeFileSync(path.join(home, 'config.json'), JSON.stringify(setting === undefined ? {} : { focusNewTabs: setting }));
      const label = `${setting} with ${focus}`;
      const args = { path: project, ...(focus !== undefined ? { focus } : {}) };
      const ide = await call('open_tab', args);
      assert.ok(!ide.isError, ide.text);
      assert.equal(seen.at(-1)!.body.focus, expected, `IDE: ${label}`);
      const term = await call('open_tab', { ...args, ide: 'fake-term' });
      assert.ok(!term.isError, term.text);
      assert.equal(openOptions.at(-1)?.focus, expected, `terminal: ${label}`);
      opened.push(ide.json.id, term.json.id);
    }
    writeFileSync(path.join(home, 'config.json'), JSON.stringify({ focusNewTabs: 'sometimes' }));
    const agents = await call('list_agents');
    assert.ok(agents.json.warnings.some((w: string) => /focusNewTabs/.test(w)));
    await call('open_tab', { path: project });
    assert.equal(seen.at(-1)!.body.focus, false, 'an unknown value falls back to auto');
    opened.push(ideTabs.at(-1)!.id as string);
    assert.ok((await call('open_tab', { path: project, focus: 'yes' })).isError);
  } finally {
    writeFileSync(path.join(home, 'config.json'), '{}');
    for (const id of opened) await call('close_tab', { id });
  }
});

test('list_agents says which agents take a model', async () => {
  const { json } = await call('list_agents');
  assert.equal(json.agents.find((a: { name: string }) => a.name === 'claude').model, true);
  assert.ok(['direct', 'ori'].includes(json.launchVia));
});

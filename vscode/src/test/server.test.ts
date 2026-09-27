import assert from 'node:assert/strict';
import * as fs from 'node:fs';
import * as http from 'node:http';
import * as os from 'node:os';
import * as path from 'node:path';
import { after, before, test } from 'node:test';
import { AgentProfile, AgentSettings } from '../profiles';
import { newToken } from '../registry';
import { OpenRequest } from '../request';
import { apiUrl, createApiServer, Host, listen, route, TabInfo } from '../server';

const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'iat-server-'));
const home = path.join(dir, 'home');
fs.mkdirSync(home);
fs.writeFileSync(path.join(home, 'agents.json'), JSON.stringify({ probe: { label: 'Probe', command: 'probe-cli', promptFlag: '-p' } }));
const token = newToken();
const settings = new AgentSettings(home, () => {});
const opened: { request: OpenRequest; profile: AgentProfile }[] = [];
const tabs = new Map<string, TabInfo>();
let folderOpen = true;

const host: Host = {
  info: () => ({ ide: 'vscode', product: 'Test Code', version: '1.100.0', pid: 7, projects: [{ name: 'repo', path: dir, focused: true }] }),
  isInstalled: p => p.name === 'probe',
  open: (request, profile) => {
    if (!folderOpen) return undefined;
    opened.push({ request, profile });
    const tab = { id: `tab-${tabs.size + 1}`, agent: profile.name, project: 'repo', path: request.path };
    tabs.set(tab.id, tab);
    return tab;
  },
  close: id => tabs.delete(id),
  list: () => [...tabs.values()],
};

const server = createApiServer(token, host, settings);
let base = '';

before(async () => {
  base = apiUrl(await listen(server));
});
after(() => server.close());

function call(name: string, body: unknown, options: { method?: string; headers?: Record<string, string>; raw?: string } = {}) {
  const text = options.raw ?? (body === undefined ? '' : JSON.stringify(body));
  const headers = options.headers ?? { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` };
  return new Promise<{ status: number; json: any; type: string | undefined }>((resolve, reject) => {
    const req = http.request(`${base}/${name}`, { method: options.method ?? 'POST', headers }, res => {
      let data = '';
      res.setEncoding('utf8');
      res.on('data', d => (data += d));
      res.on('end', () => resolve({ status: res.statusCode!, json: JSON.parse(data), type: res.headers['content-type'] }));
    });
    req.on('error', reject);
    req.end(options.method === 'GET' ? undefined : text);
  });
}

test('url is the loopback API base', () => {
  assert.match(base, /^http:\/\/127\.0\.0\.1:\d+\/ide-agent-tabs$/);
});

test('routes sit under the API base only', () => {
  assert.equal(route('/ide-agent-tabs/open'), 'open');
  assert.equal(route('/ide-agent-tabs/list?x=1'), 'list');
  assert.equal(route('/ide-agent-tabs/nope'), undefined);
  assert.equal(route('/other/open'), undefined);
  assert.equal(route('/ide-agent-tabs'), undefined);
});

test('info replies with the window and its folders', async () => {
  const { status, json, type } = await call('info', {});
  assert.equal(status, 200);
  assert.equal(type, 'application/json; charset=utf-8');
  assert.deepEqual(json, { ok: true, ide: 'vscode', product: 'Test Code', version: '1.100.0', pid: 7, projects: [{ name: 'repo', path: dir, focused: true }] });
  assert.equal((await call('info', undefined)).status, 200);
});

test('agents lists every profile with the default and installed flags', async () => {
  const { status, json } = await call('agents', {});
  assert.equal(status, 200);
  assert.equal(json.default, 'claude');
  assert.deepEqual(json.agents.at(-1), { name: 'probe', label: 'Probe', command: 'probe-cli', installed: true });
  assert.deepEqual(json.agents.map((a: { name: string }) => a.name), ['claude', 'codex', 'gemini', 'copilot', 'probe']);
});

test('open, list, close, and close again', async () => {
  const opening = await call('open', { path: dir, agent: 'probe', prompt: 'hi', args: ['--x'], env: { FOO: 'bar' } });
  assert.equal(opening.status, 200);
  assert.deepEqual(opening.json, { ok: true, id: 'tab-1', agent: 'probe', project: 'repo', path: dir });
  assert.deepEqual(opened.at(-1), { request: { path: dir, prompt: 'hi', args: ['--x'], env: { FOO: 'bar' }, agent: 'probe' }, profile: settings.profile('probe') });

  assert.deepEqual((await call('list', {})).json, { ok: true, tabs: [{ id: 'tab-1', agent: 'probe', project: 'repo', path: dir }] });
  assert.deepEqual((await call('close', { id: 'tab-1' })).json, { ok: true, id: 'tab-1' });
  const again = await call('close', { id: 'tab-1' });
  assert.equal(again.status, 404);
  assert.equal(again.json.ok, false);
  assert.match(again.json.error, /no open agent tab/);
  assert.deepEqual((await call('list', {})).json, { ok: true, tabs: [] });
});

test('open without an agent uses the default', async () => {
  fs.writeFileSync(path.join(home, 'config.json'), '{"defaultAgent":"codex"}');
  const { json } = await call('open', { path: dir });
  assert.equal(json.agent, 'codex');
  fs.rmSync(path.join(home, 'config.json'));
});

test('bad requests get 400 with an error', async () => {
  for (const [name, body] of [
    ['open', { path: 'relative' }],
    ['open', { path: path.join(dir, 'absent') }],
    ['open', { path: dir, agent: 'nobody' }],
    ['open', { path: dir, env: { IDE_AGENT_TABS_ID: 'x' } }],
    ['open', { path: dir, prompt: 'x'.repeat(30_001) }],
    ['open', { path: dir, args: Array(65).fill('x') }],
    ['close', {}],
    ['list', []],
  ] as const) {
    const { status, json } = await call(name, body);
    assert.equal(status, 400, `${name} ${JSON.stringify(body).slice(0, 80)}`);
    assert.equal(json.ok, false);
    assert.equal(typeof json.error, 'string');
  }
  assert.equal((await call('open', undefined, { raw: 'not json' })).status, 400);
  assert.match((await call('open', { path: dir, agent: 'nobody' })).json.error, /unknown agent: nobody/);
});

test('open with no folder open is a conflict', async () => {
  folderOpen = false;
  try {
    const { status, json } = await call('open', { path: dir });
    assert.equal(status, 409);
    assert.equal(json.ok, false);
  } finally {
    folderOpen = true;
  }
});

test('admission rules apply over HTTP', async () => {
  assert.equal((await call('info', {}, { headers: { 'Content-Type': 'application/json' } })).status, 401);
  assert.equal((await call('info', {}, { headers: { 'Content-Type': 'application/json', Authorization: 'Bearer nope' } })).status, 401);
  assert.equal((await call('info', {}, { headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'text/plain' } })).status, 415);
  assert.equal(
    (await call('info', {}, { headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}`, Origin: 'https://example.com' } })).status,
    403,
  );
  assert.equal((await call('info', {}, { headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}`, Referer: 'x' } })).status, 403);
  assert.equal((await call('info', undefined, { method: 'GET' })).status, 405);
  const unknown = await call('nope', {});
  assert.equal(unknown.status, 404);
  assert.equal(unknown.json.ok, false);
});

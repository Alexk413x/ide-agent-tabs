import assert from 'node:assert/strict';
import * as fs from 'node:fs';
import * as http from 'node:http';
import * as os from 'node:os';
import * as path from 'node:path';
import { after, before, test } from 'node:test';
import { AgentLaunch, AgentProfile, AgentSettings } from '../profiles';
import { newToken } from '../registry';
import { checkRevealTarget, OpenRequest, RevealDeps } from '../request';
import { apiUrl, createApiServer, Host, listen, route, systemReveal, TabInfo } from '../server';

const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'iat-server-'));
const home = path.join(dir, 'home');
fs.mkdirSync(home);
fs.writeFileSync(path.join(home, 'agents.json'), JSON.stringify({ probe: { label: 'Probe', command: 'probe-cli', promptFlag: '-p' } }));
const token = newToken();
const settings = new AgentSettings(home, () => {});
const opened: { request: OpenRequest; profile: AgentProfile; launch: AgentLaunch }[] = [];
const tabs = new Map<string, TabInfo>();
const typed: { id: string; text: string }[] = [];
let folderOpen = true;
const revealed: string[] = [];
let revealWorks = true;

const host: Host = {
  info: () => ({ ide: 'vscode', product: 'Test Code', version: '1.100.0', pid: 7, projects: [{ name: 'repo', path: dir, focused: true }] }),
  isInstalled: p => p.name === 'probe',
  open: (request, profile, launch) => {
    if (!folderOpen) return undefined;
    opened.push({ request, profile, launch });
    const tab = { id: `tab-${tabs.size + 1}`, agent: profile.name, project: 'repo', path: request.path };
    tabs.set(tab.id, tab);
    return tab;
  },
  close: id => tabs.delete(id),
  input: (id, text) => {
    if (!tabs.has(id)) return false;
    typed.push({ id, text });
    return true;
  },
  list: () => [...tabs.values()],
  reveal: async target => {
    revealed.push(target);
    return revealWorks;
  },
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
  assert.equal(route('/ide-agent-tabs/input'), 'input');
  assert.equal(route('/ide-agent-tabs/reveal'), 'reveal');
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
  assert.deepEqual(json.agents.map((a: { name: string }) => a.name), ['claude', 'codex', 'agy', 'copilot', 'gemini', 'grok', 'pi', 'hermes', 'opencode', 'qwen', 'goose', 'codex-local', 'probe']);
});

test('open, list, close, and close again', async () => {
  const opening = await call('open', { path: dir, agent: 'probe', prompt: 'hi', args: ['--x'], env: { FOO: 'bar' } });
  assert.equal(opening.status, 200);
  assert.deepEqual(opening.json, { ok: true, id: 'tab-1', agent: 'probe', project: 'repo', path: dir, via: 'direct' });
  assert.deepEqual(opened.at(-1)?.request, { path: dir, prompt: 'hi', args: ['--x'], env: { FOO: 'bar' }, agent: 'probe', model: undefined, via: undefined, focus: false });
  assert.equal(opened.at(-1)?.profile, settings.profile('probe'));
  assert.deepEqual(opened.at(-1)?.launch, { agent: 'probe', command: 'probe-cli', args: ['--x', '-p'], prompt: 'hi', env: { FOO: 'bar' }, via: 'direct' });

  assert.deepEqual((await call('list', {})).json, { ok: true, tabs: [{ id: 'tab-1', agent: 'probe', project: 'repo', path: dir }] });
  assert.deepEqual((await call('close', { id: 'tab-1' })).json, { ok: true, id: 'tab-1' });
  const again = await call('close', { id: 'tab-1' });
  assert.equal(again.status, 404);
  assert.equal(again.json.ok, false);
  assert.match(again.json.error, /no open agent tab/);
  assert.deepEqual((await call('list', {})).json, { ok: true, tabs: [] });
});

test('input types into an open tab and 404s once it closes', async () => {
  const { json: tab } = await call('open', { path: dir, agent: 'probe' });
  const text = 'Agent Tabs: new message from codex 1a2b. Call read_messages.';
  assert.deepEqual((await call('input', { id: tab.id, text })).json, { ok: true, id: tab.id });
  assert.deepEqual(typed.at(-1), { id: tab.id, text });

  await call('close', { id: tab.id });
  const gone = await call('input', { id: tab.id, text });
  assert.equal(gone.status, 404);
  assert.equal(gone.json.ok, false);
  assert.match(gone.json.error, /no open agent tab/);
  assert.equal(typed.length, 1);
});

test('open passes focus to the host, false unless the request asks', async () => {
  for (const [fields, focus] of [[{}, false], [{ focus: true }, true], [{ focus: false }, false]] as const) {
    const { status, json } = await call('open', { path: dir, agent: 'probe', ...fields });
    assert.equal(status, 200);
    assert.equal(opened.at(-1)!.request.focus, focus, JSON.stringify(fields));
    tabs.delete(json.id);
  }
  assert.equal((await call('open', { path: dir, agent: 'probe', focus: 'yes' })).status, 400);
});

test('open passes the model to the launch line and reports via', async () => {
  const { status, json } = await call('open', { path: dir, agent: 'claude', model: 'opus' });
  assert.equal(status, 200);
  assert.equal(json.via, 'direct');
  assert.deepEqual(opened.at(-1)?.launch.args, ['--model', 'opus']);
  const noOption = await call('open', { path: dir, agent: 'probe', model: 'opus' });
  assert.equal(noOption.status, 400);
  assert.equal(noOption.json.error, 'probe has no model option; open it without model, or set modelFlag for it in agents.json');
  assert.equal((await call('open', { path: dir, model: 'bad model' })).status, 400);
});

test('open through Ori follows via, then the launchVia setting, then Ori detection', async () => {
  const noOri = await call('open', { path: dir, agent: 'claude', via: 'ori' });
  assert.equal(noOri.status, 400);
  assert.equal(noOri.json.error, "claude can't launch through Ori: Ori is not installed");

  fs.writeFileSync(path.join(home, 'detected.json'), JSON.stringify({ ori: { path: '/x/ori', version: '0.14.3', agents: ['claude'] } }));
  try {
    const explicit = await call('open', { path: dir, agent: 'claude', via: 'ori', model: 'anthropic/claude-sonnet-4.5' });
    assert.equal(explicit.json.via, 'ori');
    assert.equal(opened.at(-1)?.launch.command, 'ori');
    assert.deepEqual(opened.at(-1)?.launch.args, ['claude', '--model', 'anthropic/claude-sonnet-4.5']);
    assert.equal(opened.at(-1)?.launch.agent, 'claude');

    assert.equal((await call('open', { path: dir, agent: 'claude' })).json.via, 'direct');
    fs.writeFileSync(path.join(home, 'config.json'), '{"launchVia":"ori"}');
    assert.equal((await call('open', { path: dir, agent: 'claude' })).json.via, 'ori');
    assert.equal((await call('open', { path: dir, agent: 'claude', via: 'direct' })).json.via, 'direct');
    const fallback = await call('open', { path: dir, agent: 'probe' });
    assert.equal(fallback.status, 200);
    assert.equal(fallback.json.via, 'direct');
  } finally {
    fs.rmSync(path.join(home, 'detected.json'), { force: true });
    fs.rmSync(path.join(home, 'config.json'), { force: true });
  }
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
    ['input', { text: 'hi' }],
    ['input', { id: 'tab-1' }],
    ['input', { id: 'tab-1', text: 'two\nlines' }],
    ['input', { id: 'tab-1', text: '\u001b[A' }],
    ['input', { id: 'tab-1', text: 'x'.repeat(501) }],
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

test('reveal shows a known folder through the host and refuses anything else', async () => {
  revealed.length = 0;
  const file = path.join(dir, 'note.txt');
  fs.writeFileSync(file, 'x');
  const okReply = await call('reveal', { path: dir });
  assert.equal(okReply.status, 200);
  assert.deepEqual(okReply.json, { ok: true, path: fs.realpathSync.native(dir) });
  assert.deepEqual(revealed, [fs.realpathSync.native(dir)]);
  const outside = fs.mkdtempSync(path.join(os.tmpdir(), 'iat-outside-'));
  for (const [body, error] of [
    [{}, 'path is required'],
    [{ path: 'relative/dir' }, 'path must be absolute: relative/dir'],
    [{ path: path.join(dir, 'missing') }, `not a folder on this machine: ${path.join(dir, 'missing')}`],
    [{ path: file }, `not a folder on this machine: ${file}`],
    [{ path: outside }, `refused: ${outside} is not a folder of this window or its agent tabs`],
    [{ path: `${dir}\n` }, 'path must have no control characters'],
  ] as const) {
    const refused = await call('reveal', body);
    assert.equal(refused.status, 400, JSON.stringify(body));
    assert.equal(refused.json.error, error);
  }
  assert.equal(revealed.length, 1, 'a refused path never reaches the host');
  revealWorks = false;
  const failed = await call('reveal', { path: dir });
  assert.equal(failed.status, 500);
  revealWorks = true;
});

test('reveal resolves links first, refuses one that lands outside the known folders, and refuses macOS bundles', () => {
  const real = new Map<string, string>([
    ['/work/repo', '/work/repo'],
    ['/work/link', '/elsewhere/secret'],
    ['/Applications/Foo.app/Contents', '/Applications/Foo.app/Contents'],
    ['/work/repo/', '/work/repo'],
  ]);
  const deps = (platform: NodeJS.Platform): RevealDeps => ({ realpath: p => real.get(p), isDirectory: () => true, platform });
  assert.equal(checkRevealTarget('/work/repo', ['/work/repo/'], deps('linux')), '/work/repo');
  assert.throws(() => checkRevealTarget('/work/link', ['/work/repo'], deps('linux')), /refused: \/work\/link is not a folder/);
  assert.throws(() => checkRevealTarget('/Applications/Foo.app/Contents', ['/Applications/Foo.app/Contents'], deps('darwin')), /inside a macOS bundle/);
  assert.equal(checkRevealTarget('/Applications/Foo.app/Contents', ['/Applications/Foo.app/Contents'], deps('linux')), '/Applications/Foo.app/Contents');
  assert.throws(() => checkRevealTarget('/work/repo', [], deps('linux')), /not a folder of this window/);
  if (process.platform !== 'win32') {
    const linked = path.join(dir, 'away');
    fs.symlinkSync(fs.mkdtempSync(path.join(os.tmpdir(), 'iat-away-')), linked);
    assert.throws(() => checkRevealTarget(linked, [dir], systemReveal), /is not a folder of this window/);
  }
});

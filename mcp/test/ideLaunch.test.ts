import assert from 'node:assert/strict';
import { existsSync, mkdirSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import { handoffPath, Handoffs } from '../src/handoff.js';
import type { IdeInstall } from '../src/ideInstalls.js';
import type { Endpoint } from '../src/registry.js';
import { Service } from '../src/service.js';
import type { TerminalDriver } from '../src/terminals/types.js';
import { tempDir } from './tempDir.js';

interface Ide {
  product: string;
  projects: string[];
  tabs: string[];
  startedAt: number;
}

const STUDIO: IdeInstall = { key: 'android-studio', product: 'Android Studio', kind: 'jetbrains', version: '2026.1', launcher: '/opt/android-studio/bin/studio.sh' };
const CODE: IdeInstall = { key: 'vscode', product: 'VS Code', kind: 'vscode', launcher: '/usr/bin/code' };

function setup(options: { installs?: IdeInstall[]; timeoutSec?: number; callerTab?: string; terminal?: boolean; register?: (folder: string) => void; launchError?: string; syncMs?: number } = {}) {
  const home = tempDir('iat-launch-');
  const work = tempDir('iat-launch-w-');
  const other = tempDir('iat-launch-o-');
  mkdirSync(path.join(home, 'endpoints'));
  writeFileSync(path.join(home, 'config.json'), JSON.stringify({ ideStartTimeoutSec: options.timeoutSec ?? 0.4, terminal: 'fake-term' }));
  const ides = new Map<string, Ide>();
  const opens: { ide: string; body: Record<string, unknown> }[] = [];
  const launches: { install: IdeInstall; folder: string }[] = [];
  const terminalOpens: string[] = [];
  const logs: string[] = [];
  let tabs = 0;

  const addIde = (id: string, product: string, projects: string[], startedAt = Date.now(), tabsInIde: string[] = []) => {
    ides.set(id, { product, projects, tabs: tabsInIde, startedAt });
    writeFileSync(
      path.join(home, 'endpoints', `${id}.json`),
      JSON.stringify({ protocol: 1, ide: 'jetbrains', product, version: '1', pid: process.pid, url: 'http://127.0.0.1:9/ide-agent-tabs', token: 't', startedAt }),
    );
  };

  const driver: TerminalDriver = {
    name: 'fake-term',
    label: 'Fake Terminal',
    capabilities: { open: 'tab', list: 'yes', close: 'yes' },
    available: async () => options.terminal !== false,
    open: async (_ctx, spec) => {
      terminalOpens.push(spec.id);
      return { id: spec.id, terminal: 'fake-term', agent: spec.agent, path: spec.cwd, createdAt: Date.now() };
    },
    alive: async (_ctx, list) => new Set(list.map((t) => t.id)),
    close: async () => undefined,
  };

  const service = new Service({
    home,
    scriptsDir: home,
    platform: 'linux',
    env: { PATH: '', ...(options.callerTab ? { IDE_AGENT_TABS_ID: options.callerTab } : {}) },
    isAlive: () => true,
    drivers: [driver],
    newId: () => `term-${++tabs}`,
    log: (m) => logs.push(m),
    ideWait: { pollMs: 10, syncMs: options.syncMs ?? 60, progressMs: 20 },
    callIde: async (endpoint: Endpoint, route, body) => {
      const ide = ides.get(endpoint.id);
      if (!ide) throw new Error(`${endpoint.id} is gone`);
      if (route === 'info') return { product: ide.product, projects: ide.projects.map((p) => ({ name: path.basename(p), path: p, focused: false })) };
      if (route === 'list') return { tabs: ide.tabs.map((id) => ({ id })) };
      if (route === 'open') {
        const id = `tab-${++tabs}`;
        opens.push({ ide: endpoint.id, body: body as Record<string, unknown> });
        ide.tabs.push(id);
        return { id, agent: 'claude', project: 'p', path: (body as { path: string }).path };
      }
      return {};
    },
    ides: {
      discover: () => options.installs ?? [],
      launch: async (install, folder) => {
        launches.push({ install, folder });
        if (options.launchError) throw new Error(options.launchError);
        options.register?.(folder);
      },
    },
  });
  return { home, work, other, service, addIde, opens, launches, terminalOpens, logs, ides };
}

const open = (service: Service, ...args: Parameters<Service['openTab']>): Promise<Record<string, unknown>> => service.openTab(...args);
const later = (ms: number, fn: () => void) => void setTimeout(fn, ms);

test('a named IDE that runs takes the tab: the one whose project holds the path, else the most recently started', async () => {
  const t = setup({ installs: [STUDIO] });
  t.addIde('jetbrains-1', 'Android Studio', [t.work], 1000);
  t.addIde('jetbrains-2', 'Android Studio', [t.other], 2000);
  t.addIde('jetbrains-3', 'IntelliJ IDEA', [t.work], 3000);
  const near = await open(t.service, { path: t.work, ide: 'Android Studio' });
  assert.equal(near.ide, 'jetbrains-1');
  assert.match(String(near.reason), /Android Studio is running; an open project contains the path/);
  const far = await open(t.service, { path: path.dirname(t.work), ide: 'STUDIO' });
  assert.equal(far.ide, 'jetbrains-2');
  assert.match(String(far.reason), /most recently started/);
  assert.deepEqual(t.launches, []);
  const byId = await open(t.service, { path: t.work, ide: 'jetbrains-3' });
  assert.equal(byId.ide, 'jetbrains-3');
});

test('an unknown name is an error, and a product outside the catalog matches a running IDE', async () => {
  const t = setup();
  await assert.rejects(open(t.service, { path: t.work, ide: 'notepad' }), /no running IDE or terminal with id notepad, and no IDE by that name/);
  t.addIde('jetbrains-9', 'Fake Studio', [t.work]);
  assert.equal((await open(t.service, { path: t.work, ide: 'fake studio' })).ide, 'jetbrains-9');
});

test('an installed IDE starts with the folder, and the tab opens once a new endpoint answers with a project', async () => {
  let t!: ReturnType<typeof setup>;
  t = setup({
    installs: [CODE, STUDIO],
    timeoutSec: 5,
    syncMs: 3000,
    register: (folder) => {
      later(20, () => t.addIde('jetbrains-new', 'Android Studio', []));
      later(50, () => t.ides.set('jetbrains-new', { ...t.ides.get('jetbrains-new')!, projects: [folder] }));
    },
  });
  t.addIde('jetbrains-old', 'IntelliJ IDEA', [t.work]);
  const progress: number[] = [];
  const opened = await open(t.service, { path: t.work, ide: 'android-studio' }, { wait: 'background', onProgress: (ms) => progress.push(ms) });
  assert.equal(opened.ide, 'jetbrains-new');
  assert.equal(opened.reason, 'started Android Studio');
  assert.equal('pending' in opened, false);
  assert.deepEqual(t.launches.map((l) => [l.install.key, l.folder]), [['android-studio', t.work]]);
  assert.equal(progress[0], 0);
});

test('a slow IDE returns pending at once, and the server opens the tab there when it loads', async () => {
  let t!: ReturnType<typeof setup>;
  t = setup({ installs: [STUDIO], timeoutSec: 2, register: (folder) => later(200, () => t.addIde('jetbrains-new', 'Android Studio', [folder])) });
  const result = await open(t.service, { path: t.work, ide: 'Android Studio', prompt: 'hi' }, { wait: 'background' });
  assert.equal(result.pending, true);
  assert.equal(result.product, 'Android Studio');
  assert.equal('id' in result, false);
  assert.match(String(result.note), /Android Studio is starting\. The agent tab opens there once it loads, up to 2 s/);
  assert.equal(t.opens.length, 0);

  const again = await open(t.service, { path: t.work, ide: 'studio' }, { wait: 'background' });
  assert.equal(again.pending, true);
  assert.equal(t.launches.length, 1, 'a second call waits on the same launch');

  await t.service.settled();
  assert.deepEqual(t.opens.map((o) => o.ide), ['jetbrains-new', 'jetbrains-new']);
  assert.equal(t.opens[0]!.body.prompt, 'hi');
  assert.match(t.logs.join('\n'), /opened tab tab-\d+ in Android Studio after starting Android Studio/);
});

test("an IDE that never registers falls back in the background to the caller's IDE", async () => {
  const t = setup({ installs: [STUDIO], timeoutSec: 0.2, callerTab: 'caller-tab' });
  t.addIde('vscode-caller', 'Visual Studio Code', [t.other], Date.now(), ['caller-tab']);
  const result = await open(t.service, { path: t.work, ide: 'android studio' }, { wait: 'background' });
  assert.equal(result.pending, true);
  await t.service.settled();
  assert.deepEqual(t.opens.map((o) => o.ide), ['vscode-caller']);
  assert.match(t.logs.join('\n'), /in Visual Studio Code after starting Android Studio/);
});

test("a full wait falls back with the reason when the IDE never registers, and names the plugin to install", async () => {
  const t = setup({ installs: [STUDIO], timeoutSec: 0.1, callerTab: 'caller-tab' });
  t.addIde('vscode-caller', 'Visual Studio Code', [t.other], Date.now(), ['caller-tab']);
  const result = await open(t.service, { path: t.work, ide: 'android-studio' });
  assert.equal(result.ide, 'vscode-caller');
  assert.match(String(result.reason), /^Android Studio started but didn't register within 0\.1 s; if the Agent Tabs plugin isn't installed in it, run \/ide-agent-tabs:setup; the caller's IDE$/);
  assert.match(String(result.note), /so the tab opened in Visual Studio Code instead\.$/);
});

test("a name that isn't installed opens in the caller's IDE, else the caller's terminal, else the automatic route", async () => {
  const ide = setup({ installs: [CODE], callerTab: 'caller-tab' });
  ide.addIde('jetbrains-caller', 'IntelliJ IDEA', [ide.other], Date.now(), ['caller-tab']);
  const inIde = await open(ide.service, { path: ide.work, ide: 'Android Studio' });
  assert.equal(inIde.ide, 'jetbrains-caller');
  assert.match(String(inIde.note), /^Android Studio isn't installed, so the tab opened in IntelliJ IDEA instead\.$/);
  assert.deepEqual(ide.launches, []);

  const term = setup({ callerTab: 'term-1' });
  await open(term.service, { path: term.work, ide: 'fake-term' });
  const inTerminal = await open(term.service, { path: term.work, ide: 'pycharm' });
  assert.equal(inTerminal.ide, 'fake-term');
  assert.match(String(inTerminal.reason), /^PyCharm isn't installed; the caller's terminal$/);
  assert.deepEqual(term.terminalOpens, ['term-1', 'term-2']);

  const plain = setup();
  plain.addIde('jetbrains-5', 'IntelliJ IDEA', [plain.work]);
  const routed = await open(plain.service, { path: plain.work, ide: 'rider' });
  assert.equal(routed.ide, 'jetbrains-5');
  assert.match(String(routed.reason), /^Rider isn't installed; open project .* contains the path$/);
});

test('a launch that fails falls back at once', async () => {
  const t = setup({ installs: [STUDIO], launchError: 'spawn EACCES' });
  const result = await open(t.service, { path: t.work, ide: 'android-studio' }, { wait: 'background' });
  assert.equal(result.ide, 'fake-term');
  assert.match(String(result.note), /^Android Studio couldn't start: spawn EACCES, so the tab opened in Fake Terminal instead\./);
});

test('list-ides lists installed IDEs that are not running', async () => {
  const t = setup({ installs: [CODE, STUDIO, { ...STUDIO, version: '2025.3', launcher: '/opt/old/bin/studio.sh' }] });
  t.addIde('vscode-1', 'Visual Studio Code', [t.work]);
  const listed = await t.service.listIdes();
  assert.deepEqual(listed.installed, [{ name: 'android-studio', product: 'Android Studio', kind: 'jetbrains', version: '2026.1' }]);
});

test('a handoff to a named IDE waits for it, and closes nothing when no tab opens', async () => {
  const t = setup({ installs: [STUDIO], timeoutSec: 0.1, callerTab: 'old-session', terminal: false });
  const handoffs = (service: Service) =>
    new Handoffs({
      home: t.home,
      env: { IDE_AGENT_TABS_ID: 'old-session' },
      sessionId: () => 'old-session',
      openTab: (input) => service.openTab(input),
      findHost: (id) => service.findHost(id),
      randomId: () => 'h-0123456789ab',
    });
  await assert.rejects(handoffs(t.service).start({ path: t.work, goal: 'g', ide: 'android-studio' }), /the new tab did not open, so this session keeps the work and nothing was closed/);
  assert.equal(existsSync(handoffPath(t.home, 'h-0123456789ab', 'json')), false);
  assert.deepEqual(t.opens, []);

  let s!: ReturnType<typeof setup>;
  s = setup({ installs: [STUDIO], timeoutSec: 2, register: (folder) => later(150, () => s.addIde('jetbrains-new', 'Android Studio', [folder])) });
  const result = await new Handoffs({
    home: s.home,
    env: {},
    sessionId: () => 'old-session',
    openTab: (input) => s.service.openTab(input),
    findHost: (id) => s.service.findHost(id),
    randomId: () => 'h-0123456789ab',
  }).start({ path: s.work, goal: 'g', ide: 'Android Studio' });
  assert.equal(result.ide, 'jetbrains-new');
  assert.equal(s.opens.length, 1);
});

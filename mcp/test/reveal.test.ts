import assert from 'node:assert/strict';
import { mkdirSync, mkdtempSync, symlinkSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { test } from 'node:test';
import { IdeError, type Route } from '../src/ideClient.js';
import { checkRevealTarget, systemReveal, type RevealDeps } from '../src/reveal.js';
import { Service } from '../src/service.js';
import { tempDir } from './tempDir.js';

function setup(failing: string[] = [], openFails = false) {
  const home = tempDir('iat-reveal-');
  const project = tempDir('iat-reveal-p-');
  const session = tempDir('iat-reveal-s-');
  mkdirSync(path.join(home, 'endpoints'));
  const base = { protocol: 1, version: '1.0', token: 't', pid: process.pid };
  writeFileSync(path.join(home, 'endpoints', 'jetbrains-1.json'), JSON.stringify({ ...base, ide: 'jetbrains', product: 'Android Studio', url: 'http://127.0.0.1:1/ide-agent-tabs' }));
  writeFileSync(path.join(home, 'endpoints', 'vscode-2.json'), JSON.stringify({ ...base, ide: 'vscode', product: 'Antigravity', url: 'http://127.0.0.1:2/ide-agent-tabs' }));
  const calls: [string, Route, unknown][] = [];
  const opened: string[] = [];
  const reveal: RevealDeps = {
    ...systemReveal(process.platform),
    open: async (folder) => {
      if (openFails) throw new Error('spawn explorer.exe ENOENT');
      opened.push(folder);
    },
  };
  const service = new Service({
    home,
    scriptsDir: home,
    platform: process.platform,
    env: { PATH: '' },
    drivers: [],
    reveal,
    callIde: async (endpoint, route, body) => {
      calls.push([endpoint.id, route, body]);
      if (route === 'info') return { ok: true, projects: endpoint.id.startsWith('jetbrains') ? [{ name: 'p', path: project, focused: true }] : [] };
      if (failing.includes(endpoint.id)) throw new IdeError(`${endpoint.id} reveal answered HTTP 404: no such route`, 404);
      return { ok: true };
    },
  });
  return { home, project, session, service, calls, opened, reveals: () => calls.filter((c) => c[1] === 'reveal').map((c) => c[0]) };
}

test("reveal goes to the session's own IDE first, then any other, then the OS file manager, and returns ok: false when none can", async () => {
  const s = setup();
  const ids = (await s.service.reveal(s.session, undefined, [s.session])) as { ok: true; ide: string };
  assert.equal(ids.ok, true);
  const [first, second] = ['jetbrains-1', 'vscode-2'];
  s.calls.length = 0;
  const own = await s.service.reveal(s.session, second, [s.session]);
  assert.deepEqual([own.ok, s.reveals()], [true, [second]]);

  const f = setup(['vscode-2']);
  const fallback = await f.service.reveal(f.project, 'vscode-2', []);
  assert.equal(fallback.ok, true);
  assert.deepEqual(f.reveals(), ['vscode-2', first], 'an IDE without the route is skipped');

  assert.deepEqual([s.opened, f.opened], [[], []], 'the file manager runs only when no IDE can reveal');

  const sys = setup(['vscode-2', 'jetbrains-1']);
  const system = await sys.service.reveal(sys.session, undefined, [sys.session]);
  assert.deepEqual([system.ok, (system as { ide: string }).ide, sys.opened.length], [true, 'system', 1]);

  const n = setup(['vscode-2', 'jetbrains-1'], true);
  const none = await n.service.reveal(n.session, undefined, [n.session]);
  assert.equal(none.ok, false);
  assert.match((none as { reason: string }).reason, /no such route.*ENOENT/);
});

test('reveal refuses a file, a folder no session or project has, and never asks an IDE then', async () => {
  const s = setup();
  const file = path.join(s.session, 'note.txt');
  writeFileSync(file, 'x');
  const outside = tempDir('iat-reveal-o-');
  for (const [target, reason] of [
    [file, /not a folder on this machine/],
    [outside, /is not the folder of a live session or an open IDE project/],
    ['relative/dir', /not an absolute path/],
  ] as const) {
    const refused = await s.service.reveal(target, undefined, [s.session]);
    assert.equal(refused.ok, false);
    assert.match((refused as { reason: string }).reason, reason);
  }
  assert.deepEqual(s.reveals(), []);
  assert.equal((await s.service.reveal(s.project, undefined, [])).ok, true, "an open IDE project's folder is known");
});

test('reveal resolves links before it compares, and refuses a macOS bundle', async () => {
  const links = new Map([
    ['/work/repo', '/work/repo'],
    ['/work/link', '/elsewhere/secret'],
    ['/Applications/Foo.app/Contents', '/Applications/Foo.app/Contents'],
  ]);
  const deps = (platform: NodeJS.Platform): RevealDeps => ({ realpath: async (p) => links.get(p), isDirectory: async () => true, open: async () => undefined, platform });
  assert.equal(await checkRevealTarget('/work/repo', ['/work/repo'], deps('linux')), '/work/repo');
  await assert.rejects(checkRevealTarget('/work/link', ['/work/repo'], deps('linux')), /is not the folder of a live session/);
  await assert.rejects(checkRevealTarget('/Applications/Foo.app/Contents', ['/Applications/Foo.app/Contents'], deps('darwin')), /inside a macOS bundle/);
  if (process.platform !== 'win32') {
    const known = mkdtempSync(path.join(os.tmpdir(), 'iat-known-'));
    const linked = path.join(known, 'away');
    symlinkSync(mkdtempSync(path.join(os.tmpdir(), 'iat-away-')), linked);
    await assert.rejects(checkRevealTarget(linked, [known], systemReveal(process.platform)), /is not the folder of a live session/);
  }
});

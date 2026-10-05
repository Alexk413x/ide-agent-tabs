import assert from 'node:assert/strict';
import { test } from 'node:test';
import { chooseIde, chooseTerminal, projectDepth, type IdeCandidate } from '../src/routing.js';

test('a project contains a path by whole segments', () => {
  assert.equal(projectDepth('/work/app', '/work/app', false), 2);
  assert.equal(projectDepth('/work/app', '/work/app/src/x', false), 2);
  assert.equal(projectDepth('/work/app', '/work/application', false), undefined);
  assert.equal(projectDepth('/work/app/', '/work/app/src', false), 2);
  assert.equal(projectDepth('/Work/App', '/work/app', false), undefined);
  assert.equal(projectDepth('C:\\Work\\App', 'c:/work/app/src', true), 3);
  assert.equal(projectDepth('C:\\Work\\App', 'D:\\Work\\App', true), undefined);
  assert.equal(projectDepth('', '/x', false), undefined);
});

const ide = (id: string, startedAt: number, projects: [string, string, boolean][]): IdeCandidate => ({
  id,
  startedAt,
  projects: projects.map(([name, p, focused]) => ({ name, path: p, focused })),
});

test("the IDE whose project contains the path wins: the deepest project, then the caller's IDE, then the focused window", () => {
  const a = ide('a', 1, [['app', '/w/app', false]]);
  const b = ide('b', 2, [['lib', '/w/lib', true]]);
  const c = ide('c', 3, [['w', '/w', true]]);
  assert.equal(chooseIde([a, b], '/w/app/src', false)?.id, 'a');
  assert.equal(chooseIde([a, c], '/w/app/src', false)?.id, 'a');
  assert.equal(chooseIde([ide('e', 1, [['w', '/w', false]]), c], '/w/app', false)?.id, 'c');
  assert.equal(chooseIde([ide('e', 1, [['w', '/w', false]]), c], '/w/app', false, 'e')?.id, 'e');
  assert.match(chooseIde([ide('e', 1, [['w', '/w', false]]), c], '/w/app', false, 'e')!.reason, /the caller's IDE/);
  assert.equal(chooseIde([a, ide('d', 0, [['w', '/w', false]])], '/w/app', false)?.id, 'a');
  assert.equal(chooseIde([ide('x', 5, [['app', '/w/app', false]]), ide('y', 9, [['app', '/w/app', false]])], '/w/app', false)?.id, 'y');
  assert.match(chooseIde([a], '/w/app', false)!.reason, /open project app contains the path/);
});

test("with no containing project, the caller's IDE wins when it is running", () => {
  const old = ide('old', 1, [['p', '/p', true]]);
  const recent = ide('recent', 5, [['q', '/q', false]]);
  const choice = chooseIde([old, recent], '/elsewhere', false, 'old');
  assert.equal(choice?.id, 'old');
  assert.equal(choice?.reason, "no open project contains the path; the caller's IDE");
  assert.equal(chooseIde([old, recent], '/elsewhere', false, 'gone')?.id, 'recent');
  assert.equal(chooseIde([old, recent], '/elsewhere', false)?.id, 'recent');
  assert.equal(chooseIde([old, recent], '/q/src', false, 'old')?.id, 'recent');
});

test('with no containing project, the most recently started IDE with a project wins', () => {
  const old = ide('old', 1, [['p', '/p', true]]);
  const recent = ide('recent', 5, [['q', '/q', false]]);
  const empty = ide('empty', 9, []);
  assert.equal(chooseIde([old, recent, empty], '/elsewhere', false)?.id, 'recent');
  assert.equal(chooseIde([empty], '/elsewhere', false), undefined);
  assert.equal(chooseIde([], '/elsewhere', false), undefined);
});

test('terminal choice: preferred, then platform default, else an error', () => {
  assert.deepEqual(chooseTerminal('ghostty', 'ghostty', ['ghostty']), { name: 'ghostty', reason: 'no IDE is running; preferred terminal from config.json' });
  assert.ok('error' in chooseTerminal('kitty', 'windows-terminal', ['windows-terminal']));
  assert.equal((chooseTerminal(undefined, 'windows-terminal', ['windows-terminal']) as { name: string }).name, 'windows-terminal');
  assert.ok('error' in chooseTerminal(undefined, undefined, []));
});

test("caller routing picks the caller's IDE even when another IDE has the project", () => {
  const owner = ide('owner', 9, [['app', '/w/app', true]]);
  const caller = ide('caller', 1, [['other', '/o', false]]);
  const choice = chooseIde([owner, caller], '/w/app/src', false, 'caller', 'caller');
  assert.deepEqual(choice, { id: 'caller', reason: "tabRouting is caller; the caller's IDE" });
  assert.equal(chooseIde([owner, caller], '/w/app/src', false, 'caller', 'project')?.id, 'owner');
  assert.equal(chooseIde([owner, caller], '/w/app/src', false, 'caller')?.id, 'owner');
});

test('caller routing falls back to project matching when the caller is not in a running IDE', () => {
  const owner = ide('owner', 1, [['app', '/w/app', true]]);
  const recent = ide('recent', 9, [['q', '/q', false]]);
  assert.equal(chooseIde([owner, recent], '/w/app', false, undefined, 'caller')?.id, 'owner');
  assert.equal(chooseIde([owner, recent], '/w/app', false, 'gone', 'caller')?.id, 'owner');
  assert.equal(chooseIde([owner, recent], '/elsewhere', false, 'windows-terminal', 'caller')?.id, 'recent');
  assert.equal(chooseIde([ide('empty', 1, [])], '/w', false, 'empty', 'caller'), undefined);
});

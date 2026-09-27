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

test('the IDE whose project contains the path wins, focused first, then the deepest project', () => {
  const a = ide('a', 1, [['app', '/w/app', false]]);
  const b = ide('b', 2, [['lib', '/w/lib', true]]);
  const c = ide('c', 3, [['w', '/w', true]]);
  assert.equal(chooseIde([a, b], '/w/app/src', false)?.id, 'a');
  assert.equal(chooseIde([a, c], '/w/app/src', false)?.id, 'c');
  assert.equal(chooseIde([a, ide('d', 0, [['w', '/w', false]])], '/w/app', false)?.id, 'a');
  assert.equal(chooseIde([ide('x', 5, [['app', '/w/app', false]]), ide('y', 9, [['app', '/w/app', false]])], '/w/app', false)?.id, 'y');
  assert.match(chooseIde([a], '/w/app', false)!.reason, /open project app contains the path/);
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

import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { cpSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, statSync, utimesSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { test } from 'node:test';
import { root as repo } from '../shared.mjs';

const sync = path.join(repo, 'scripts', 'sync-shared.mjs');
const VSCODE = 'vscode/resources';
const JETBRAINS = 'jetbrains/src/main/resources';

function withRepo(body) {
  const dir = mkdtempSync(path.join(os.tmpdir(), 'iat-sync-'));
  try {
    cpSync(path.join(repo, 'shared'), path.join(dir, 'shared'), { recursive: true });
    for (const target of [VSCODE, JETBRAINS]) {
      for (const sub of ['icons', 'launch']) {
        mkdirSync(path.join(dir, target, sub), { recursive: true });
        cpSync(path.join(repo, target, sub), path.join(dir, target, sub), { recursive: true });
      }
    }
    body(dir);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

const run = (dir, ...args) => spawnSync(process.execPath, [sync, '--root', dir, ...args], { encoding: 'utf8' });
const read = (dir, file) => readFileSync(path.join(dir, file), 'utf8');

test('--check passes on the committed tree', () => {
  const result = spawnSync(process.execPath, [sync, '--check'], { encoding: 'utf8' });
  assert.equal(result.status, 0, result.stderr);
});

test('--check lists drifted and missing copies without writing', () => {
  withRepo((dir) => {
    writeFileSync(path.join(dir, VSCODE, 'launch/agent.sh'), 'edited\n');
    rmSync(path.join(dir, JETBRAINS, 'icons/agents/claude.svg'));
    const result = run(dir, '--check');
    assert.equal(result.status, 1);
    assert.match(result.stderr, /vscode\/resources\/launch\/agent\.sh/);
    assert.match(result.stderr, /jetbrains\/src\/main\/resources\/icons\/agents\/claude\.svg/);
    assert.doesNotMatch(result.stderr, /agent\.ps1/);
    assert.equal(read(dir, `${VSCODE}/launch/agent.sh`), 'edited\n');
    assert.equal(existsSync(path.join(dir, JETBRAINS, 'icons/agents/claude.svg')), false);
  });
});

test('copies the shared files into both trees and then passes --check', () => {
  withRepo((dir) => {
    writeFileSync(path.join(dir, 'shared/launch/agent.fish'), '# changed\n');
    mkdirSync(path.join(dir, 'shared/icons/extra'), { recursive: true });
    writeFileSync(path.join(dir, 'shared/icons/extra/new.svg'), '<svg/>');
    const result = run(dir);
    assert.equal(result.status, 0, result.stderr);
    for (const target of [VSCODE, JETBRAINS]) {
      assert.equal(read(dir, `${target}/launch/agent.fish`), '# changed\n');
      assert.equal(read(dir, `${target}/icons/extra/new.svg`), '<svg/>');
    }
    assert.equal(run(dir, '--check').status, 0);
  });
});

test('leaves unchanged files and target-only files alone', () => {
  withRepo((dir) => {
    const unchanged = path.join(dir, VSCODE, 'icons/agentTab.svg');
    const old = new Date('2020-01-01T00:00:00Z');
    utimesSync(unchanged, old, old);
    writeFileSync(path.join(dir, JETBRAINS, 'icons/only-here.svg'), '<svg/>');
    assert.equal(run(dir).status, 0);
    assert.equal(statSync(unchanged).mtime.getTime(), old.getTime());
    assert.equal(read(dir, `${JETBRAINS}/icons/only-here.svg`), '<svg/>');
  });
});

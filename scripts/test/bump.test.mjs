import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { cpSync, mkdirSync, mkdtempSync, readFileSync, rmSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';

const repo = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const bump = path.join(repo, 'scripts', 'bump.mjs');
const FILES = [
  'claude-plugin/.claude-plugin/plugin.json',
  'vscode/package.json',
  'vscode/package-lock.json',
  'jetbrains/gradle.properties',
  'CHANGELOG.md',
];

function copyRepo() {
  const dir = mkdtempSync(path.join(os.tmpdir(), 'iat-bump-'));
  for (const file of FILES) {
    mkdirSync(path.dirname(path.join(dir, file)), { recursive: true });
    cpSync(path.join(repo, file), path.join(dir, file));
  }
  return dir;
}

const run = (dir, ...args) => spawnSync(process.execPath, [bump, '--root', dir, ...args], { encoding: 'utf8' });
const text = (dir, file) => readFileSync(path.join(dir, file), 'utf8');
const original = (file) => readFileSync(path.join(repo, file), 'utf8');
const json = (dir, file) => JSON.parse(text(dir, file));

test('bumps the plugin version in every file and adds a changelog stub', () => {
  const dir = copyRepo();
  try {
    const result = run(dir, '9.8.7');
    assert.equal(result.status, 0, result.stderr);
    assert.equal(json(dir, 'claude-plugin/.claude-plugin/plugin.json').version, '9.8.7');
    assert.equal(text(dir, 'vscode/package.json'), original('vscode/package.json'));
    assert.equal(text(dir, 'jetbrains/gradle.properties'), original('jetbrains/gradle.properties'));
    const stub = '## 9.8.7\n\nPlugin and MCP server 9.8.7.\n\n';
    assert.ok(text(dir, 'CHANGELOG.md').includes(`${stub}## `));
    assert.equal(text(dir, 'CHANGELOG.md').replace(stub, ''), original('CHANGELOG.md'));
    assert.match(result.stdout, /git tag ide-agent-tabs--v9\.8\.7/);
    assert.doesNotMatch(result.stdout, /pack-ides/);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('--vscode and --jetbrains bump the IDE packages', () => {
  const dir = copyRepo();
  try {
    const result = run(dir, '9.8.7', '--vscode', '1.2.3', '--jetbrains', '4.5.6');
    assert.equal(result.status, 0, result.stderr);
    assert.equal(json(dir, 'vscode/package.json').version, '1.2.3');
    const lock = json(dir, 'vscode/package-lock.json');
    assert.equal(lock.version, '1.2.3');
    assert.equal(lock.packages[''].version, '1.2.3');
    assert.match(text(dir, 'jetbrains/gradle.properties'), /^pluginVersion=4\.5\.6$/m);
    assert.match(result.stdout, /pack-ides/);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('a second run for the same version keeps the changelog entry', () => {
  const dir = copyRepo();
  try {
    assert.equal(run(dir, '9.8.7').status, 0);
    const once = text(dir, 'CHANGELOG.md');
    const again = run(dir, '9.8.7');
    assert.equal(again.status, 0, again.stderr);
    assert.equal(text(dir, 'CHANGELOG.md'), once);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('a bad version or missing argument fails and writes nothing', () => {
  const dir = copyRepo();
  try {
    for (const args of [[], ['1.2'], ['1.2.3', '--vscode', 'x'], ['1.2.3', '--jetbrains']]) {
      assert.equal(run(dir, ...args).status, 1, args.join(' '));
    }
    assert.equal(text(dir, 'claude-plugin/.claude-plugin/plugin.json'), original('claude-plugin/.claude-plugin/plugin.json'));
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

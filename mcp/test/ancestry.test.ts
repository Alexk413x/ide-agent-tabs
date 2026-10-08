import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { writeFileSync } from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { findAgent, parsePsTable, parseWindowsTable, type ProcessInfo, type ProcessTable } from '../src/shared/ancestry.js';
import { tempDir } from './tempDir.js';

const table = (rows: [pid: number, ppid: number, name: string, startMs: number][]): ProcessTable =>
  new Map(rows.map(([pid, ppid, name, startMs]): [number, ProcessInfo] => [pid, { pid, ppid, name, startMs }]));

test('the agent is the nearest ancestor that is not a shell, even inside another session', () => {
  const helper = table([
    [10, 9, 'node.exe', 500],
    [9, 8, 'cmd.exe', 400],
    [8, 7, 'claude.exe', 300],
    [7, 6, 'bash.exe', 200],
    [6, 1, 'claude.exe', 100],
  ]);
  assert.equal(findAgent(helper, 10)?.pid, 8);
  const hook = table([
    [20, 19, 'node', 500],
    [19, 18, 'bash', 450],
    [18, 8, '/bin/bash', 440],
    [8, 1, '/Users/a/.local/bin/claude', 300],
  ]);
  assert.equal(findAgent(hook, 20)?.pid, 8);
});

test('a Claude Code that runs as node is found by the same rule', () => {
  const npm = table([
    [10, 9, 'node', 500],
    [9, 8, 'sh', 400],
    [8, 7, 'node', 300],
    [7, 1, 'zsh', 100],
  ]);
  assert.deepEqual(findAgent(npm, 10), { pid: 8, ppid: 7, name: 'node', startMs: 300 });
});

test('a parent that started after its child is a reused pid, and the walk stops there', () => {
  const reused = table([
    [10, 9, 'node', 500],
    [9, 8, 'cmd.exe', 400],
    [8, 1, 'claude.exe', 9_000],
  ]);
  assert.equal(findAgent(reused, 10), undefined);
  assert.equal(findAgent(table([[10, 10, 'node', 1]]), 10), undefined);
  assert.equal(findAgent(new Map(), 10), undefined);
});

test('the Windows and ps process listings parse into pids, parents, names and start times', () => {
  const windows = parseWindowsTable('4\t0\t0\tSystem\r\n1856\t44100\t134359032872440350\tclaude.exe\r\n22260\t1856\t134359032883981200\tcmd.exe\r\n');
  assert.equal(windows.size, 2);
  assert.deepEqual(windows.get(22260), { pid: 22260, ppid: 1856, name: 'cmd.exe', startMs: 134359032883981200 / 10_000 - 11_644_473_600_000 });
  assert.ok(Math.abs(windows.get(1856)!.startMs - Date.parse('2026-10-08T03:21:27.244Z')) < 1_000);
  const ps = parsePsTable('  812     1 Wed Oct  8 03:21:28 2026     /Applications/Claude Code.app/claude\n  900   812 Wed Oct  8 03:21:29 2026     node\nbad line\n');
  assert.equal(ps.get(812)?.name, '/Applications/Claude Code.app/claude');
  assert.equal(ps.get(900)?.ppid, 812);
  assert.equal(ps.get(900)!.startMs - ps.get(812)!.startMs, 1_000);
});

test('a helper run through a shell finds the process that started it, with its start time', { timeout: 60_000 }, async () => {
  const mcp = path.join(path.dirname(fileURLToPath(import.meta.url)), '..');
  const agent = path.join(tempDir('iat-ancestry-'), 'agent.mjs');
  const command = `"${process.execPath}" --import tsx test/ancestryProbe.ts`;
  writeFileSync(
    agent,
    [
      "import { execSync } from 'node:child_process';",
      `const out = execSync(${JSON.stringify(command)}, { encoding: 'utf8', cwd: ${JSON.stringify(mcp)} });`,
      'console.log(JSON.stringify({ pid: process.pid, found: JSON.parse(out.trim()) }));',
    ].join('\n'),
  );
  const started = Date.now();
  const child = spawn(process.execPath, [agent], { stdio: ['ignore', 'pipe', 'inherit'] });
  let text = '';
  child.stdout.setEncoding('utf8').on('data', (c: string) => (text += c));
  await new Promise((r) => child.once('close', r));
  const { pid, found } = JSON.parse(text.trim()) as { pid: number; found: ProcessInfo };
  assert.equal(found.pid, pid);
  assert.ok(Math.abs(found.startMs - started) < 5_000, `start ${found.startMs} vs spawn ${started}`);
});

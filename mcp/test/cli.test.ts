import assert from 'node:assert/strict';
import { test } from 'node:test';
import { runCli } from '../src/cli.js';
import { runServerCli } from '../src/serverCli.js';
import { freePort } from './httpClient.js';
import { tempDir } from './tempDir.js';

process.env.IDE_AGENT_TABS_HOME = tempDir('iat-cli-');

function capture() {
  const out: string[] = [];
  const err: string[] = [];
  return { out, err, io: { readStdin: async () => '{}', stdout: (t: string) => void out.push(t), stderr: (t: string) => void err.push(t) } };
}

test('list-ides prints the IDEs and terminals as JSON, and refuses arguments', { timeout: 60_000 }, async () => {
  const ok = capture();
  assert.equal(await runCli('list-ides', [], ok.io), 0);
  const listed = JSON.parse(ok.out.join(''));
  assert.ok(Array.isArray(listed.ides) && Array.isArray(listed.terminals) && Array.isArray(listed.installed));
  const bad = capture();
  assert.equal(await runCli('list-ides', ['extra'], bad.io), 1);
  assert.match(JSON.parse(bad.err.join('')).error, /takes no arguments/);
});

test('jev status says Jev is off until config.json turns it on', async () => {
  const off = capture();
  assert.equal(await runCli('jev', ['status'], off.io), 1);
  assert.match(JSON.parse(off.err.join('')).error, /Jev is off/);
});

test('server status reports a free port, and a bad port is a usage error', async () => {
  const port = await freePort();
  const out: string[] = [];
  assert.equal(await runServerCli(['status', '--port', String(port)], (t) => void out.push(t)), 1);
  assert.deepEqual(JSON.parse(out.join('')), { port, running: false });
  assert.equal(await runServerCli(['status', '--port', 'x'], () => undefined), 2);
  assert.equal(await runServerCli(['restart'], () => undefined), 2);
});

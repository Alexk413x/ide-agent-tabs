import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { readFileSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { after, before, test } from 'node:test';
import { ledgerPath } from '../src/jev/ledger.js';
import { FAKE_KEY, FAKE_MODEL, FakeTypeSafe } from './fakeTypeSafe.js';
import { tempDir } from './tempDir.js';

const fake = new FakeTypeSafe();
const mcpDir = path.join(import.meta.dirname, '..');

before(() => fake.start());
after(() => fake.stop());

function cli(home: string, args: string[], stdin: string) {
  const env: NodeJS.ProcessEnv = { ...process.env, IDE_AGENT_TABS_HOME: home, TYPESAFE_API_KEY: FAKE_KEY, TYPESAFE_BASE_URL: fake.url };
  env.IDE_AGENT_TABS_AGENT = 'codex';
  env.IDE_AGENT_TABS_ID = 'wt-3';
  const child = spawn(process.execPath, ['--import', 'tsx', path.join(mcpDir, 'src', 'main.ts'), 'jev', ...args], { cwd: mcpDir, env });
  let stdout = '';
  let stderr = '';
  child.stdout.setEncoding('utf8').on('data', (d: string) => (stdout += d));
  child.stderr.setEncoding('utf8').on('data', (d: string) => (stderr += d));
  child.stdin.end(stdin);
  return new Promise<{ code: number | null; stdout: string; stderr: string }>((resolve, reject) => {
    child.on('error', reject);
    child.on('close', (code) => resolve({ code, stdout, stderr }));
  });
}

test('jev on the command line reads JSON on stdin and prints the reply', { timeout: 60_000 }, async () => {
  const home = tempDir('iat-jev-cli-');
  writeFileSync(path.join(home, 'config.json'), JSON.stringify({ jev: { enabled: true } }));

  const choose = await cli(home, ['choose'], JSON.stringify({ instruction: 'Which?', options: [{ id: 'a', description: 'A' }, { id: 'b', description: 'B' }] }));
  assert.equal(choose.code, 0, choose.stderr);
  const reply = JSON.parse(choose.stdout);
  assert.equal(reply.choice, 'a');
  assert.equal(reply.model, FAKE_MODEL);
  assert.equal(fake.seen.length, 1);
  assert.deepEqual(Object.keys(fake.seen[0]!.body.questions.pick!.criteria as object), ['a', 'b', 'none']);

  const status = await cli(home, ['status'], '{}');
  assert.equal(status.code, 0, status.stderr);
  assert.deepEqual(JSON.parse(status.stdout).today, { calls: 1, failed: 0, input_tokens: 1000, cost_usd: 0.000042 });
  assert.equal(JSON.parse(status.stdout).key, 'env');

  const line = JSON.parse(readFileSync(ledgerPath(home), 'utf8').trim());
  assert.equal(line.agent, 'codex');
  assert.equal(line.tab, 'wt-3');

  const bad = await cli(home, ['choose'], '{"instruction": "x"}');
  assert.equal(bad.code, 1);
  assert.match(JSON.parse(bad.stderr).error, /options/);
  const notJson = await cli(home, ['check'], 'nope');
  assert.match(JSON.parse(notJson.stderr).error, /stdin is not JSON/);
  const unknown = await cli(home, ['gate'], '{}');
  assert.equal(unknown.code, 1);
  assert.match(JSON.parse(unknown.stderr).error, /Usage: node mcp-server\.mjs jev <status\|ask\|choose\|check\|rank\|route>/);
});

test('jev on the command line refuses to run while Jev is off', { timeout: 60_000 }, async () => {
  const home = tempDir('iat-jev-off-');
  writeFileSync(path.join(home, 'config.json'), JSON.stringify({ jev: { enabled: 'yes' } }));
  const r = await cli(home, ['status'], '{}');
  assert.equal(r.code, 1);
  assert.equal(r.stdout, '');
  const error = JSON.parse(r.stderr).error as string;
  assert.match(error, /Jev is off/);
  assert.match(error, /jev\.enabled must be true or false/);
});

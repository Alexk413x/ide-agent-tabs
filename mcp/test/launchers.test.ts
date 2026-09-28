import assert from 'node:assert/strict';
import { existsSync, readFileSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import { tempDir } from './tempDir.js';
import { findOnPath } from '../src/installed.js';
import { run } from '../src/process.js';
import { launchOf, type AgentProfile } from '../src/profiles.js';
import { launchSpec, posixSpec, powerShellSpec } from '../src/spec.js';
import { argvModeCommand } from '../src/terminals/shell.js';

const launchDir = path.join(import.meta.dirname, '..', 'launch');
const work = tempDir('iat launch ');
const probe = path.join(work, 'probe.cjs');
writeFileSync(
  probe,
  `require('fs').writeFileSync(process.env.PROBE_OUT, JSON.stringify({
    args: process.argv.slice(2), cwd: process.cwd(),
    id: process.env.IDE_AGENT_TABS_ID, agent: process.env.IDE_AGENT_TABS_AGENT,
    spec: process.env.IDE_AGENT_TABS_SPEC ?? null, extra: process.env.IAT_EXTRA ?? null }))`,
);

const prompt = `Say "hi" & run $(whoami); \`tick\` 'quote' --flag é ✓ 🙂 2024-01-01T00:00:00\nsecond line`;
const callerArgs = ['--plugin-dir', 'C:\\Program Files\\a b', `say "hi" $(whoami) \`t\` 'q'`, 'é ✓', '2026-09-26', '', 'C:\\dir with space\\', 'x\\"y z', '{"a": 1}'];
const profile: AgentProfile = { name: 'probe', label: 'Probe', command: process.execPath, args: [probe], promptFlag: '--prompt', env: {} };

function expected(out: string, withPrompt: boolean) {
  return {
    args: [...callerArgs, ...(withPrompt ? ['--prompt', prompt] : [])],
    id: 'tab-1',
    agent: 'probe',
    spec: null,
    extra: 'value with spaces & $(x)',
    out,
  };
}

function readProbe(out: string) {
  const got = JSON.parse(readFileSync(out, 'utf8'));
  return { ...got, out };
}

for (const shell of ['pwsh.exe', 'powershell.exe']) {
  test(`the PowerShell launcher passes the prompt and args intact (${shell})`, async (t) => {
    const exe = process.platform === 'win32' ? findOnPath(process.env.PATH ?? '', shell) : undefined;
    if (!exe) {
      t.skip(`${shell} not found`);
      return;
    }
    for (const withPrompt of [true, false]) {
      const out = path.join(work, `ps-${shell}-${withPrompt}.json`);
      const specFile = path.join(work, `ps-${shell}-${withPrompt}.spec.json`);
      const pidFile = path.join(work, `ps-${shell}-${withPrompt}.pid`);
      const launch = launchOf(profile, withPrompt ? prompt : undefined, callerArgs, { PROBE_OUT: out, IAT_EXTRA: 'value with spaces & $(x)' });
      writeFileSync(specFile, powerShellSpec(launchSpec('tab-1', work, launch, pidFile)));
      const result = await run(exe, ['-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', path.join(launchDir, 'agent-launch.ps1'), specFile]);
      assert.equal(result.code, 0, result.stderr);
      const got = readProbe(out);
      assert.equal(path.resolve(got.cwd), path.resolve(work));
      delete got.cwd;
      assert.deepEqual(got, expected(out, withPrompt));
      assert.ok(!existsSync(specFile), 'the launcher deletes its spec');
      assert.match(readFileSync(pidFile, 'utf8'), /^\d+$/);
    }
  });
}

function findBash(): string | undefined {
  const bash = process.platform === 'win32' ? findOnPath(process.env.PATH ?? '', 'bash.exe') : '/bin/bash';
  return bash && !(process.platform === 'win32' && /System32/i.test(bash)) ? bash : undefined;
}

const bashEnv = { ...process.env, MSYS_NO_PATHCONV: '1', MSYS2_ARG_CONV_EXCL: '*' };

test('the POSIX launcher passes the prompt and args intact (bash)', async (t) => {
  const bash = findBash();
  if (!bash) {
    t.skip('no bash found');
    return;
  }
  for (const withPrompt of [true, false]) {
    const out = path.join(work, `sh-${withPrompt}.json`);
    const specFile = path.join(work, `sh-${withPrompt}.spec`);
    const launch = launchOf(profile, withPrompt ? prompt : undefined, callerArgs, { PROBE_OUT: out, IAT_EXTRA: 'value with spaces & $(x)' });
    writeFileSync(specFile, posixSpec(launchSpec('tab-1', work, launch)));
    const result = await run(bash, ['--noprofile', '--norc', '-c', '. "$IDE_AGENT_TABS_LAUNCHER"'], {
      env: { ...bashEnv, IDE_AGENT_TABS_LAUNCHER: path.join(launchDir, 'agent-launch.sh'), IDE_AGENT_TABS_SPEC: specFile },
    });
    assert.equal(result.code, 0, result.stderr);
    const got = readProbe(out);
    delete got.cwd;
    assert.deepEqual(got, expected(out, withPrompt));
    assert.ok(!existsSync(specFile), 'the launcher deletes its spec');
  }
});

test('the POSIX launcher runs in argv mode and writes its shell pid (bash)', async (t) => {
  const bash = findBash();
  if (!bash) {
    t.skip('no bash found');
    return;
  }
  const out = path.join(work, 'argv.json');
  const specFile = path.join(work, 'argv.spec');
  const pidFile = path.join(work, 'argv.pid');
  const launch = launchOf(profile, prompt, callerArgs, { PROBE_OUT: out, IAT_EXTRA: 'value with spaces & $(x)' });
  writeFileSync(specFile, posixSpec(launchSpec('tab-1', work, launch, pidFile)));
  const [, , , , script, ...positional] = argvModeCommand({ path: '/bin/bash', kind: 'posix' }, path.join(launchDir, 'agent-launch.sh'), specFile);
  const once = script!.replace(/; exec .*$/, () => '; printf %s "$$"');
  const result = await run(bash, ['--noprofile', '--norc', '-c', once, ...positional], { env: bashEnv });
  assert.equal(result.code, 0, result.stderr);
  const got = readProbe(out);
  delete got.cwd;
  assert.deepEqual(got, expected(out, true));
  assert.ok(!existsSync(specFile), 'the launcher deletes its spec');
  assert.equal(readFileSync(pidFile, 'utf8').trim(), result.stdout);
});

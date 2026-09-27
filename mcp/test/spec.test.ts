import assert from 'node:assert/strict';
import { test } from 'node:test';
import { launchOf, type AgentProfile } from '../src/profiles.js';
import { checkPosixEnvNames, launchSpec, parsePosixSpec, posixSpec, powerShellSpec } from '../src/spec.js';

const profile: AgentProfile = { name: 'x', label: 'X', command: 'C:\\a b\\x;y.exe', args: ['$(whoami)'], promptFlag: '-p', env: { P: '1' } };
const prompt = `Say "hi" & run $(whoami); \`tick\` 'quote' --flag é ✓ 🙂\nsecond line\n`;
const spec = launchSpec('tab-9', '/w/app', launchOf(profile, prompt, ['`t', ''], { C: 'caller' }), '/h/launch/tab-9.pid');

test('the PowerShell spec is JSON with env as name/value pairs', () => {
  assert.deepEqual(JSON.parse(powerShellSpec(spec)), {
    version: 1,
    id: 'tab-9',
    agent: 'x',
    cwd: '/w/app',
    command: 'C:\\a b\\x;y.exe',
    args: ['$(whoami)', '`t', '', '-p'],
    prompt,
    env: [
      { name: 'P', value: '1' },
      { name: 'C', value: 'caller' },
    ],
    pidFile: '/h/launch/tab-9.pid',
  });
  const noPrompt = JSON.parse(powerShellSpec(launchSpec('t', '/w', launchOf(profile))));
  assert.equal(noPrompt.prompt, null);
  assert.deepEqual(noPrompt.args, ['$(whoami)']);
  assert.equal(noPrompt.pidFile, null);
});

test('the POSIX spec is NUL-separated fields and round-trips', () => {
  const bytes = posixSpec(spec);
  assert.equal(bytes.at(-1), 0);
  assert.ok(bytes.toString('utf8').startsWith('ide-agent-tabs-spec-1\0tab-9\0x\0/w/app\0'));
  const { pidFile: _pid, ...withoutPid } = spec;
  assert.deepEqual(parsePosixSpec(bytes), withoutPid);
  const bare = launchSpec('t', '/w', launchOf({ ...profile, args: [], env: {} }));
  assert.deepEqual(parsePosixSpec(posixSpec(bare)), bare);
  assert.throws(() => posixSpec({ ...bare, command: 'a\0b' }));
});

test('terminal env names must be shell identifiers', () => {
  checkPosixEnvNames({ A_1: '', _b: '' });
  assert.throws(() => checkPosixEnvNames({ 'a.b': '' }), /a\.b/);
  assert.throws(() => checkPosixEnvNames({ '1A': '' }));
});

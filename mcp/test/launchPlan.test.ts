import assert from 'node:assert/strict';
import { test } from 'node:test';
import { ORI_AGENTS, planLaunch, type OriDetection } from '../src/launchPlan.js';
import { BUILTIN_PROFILES, ConfigError, type AgentProfile } from '../src/profiles.js';

const claude = BUILTIN_PROFILES.find((p) => p.name === 'claude')!;
const codex = BUILTIN_PROFILES.find((p) => p.name === 'codex')!;
const bare: AgentProfile = { name: 'bare', label: 'Bare', command: 'bare-cli', args: [], env: {} };
const ori: OriDetection = { path: 'C:\\Users\\a\\.local\\bin\\ori.exe', version: '0.14.3', agents: ['claude', 'codex'] };
const base = { args: [] as string[], env: {}, platform: 'linux' as NodeJS.Platform, ori, launchVia: 'direct' as const };

test('every built-in profile has a model flag', () => {
  assert.deepEqual(
    BUILTIN_PROFILES.map((p) => [p.name, p.modelFlag]),
    [
      ['claude', '--model'],
      ['codex', '-m'],
      ['agy', '--model'],
      ['copilot', '--model'],
      ['gemini', '-m'],
      ['grok', '-m'],
      ['pi', '--model'],
      ['hermes', '-m'],
      ['opencode', '-m'],
      ['qwen', '-m'],
      ['goose', '--model'],
      ['codex-local', '-m'],
    ],
  );
});

test('a model reaches each new agent with its own flag, before the prompt flag', () => {
  const plan = (name: string, prompt?: string) => planLaunch(BUILTIN_PROFILES.find((p) => p.name === name)!, { ...base, model: 'm1', prompt }).launch;
  assert.deepEqual(plan('hermes', 'go').args, ['chat', '-m', 'm1', '-q']);
  assert.deepEqual(plan('qwen', 'go').args, ['-m', 'm1', '-i']);
  assert.deepEqual(plan('opencode', 'go').args, ['-m', 'm1', '--prompt']);
  assert.deepEqual(plan('pi', 'go').args, ['--model', 'm1']);
  assert.deepEqual(plan('grok', 'go').args, ['-m', 'm1']);
  assert.deepEqual(plan('codex-local').args.slice(-5), ['--oss', '--local-provider', 'ollama', '-m', 'm1']);
});

test('Goose starts with goose run -s -t for a first message and goose session without one', () => {
  const goose = BUILTIN_PROFILES.find((p) => p.name === 'goose')!;
  const withPrompt = planLaunch(goose, { ...base, prompt: 'hi', model: 'm1' }).launch;
  assert.deepEqual([withPrompt.command, ...withPrompt.args, withPrompt.prompt], ['goose', 'run', '-s', '--model', 'm1', '-t', 'hi']);
  const empty = planLaunch(goose, { ...base, model: 'm1' }).launch;
  assert.deepEqual([empty.command, ...empty.args], ['goose', 'session', '--model', 'm1']);
  const custom = { ...goose, args: ['run', '-s', '--debug'] };
  assert.deepEqual(planLaunch(custom, base).launch.args, ['run', '-s', '--debug'], 'a custom goose profile keeps its own args');
});

test('a direct launch puts the model flag after the profile args and before the caller args and prompt', () => {
  const plan = planLaunch(claude, { ...base, model: 'opus', args: ['--verbose'], prompt: 'hi' });
  assert.equal(plan.via, 'direct');
  assert.equal(plan.launch.command, 'claude');
  assert.deepEqual(plan.launch.args, ['--model', 'opus', '--verbose']);
  assert.equal(plan.launch.prompt, 'hi');
});

test('a model for a profile without a model flag is an error, never ignored', () => {
  assert.throws(() => planLaunch(bare, { ...base, model: 'x' }), (e: Error) => e instanceof ConfigError && /bare has no model option/.test(e.message));
  assert.throws(() => planLaunch(claude, { ...base, model: 'two words' }), /model/);
});

test('through Ori the agent runs as ori <agent>, with Ori taking the model', () => {
  const plan = planLaunch(claude, { ...base, via: 'ori', model: 'anthropic/claude-sonnet-5.5', prompt: 'go' });
  assert.equal(plan.via, 'ori');
  assert.equal(plan.launch.command, 'ori');
  assert.deepEqual(plan.launch.args, ['claude', '--model', 'anthropic/claude-sonnet-5.5']);
  assert.equal(plan.launch.agent, 'claude', 'the session keeps the inner agent identity');
});

test('the setting launches supported agents through Ori and others directly', () => {
  assert.equal(planLaunch(claude, { ...base, launchVia: 'ori' }).via, 'ori');
  assert.equal(planLaunch(bare, { ...base, launchVia: 'ori' }).via, 'direct');
  assert.equal(planLaunch(claude, { ...base, launchVia: 'ori', ori: null }).via, 'direct');
  assert.equal(planLaunch(claude, { ...base, launchVia: 'ori', via: 'direct' }).via, 'direct', 'explicit via beats the setting');
});

test('an explicit via ori that cannot work is an error', () => {
  assert.throws(() => planLaunch(bare, { ...base, via: 'ori' }), /bare can't launch through Ori: Ori has no bare launcher/);
  assert.throws(() => planLaunch(claude, { ...base, via: 'ori', ori: null }), /Ori isn't installed/);
  assert.throws(() => planLaunch(BUILTIN_PROFILES.find((p) => p.name === 'gemini')!, { ...base, via: 'ori' }), /Ori has no gemini launcher/);
  const noCodex = { ...ori, agents: ['claude'] };
  assert.throws(() => planLaunch(codex, { ...base, via: 'ori', ori: noCodex }), /Ori lists codex as not installed/);
});

test('on Windows an argument with a cmd.exe special character cannot pass through Ori to a .cmd shim', () => {
  const windows = { ...base, platform: 'win32' as NodeJS.Platform, cmdShim: true };
  assert.throws(() => planLaunch(claude, { ...windows, via: 'ori', prompt: 'say "hi"' }), /cmd\.exe/, 'the prompt is an argument too');
  assert.equal(planLaunch(claude, { ...windows, via: 'ori', prompt: 'say "hi"', cmdShim: false }).via, 'ori', 'a native exe takes it');
  assert.throws(() => planLaunch(codex, { ...windows, via: 'ori' }), /cmd\.exe/);
  assert.equal(planLaunch(codex, { ...windows, launchVia: 'ori' }).via, 'direct', 'the setting falls back to a direct launch');
  assert.equal(planLaunch(codex, { ...base, via: 'ori' }).via, 'ori', 'other platforms have no such limit');
  assert.ok(ORI_AGENTS.includes('codex'));
});

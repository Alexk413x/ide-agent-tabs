import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  BUILTIN_PROFILES,
  checkEnv,
  CODEX_TAB_ARGS,
  ConfigError,
  launchOf,
  mergeProfiles,
  parseProfiles,
  readDefaultAgent,
  resolveSettings,
  type AgentProfile,
} from '../src/profiles.js';

const p = (over: Partial<AgentProfile> & { name: string }): AgentProfile => ({
  label: over.name,
  command: over.name,
  args: [],
  env: {},
  ...over,
});

test('built-in profiles match the design', () => {
  const s = resolveSettings(undefined, undefined);
  assert.deepEqual(s.profiles.map((x) => x.name), ['claude', 'codex', 'agy', 'copilot', 'gemini', 'grok', 'pi', 'hermes', 'opencode', 'qwen', 'goose', 'codex-local']);
  assert.deepEqual(s.profiles.map((x) => x.label), [
    'Claude Code',
    'Codex',
    'Antigravity CLI',
    'Copilot CLI',
    'Gemini CLI',
    'Grok Build',
    'Pi',
    'Hermes',
    'OpenCode',
    'Qwen Code',
    'Goose',
    'Codex (local)',
  ]);
  assert.deepEqual(s.profiles.map((x) => x.command), ['claude', 'codex', 'agy', 'copilot', 'gemini', 'grok', 'pi', 'hermes', 'opencode', 'qwen', 'goose', 'codex']);
  assert.deepEqual(s.profiles.map((x) => x.promptFlag), [undefined, undefined, '-i', '-i', '-i', undefined, undefined, '-q', '--prompt', '-i', '-t', undefined]);
  assert.deepEqual(s.profiles.find((x) => x.name === 'hermes')!.args, ['chat']);
  assert.deepEqual(s.profiles.find((x) => x.name === 'goose')!.args, ['run', '-s']);
  assert.deepEqual(s.profiles.find((x) => x.name === 'codex-local')!.args, [...CODEX_TAB_ARGS, '--oss', '--local-provider', 'ollama']);
  assert.equal(s.defaultAgent.name, 'claude');
  assert.deepEqual(s.warnings, []);
});

test('agents file overrides a built-in by name and adds new profiles', () => {
  const s = resolveSettings(
    JSON.stringify({
      codex: { label: 'Codex (fast)', command: 'codex', args: ['--model', 'o4'] },
      'opencode-local': {
        label: 'OpenCode (LM Studio)',
        command: 'opencode',
        args: ['--model', 'lmstudio/qwen3-coder'],
        promptFlag: '--prompt',
        env: { LMSTUDIO: '1' },
        icon: 'icons/opencode.svg',
      },
      bare: { command: 'bare-cli' },
    }),
    undefined,
  );
  assert.deepEqual(s.profiles.map((x) => x.name), ['claude', 'codex', 'agy', 'copilot', 'gemini', 'grok', 'pi', 'hermes', 'opencode', 'qwen', 'goose', 'codex-local', 'opencode-local', 'bare']);
  assert.deepEqual(s.profiles[1], p({ name: 'codex', label: 'Codex (fast)', args: ['--model', 'o4'] }));
  assert.deepEqual(s.profiles[12], {
    name: 'opencode-local',
    label: 'OpenCode (LM Studio)',
    command: 'opencode',
    args: ['--model', 'lmstudio/qwen3-coder'],
    promptFlag: '--prompt',
    env: { LMSTUDIO: '1' },
    icon: 'icons/opencode.svg',
  });
  assert.equal(s.profiles[13]!.label, 'bare');
  assert.deepEqual(s.warnings, []);
});

test('null fields count as absent and blank labels and icons fall back', () => {
  const [x] = parseProfiles(JSON.stringify({ x: { command: 'x', label: ' ', icon: '', args: null, env: null, promptFlag: null } }));
  assert.deepEqual(x, p({ name: 'x' }));
});

test('a bad agents file warns and leaves the built-ins', () => {
  const bad = [
    'not json',
    '[]',
    '{"x": "codex"}',
    '{"x": {}}',
    '{"x": {"command": ""}}',
    '{"x": {"command": 1}}',
    '{"x": {"command": "x", "args": "--a"}}',
    '{"x": {"command": "x", "args": [1]}}',
    '{"x": {"command": "x", "promptFlag": " "}}',
    '{"x": {"command": "x", "env": {"A": 1}}}',
    '{"x": {"command": "x", "env": {"A=B": "1"}}}',
    '{"x": {"command": "x", "env": {"IDE_AGENT_TABS_COMMAND": "evil"}}}',
    '{"x": {"command": "x", "env": {"jediterm_source": "evil"}}}',
    '{"bad name": {"command": "x"}}',
    '{"claude": {"command": "x"}, "y": {"command": "y", "args": {}}}',
    `{"x": {"command": "x", "args": ${JSON.stringify(Array(65).fill('a'))}}}`,
    '{"x": {"command": "x\\u0000"}}',
  ];
  for (const text of bad) {
    const s = resolveSettings(text, undefined, '/h/agents.json');
    assert.deepEqual(s.profiles, BUILTIN_PROFILES, text);
    assert.equal(s.warnings.length, 1, text);
    assert.match(s.warnings[0]!, /^Ignoring \/h\/agents\.json and using the built-in agent profiles: /);
  }
});

test('arguments are profile args, caller args, prompt flag, then the prompt', () => {
  const launch = launchOf(p({ name: 'p', command: 'cli', args: ['--model', 'm'], promptFlag: '-i' }), 'hello', ['--yolo']);
  assert.equal(launch.agent, 'p');
  assert.equal(launch.command, 'cli');
  assert.deepEqual(launch.args, ['--model', 'm', '--yolo', '-i']);
  assert.equal(launch.prompt, 'hello');
});

test('no prompt means no prompt flag', () => {
  const launch = launchOf(p({ name: 'p', args: ['--model', 'm'], promptFlag: '-i' }), undefined, ['--yolo']);
  assert.deepEqual(launch.args, ['--model', 'm', '--yolo']);
  assert.equal(launch.prompt, undefined);
  assert.deepEqual(launchOf(BUILTIN_PROFILES[0]!, 'hi').args, []);
});

test('caller env wins over profile env', () => {
  const launch = launchOf(p({ name: 'p', env: { A: 'profile', B: 'profile' } }), undefined, [], { A: 'caller', C: 'caller' });
  assert.deepEqual(launch.env, { A: 'caller', B: 'profile', C: 'caller' });
});

test('reserved names are refused in env', () => {
  for (const name of ['IDE_AGENT_TABS_ID', 'ide_agent_tabs_agent', 'JEDITERM_SOURCE', 'JEDITERM_SOURCE_ARGS']) {
    assert.throws(() => checkEnv({ [name]: 'x' }, 'env'), ConfigError, name);
  }
  for (const name of ['', ' ', 'A B', 'A\tB', 'A B']) assert.throws(() => checkEnv({ [name]: 'x' }, 'env'), ConfigError);
  assert.throws(() => checkEnv({ A: 'x'.repeat(30_001) }, 'env'), ConfigError);
  assert.throws(() => checkEnv({ A: 'a\0b' }, 'env'), ConfigError);
  assert.throws(() => checkEnv(Object.fromEntries(Array.from({ length: 65 }, (_, i) => [`V${i}`, ''])), 'env'), ConfigError);
  checkEnv({ CLAUDE_CODE_USE_BEDROCK: '1' }, 'env');
});

test('default agent comes from config and falls back to claude', () => {
  assert.equal(resolveSettings(undefined, '{"defaultAgent": "gemini"}').defaultAgent.name, 'gemini');
  assert.equal(resolveSettings(undefined, '{"defaultAgent": "nope", "other": 1}').defaultAgent.name, 'claude');
  assert.equal(resolveSettings(undefined, '{"defaultAgent": 3}').defaultAgent.name, 'claude');
  const broken = resolveSettings(undefined, 'broken');
  assert.equal(broken.defaultAgent.name, 'claude');
  assert.equal(broken.warnings.length, 1);
  assert.equal(resolveSettings('{"mine": {"command": "m"}}', '{"defaultAgent": "mine"}').defaultAgent.name, 'mine');
  assert.equal(readDefaultAgent('{}'), undefined);
});

test('the preferred terminal comes from config', () => {
  assert.equal(resolveSettings(undefined, '{"terminal": "ghostty"}').preferredTerminal, 'ghostty');
  assert.equal(resolveSettings(undefined, '{"terminal": ""}').preferredTerminal, undefined);
  assert.equal(resolveSettings(undefined, '{}').preferredTerminal, undefined);
  assert.equal(resolveSettings(undefined, '{"terminal": "auto"}').preferredTerminal, undefined);
});

test('tab routing, shell and terminal window come from config, with defaults and warnings', () => {
  const none = resolveSettings(undefined, undefined);
  assert.equal(none.tabRouting, 'project');
  assert.equal(none.terminalWindow, 'last');
  assert.equal(none.shell, undefined);

  const set = resolveSettings(undefined, JSON.stringify({ tabRouting: 'caller', terminalWindow: 'dedicated', shell: 'C:/Tools/pwsh.exe' }));
  assert.equal(set.tabRouting, 'caller');
  assert.equal(set.terminalWindow, 'dedicated');
  assert.equal(set.shell, 'C:/Tools/pwsh.exe');
  assert.deepEqual(set.warnings, []);
  assert.equal(resolveSettings(undefined, '{"shell": "/usr/bin/pwsh"}').shell, '/usr/bin/pwsh');
  assert.equal(resolveSettings(undefined, '{"shell": "auto"}').shell, undefined);

  const bad = resolveSettings(undefined, JSON.stringify({ tabRouting: 'nearest', terminalWindow: 3, shell: 'pwsh.exe', terminal: 7, defaultAgent: 'codex' }));
  assert.equal(bad.tabRouting, 'project');
  assert.equal(bad.terminalWindow, 'last');
  assert.equal(bad.shell, undefined);
  assert.equal(bad.preferredTerminal, undefined);
  assert.equal(bad.defaultAgent.name, 'codex');
  assert.equal(bad.warnings.length, 4);
  const all = bad.warnings.join(' | ');
  assert.match(all, /tabRouting .*"project" or "caller"/);
  assert.match(all, /terminalWindow .*"last" or "dedicated"/);
  assert.match(all, /shell .*absolute path/);
  assert.match(all, /terminal .*must be a string/);
});

test('merge keeps built-in order and appends new profiles in file order', () => {
  const merged = mergeProfiles(BUILTIN_PROFILES, [p({ name: 'z' }), p({ name: 'gemini', label: 'G' }), p({ name: 'a' })]);
  assert.deepEqual(merged.map((x) => x.name), ['claude', 'codex', 'agy', 'copilot', 'gemini', 'grok', 'pi', 'hermes', 'opencode', 'qwen', 'goose', 'codex-local', 'z', 'a']);
  assert.equal(merged[4]!.label, 'G');
});

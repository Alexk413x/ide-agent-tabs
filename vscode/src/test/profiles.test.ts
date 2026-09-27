import assert from 'node:assert/strict';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { test } from 'node:test';
import {
  AGENTS_FILE,
  AgentSettings,
  BUILTIN_PROFILES,
  CONFIG_FILE,
  findOnPath,
  isInstalled,
  launchOf,
  profile,
  readDefaultAgent,
} from '../profiles';
import { BadRequest, checkEnv } from '../request';

function fixture() {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'iat-agents-'));
  const warnings: string[] = [];
  const settings = new AgentSettings(home, w => warnings.push(w));
  let clock = Date.now() / 1000;
  const write = (name: string, text: string) => {
    const file = path.join(home, name);
    fs.writeFileSync(file, text);
    clock += 10;
    fs.utimesSync(file, clock, clock);
  };
  return { home, warnings, settings, write, writeAgents: (text: string) => write(AGENTS_FILE, text) };
}

test('built-in profiles match the design', () => {
  const { settings, warnings } = fixture();
  const profiles = settings.profiles();
  assert.deepEqual(profiles.map(p => p.name), ['claude', 'codex', 'gemini', 'copilot']);
  assert.deepEqual(profiles.map(p => p.label), ['Claude Code', 'Codex', 'Gemini CLI', 'Copilot CLI']);
  assert.deepEqual(profiles.map(p => p.command), ['claude', 'codex', 'gemini', 'copilot']);
  assert.deepEqual(profiles.map(p => p.promptFlag), [undefined, undefined, '-i', '-i']);
  assert.equal(settings.defaultProfile().name, 'claude');
  assert.deepEqual(warnings, []);
});

test('agents file overrides a built-in by name and adds new profiles', () => {
  const { settings, warnings, writeAgents } = fixture();
  writeAgents(`{
    "codex": {"label": "Codex (fast)", "command": "codex", "args": ["--model", "o4"]},
    "opencode-local": {
      "label": "OpenCode (LM Studio)", "command": "opencode",
      "args": ["--model", "lmstudio/qwen3-coder"], "promptFlag": "--prompt",
      "env": {"LMSTUDIO": "1"}, "icon": "icons/opencode.svg"
    },
    "bare": {"command": "bare-cli"}
  }`);
  assert.deepEqual(settings.profiles().map(p => p.name), ['claude', 'codex', 'gemini', 'copilot', 'opencode-local', 'bare']);
  assert.deepEqual(settings.profile('codex'), profile('codex', 'Codex (fast)', 'codex', { args: ['--model', 'o4'], promptFlag: undefined, icon: undefined }));
  assert.deepEqual(
    settings.profile('opencode-local'),
    profile('opencode-local', 'OpenCode (LM Studio)', 'opencode', {
      args: ['--model', 'lmstudio/qwen3-coder'],
      promptFlag: '--prompt',
      env: { LMSTUDIO: '1' },
      icon: 'icons/opencode.svg',
    }),
  );
  assert.equal(settings.profile('bare')?.label, 'bare');
  assert.equal(settings.profile('absent'), undefined);
  assert.deepEqual(warnings, []);
});

test('a bad agents file logs a warning and leaves the built-ins', () => {
  const { settings, warnings, writeAgents } = fixture();
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
    '{"x": {"command": "x", "label": 5}}',
    '{"bad name": {"command": "x"}}',
    '{"claude": {"command": "x"}, "y": {"command": "y", "args": {}}}',
  ];
  bad.forEach((text, i) => {
    writeAgents(text);
    assert.deepEqual(settings.profiles(), BUILTIN_PROFILES, text);
    assert.equal(warnings.length, i + 1, text);
  });
});

test('the agents file is read again only when it changes', () => {
  const { settings, home, writeAgents } = fixture();
  writeAgents('{"x": {"command": "x"}}');
  assert.equal(settings.profiles().at(-1)?.name, 'x');
  assert.equal(settings.profiles(), settings.profiles());
  writeAgents('{"y": {"command": "y"}}');
  assert.equal(settings.profiles().at(-1)?.name, 'y');
  fs.rmSync(path.join(home, AGENTS_FILE));
  assert.deepEqual(settings.profiles(), BUILTIN_PROFILES);
});

test('arguments are profile args, caller args, prompt flag, then the prompt', () => {
  const p = profile('p', 'P', 'cli', { args: ['--model', 'm'], promptFlag: '-i' });
  const launch = launchOf(p, 'hello', ['--yolo']);
  assert.equal(launch.agent, 'p');
  assert.equal(launch.command, 'cli');
  assert.deepEqual(launch.args, ['--model', 'm', '--yolo', '-i']);
  assert.equal(launch.prompt, 'hello');
});

test('no prompt means no prompt flag', () => {
  const p = profile('p', 'P', 'cli', { args: ['--model', 'm'], promptFlag: '-i' });
  const launch = launchOf(p, undefined, ['--yolo']);
  assert.deepEqual(launch.args, ['--model', 'm', '--yolo']);
  assert.equal(launch.prompt, undefined);
  assert.deepEqual(launchOf(profile('claude', 'Claude Code', 'claude'), 'hi').args, []);
});

test('caller env wins over profile env', () => {
  const p = profile('p', 'P', 'cli', { env: { A: 'profile', B: 'profile' } });
  assert.deepEqual(launchOf(p, undefined, [], { A: 'caller', C: 'caller' }).env, { A: 'caller', B: 'profile', C: 'caller' });
});

test('reserved names are refused in profile env', () => {
  for (const name of ['IDE_AGENT_TABS_ID', 'ide_agent_tabs_agent', 'JEDITERM_SOURCE', 'JEDITERM_SOURCE_ARGS']) {
    assert.throws(() => checkEnv({ [name]: 'x' }, 'env'), BadRequest, name);
  }
  checkEnv({ CLAUDE_CODE_USE_BEDROCK: '1' }, 'env');
});

test('default agent comes from config and falls back to claude', () => {
  const { settings, warnings, write } = fixture();
  write(CONFIG_FILE, '{"defaultAgent": "gemini"}');
  assert.equal(settings.defaultProfile().name, 'gemini');
  write(CONFIG_FILE, '{"defaultAgent": "nope", "other": 1}');
  assert.equal(settings.defaultProfile().name, 'claude');
  write(CONFIG_FILE, '{"defaultAgent": 3}');
  assert.equal(settings.defaultProfile().name, 'claude');
  write(CONFIG_FILE, 'broken');
  assert.equal(settings.defaultProfile().name, 'claude');
  assert.equal(warnings.length, 1);
});

test('saving the default agent keeps the other settings', () => {
  const { settings, home } = fixture();
  const config = path.join(home, CONFIG_FILE);
  settings.setDefaultAgent('codex');
  assert.equal(readDefaultAgent(fs.readFileSync(config, 'utf8')), 'codex');
  assert.equal(settings.defaultProfile().name, 'codex');

  fs.writeFileSync(config, '{"defaultAgent": "claude", "theme": {"x": [1, 2]}, "flag": true}');
  settings.setDefaultAgent('copilot');
  const saved = JSON.parse(fs.readFileSync(config, 'utf8'));
  assert.deepEqual(saved, { defaultAgent: 'copilot', theme: { x: [1, 2] }, flag: true });
  assert.deepEqual(fs.readdirSync(home), [CONFIG_FILE]);
});

test('saving the default agent leaves a broken config alone', () => {
  const { settings, home, warnings } = fixture();
  const config = path.join(home, CONFIG_FILE);
  fs.writeFileSync(config, '{broken');
  settings.setDefaultAgent('codex');
  assert.equal(fs.readFileSync(config, 'utf8'), '{broken');
  assert.equal(warnings.length, 1);
});

test('installed means the command is on PATH, with Windows extensions on Windows', () => {
  const { home } = fixture();
  const bin = path.join(home, 'bin');
  fs.mkdirSync(bin);
  for (const name of ['posix-cli', 'npm-cli.cmd', 'shim-cli.ps1', 'native-cli.exe', 'old-cli.bat']) fs.writeFileSync(path.join(bin, name), '');
  const searchPath = [path.join(home, 'missing'), bin].join(path.delimiter);
  for (const command of ['posix-cli', 'npm-cli', 'shim-cli', 'native-cli', 'old-cli', 'native-cli.exe']) {
    assert.ok(isInstalled(command, searchPath, true), command);
  }
  assert.ok(isInstalled('posix-cli', searchPath, false));
  assert.ok(!isInstalled('npm-cli', searchPath, false));
  assert.ok(!isInstalled('absent', searchPath, true));
  assert.ok(isInstalled(path.join(bin, 'posix-cli'), '', false));
  assert.ok(isInstalled(path.join(bin, 'npm-cli'), '', true));
  assert.ok(!isInstalled(path.join(bin, 'absent'), searchPath, false));
  assert.ok(!isInstalled(`bin${path.sep}posix-cli`, searchPath, false));
});

test('finds an executable on PATH and skips blank, quoted and invalid entries', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'iat-path-'));
  fs.writeFileSync(path.join(dir, 'tool.exe'), '');
  const searchPath = ['', '  ', path.join(dir, 'no', 'such'), 'bad<>|dir', `"${dir}"`].join(path.delimiter);
  assert.equal(findOnPath(searchPath, 'tool.exe'), path.join(dir, 'tool.exe'));
  assert.equal(findOnPath(searchPath, 'absent.exe'), undefined);
});

test('finds the Microsoft Store pwsh alias', t => {
  const windowsApps = path.join(process.env.LOCALAPPDATA ?? '', 'Microsoft', 'WindowsApps');
  const alias = path.join(windowsApps, 'pwsh.exe');
  let present = false;
  try {
    present = fs.lstatSync(alias).isSymbolicLink();
  } catch {
    present = false;
  }
  if (process.platform !== 'win32' || !present) return t.skip('Store pwsh not installed');
  assert.equal(fs.existsSync(alias), false, 'existsSync follows the alias and misses it');
  assert.equal(findOnPath(windowsApps, 'pwsh.exe'), alias);
});

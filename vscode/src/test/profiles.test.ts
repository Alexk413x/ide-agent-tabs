import assert from 'node:assert/strict';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { test } from 'node:test';
import {
  AGENTS_FILE,
  AgentProfile,
  AgentSettings,
  BUILTIN_PROFILES,
  CODEX_TAB_ARGS,
  CONFIG_FILE,
  findOnPath,
  isInstalled,
  launchOf,
  LaunchContext,
  planLaunch,
  profile,
  readDefaultAgent,
} from '../profiles';
import { BadRequest, checkEnv } from '../request';
import { DETECTED_FILE, DetectedOri, parseDetected, SHARED_DEFAULTS, userSettingValue, withSharedValue } from '../sharedSettings';

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
  assert.deepEqual(profiles.map(p => p.name), ['claude', 'codex', 'agy', 'copilot', 'gemini', 'grok', 'pi', 'hermes', 'opencode', 'qwen', 'goose', 'codex-local']);
  assert.deepEqual(profiles.map(p => p.label), [
    'Claude Code', 'Codex', 'Antigravity CLI', 'Copilot CLI', 'Gemini CLI', 'Grok Build', 'Pi', 'Hermes', 'OpenCode', 'Qwen Code', 'Goose', 'Codex (local)',
  ]);
  assert.deepEqual(profiles.map(p => p.command), ['claude', 'codex', 'agy', 'copilot', 'gemini', 'grok', 'pi', 'hermes', 'opencode', 'qwen', 'goose', 'codex']);
  assert.deepEqual(profiles.map(p => p.promptFlag), [
    undefined, undefined, '-i', '-i', '-i', undefined, undefined, '-q', '--prompt', '-i', '-t', undefined,
  ]);
  assert.deepEqual(profiles.map(p => p.args.length), [0, CODEX_TAB_ARGS.length, 0, 0, 0, 0, 0, 1, 0, 0, 2, CODEX_TAB_ARGS.length + 3]);
  assert.deepEqual(profiles[1]!.args, CODEX_TAB_ARGS);
  assert.deepEqual(profiles[7]!.args, ['chat']);
  assert.deepEqual(profiles[10]!.args, ['run', '-s']);
  assert.deepEqual(profiles[11]!.args, [...CODEX_TAB_ARGS, '--oss', '--local-provider', 'ollama']);
  assert.deepEqual(profiles[1]!.args.slice(0, 2), ['--no-daemon', '-c']);
  assert.equal(settings.defaultProfile().name, 'claude');
  assert.deepEqual(warnings, []);
});

test('the manifest wires every built-in profile and its icons', () => {
  const root = path.resolve(__dirname, '..', '..');
  const manifest = JSON.parse(fs.readFileSync(path.join(root, 'package.json'), 'utf8'));
  const names = BUILTIN_PROFILES.map(p => p.name);
  const keys = names.map(n => n.replace(/-(\w)/g, (_, c: string) => c.toUpperCase()));
  const { contributes } = manifest;
  assert.deepEqual(contributes.configuration[0].properties['ideAgentTabs.defaultAgent'].enum, names);
  assert.deepEqual(contributes.configuration[0].properties['ideAgentTabs.defaultAgent'].enumItemLabels, BUILTIN_PROFILES.map(p => p.label));
  const commands = contributes.commands.map((c: { command: string }) => c.command);
  const menu = contributes.menus['ideAgentTabs.agents'].filter((m: { command: string }) => m.command.startsWith('ideAgentTabs.open.'));
  assert.deepEqual(menu.map((m: { command: string }) => m.command), keys.map(k => `ideAgentTabs.open.${k}`));
  for (const key of keys) {
    assert.ok(commands.includes(`ideAgentTabs.newTab.${key}`), key);
    assert.ok(commands.includes(`ideAgentTabs.open.${key}`), key);
    assert.ok(contributes.menus['editor/title'].some((m: { when?: string }) => m.when === `ideAgentTabs.buttonAgent == ${key}`), key);
  }
  for (const c of contributes.commands) {
    if (typeof c.icon !== 'object') continue;
    for (const file of Object.values<string>(c.icon)) assert.ok(fs.existsSync(path.join(root, file)), file);
  }
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
  assert.deepEqual(settings.profiles().map(p => p.name), ['claude', 'codex', 'agy', 'copilot', 'gemini', 'grok', 'pi', 'hermes', 'opencode', 'qwen', 'goose', 'codex-local', 'opencode-local', 'bare']);
  assert.deepEqual(settings.profile('codex'), profile('codex', 'Codex (fast)', 'codex', { args: ['--model', 'o4'], promptFlag: undefined, modelFlag: undefined, icon: undefined }));
  assert.deepEqual(
    settings.profile('opencode-local'),
    profile('opencode-local', 'OpenCode (LM Studio)', 'opencode', {
      args: ['--model', 'lmstudio/qwen3-coder'],
      promptFlag: '--prompt',
      modelFlag: undefined,
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

test('a preferred agent wins over config unless it has no profile', () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'iat-agents-'));
  let preferred: string | undefined = 'codex';
  const settings = new AgentSettings(home, () => {}, () => preferred);
  fs.writeFileSync(path.join(home, CONFIG_FILE), '{"defaultAgent": "gemini"}');
  assert.equal(settings.defaultProfile().name, 'codex');
  preferred = 'nope';
  assert.equal(settings.defaultProfile().name, 'gemini');
  preferred = undefined;
  assert.equal(settings.defaultProfile().name, 'gemini');
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

test('shared settings default when config.json is missing or lacks the keys', () => {
  const { settings, write, warnings } = fixture();
  assert.deepEqual(settings.shared(), SHARED_DEFAULTS);
  assert.deepEqual(settings.sharedFound(), {});
  write(CONFIG_FILE, '{"defaultAgent": "codex"}');
  assert.deepEqual(settings.shared(), { tabRouting: 'project', terminal: 'auto', shell: 'auto', terminalWindow: 'last', launchVia: 'direct', closeAfterHandoff: true, focusNewTabs: 'auto' });
  assert.deepEqual(warnings, []);
});

test('shared settings read the seven keys and drop invalid values', () => {
  const { settings, write, warnings } = fixture();
  write(CONFIG_FILE, '{"tabRouting": "caller", "terminal": "wezterm", "shell": "/opt/pwsh", "terminalWindow": "dedicated", "launchVia": "ori"}');
  assert.deepEqual(settings.shared(), { tabRouting: 'caller', terminal: 'wezterm', shell: '/opt/pwsh', terminalWindow: 'dedicated', launchVia: 'ori', closeAfterHandoff: true, focusNewTabs: 'auto' });
  write(CONFIG_FILE, '{"tabRouting": "nowhere", "terminal": 3, "shell": " ", "terminalWindow": "", "launchVia": "both"}');
  assert.deepEqual(settings.sharedFound(), { shell: 'auto' });
  assert.deepEqual(settings.shared(), SHARED_DEFAULTS);
  write(CONFIG_FILE, 'broken');
  assert.equal(settings.sharedFound(), undefined);
  assert.deepEqual(settings.shared(), SHARED_DEFAULTS);
  assert.equal(warnings.length, 2);
});

test('saving a shared setting keeps unknown keys', () => {
  const { settings, home } = fixture();
  const config = path.join(home, CONFIG_FILE);
  assert.ok(settings.setShared('tabRouting', 'caller'));
  assert.deepEqual(JSON.parse(fs.readFileSync(config, 'utf8')), { tabRouting: 'caller' });

  fs.writeFileSync(config, '{"defaultAgent": "codex", "jev": {"enabled": true, "tiers": {"codex": "x"}}, "terminal": "kitty"}');
  assert.ok(settings.setShared('terminalWindow', 'dedicated'));
  assert.ok(settings.setShared('shell', '/opt/microsoft/powershell/7/pwsh'));
  assert.deepEqual(JSON.parse(fs.readFileSync(config, 'utf8')), {
    defaultAgent: 'codex',
    jev: { enabled: true, tiers: { codex: 'x' } },
    terminal: 'kitty',
    terminalWindow: 'dedicated',
    shell: '/opt/microsoft/powershell/7/pwsh',
  });
  assert.deepEqual(fs.readdirSync(home), [CONFIG_FILE]);
});

test('choosing Automatic removes the terminal and shell keys', () => {
  const { settings, home } = fixture();
  const config = path.join(home, CONFIG_FILE);
  fs.writeFileSync(config, '{"terminal": "tmux", "shell": "/opt/pwsh", "other": 1}');
  assert.ok(settings.setShared('terminal', 'auto'));
  assert.ok(settings.setShared('shell', ''));
  assert.deepEqual(JSON.parse(fs.readFileSync(config, 'utf8')), { other: 1 });
  assert.equal(withSharedValue(undefined, CONFIG_FILE, 'terminal', 'auto'), '{}' + String.fromCharCode(10));
});

test('saving a shared setting leaves a broken config alone', () => {
  const { settings, home, warnings } = fixture();
  const config = path.join(home, CONFIG_FILE);
  fs.writeFileSync(config, '{broken');
  assert.equal(settings.setShared('terminal', 'wezterm'), false);
  assert.equal(fs.readFileSync(config, 'utf8'), '{broken');
  assert.equal(warnings.length, 1);
});

test('detection lists terminals and shells, and is empty when the file is missing or broken', () => {
  const { settings, write } = fixture();
  assert.deepEqual(settings.detected(), { terminals: [], shells: [], ori: null });
  write(
    DETECTED_FILE,
    JSON.stringify({
      version: 1,
      terminals: [{ id: 'wezterm', name: 'WezTerm' }, { id: 'kitty' }, { name: 'no id' }, 7],
      shells: [{ path: '/opt/pwsh', label: 'PowerShell 7.5.2 (MSI)', source: 'msi' }, { path: '/opt/ps' }, { label: 'no path' }],
    }),
  );
  assert.deepEqual(settings.detected(), {
    terminals: [{ id: 'wezterm', name: 'WezTerm' }, { id: 'kitty', name: 'kitty' }],
    shells: [{ path: '/opt/pwsh', label: 'PowerShell 7.5.2 (MSI)' }, { path: '/opt/ps', label: '/opt/ps' }],
    ori: null,
  });
  write(DETECTED_FILE, '{"terminals": "none", "shells": {}}');
  assert.deepEqual(settings.detected(), { terminals: [], shells: [], ori: null });
  write(DETECTED_FILE, 'broken');
  assert.deepEqual(settings.detected(), { terminals: [], shells: [], ori: null });
  assert.deepEqual(parseDetected('{}'), { terminals: [], shells: [], ori: null });
});

test('only the user-level value of a shared setting is shared; workspace values are ignored', () => {
  const workspace = { workspaceValue: '/tmp/evil', workspaceFolderValue: '/tmp/evil' };
  assert.equal(userSettingValue('shell', workspace), 'auto');
  assert.equal(userSettingValue('terminal', { ...workspace, globalValue: 'wezterm' }), 'wezterm');
  assert.equal(userSettingValue('tabRouting', { workspaceValue: 'caller' }), 'project');
  assert.equal(userSettingValue('terminalWindow', { globalValue: ' ', workspaceValue: 'dedicated' }), 'last');
  assert.equal(userSettingValue('shell', undefined), 'auto');
});

test('built-in profiles carry the model flag of each CLI', () => {
  assert.deepEqual(
    BUILTIN_PROFILES.map(p => [p.name, p.modelFlag]),
    [
      ['claude', '--model'], ['codex', '-m'], ['agy', '--model'], ['copilot', '--model'], ['gemini', '-m'], ['grok', '-m'], ['pi', '--model'],
      ['hermes', '-m'], ['opencode', '-m'], ['qwen', '-m'], ['goose', '--model'], ['codex-local', '-m'],
    ],
  );
});

test('a custom profile sets modelFlag in the agents file', () => {
  const { settings, warnings, writeAgents } = fixture();
  writeAgents('{"mine": {"command": "mine-cli", "modelFlag": "--use"}, "codex": {"command": "codex"}}');
  assert.equal(settings.profile('mine')?.modelFlag, '--use');
  assert.equal(settings.profile('codex')?.modelFlag, undefined);
  assert.deepEqual(warnings, []);
  writeAgents('{"mine": {"command": "mine-cli", "modelFlag": " "}}');
  assert.deepEqual(settings.profiles(), BUILTIN_PROFILES);
  writeAgents('{"mine": {"command": "mine-cli", "modelFlag": 1}}');
  assert.deepEqual(settings.profiles(), BUILTIN_PROFILES);
  assert.equal(warnings.length, 2);
});

const oriAll: DetectedOri = { path: '/home/u/.local/bin/ori', version: '0.14.3', agents: ['claude', 'codex'] };
const claudeProfile = profile('claude', 'Claude Code', 'claude', { modelFlag: '--model' });
const codexProfile = profile('codex', 'Codex', 'codex', { args: ['--no-daemon'], modelFlag: '-m' });
const geminiProfile = profile('gemini', 'Gemini CLI', 'gemini', { promptFlag: '-i', modelFlag: '-m' });
const bareProfile = profile('bare', 'Bare', 'bare-cli', { promptFlag: '-i' });

function context(extra: Partial<LaunchContext> = {}): LaunchContext {
  return { setting: 'direct', ori: oriAll, windows: false, searchPath: '', ...extra };
}

test('a direct launch puts the model flag after the profile args', () => {
  const launch = planLaunch(codexProfile, context({ model: 'gpt-5', args: ['--yolo'], prompt: 'hi' }));
  assert.equal(launch.via, 'direct');
  assert.equal(launch.command, 'codex');
  assert.deepEqual(launch.args, ['--no-daemon', '-m', 'gpt-5', '--yolo']);
  const withFlag = planLaunch(geminiProfile, context({ model: 'gemini-2.5-pro', prompt: 'hi' }));
  assert.deepEqual(withFlag.args, ['-m', 'gemini-2.5-pro', '-i']);
  assert.equal(withFlag.prompt, 'hi');
  assert.deepEqual(planLaunch(claudeProfile, context()).args, []);
});

test('Goose starts with goose run -s -t for a first message and goose session without one', () => {
  const goose = BUILTIN_PROFILES.find(p => p.name === 'goose')!;
  const withPrompt = planLaunch(goose, context({ prompt: 'hi', model: 'm1' }));
  assert.deepEqual([withPrompt.command, ...withPrompt.args, withPrompt.prompt], ['goose', 'run', '-s', '--model', 'm1', '-t', 'hi']);
  const empty = planLaunch(goose, context({ model: 'm1' }));
  assert.deepEqual([empty.command, ...empty.args], ['goose', 'session', '--model', 'm1']);
  assert.deepEqual(planLaunch(goose, context({ setting: 'ori' })).args, ['session']);
  assert.throws(() => planLaunch(goose, context({ via: 'ori' })), /goose can't launch through Ori/);
  const custom = { ...goose, args: ['run', '-s', '--debug'] };
  assert.deepEqual(planLaunch(custom, context()).args, ['run', '-s', '--debug']);
  const renamed = { ...goose, command: 'goose-cli' };
  assert.deepEqual(planLaunch(renamed, context()).args, ['run', '-s']);
});

test('a model for a profile without modelFlag fails, never silently', () => {
  const message = 'bare has no model option; open it without model, or set modelFlag for it in agents.json';
  assert.throws(() => planLaunch(bareProfile, context({ model: 'm' })), new BadRequest(message));
  assert.throws(() => planLaunch(bareProfile, context({ model: 'm', via: 'direct' })), new BadRequest(message));
  assert.deepEqual(planLaunch(bareProfile, context()).args, []);
});

test('an Ori launch runs ori with the agent, the model, then the profile args', () => {
  const launch = planLaunch(codexProfile, context({ via: 'ori', model: 'openai/gpt-5', args: ['--yolo'], prompt: 'hi', env: { A: '1' } }));
  assert.equal(launch.via, 'ori');
  assert.equal(launch.agent, 'codex');
  assert.equal(launch.command, 'ori');
  assert.deepEqual(launch.args, ['codex', '--model', 'openai/gpt-5', '--no-daemon', '--yolo']);
  assert.equal(launch.prompt, 'hi');
  assert.deepEqual(launch.env, { A: '1' });
  assert.deepEqual(planLaunch(claudeProfile, context({ via: 'ori' })).args, ['claude']);
});

test('an Ori launch needs no modelFlag on the profile', () => {
  const noFlag = profile('claude', 'Claude Code', 'claude');
  assert.deepEqual(planLaunch(noFlag, context({ via: 'ori', model: 'anthropic/claude-sonnet-4.5' })).args, ['claude', '--model', 'anthropic/claude-sonnet-4.5']);
});

test('an explicit via beats the setting in both directions', () => {
  assert.equal(planLaunch(claudeProfile, context({ setting: 'ori' })).via, 'ori');
  assert.equal(planLaunch(claudeProfile, context({ setting: 'ori', via: 'direct' })).via, 'direct');
  assert.equal(planLaunch(claudeProfile, context({ setting: 'direct', via: 'ori' })).via, 'ori');
  assert.equal(planLaunch(claudeProfile, context({ setting: 'direct' })).via, 'direct');
});

test('the setting falls back to a direct launch when Ori cannot run the agent', () => {
  const cases: [AgentProfile, Partial<LaunchContext>][] = [
    [claudeProfile, { ori: null }],
    [geminiProfile, {}],
    [profile('grok', 'Grok', 'grok'), {}],
    [profile('pi', 'Pi', 'pi'), { ori: { ...oriAll, agents: ['claude'] } }],
  ];
  for (const [p, extra] of cases) {
    const launch = planLaunch(p, context({ setting: 'ori', ...extra }));
    assert.equal(launch.via, 'direct', p.name);
    assert.equal(launch.command, p.command);
  }
  const withModel = planLaunch(geminiProfile, context({ setting: 'ori', model: 'gemini-2.5-pro' }));
  assert.deepEqual(withModel.args, ['-m', 'gemini-2.5-pro']);
  assert.throws(() => planLaunch(bareProfile, context({ setting: 'ori', model: 'm' })), /has no model option/);
});

test('an explicit Ori launch that cannot run fails with the reason', () => {
  assert.throws(() => planLaunch(claudeProfile, context({ via: 'ori', ori: null })), new BadRequest("claude can't launch through Ori: Ori is not installed"));
  assert.throws(() => planLaunch(geminiProfile, context({ via: 'ori' })), new BadRequest("gemini can't launch through Ori: Ori does not support gemini"));
  assert.throws(
    () => planLaunch(profile('pi', 'Pi', 'pi'), context({ via: 'ori', ori: { ...oriAll, agents: ['claude'] } })),
    new BadRequest("pi can't launch through Ori: Ori does not list pi as launchable"),
  );
});

function shimFixture(files: string[]) {
  const bin = fs.mkdtempSync(path.join(os.tmpdir(), 'iat-shim-'));
  for (const name of files) fs.writeFileSync(path.join(bin, name), '');
  return bin;
}

test('on Windows, a .cmd shim agent refuses Ori arguments with | " % ^ & < or >', () => {
  const bin = shimFixture(['codex.cmd', 'claude.exe']);
  for (const bad of ['a|b', 'say "hi"', '50%', 'a^b', 'a&b', '<x', 'x>']) {
    const own = { via: 'ori', windows: true, searchPath: bin } as const;
    assert.throws(() => planLaunch(codexProfile, context({ ...own, prompt: bad })), /can't launch through Ori: .*\.cmd shim/, bad);
    assert.throws(() => planLaunch(codexProfile, context({ ...own, args: [bad] })), /\.cmd shim/, bad);
    assert.throws(() => planLaunch(codexProfile, context({ ...own, model: bad })), /\.cmd shim/, bad);
    assert.equal(planLaunch(claudeProfile, context({ ...own, prompt: bad })).via, 'ori', `claude.exe takes ${bad}`);
    assert.equal(planLaunch(codexProfile, context({ ...own, windows: false, prompt: bad })).via, 'ori', `non-Windows takes ${bad}`);
  }
  assert.equal(planLaunch(codexProfile, context({ via: 'ori', windows: true, searchPath: bin, prompt: 'plain text' })).via, 'ori');
});

test('on Windows, the setting falls back to a direct launch when a .cmd shim refuses an argument', () => {
  const bin = shimFixture(['codex.cmd']);
  const launch = planLaunch(codexProfile, context({ setting: 'ori', windows: true, searchPath: bin, prompt: 'say "hi"' }));
  assert.equal(launch.via, 'direct');
  assert.equal(launch.command, 'codex');
});

test('the Codex tab arguments hold characters a .cmd shim refuses, so Codex falls back or fails on Windows', () => {
  const real = BUILTIN_PROFILES.find(p => p.name === 'codex')!;
  assert.ok(real.args.some(a => /[|"%^&<>]/.test(a)));
  const bin = shimFixture(['codex.cmd']);
  assert.equal(planLaunch(real, context({ setting: 'ori', windows: true, searchPath: bin })).via, 'direct');
  assert.throws(() => planLaunch(real, context({ via: 'ori', windows: true, searchPath: bin })), /can't launch through Ori/);
  assert.equal(planLaunch(real, context({ via: 'ori', windows: false })).via, 'ori');
});

test('detection reads the Ori entry and is null without it', () => {
  assert.deepEqual(parseDetected('{"ori": {"path": "/x/ori", "version": "0.14.3", "agents": ["claude", 3, "codex"]}}').ori, {
    path: '/x/ori',
    version: '0.14.3',
    agents: ['claude', 'codex'],
  });
  assert.equal(parseDetected('{"ori": null}').ori, null);
  assert.equal(parseDetected('{"ori": {"agents": ["claude"]}}').ori, null);
  assert.equal(parseDetected('{"ori": []}').ori, null);
  assert.deepEqual(parseDetected('{"ori": {"path": "/x/ori"}}').ori, { path: '/x/ori', version: undefined, agents: [] });
});

test('launchVia saves to config.json, defaults to direct and keeps other keys', () => {
  const { settings, home, write } = fixture();
  assert.equal(settings.shared().launchVia, 'direct');
  write(CONFIG_FILE, '{"defaultAgent": "codex", "launchVia": "ori"}');
  assert.equal(settings.shared().launchVia, 'ori');
  assert.ok(settings.setShared('launchVia', 'direct'));
  assert.deepEqual(JSON.parse(fs.readFileSync(path.join(home, CONFIG_FILE), 'utf8')), { defaultAgent: 'codex', launchVia: 'direct' });
  assert.equal(userSettingValue('launchVia', { globalValue: 'ori', workspaceValue: 'direct' }), 'ori');
  assert.equal(userSettingValue('launchVia', { workspaceValue: 'ori' }), 'direct');
});

test('closeAfterHandoff defaults to true, reads a boolean and drops other values', () => {
  const { settings, write } = fixture();
  assert.equal(settings.shared().closeAfterHandoff, true);
  write(CONFIG_FILE, '{"closeAfterHandoff": false}');
  assert.equal(settings.shared().closeAfterHandoff, false);
  assert.deepEqual(settings.sharedFound(), { closeAfterHandoff: false });
  write(CONFIG_FILE, '{"closeAfterHandoff": "false"}');
  assert.deepEqual(settings.sharedFound(), {});
  assert.equal(settings.shared().closeAfterHandoff, true);
});

test('focusNewTabs defaults to auto, reads the three modes, and auto removes the key', () => {
  const { settings, home, write } = fixture();
  const config = path.join(home, CONFIG_FILE);
  assert.equal(settings.shared().focusNewTabs, 'auto');
  for (const mode of ['always', 'never', 'auto'] as const) {
    write(CONFIG_FILE, JSON.stringify({ focusNewTabs: mode }));
    assert.equal(settings.shared().focusNewTabs, mode);
  }
  write(CONFIG_FILE, '{"focusNewTabs": true}');
  assert.deepEqual(settings.sharedFound(), {});
  fs.writeFileSync(config, '{"defaultAgent": "codex"}');
  assert.ok(settings.setShared('focusNewTabs', 'always'));
  assert.deepEqual(JSON.parse(fs.readFileSync(config, 'utf8')), { defaultAgent: 'codex', focusNewTabs: 'always' });
  assert.ok(settings.setShared('focusNewTabs', 'auto'));
  assert.deepEqual(JSON.parse(fs.readFileSync(config, 'utf8')), { defaultAgent: 'codex' });
  assert.equal(userSettingValue('focusNewTabs', { globalValue: 'never', workspaceValue: 'always' }), 'never');
  assert.equal(userSettingValue('focusNewTabs', { workspaceValue: 'always' }), 'auto');
});

test('closeAfterHandoff writes false only when unchecked and removes the key when checked', () => {
  const { settings, home } = fixture();
  const config = path.join(home, CONFIG_FILE);
  fs.writeFileSync(config, '{"defaultAgent": "codex"}');
  assert.ok(settings.setShared('closeAfterHandoff', true));
  assert.deepEqual(JSON.parse(fs.readFileSync(config, 'utf8')), { defaultAgent: 'codex' });
  assert.ok(settings.setShared('closeAfterHandoff', false));
  assert.deepEqual(JSON.parse(fs.readFileSync(config, 'utf8')), { defaultAgent: 'codex', closeAfterHandoff: false });
  assert.ok(settings.setShared('closeAfterHandoff', true));
  assert.deepEqual(JSON.parse(fs.readFileSync(config, 'utf8')), { defaultAgent: 'codex' });
  assert.equal(userSettingValue('closeAfterHandoff', { globalValue: false, workspaceValue: true }), false);
  assert.equal(userSettingValue('closeAfterHandoff', { workspaceValue: false }), true);
  assert.equal(userSettingValue('closeAfterHandoff', { globalValue: 'no' }), true);
});

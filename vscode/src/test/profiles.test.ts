import assert from 'node:assert/strict';
import * as fs from 'node:fs';
import { createServer } from 'node:http';
import type { AddressInfo } from 'node:net';
import * as os from 'node:os';
import * as path from 'node:path';
import { test } from 'node:test';
import {
  AGENTS_FILE,
  agentEntries,
  AgentProfile,
  AgentSettings,
  answersHealth,
  BUILTIN_PROFILES,
  CLAUDE_TAB_SETTINGS_FILE,
  claudeTabSettings,
  CODEX_TAB_ARGS,
  codexHttp,
  codexHttpArg,
  codexPython,
  CONFIG_FILE,
  copilotHooksFile,
  copilotHttp,
  CWD_ENV,
  cwdHeader,
  findOnPath,
  isCopilotCommand,
  isInstalled,
  launchOf,
  LaunchContext,
  planLaunch,
  profile,
  readDefaultAgent,
  withCodexHttp,
  withCodexPython,
  withCopilotHttp,
} from '../profiles';
import { BadRequest, checkEnv } from '../request';
import { DETECTED_FILE, DetectedOri, parseDetected, SHARED_DEFAULTS, userSettingValue, withSharedValue } from '../sharedSettings';

function fixture() {
  const home = fs.mkdtempSync(path.join(fs.realpathSync.native(os.tmpdir()), 'iat-agents-'));
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
  assert.deepEqual(profiles.map(p => p.name), ['claude', 'codex', 'agy', 'copilot', 'grok', 'pi', 'hermes', 'opencode', 'qwen', 'goose', 'codex-local']);
  assert.deepEqual(profiles.map(p => p.label), [
    'Claude Code', 'Codex', 'Antigravity CLI', 'Copilot CLI', 'Grok Build', 'Pi', 'Hermes', 'OpenCode', 'Qwen Code', 'Goose', 'Codex (local)',
  ]);
  assert.deepEqual(profiles.map(p => p.command), ['claude', 'codex', 'agy', 'copilot', 'grok', 'pi', 'hermes', 'opencode', 'qwen', 'goose', 'codex']);
  assert.deepEqual(profiles.map(p => p.promptFlag), [
    undefined, undefined, '-i', '-i', undefined, undefined, '-q', '--prompt', '-i', '-t', undefined,
  ]);
  assert.deepEqual(profiles.map(p => p.args.length), [0, CODEX_TAB_ARGS.length, 0, 0, 0, 0, 1, 0, 0, 2, CODEX_TAB_ARGS.length + 3]);
  assert.deepEqual(profiles[1]!.args, CODEX_TAB_ARGS);
  assert.deepEqual(profiles[6]!.args, ['chat']);
  assert.deepEqual(profiles[9]!.args, ['run', '-s']);
  assert.deepEqual(profiles[10]!.args, [...CODEX_TAB_ARGS, '--oss', '--local-provider', 'ollama']);
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
  assert.deepEqual(settings.profiles().map(p => p.name), ['claude', 'codex', 'agy', 'copilot', 'grok', 'pi', 'hermes', 'opencode', 'qwen', 'goose', 'codex-local', 'opencode-local', 'bare']);
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
  write(CONFIG_FILE, '{"defaultAgent": "copilot"}');
  assert.equal(settings.defaultProfile().name, 'copilot');
  write(CONFIG_FILE, '{"defaultAgent": "nope", "other": 1}');
  assert.equal(settings.defaultProfile().name, 'claude');
  write(CONFIG_FILE, '{"defaultAgent": 3}');
  assert.equal(settings.defaultProfile().name, 'claude');
  write(CONFIG_FILE, 'broken');
  assert.equal(settings.defaultProfile().name, 'claude');
  assert.equal(warnings.length, 1);
});

test('a preferred agent wins over config unless it has no profile', () => {
  const home = fs.mkdtempSync(path.join(fs.realpathSync.native(os.tmpdir()), 'iat-agents-'));
  let preferred: string | undefined = 'codex';
  const settings = new AgentSettings(home, () => {}, () => preferred);
  fs.writeFileSync(path.join(home, CONFIG_FILE), '{"defaultAgent": "copilot"}');
  assert.equal(settings.defaultProfile().name, 'codex');
  preferred = 'nope';
  assert.equal(settings.defaultProfile().name, 'copilot');
  preferred = undefined;
  assert.equal(settings.defaultProfile().name, 'copilot');
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
  const dir = fs.mkdtempSync(path.join(fs.realpathSync.native(os.tmpdir()), 'iat-path-'));
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
  assert.deepEqual(settings.shared(), { tabRouting: 'project', terminal: 'auto', shell: 'auto', terminalWindow: 'last', launchVia: 'direct', closeAfterHandoff: true, allowResume: true, showOriAgents: false, focusNewTabs: 'auto', claudeMod: 'on' });
  assert.deepEqual(warnings, []);
});

test('shared settings read the seven keys and drop invalid values', () => {
  const { settings, write, warnings } = fixture();
  write(CONFIG_FILE, '{"tabRouting": "caller", "terminal": "wezterm", "shell": "/opt/pwsh", "terminalWindow": "dedicated", "launchVia": "ori"}');
  assert.deepEqual(settings.shared(), { tabRouting: 'caller', terminal: 'wezterm', shell: '/opt/pwsh', terminalWindow: 'dedicated', launchVia: 'ori', closeAfterHandoff: true, allowResume: true, showOriAgents: false, focusNewTabs: 'auto', claudeMod: 'on' });
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
      ['claude', '--model'], ['codex', '-m'], ['agy', '--model'], ['copilot', '--model'], ['grok', '-m'], ['pi', '--model'],
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

test('showOriAgents is a shared flag that defaults to off and saves only when on', () => {
  const { settings, write } = fixture();
  assert.equal(settings.shared().showOriAgents, false);
  write(CONFIG_FILE, '{"showOriAgents": true}');
  assert.equal(settings.shared().showOriAgents, true);
  write(CONFIG_FILE, '{"showOriAgents": "yes"}');
  assert.deepEqual(settings.sharedFound(), {});
  assert.equal(withSharedValue('{"showOriAgents": true}', CONFIG_FILE, 'showOriAgents', false), '{}\n');
  assert.equal(withSharedValue(undefined, CONFIG_FILE, 'showOriAgents', true), '{\n  "showOriAgents": true\n}\n');
});

test('agent entries put an Ori entry under each agent Ori can launch, only when shown', () => {
  const qwen = profile('qwen', 'Qwen Code', 'qwen');
  const all = [claudeProfile, qwen, codexProfile, profile('copilot', 'Copilot CLI', 'copilot')];
  const ori: DetectedOri = { ...oriAll, agents: ['claude', 'codex', 'qwen'] };
  const installed = (p: AgentProfile) => p.name !== 'codex';
  const names = (shown: boolean, detected: DetectedOri | null = ori) => agentEntries(all, detected, shown, installed).map(e => e.name);
  assert.deepEqual(names(false), ['claude', 'qwen', 'copilot']);
  assert.deepEqual(names(true), ['claude', 'ori:claude', 'qwen', 'ori:codex', 'copilot']);
  assert.deepEqual(names(true, null), ['claude', 'qwen', 'copilot']);
  const entry = agentEntries(all, ori, true, installed)[1];
  assert.deepEqual(entry, { name: 'ori:claude', label: 'Ori - Claude Code', profile: claudeProfile, via: 'ori' });
});
const claudeProfile = profile('claude', 'Claude Code', 'claude', { modelFlag: '--model' });
const codexProfile = profile('codex', 'Codex', 'codex', { args: ['--no-daemon'], modelFlag: '-m' });
const copilotProfile = profile('copilot', 'Copilot CLI', 'copilot', { promptFlag: '-i', modelFlag: '--model' });
const bareProfile = profile('bare', 'Bare', 'bare-cli', { promptFlag: '-i' });

function context(extra: Partial<LaunchContext> = {}): LaunchContext {
  return { setting: 'direct', ori: oriAll, windows: false, searchPath: '', ...extra };
}

test('a direct launch puts the model flag after the profile args', () => {
  const launch = planLaunch(codexProfile, context({ model: 'gpt-5', args: ['--yolo'], prompt: 'hi' }));
  assert.equal(launch.via, 'direct');
  assert.equal(launch.command, 'codex');
  assert.deepEqual(launch.args, ['--no-daemon', '-m', 'gpt-5', '--yolo']);
  const withFlag = planLaunch(copilotProfile, context({ model: 'gpt-5', prompt: 'hi' }));
  assert.deepEqual(withFlag.args, ['--model', 'gpt-5', '-i']);
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
    [copilotProfile, {}],
    [profile('grok', 'Grok', 'grok'), {}],
    [profile('pi', 'Pi', 'pi'), { ori: { ...oriAll, agents: ['claude'] } }],
  ];
  for (const [p, extra] of cases) {
    const launch = planLaunch(p, context({ setting: 'ori', ...extra }));
    assert.equal(launch.via, 'direct', p.name);
    assert.equal(launch.command, p.command);
  }
  const withModel = planLaunch(copilotProfile, context({ setting: 'ori', model: 'gpt-5' }));
  assert.deepEqual(withModel.args, ['--model', 'gpt-5']);
  assert.throws(() => planLaunch(bareProfile, context({ setting: 'ori', model: 'm' })), /has no model option/);
});

test('an explicit Ori launch that cannot run fails with the reason', () => {
  assert.throws(() => planLaunch(claudeProfile, context({ via: 'ori', ori: null })), new BadRequest("claude can't launch through Ori: Ori is not installed"));
  assert.throws(() => planLaunch(copilotProfile, context({ via: 'ori' })), new BadRequest("copilot can't launch through Ori: Ori does not support copilot"));
  assert.throws(
    () => planLaunch(profile('pi', 'Pi', 'pi'), context({ via: 'ori', ori: { ...oriAll, agents: ['claude'] } })),
    new BadRequest("pi can't launch through Ori: Ori does not list pi as launchable"),
  );
});

function shimFixture(files: string[]) {
  const bin = fs.mkdtempSync(path.join(fs.realpathSync.native(os.tmpdir()), 'iat-shim-'));
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

test('allowResume defaults to true, reads a boolean, writes false only when unchecked and drops other values', () => {
  const { settings, home, write } = fixture();
  const config = path.join(home, CONFIG_FILE);
  assert.equal(settings.shared().allowResume, true);
  write(CONFIG_FILE, '{"allowResume": false}');
  assert.equal(settings.shared().allowResume, false);
  assert.deepEqual(settings.sharedFound(), { allowResume: false });
  write(CONFIG_FILE, '{"allowResume": "no"}');
  assert.deepEqual(settings.sharedFound(), {});
  assert.equal(settings.shared().allowResume, true);
  fs.writeFileSync(config, '{"defaultAgent": "codex"}');
  assert.ok(settings.setShared('allowResume', false));
  assert.deepEqual(JSON.parse(fs.readFileSync(config, 'utf8')), { defaultAgent: 'codex', allowResume: false });
  assert.ok(settings.setShared('allowResume', true));
  assert.deepEqual(JSON.parse(fs.readFileSync(config, 'utf8')), { defaultAgent: 'codex' });
  assert.equal(userSettingValue('allowResume', { globalValue: false, workspaceValue: true }), false);
  assert.equal(userSettingValue('allowResume', { workspaceValue: false }), true);
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

test('claudeMod defaults to on, reads off, and on removes the key', () => {
  const { settings, home, write } = fixture();
  const config = path.join(home, CONFIG_FILE);
  assert.equal(settings.shared().claudeMod, 'on');
  write(CONFIG_FILE, JSON.stringify({ claudeMod: 'off' }));
  assert.equal(settings.shared().claudeMod, 'off');
  write(CONFIG_FILE, '{"claudeMod": false}');
  assert.deepEqual(settings.sharedFound(), {});
  fs.writeFileSync(config, '{"defaultAgent": "codex"}');
  assert.ok(settings.setShared('claudeMod', 'off'));
  assert.deepEqual(JSON.parse(fs.readFileSync(config, 'utf8')), { defaultAgent: 'codex', claudeMod: 'off' });
  assert.ok(settings.setShared('claudeMod', 'on'));
  assert.deepEqual(JSON.parse(fs.readFileSync(config, 'utf8')), { defaultAgent: 'codex' });
  assert.equal(userSettingValue('claudeMod', { globalValue: 'off', workspaceValue: 'on' }), 'off');
  assert.equal(userSettingValue('claudeMod', { workspaceValue: 'off' }), 'on');
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

test('a Codex tab runs its server on the interpreter python.json records, or on py -3 or python3', () => {
  const home = fs.mkdtempSync(path.join(fs.realpathSync.native(os.tmpdir()), 'iat-python-'));
  const python = path.join(home, 'python.exe');
  fs.writeFileSync(python, '');
  assert.deepEqual(codexPython(home, process.platform === 'win32'), process.platform === 'win32' ? ['py', '-3'] : ['python3']);
  fs.mkdirSync(path.join(home, 'mcp'));
  fs.writeFileSync(path.join(home, 'mcp', 'python.json'), JSON.stringify({ python }));
  assert.deepEqual(codexPython(home, process.platform === 'win32'), [python]);
  fs.writeFileSync(path.join(home, 'mcp', 'python.json'), JSON.stringify({ python: `${python}'x` }));
  assert.deepEqual(codexPython(home, true), ['py', '-3']);
  assert.deepEqual(codexPython(home, false), ['python3']);

  const args = withCodexPython(CODEX_TAB_ARGS, ['py', '-3']);
  assert.equal(args.length, CODEX_TAB_ARGS.length);
  assert.match(args[2]!, /^mcp_servers\.ide-agent-tabs=\{ command = 'py', args = \['-3', '-I', '-S', '-c', '''/);
  assert.equal(args[2]!.slice(args[2]!.indexOf("'-I'")), CODEX_TAB_ARGS[2]!.slice(CODEX_TAB_ARGS[2]!.indexOf("'-I'")));
  assert.deepEqual(args.filter((_, i) => i !== 2), CODEX_TAB_ARGS.filter((_, i) => i !== 2));
  assert.deepEqual(withCodexPython(CODEX_TAB_ARGS, undefined), [...CODEX_TAB_ARGS]);
  const plan = planLaunch(BUILTIN_PROFILES[1]!, { setting: 'direct', ori: null, windows: false, searchPath: '', python: ['/usr/bin/python3'] });
  assert.match(plan.args[2]!, /command = '\/usr\/bin\/python3', args = \['-I'/);
});

const onWindows = process.platform === 'win32';

function httpHome(options: { port?: unknown; stub?: boolean; python?: string } = {}): string {
  const home = fs.mkdtempSync(path.join(fs.realpathSync.native(os.tmpdir()), 'iat-http-'));
  const mcp = path.join(home, 'mcp');
  fs.mkdirSync(path.join(mcp, 'py', 'launch'), { recursive: true });
  const python = options.python ?? path.join(home, 'python.exe');
  if (options.python === undefined) fs.writeFileSync(python, '');
  fs.writeFileSync(path.join(mcp, 'python.json'), JSON.stringify({ python }));
  if (options.port !== null) fs.writeFileSync(path.join(mcp, 'port.json'), JSON.stringify({ port: options.port ?? 47828 }));
  if (options.stub !== false) fs.writeFileSync(path.join(mcp, 'py', 'launch', 'headers.py'), '');
  return home;
}

test('a Codex tab uses the shared server when the copy, the port and the interpreter are recorded', () => {
  const home = httpHome({ port: 47831 });
  const http = codexHttp(home, onWindows, 'tab-1', 'codex')!;
  assert.ok(http);
  const headers = path.join(home, 'mcp', 'py', 'launch', 'headers.py');
  assert.deepEqual(http, {
    python: path.join(home, 'python.exe'),
    headers: onWindows ? headers.replace(/\\/g, '/') : headers,
    port: 47831,
    tab: 'tab-1',
    agent: 'codex',
    home,
  });
  const helper = `${http.python} -I -S ${http.headers} --tab tab-1 --agent codex --port 47831 --home ${home}`;
  assert.equal(
    codexHttpArg(http),
    `mcp_servers.ide-agent-tabs={ url = 'http://127.0.0.1:47831/mcp', http_headers_helper = '${helper}', startup_timeout_sec = 30, tool_timeout_sec = 660 }`,
  );
});

test('a Codex tab stays on the stdio server without a port, a headers stub or a safe path', () => {
  assert.equal(codexHttp(httpHome({ port: null }), onWindows, 'tab-1', 'codex'), undefined);
  assert.equal(codexHttp(httpHome({ port: 0 }), onWindows, 'tab-1', 'codex'), undefined);
  assert.equal(codexHttp(httpHome({ port: 65536 }), onWindows, 'tab-1', 'codex'), undefined);
  assert.equal(codexHttp(httpHome({ port: 1.5 }), onWindows, 'tab-1', 'codex'), undefined);
  assert.equal(codexHttp(httpHome({ port: '47828' }), onWindows, 'tab-1', 'codex'), undefined);
  assert.equal(codexHttp(httpHome({ stub: false }), onWindows, 'tab-1', 'codex'), undefined);
  assert.equal(codexHttp(httpHome(), onWindows, 'tab 1', 'codex'), undefined);
  assert.equal(codexHttp(httpHome(), onWindows, 'tab-1', 'co dex'), undefined);
  assert.equal(codexHttp(httpHome(), onWindows, 'a'.repeat(129), 'codex'), undefined);
  const spacedDir = fs.mkdtempSync(path.join(fs.realpathSync.native(os.tmpdir()), 'iat-sp ace-'));
  const spaced = path.join(spacedDir, 'python.exe');
  fs.writeFileSync(spaced, '');
  assert.equal(codexHttp(httpHome({ python: spaced }), onWindows, 'tab-1', 'codex'), undefined);
});

test('withCodexHttp changes only the server argument', () => {
  const http = codexHttp(httpHome(), onWindows, 'tab-1', 'codex-local')!;
  const swapped = withCodexHttp(CODEX_TAB_ARGS, http)!;
  assert.deepEqual(swapped.filter((a, i) => a !== CODEX_TAB_ARGS[i]), [codexHttpArg(http)]);
  assert.equal(swapped.length, CODEX_TAB_ARGS.length);
  assert.equal(withCodexHttp(CODEX_TAB_ARGS, undefined), undefined);
  assert.equal(withCodexHttp(['--other'], http), undefined);
  assert.doesNotMatch(codexHttpArg(http), /["%!\n]/);
});

test('planLaunch gives a Codex tab the HTTP server argument when http is set and the stdio one otherwise', () => {
  const codex = BUILTIN_PROFILES[1]!;
  const http = codexHttp(httpHome(), onWindows, 'tab-1', 'codex')!;
  const shared = planLaunch(codex, context({ http, python: ['/usr/bin/python3'] }));
  assert.equal(shared.args[2], codexHttpArg(http));
  assert.deepEqual(shared.args.filter((_, i) => i !== 2), CODEX_TAB_ARGS.filter((_, i) => i !== 2));
  const stdio = planLaunch(codex, context({ python: ['/usr/bin/python3'] }));
  assert.match(stdio.args[2]!, /command = '\/usr\/bin\/python3', args = \['-I'/);
  assert.equal(planLaunch(codex, context()).args[2], CODEX_TAB_ARGS[2]);
  assert.deepEqual(planLaunch(claudeProfile, context({ http })).args, []);
});

test('a Claude Code tab gets the tab settings file after its profile arguments', () => {
  const home = fs.mkdtempSync(path.join(fs.realpathSync.native(os.tmpdir()), 'iat-claude-'));
  const windows = process.platform === 'win32';
  const settings = path.join(home, 'mcp', CLAUDE_TAB_SETTINGS_FILE);
  fs.mkdirSync(path.join(home, 'mcp'));
  assert.equal(claudeTabSettings(home, windows), undefined);
  fs.writeFileSync(settings, '{}');
  assert.equal(claudeTabSettings(home, windows), undefined);
  const python = path.join(home, 'python.exe');
  fs.writeFileSync(python, '');
  fs.writeFileSync(path.join(home, 'mcp', 'python.json'), JSON.stringify({ python }));
  assert.equal(claudeTabSettings(home, windows), settings);
  fs.rmSync(settings);
  assert.equal(claudeTabSettings(home, windows), undefined);

  const claude = BUILTIN_PROFILES[0]!;
  const ctx: LaunchContext = { setting: 'direct', ori: null, windows: false, searchPath: '', prompt: 'hi', claudeSettings: '/h/s.json' };
  assert.deepEqual(planLaunch(claude, { ...ctx, model: 'opus', args: ['--resume', 'abc'] }).args, [
    '--settings',
    '/h/s.json',
    '--model',
    'opus',
    '--resume',
    'abc',
  ]);
  const bypass = { ...claude, args: ['--permission-mode', 'bypassPermissions'] };
  assert.deepEqual(planLaunch(bypass, ctx).args, ['--permission-mode', 'bypassPermissions', '--settings', '/h/s.json']);
  for (const command of ['/usr/local/bin/claude', 'C:\\Users\\a b\\.local\\bin\\claude.exe', 'CLAUDE.CMD']) {
    assert.deepEqual(planLaunch({ ...claude, command }, ctx).args, ['--settings', '/h/s.json'], command);
  }
  const ori = planLaunch(claude, { ...ctx, setting: 'ori', ori: { path: '/bin/ori', agents: ['claude'] } });
  assert.deepEqual([ori.via, ori.args], ['ori', ['claude', '--settings', '/h/s.json']]);
  for (const name of ['copilot', 'pi']) {
    assert.ok(!planLaunch(BUILTIN_PROFILES.find(p => p.name === name)!, ctx).args.includes('--settings'), name);
  }
  assert.deepEqual(planLaunch({ ...claude, command: 'claude-wrapper' }, ctx).args, []);
  assert.deepEqual(planLaunch({ ...claude, args: ['--settings=/missing/x.json'] }, ctx).args, ['--settings=/missing/x.json']);
  assert.deepEqual(planLaunch(claude, { ...ctx, args: ['--settings', '{not json'] }).args, ['--settings', '{not json']);
  assert.deepEqual(planLaunch(claude, { ...ctx, claudeSettings: undefined }).args, []);
});

test('a Claude Code tab that brings its own settings gets one file that merges them with the tab hooks', () => {
  const home = fs.mkdtempSync(path.join(fs.realpathSync.native(os.tmpdir()), 'iat-merge-'));
  const ours = { hooks: { Stop: [{ hooks: [{ type: 'command', command: '/py', args: ['hook', 'claude', 'Stop'] }] }] } };
  const settings = path.join(home, 'mcp', CLAUDE_TAB_SETTINGS_FILE);
  fs.mkdirSync(path.dirname(settings));
  fs.writeFileSync(settings, JSON.stringify(ours));
  const user = { permissions: { allow: ['Bash(git *)'] }, hooks: { Stop: [{ hooks: [{ type: 'command', command: 'mine' }] }] } };
  fs.writeFileSync(path.join(home, 'mine.json'), JSON.stringify(user));
  const merged = (args: string[]) => {
    const i = args.indexOf('--settings');
    return { rest: [...args.slice(0, i), ...args.slice(i + 2)], file: JSON.parse(fs.readFileSync(args[i + 1]!, 'utf8')) };
  };
  const claude = BUILTIN_PROFILES[0]!;
  const override = { ...claude, args: ['--permission-mode', 'bypassPermissions', '--settings', path.join(home, 'mine.json')] };
  const ctx: LaunchContext = { setting: 'direct', ori: null, windows: false, searchPath: '', claudeSettings: settings, model: 'opus' };
  const args = planLaunch(override, ctx).args;
  assert.equal(args.filter(a => a === '--settings').length, 1);
  const { rest, file } = merged(args);
  assert.deepEqual(rest, ['--permission-mode', 'bypassPermissions', '--model', 'opus']);
  assert.deepEqual(file, { permissions: user.permissions, hooks: { Stop: [...user.hooks.Stop, ...ours.hooks.Stop] } });
  assert.deepEqual(planLaunch(override, ctx).args, args);
  const relative = merged(planLaunch({ ...claude, args: ['--settings=mine.json'] }, { ...ctx, model: undefined, cwd: home }).args);
  assert.deepEqual([relative.rest, relative.file.permissions], [[], user.permissions]);
  assert.deepEqual(planLaunch({ ...claude, args: ['--settings=mine.json'] }, { ...ctx, model: undefined }).args, ['--settings=mine.json']);
  const inline = merged(planLaunch({ ...claude, args: ['--settings', '{"model":"profile"}'] }, { ...ctx, model: undefined, args: ['--settings', '{"model":"request"}'] }).args);
  assert.deepEqual([inline.rest, inline.file], [[], { model: 'request', hooks: ours.hooks }]);
  for (const value of ['{"hooks": []}', '{"hooks": {"Stop": {}}}', '[]']) {
    assert.deepEqual(planLaunch(claude, { ...ctx, model: undefined, args: ['--settings', value] }).args, ['--settings', value], value);
  }
});

function copilotHome(options: { port?: number | null; configPort?: number | null; prefix?: string; hooks?: boolean } = {}): string {
  const home = fs.mkdtempSync(path.join(fs.realpathSync.native(os.tmpdir()), options.prefix ?? 'iat-copilot-'));
  fs.mkdirSync(path.join(home, 'mcp'));
  if (options.port !== null) fs.writeFileSync(path.join(home, 'mcp', 'port.json'), JSON.stringify({ port: options.port ?? 47951 }));
  if (options.configPort !== null) {
    const entry = { type: 'http', url: `http://127.0.0.1:${options.configPort ?? 47951}/mcp`, headers: {}, tools: ['*'] };
    fs.writeFileSync(path.join(home, 'mcp', 'copilot-http.json'), JSON.stringify({ mcpServers: { 'ide-agent-tabs': entry } }));
  }
  if (options.hooks !== false) {
    const hooks = hooksOf(home);
    fs.mkdirSync(path.dirname(hooks), { recursive: true });
    fs.writeFileSync(hooks, '{}');
  }
  return home;
}

function hooksOf(home: string): string {
  return copilotHooksFile({}, path.join(home, 'user'));
}

function copilotTab(home: string, answers: (port: number) => Promise<boolean>) {
  return copilotHttp(home, onWindows, hooksOf(home), answers);
}

test('a Copilot tab uses the shared server when the port, the config and a running server agree', async () => {
  const home = copilotHome();
  const asked: number[] = [];
  const http = await copilotTab(home, async port => (asked.push(port), true));
  const file = path.join(home, 'mcp', 'copilot-http.json');
  assert.deepEqual(http, { config: onWindows ? file.replace(/\\/g, '/') : file, port: 47951 });
  assert.deepEqual(asked, [47951]);
});

test('a Copilot tab stays on the stdio server without a server, a port, a matching config or a safe path', async () => {
  const yes = async () => true;
  assert.equal(await copilotTab(copilotHome(), async () => false), undefined);
  assert.equal(await copilotTab(copilotHome({ port: null }), yes), undefined);
  assert.equal(await copilotTab(copilotHome({ configPort: null }), yes), undefined);
  assert.equal(await copilotTab(copilotHome({ configPort: 47952 }), yes), undefined);
  assert.equal(await copilotTab(copilotHome({ prefix: 'iat-copilot sp ace-' }), yes), undefined);
});

test('a Copilot tab stays on the stdio server without the registered hooks file', async () => {
  const asked: number[] = [];
  const answers = async (port: number) => (asked.push(port), true);
  const home = copilotHome({ hooks: false });
  assert.equal(await copilotTab(home, answers), undefined);
  assert.equal(await copilotHttp(home, onWindows, path.dirname(hooksOf(home)), answers), undefined);
  assert.deepEqual(asked, []);
  fs.mkdirSync(path.dirname(hooksOf(home)), { recursive: true });
  fs.writeFileSync(hooksOf(home), '{}');
  assert.notEqual(await copilotTab(home, answers), undefined);
});

test('COPILOT_HOME moves the hooks file and an empty value falls back to the user home', () => {
  assert.equal(copilotHooksFile({ COPILOT_HOME: path.join('c', 'h') }, 'u'), path.join('c', 'h', 'hooks', 'ide-agent-tabs.json'));
  assert.equal(copilotHooksFile({ COPILOT_HOME: '' }, 'u'), path.join('u', '.copilot', 'hooks', 'ide-agent-tabs.json'));
  assert.equal(copilotHooksFile({}, 'u'), path.join('u', '.copilot', 'hooks', 'ide-agent-tabs.json'));
});

test('the health check needs this service on the port', async () => {
  const server = createServer((req, res) => res.end(JSON.stringify({ service: req.url === '/health' ? 'ide-agent-tabs' : 'other' })));
  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve));
  const port = (server.address() as AddressInfo).port;
  assert.equal(await answersHealth(port), true);
  await new Promise<void>(resolve => server.close(() => resolve()));
  assert.equal(await answersHealth(port), false);
});

test('planLaunch gives a Copilot tab the config file and its folder, and leaves other agents alone', () => {
  const http = { config: 'C:/h/mcp/copilot-http.json', port: 47951 };
  const folder = 'C:\\work\\my repo\\100%';
  const launch = planLaunch(copilotProfile, context({ copilot: http, cwd: folder, prompt: 'hi', env: { X: '1' } }));
  assert.deepEqual(launch.args, ['--additional-mcp-config', '@C:/h/mcp/copilot-http.json', '-i']);
  assert.deepEqual(launch.env, { X: '1', [CWD_ENV]: 'C:\\work\\my repo\\100%25' });
  for (const arg of launch.args) assert.doesNotMatch(arg, /[\s"%]/);
  assert.deepEqual(planLaunch(copilotProfile, context({ cwd: folder })).args, []);
  assert.deepEqual(planLaunch(copilotProfile, context({ cwd: folder })).env, {});
  assert.deepEqual(planLaunch(codexProfile, context({ copilot: http, cwd: '/w' })).args, ['--no-daemon']);
  assert.deepEqual(planLaunch(BUILTIN_PROFILES[1]!, context({ copilot: http, cwd: '/w' })).args, [...CODEX_TAB_ARGS]);
  for (const command of ['copilot', 'COPILOT.CMD', 'C:\\npm\\copilot.ps1', '/usr/local/bin/copilot']) assert.ok(isCopilotCommand(command), command);
  for (const command of ['codex', 'copilot-x', 'notcopilot']) assert.ok(!isCopilotCommand(command), command);
  assert.deepEqual(withCopilotHttp('copilot', [], {}, http, undefined), { args: ['--additional-mcp-config', '@C:/h/mcp/copilot-http.json'], env: {} });
});

test('the folder header percent-encodes as the Python server copy does', () => {
  assert.equal(cwdHeader('C:\\work\\my repo\\100%'), 'C:\\work\\my repo\\100%25');
  assert.equal(cwdHeader('/tmp/Zoë/${HOME}/a&b~c'), '/tmp/Zo%C3%AB/%24%7BHOME%7D/a%26b~c');
  assert.equal(cwdHeader('/tmp/日本'), '/tmp/%E6%97%A5%E6%9C%AC');
});

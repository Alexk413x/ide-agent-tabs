import assert from 'node:assert/strict';
import { chmodSync, cpSync, existsSync, mkdirSync, readdirSync, readFileSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import {
  agentsReport,
  CODEX_WINDOWS_REFUSAL,
  configFile,
  copilotEntry,
  entryServerPath,
  opencodeEntry,
  parseCodexGet,
  registerAgents,
  registerArgs,
  samePath,
  stripJsonComments,
  takesHooks,
  unregisterAgents,
  unregisterArgs,
  withServerEntry,
} from '../src/register.js';
import { agyHookCommand, hasOurHooks, hookCommand, mergeHookSettings, withCodexSettings } from '../src/hookConfig.js';
import { copyVersion, hookCopyPath, refreshServerCopy, serverCopyDir, serverCopyPath, serverHash } from '../src/serverCopy.js';
import { makeServerDir } from './serverDir.js';
import { tempDir } from './tempDir.js';

const SERVER = 'C:/Users/a/.ide-agent-tabs/mcp/mcp-server.mjs';

test('builds the codex and gemini commands', () => {
  assert.deepEqual(registerArgs('codex', SERVER), ['mcp', 'add', 'ide-agent-tabs', '--', 'node', SERVER]);
  assert.deepEqual(registerArgs('gemini', SERVER), ['mcp', 'add', '--scope', 'user', 'ide-agent-tabs', 'node', SERVER]);
  assert.deepEqual(unregisterArgs('codex'), ['mcp', 'remove', 'ide-agent-tabs']);
  assert.deepEqual(unregisterArgs('gemini'), ['mcp', 'remove', '--scope', 'user', 'ide-agent-tabs']);
});

test('finds each agent config file, honoring the relocation variables', () => {
  const home = path.join('h');
  const none = () => false;
  assert.equal(configFile('codex', {}, home, none), path.join('h', '.codex', 'config.toml'));
  assert.equal(configFile('codex', { CODEX_HOME: 'c' }, home, none), path.join('c', 'config.toml'));
  assert.equal(configFile('gemini', {}, home, none), path.join('h', '.gemini', 'settings.json'));
  assert.equal(configFile('gemini', { GEMINI_CLI_HOME: 'g' }, home, none), path.join('g', '.gemini', 'settings.json'));
  assert.equal(configFile('copilot', {}, home, none), path.join('h', '.copilot', 'mcp-config.json'));
  assert.equal(configFile('copilot', { COPILOT_HOME: 'p' }, home, none), path.join('p', 'mcp-config.json'));
  assert.equal(configFile('agy', { GEMINI_CLI_HOME: 'g' }, home, none), path.join('h', '.gemini', 'config', 'mcp_config.json'));
  assert.equal(configFile('opencode', {}, home, none), path.join('h', '.config', 'opencode', 'opencode.json'));
  assert.equal(configFile('opencode', { XDG_CONFIG_HOME: 'x' }, home, none), path.join('x', 'opencode', 'opencode.json'));
  const jsonc = path.join('x', 'opencode', 'opencode.jsonc');
  assert.equal(configFile('opencode', { XDG_CONFIG_HOME: 'x' }, home, (f) => f === jsonc), jsonc);
});

test('adds, keeps and removes the server entry in a JSON config', () => {
  assert.equal(
    withServerEntry(undefined, 'f', 'mcp', opencodeEntry(SERVER), { $schema: 's' }),
    `{\n  "$schema": "s",\n  "mcp": {\n    "ide-agent-tabs": {\n      "type": "local",\n      "command": [\n        "node",\n        "${SERVER}"\n      ],\n      "enabled": true\n    }\n  }\n}\n`,
  );
  const existing = '{\r\n    "mcpServers": {\r\n        "other": { "command": "x" }\r\n    },\r\n    "theme": "dark"\r\n}\r\n';
  const added = withServerEntry(existing, 'f', 'mcpServers', copilotEntry(SERVER))!;
  assert.deepEqual(Object.keys(JSON.parse(added)), ['mcpServers', 'theme']);
  assert.deepEqual(Object.keys(JSON.parse(added).mcpServers), ['other', 'ide-agent-tabs']);
  assert.match(added, /^\{\r\n {4}"mcpServers": \{\r\n {8}"other"/);
  assert.ok(!/[^\r]\n/.test(added));
  assert.equal(withServerEntry(added, 'f', 'mcpServers', copilotEntry(SERVER)), undefined);
  const removed = withServerEntry(added, 'f', 'mcpServers', undefined)!;
  assert.deepEqual(JSON.parse(removed), { mcpServers: { other: { command: 'x' } }, theme: 'dark' });
  assert.equal(withServerEntry(removed, 'f', 'mcpServers', undefined), undefined);
  assert.equal(withServerEntry(undefined, 'f', 'mcpServers', undefined), undefined);
  assert.throws(() => withServerEntry('{ // comment\n}', 'f', 'mcp', {}), /isn't plain JSON/);
  assert.throws(() => withServerEntry('[]', 'f', 'mcp', {}), /JSON object/);
  assert.throws(() => withServerEntry('{"mcp": []}', 'f', 'mcp', {}), /isn't an object/);
});

test('strips JSON comments outside strings', () => {
  const text = '{\n  // a comment\n  "url": "http://x/*y*/", /* block */ "n": "a\\"//b"\n}';
  assert.deepEqual(JSON.parse(stripJsonComments(text)), { url: 'http://x/*y*/', n: 'a"//b' });
});

test('reads the server path from each config shape', () => {
  assert.equal(entryServerPath(copilotEntry(SERVER)), SERVER);
  assert.equal(entryServerPath(opencodeEntry(SERVER)), SERVER);
  assert.equal(entryServerPath({ command: 'node', args: ['--flag', 'D:\\x\\server.js'] }), 'D:\\x\\server.js');
  assert.equal(entryServerPath({ command: 'uvx', args: ['thing'] }), 'thing');
  assert.equal(entryServerPath('nope'), undefined);
  const stdout = 'WARNING: something\n{"name":"ide-agent-tabs","transport":{"type":"stdio","command":"node","args":["C:/x/mcp-server.mjs"]}}\n';
  assert.equal(entryServerPath(parseCodexGet(stdout)), 'C:/x/mcp-server.mjs');
  assert.throws(() => parseCodexGet('nothing'));
});

test('compares paths by platform rules', () => {
  assert.ok(samePath('C:\\Users\\A\\.ide-agent-tabs\\mcp\\mcp-server.mjs', 'c:/users/a/.ide-agent-tabs/mcp/mcp-server.mjs', 'win32'));
  assert.ok(!samePath('/home/A/x.mjs', '/home/a/x.mjs', 'linux'));
});

test('copies the server with its launch scripts in the layout main.ts expects', async () => {
  const source = makeServerDir('server v1');
  const home = tempDir('iat-copy-');
  assert.deepEqual((await refreshServerCopy(source, home)).sort(), ['THIRD_PARTY_NOTICES.txt', 'agent-hook.mjs', 'launch/agent-launch.ps1', 'launch/agent-launch.sh', 'mcp-server.mjs']);
  assert.deepEqual(readdirSync(serverCopyDir(home)).sort(), ['THIRD_PARTY_NOTICES.txt', 'agent-hook.mjs', 'launch', 'mcp-server.mjs']);
  assert.equal(await serverHash(serverCopyDir(home)), await serverHash(source));
  assert.deepEqual(await refreshServerCopy(source, home), []);
  writeFileSync(path.join(serverCopyDir(home), 'launch', 'removed.fish'), 'x');
  writeFileSync(path.join(source, 'mcp-server.mjs'), 'server v2');
  assert.deepEqual((await refreshServerCopy(source, home)).sort(), ['launch/removed.fish', 'mcp-server.mjs']);
  assert.equal(readFileSync(path.join(serverCopyDir(home), 'mcp-server.mjs'), 'utf8'), 'server v2');
  assert.ok(existsSync(path.join(serverCopyDir(home), 'launch', 'agent-launch.ps1')));
  assert.ok(!readdirSync(serverCopyDir(home)).some((n) => n.endsWith('.tmp')));
});

const FAKE_AGENT = `import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
const NAME = 'ide-agent-tabs';
const [agent, ...args] = process.argv.slice(2);
fs.appendFileSync(path.join(path.dirname(fileURLToPath(import.meta.url)), 'calls.txt'), JSON.stringify({ agent, args, cwd: process.cwd() }) + '\\n');
const readJson = (f) => (fs.existsSync(f) ? JSON.parse(fs.readFileSync(f, 'utf8')) : {});
const writeJson = (f, v) => { fs.mkdirSync(path.dirname(f), { recursive: true }); fs.writeFileSync(f, JSON.stringify(v, null, 2)); };
const writeToml = (servers) => {
  const file = path.join(process.env.CODEX_HOME, 'config.toml');
  const head = fs.existsSync(file) ? fs.readFileSync(file, 'utf8').split('[mcp_servers.')[0] : '';
  const tables = Object.entries(servers).map(([n, s]) => '[mcp_servers.' + n + ']\\ncommand = ' + JSON.stringify(s.command) + '\\nargs = ' + JSON.stringify(s.args) + '\\n');
  fs.writeFileSync(file, head + tables.join('\\n'));
};
if (agent === 'codex') {
  const store = path.join(process.env.CODEX_HOME, 'fake-mcp.json');
  const servers = readJson(store);
  process.stderr.write('WARNING: proceeding, even though we could not create PATH aliases\\n');
  if (args[1] === 'get') {
    if (!servers[NAME]) { process.stderr.write("Error: No MCP server named 'ide-agent-tabs' found.\\n"); process.exit(1); }
    process.stdout.write(JSON.stringify({ name: NAME, enabled: true, transport: { type: 'stdio', ...servers[NAME] } }, null, 2) + '\\n');
  } else if (args[1] === 'add') {
    const [command, ...rest] = args.slice(args.indexOf('--') + 1);
    servers[args[2]] = { command, args: rest };
    writeJson(store, servers);
    writeToml(servers);
  } else if (args[1] === 'remove') {
    delete servers[args[2]];
    writeJson(store, servers);
    writeToml(servers);
  }
} else if (agent === 'gemini') {
  const file = path.join(process.env.GEMINI_CLI_HOME, '.gemini', 'settings.json');
  const settings = readJson(file);
  settings.mcpServers ??= {};
  if (args[1] === 'add') {
    const [name, command, ...rest] = args.slice(4);
    settings.mcpServers[name] = { command, args: rest };
  } else if (args[1] === 'remove') {
    delete settings.mcpServers[args[4]];
  }
  writeJson(file, settings);
}
`;

function fakeAgents(bin: string): void {
  writeFileSync(path.join(bin, 'fake-agent.mjs'), FAKE_AGENT);
  for (const agent of ['codex', 'gemini']) {
    if (process.platform === 'win32') {
      writeFileSync(path.join(bin, `${agent}.cmd`), `@"${process.execPath}" "%~dp0fake-agent.mjs" ${agent} %*\r\n@exit /b %ERRORLEVEL%\r\n`);
    } else {
      const file = path.join(bin, agent);
      writeFileSync(file, `#!/bin/sh\nexec "${process.execPath}" "\${0%/*}/fake-agent.mjs" ${agent} "$@"\n`);
      chmodSync(file, 0o755);
    }
  }
  for (const agent of ['copilot', 'agy', 'opencode']) writeFileSync(path.join(bin, process.platform === 'win32' ? `${agent}.cmd` : agent), '');
}

test('registers and unregisters every agent in a temp home with fake CLIs', async () => {
  const root = tempDir('iat-agents-');
  const bin = path.join(root, 'bin');
  const userHome = path.join(root, 'user');
  const home = path.join(userHome, '.ide-agent-tabs');
  mkdirSync(bin);
  fakeAgents(bin);
  const env = {
    PATH: bin,
    ComSpec: process.env.ComSpec,
    SystemRoot: process.env.SystemRoot,
    CODEX_HOME: path.join(userHome, '.codex'),
    GEMINI_CLI_HOME: userHome,
    COPILOT_HOME: path.join(userHome, '.copilot'),
    XDG_CONFIG_HOME: path.join(userHome, '.config'),
  };
  const copilotFile = path.join(env.COPILOT_HOME, 'mcp-config.json');
  const opencodeFile = path.join(env.XDG_CONFIG_HOME, 'opencode', 'opencode.json');
  mkdirSync(env.COPILOT_HOME, { recursive: true });
  writeFileSync(copilotFile, '{\n  "mcpServers": {\n    "github": {\n      "type": "http",\n      "url": "https://example.test/mcp"\n    }\n  }\n}\n');
  const ctx = { serverDir: makeServerDir(), home, platform: process.platform, env, userHome };
  const server = serverCopyPath(home, process.platform);
  const hook = hookCopyPath(home, process.platform);
  const codexRegisters = process.platform !== 'win32';
  const codexHooks = path.join(env.CODEX_HOME, 'hooks.json');
  const userHook = { matcher: 'Bash', hooks: [{ type: 'command', command: 'python check.py' }] };
  mkdirSync(env.CODEX_HOME, { recursive: true });
  writeFileSync(codexHooks, JSON.stringify({ hooks: { PreToolUse: [userHook] } }, null, 2));
  writeFileSync(path.join(env.CODEX_HOME, 'config.toml'), 'model = "x"\n\n');
  const agyDir = path.join(userHome, '.gemini', 'config');
  const agySettings = path.join(userHome, '.gemini', 'antigravity-cli', 'settings.json');
  const agyUserHooks = { lint: { PostToolUse: [{ matcher: 'run_command', hooks: [{ type: 'command', command: 'lint.cmd', timeout: 10 }] }] } };
  const agyUserSettings = { permissions: { allow: ['command(adb devices)'] }, statusLine: { type: '', command: '', enabled: true } };
  mkdirSync(agyDir, { recursive: true });
  mkdirSync(path.dirname(agySettings), { recursive: true });
  writeFileSync(path.join(agyDir, 'mcp_config.json'), '');
  writeFileSync(path.join(agyDir, 'hooks.json'), JSON.stringify(agyUserHooks, null, 2));
  writeFileSync(agySettings, JSON.stringify(agyUserSettings, null, 2));

  const before = await agentsReport(ctx);
  assert.deepEqual(before.server, { path: server, exists: false, current: false });
  assert.deepEqual(
    before.agents.map((a) => [a.agent, a.installed, a.registered, a.hooks, a.error]),
    [
      ['codex', true, false, null, undefined],
      ['gemini', true, false, false, undefined],
      ['copilot', true, false, false, undefined],
      ['agy', true, false, false, undefined],
      ['opencode', true, false, null, undefined],
    ],
  );

  const report = await registerAgents(ctx, ['codex', 'gemini', 'copilot', 'agy', 'opencode', 'claude']);
  const codexErrors = codexRegisters ? [] : [`codex: ${CODEX_WINDOWS_REFUSAL}`];
  assert.deepEqual(report.errors, ['claude: Claude Code gets the server from the plugin; nothing to register', ...codexErrors]);
  assert.deepEqual(report.server, { path: server, exists: true, current: true });
  const registered = report.agents.filter((a) => codexRegisters || a.agent !== 'codex');
  assert.ok(registered.every((a) => a.ok && a.registered && a.stable && a.path === server));
  assert.deepEqual(report.agents.map((a) => a.hooks), [null, true, true, true, null]);
  assert.ok(existsSync(hook));

  assert.deepEqual(JSON.parse(readFileSync(codexHooks, 'utf8')), { hooks: { PreToolUse: [userHook] } }, 'registering leaves the Codex hooks alone');
  const toml = readFileSync(path.join(env.CODEX_HOME, 'config.toml'), 'utf8');
  if (codexRegisters) {
    assert.match(toml, /^model = "x"\n/);
    assert.match(toml, /\[mcp_servers\.ide-agent-tabs\]\nenv_vars = \["IDE_AGENT_TABS_ID", "IDE_AGENT_TABS_AGENT", "IDE_AGENT_TABS_HOME"\]\ntool_timeout_sec = 660\ncommand = "node"/);
  } else {
    assert.equal(toml, 'model = "x"\n\n');
  }

  const gemini = JSON.parse(readFileSync(path.join(userHome, '.gemini', 'settings.json'), 'utf8'));
  assert.deepEqual(Object.keys(gemini.hooks), ['BeforeAgent', 'BeforeTool', 'Notification', 'AfterTool', 'AfterAgent']);
  assert.deepEqual(gemini.hooks.AfterAgent, [{ hooks: [{ type: 'command', name: 'ide-agent-tabs', command: `node "${hook}" gemini AfterAgent`, timeout: 5000 }] }]);
  const copilotHooksFile = path.join(env.COPILOT_HOME, 'hooks', 'ide-agent-tabs.json');
  const copilotHooks = JSON.parse(readFileSync(copilotHooksFile, 'utf8'));
  assert.equal(copilotHooks.version, 1);
  assert.deepEqual(copilotHooks.hooks.agentStop, [{ type: 'command', exec: 'node', args: [hook, 'copilot', 'agentStop'], timeoutSec: 5 }]);
  assert.deepEqual(Object.keys(copilotHooks.hooks), ['sessionStart', 'userPromptSubmitted', 'preToolUse', 'notification', 'postToolUse', 'agentStop']);
  const agyHooks = JSON.parse(readFileSync(path.join(agyDir, 'hooks.json'), 'utf8'));
  assert.deepEqual(agyHooks.lint, agyUserHooks.lint, "registering keeps the user's own agy hooks");
  assert.deepEqual(agyHooks['ide-agent-tabs'], {
    PreInvocation: [{ type: 'command', command: `node ${hook} agy PreInvocation`, timeout: 5 }],
    PostToolUse: [{ matcher: '*', hooks: [{ type: 'command', command: `node ${hook} agy PostToolUse`, timeout: 5 }] }],
    Stop: [{ type: 'command', command: `node ${hook} agy Stop`, timeout: 5 }],
  });
  assert.deepEqual(JSON.parse(readFileSync(path.join(agyDir, 'mcp_config.json'), 'utf8')), { mcpServers: { 'ide-agent-tabs': { command: 'node', args: [server] } } });
  assert.deepEqual(JSON.parse(readFileSync(agySettings, 'utf8')), {
    ...agyUserSettings,
    permissions: { allow: ['command(adb devices)', 'mcp(ide-agent-tabs/*)'] },
  });


  const calls = readFileSync(path.join(bin, 'calls.txt'), 'utf8').trim().split('\n').map((l) => JSON.parse(l) as { agent: string; args: string[]; cwd: string });
  const writes = calls.filter((c) => c.args[1] !== 'get').map((c) => [c.agent, ...c.args]);
  assert.deepEqual(writes, [
    ...(codexRegisters ? [['codex', 'mcp', 'add', 'ide-agent-tabs', '--', 'node', server]] : []),
    ['gemini', 'mcp', 'add', '--scope', 'user', 'ide-agent-tabs', 'node', server],
  ]);
  assert.ok(calls.every((c) => samePath(c.cwd, home, process.platform)));
  assert.deepEqual(JSON.parse(readFileSync(path.join(userHome, '.gemini', 'settings.json'), 'utf8')).mcpServers['ide-agent-tabs'], { command: 'node', args: [server] });

  assert.equal(
    readFileSync(copilotFile, 'utf8'),
    `{\n  "mcpServers": {\n    "github": {\n      "type": "http",\n      "url": "https://example.test/mcp"\n    },\n    "ide-agent-tabs": {\n      "type": "local",\n      "command": "node",\n      "args": [\n        "${server}"\n      ],\n      "env": {\n        "IDE_AGENT_TABS_ID": "\${IDE_AGENT_TABS_ID}",\n        "IDE_AGENT_TABS_AGENT": "\${IDE_AGENT_TABS_AGENT}"\n      },\n      "tools": [\n        "*"\n      ]\n    }\n  }\n}\n`,
  );
  assert.deepEqual(JSON.parse(readFileSync(opencodeFile, 'utf8')), {
    $schema: 'https://opencode.ai/config.json',
    mcp: { 'ide-agent-tabs': { type: 'local', command: ['node', server], enabled: true } },
  });

  const again = await registerAgents(ctx, ['codex', 'gemini', 'copilot', 'agy']);
  assert.deepEqual(again.errors, codexErrors);
  assert.equal(readFileSync(path.join(env.CODEX_HOME, 'config.toml'), 'utf8').match(/env_vars/g)?.length ?? 0, codexRegisters ? 1 : 0);
  assert.equal(JSON.parse(readFileSync(agySettings, 'utf8')).permissions.allow.length, 2, 'registering again adds the rule once');

  const removed = await unregisterAgents(ctx, ['codex', 'gemini', 'copilot', 'agy', 'opencode']);
  assert.deepEqual(removed.errors, []);
  assert.ok(removed.agents.every((a) => a.ok && !a.registered && !a.hooks));
  assert.deepEqual(JSON.parse(readFileSync(codexHooks, 'utf8')), { hooks: { PreToolUse: [userHook] } });
  assert.doesNotMatch(readFileSync(path.join(env.CODEX_HOME, 'config.toml'), 'utf8'), /ide-agent-tabs|env_vars/);
  assert.equal(JSON.parse(readFileSync(path.join(userHome, '.gemini', 'settings.json'), 'utf8')).hooks, undefined);
  assert.ok(!existsSync(copilotHooksFile));
  assert.deepEqual(JSON.parse(readFileSync(copilotFile, 'utf8')), { mcpServers: { github: { type: 'http', url: 'https://example.test/mcp' } } });
  assert.deepEqual(JSON.parse(readFileSync(opencodeFile, 'utf8')).mcp, {});
  assert.deepEqual(JSON.parse(readFileSync(path.join(agyDir, 'hooks.json'), 'utf8')), agyUserHooks);
  assert.deepEqual(JSON.parse(readFileSync(path.join(agyDir, 'mcp_config.json'), 'utf8')), { mcpServers: {} });
  assert.deepEqual(JSON.parse(readFileSync(agySettings, 'utf8')), agyUserSettings, 'unregistering removes only the Agent Tabs rule');
  assert.ok(existsSync(server.replace(/\//g, path.sep)));
});

test('refuses configs it cannot edit safely and agents that are missing', async () => {
  const root = tempDir('iat-agents-bad-');
  const bin = path.join(root, 'bin');
  const userHome = path.join(root, 'user');
  mkdirSync(bin);
  for (const agent of ['copilot', 'opencode']) writeFileSync(path.join(bin, process.platform === 'win32' ? `${agent}.cmd` : agent), '');
  const env = { PATH: bin, COPILOT_HOME: path.join(userHome, '.copilot'), XDG_CONFIG_HOME: path.join(userHome, '.config') };
  const copilotFile = path.join(env.COPILOT_HOME, 'mcp-config.json');
  const jsoncFile = path.join(env.XDG_CONFIG_HOME, 'opencode', 'opencode.jsonc');
  mkdirSync(path.dirname(copilotFile), { recursive: true });
  mkdirSync(path.dirname(jsoncFile), { recursive: true });
  writeFileSync(copilotFile, '{ "mcpServers": ');
  const jsonc = '{\n  // my settings\n  "model": "x"\n}\n';
  writeFileSync(jsoncFile, jsonc);
  const ctx = { serverDir: makeServerDir(), home: path.join(userHome, '.ide-agent-tabs'), platform: process.platform, env, userHome };

  const report = await registerAgents(ctx, ['copilot', 'opencode', 'codex', 'nope']);
  const byAgent = Object.fromEntries(report.agents.map((a) => [a.agent, a]));
  assert.match(byAgent.copilot!.error!, /isn't plain JSON/);
  assert.match(byAgent.opencode!.error!, /opencode\.jsonc isn't plain JSON/);
  assert.equal(byAgent.codex!.error, 'not installed');
  assert.ok(report.errors.includes('nope: unknown agent; use codex, gemini, copilot, agy, opencode'));
  assert.equal(readFileSync(copilotFile, 'utf8'), '{ "mcpServers": ');
  assert.equal(readFileSync(jsoncFile, 'utf8'), jsonc);
  assert.ok(!existsSync(path.join(path.dirname(jsoncFile), 'opencode.json')));
});

const HOOK = 'C:/Users/a/.ide-agent-tabs/mcp/agent-hook.mjs';

test('merges and removes only the Agent Tabs hook entries', () => {
  const theirs = { matcher: 'Bash', hooks: [{ type: 'command', command: 'python check.py' }] };
  const mixed = { hooks: [{ type: 'command', command: 'echo hi' }, { type: 'command', command: `node "/old/place/agent-hook.mjs" codex Stop` }] };
  const root = { model: 'x', hooks: { PreToolUse: [theirs], Stop: [mixed], Custom: 'kept' } };
  const merged = mergeHookSettings(root, 'f', 'codex', HOOK);
  assert.ok(hasOurHooks(merged, 'codex', HOOK));
  assert.ok(!hasOurHooks(merged, 'codex', '/elsewhere/agent-hook.mjs'));
  const hooks = merged.hooks as Record<string, unknown[]>;
  assert.deepEqual(hooks.PreToolUse![0], theirs);
  assert.deepEqual(hooks.Stop, [
    { hooks: [{ type: 'command', command: 'echo hi' }] },
    { hooks: [{ type: 'command', command: `node "${HOOK}" codex Stop`, timeout: 5 }] },
  ]);
  assert.equal(hooks.Custom, 'kept');
  assert.deepEqual(mergeHookSettings(merged, 'f', 'codex', HOOK), merged);
  assert.deepEqual(mergeHookSettings(merged, 'f', 'codex', undefined), {
    model: 'x',
    hooks: { PreToolUse: [theirs], Stop: [{ hooks: [{ type: 'command', command: 'echo hi' }] }], Custom: 'kept' },
  });
  assert.deepEqual(mergeHookSettings({ a: 1 }, 'f', 'gemini', undefined), { a: 1 });
  assert.throws(() => mergeHookSettings({ hooks: [] }, 'f', 'gemini', HOOK), /isn't an object/);
  assert.throws(() => hookCommand('C:/Users/100%/x/agent-hook.mjs', 'codex', 'Stop'), /shell command/);
  assert.throws(() => hookCommand('/home/a"b/agent-hook.mjs', 'gemini', 'Stop'), /shell command/);
});

test('adds env_vars to the Codex server table without touching the rest of config.toml', () => {
  const toml = 'model = "o"\r\n\r\n[mcp_servers."ide-agent-tabs"]\r\ncommand = "node"\r\n\r\n[mcp_servers.ide-agent-tabs.env]\r\nA = "1"\r\n';
  const next = withCodexSettings(toml, 'f')!;
  assert.equal(
    next,
    'model = "o"\r\n\r\n[mcp_servers."ide-agent-tabs"]\r\nenv_vars = ["IDE_AGENT_TABS_ID", "IDE_AGENT_TABS_AGENT", "IDE_AGENT_TABS_HOME"]\r\ntool_timeout_sec = 660\r\ncommand = "node"\r\n\r\n[mcp_servers.ide-agent-tabs.env]\r\nA = "1"\r\n',
  );
  assert.equal(
    withCodexSettings('[mcp_servers.ide-agent-tabs]\ntool_timeout_sec = 30\n', 'f'),
    '[mcp_servers.ide-agent-tabs]\nenv_vars = ["IDE_AGENT_TABS_ID", "IDE_AGENT_TABS_AGENT", "IDE_AGENT_TABS_HOME"]\ntool_timeout_sec = 30\n',
    "a timeout the user set stays",
  );
  assert.equal(withCodexSettings(next, 'f'), undefined);
  assert.throws(() => withCodexSettings('[mcp_servers.ide-agent-tabs]\nenv_vars = ["OTHER"]\n', 'f'), /by hand/);
  assert.throws(() => withCodexSettings('[mcp_servers.other]\n', 'f'), /no \[mcp_servers\.ide-agent-tabs\]/);
});

test('Gemini CLI, Copilot CLI and Antigravity CLI get global hooks; Codex tabs bring their own', () => {
  assert.equal(takesHooks('codex'), false);
  assert.equal(takesHooks('gemini'), true);
  assert.equal(takesHooks('copilot'), true);
  assert.equal(takesHooks('agy'), true);
  assert.equal(takesHooks('opencode'), false);
});

test('an older plugin never replaces a newer server copy', async () => {
  const plugin = (version: string, server: string) => {
    const root = tempDir('iat-plugin-');
    const dist = path.join(root, 'dist');
    cpSync(makeServerDir(server), dist, { recursive: true });
    mkdirSync(path.join(root, '.claude-plugin'));
    writeFileSync(path.join(root, '.claude-plugin', 'plugin.json'), JSON.stringify({ version }));
    return dist;
  };
  const home = tempDir('iat-copy-');
  const server = path.join(serverCopyDir(home), 'mcp-server.mjs');
  await refreshServerCopy(plugin('0.5.0', 'new server'), home);
  assert.equal(await copyVersion(home), '0.5.0');
  assert.deepEqual(await refreshServerCopy(plugin('0.4.0', 'old server'), home), []);
  assert.equal(readFileSync(server, 'utf8'), 'new server');
  await refreshServerCopy(plugin('0.6.0', 'newer server'), home);
  assert.equal(readFileSync(server, 'utf8'), 'newer server');
  assert.equal(await copyVersion(home), '0.6.0');
});

test('an Antigravity CLI hook command holds the path bare and refuses one cmd.exe would split', () => {
  assert.equal(agyHookCommand('C:\Users\a\.ide-agent-tabs\mcp\agent-hook.mjs', 'Stop'), 'node C:\Users\a\.ide-agent-tabs\mcp\agent-hook.mjs agy Stop');
  for (const bad of ['C:\Users\John Smith\h.mjs', 'C:\a&b\h.mjs', 'C:\%X%\h.mjs', 'C:\Program Files (x86)\h.mjs']) {
    assert.throws(() => agyHookCommand(bad, 'Stop'), /Antigravity CLI/);
  }
});

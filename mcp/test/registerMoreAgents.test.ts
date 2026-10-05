import assert from 'node:assert/strict';
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import { MAX_WAIT_S } from '../src/messaging/messaging.js';
import { agentsReport, configFile, gooseEntry, hermesEntry, piEntry, qwenEntry, registerAgents, takesHooks, unregisterAgents } from '../src/register.js';
import { hookConfigFile, posixHookCommand, posixQuote } from '../src/hookConfig.js';
import { hookCopyPath, serverCopyPath } from '../src/serverCopy.js';
import { makeServerDir } from './serverDir.js';
import { tempDir } from './tempDir.js';

const NEW_AGENTS = ['grok', 'pi', 'hermes', 'qwen', 'goose'];

function write(file: string, text: string): void {
  mkdirSync(path.dirname(file), { recursive: true });
  writeFileSync(file, text);
}

const json = (file: string) => JSON.parse(readFileSync(file, 'utf8'));

test('finds the config of each new agent, honoring the relocation variables', () => {
  const h = 'h';
  const none = () => false;
  assert.equal(configFile('grok', {}, h, none), path.join(h, '.grok', 'config.toml'));
  assert.equal(configFile('grok', { GROK_HOME: 'g' }, h, none), path.join('g', 'config.toml'));
  assert.equal(hookConfigFile('grok', { GROK_HOME: 'g' }, h), path.join('g', 'hooks', 'ide-agent-tabs.json'));
  assert.equal(configFile('pi', {}, h, none), path.join(h, '.pi', 'agent', 'mcp.json'));
  assert.equal(configFile('pi', { PI_CODING_AGENT_DIR: 'p' }, h, none), path.join('p', 'mcp.json'));
  assert.equal(configFile('hermes', {}, h, none, 'linux'), path.join(h, '.hermes', 'config.yaml'));
  assert.equal(configFile('hermes', { LOCALAPPDATA: 'L' }, h, none, 'win32'), path.join('L', 'hermes', 'config.yaml'));
  assert.equal(configFile('hermes', { HERMES_HOME: 'm', LOCALAPPDATA: 'L' }, h, none, 'win32'), path.join('m', 'config.yaml'));
  assert.equal(configFile('qwen', {}, h, none), path.join(h, '.qwen', 'settings.json'));
  assert.equal(configFile('qwen', { QWEN_HOME: path.resolve('q') }, h, none), path.join(path.resolve('q'), 'settings.json'));
  assert.equal(configFile('goose', {}, h, none, 'darwin'), path.join(h, '.config', 'goose', 'config.yaml'));
  assert.equal(configFile('goose', { APPDATA: 'R' }, h, none, 'win32'), path.join('R', 'Block', 'goose', 'config', 'config.yaml'));
  const root = path.resolve('groot');
  assert.equal(configFile('goose', { GOOSE_PATH_ROOT: root }, h, none, 'linux'), path.join(root, 'config', 'config.yaml'));
  assert.equal(hookConfigFile('goose', { GOOSE_PATH_ROOT: root }, h), path.join(root, '.agents', 'plugins', 'ide-agent-tabs', 'hooks', 'hooks.json'));
  assert.equal(hookConfigFile('goose', { GOOSE_PATH_ROOT: 'relative' }, h), path.join(h, '.agents', 'plugins', 'ide-agent-tabs', 'hooks', 'hooks.json'));
});

test('Grok, Hermes, Qwen Code and Goose get hooks; Pi gets none', () => {
  assert.deepEqual(NEW_AGENTS.map((a) => takesHooks(a as never)), [true, false, true, true, true]);
});

test('every new MCP entry outlasts the longest wait_for_message', () => {
  const longest = MAX_WAIT_S + 60;
  assert.ok(piEntry('s').timeout >= longest);
  assert.ok(hermesEntry('s').timeout >= longest);
  assert.ok(gooseEntry('s').timeout >= longest);
  assert.ok(qwenEntry('s').timeout >= longest * 1000);
});

test('a POSIX hook command keeps a Windows path and a quote literal', () => {
  assert.equal(posixHookCommand('C:\\Users\\a b\\agent-hook.mjs', 'goose', 'Stop'), "node 'C:\\Users\\a b\\agent-hook.mjs' goose Stop");
  assert.equal(posixQuote("/home/o'neil/x"), `'/home/o'\\''neil/x'`);
});

test('registers and unregisters Grok, Pi, Hermes, Qwen Code and Goose in a temp home, keeping the user entries', async () => {
  const root = tempDir('iat-more-');
  const bin = path.join(root, 'bin');
  const userHome = path.join(root, 'user');
  const home = path.join(userHome, '.ide-agent-tabs');
  mkdirSync(bin);
  for (const agent of NEW_AGENTS) writeFileSync(path.join(bin, process.platform === 'win32' ? `${agent}.cmd` : agent), '');
  const env = { PATH: bin, HERMES_HOME: path.join(userHome, '.hermes') };
  const ctx = { serverDir: makeServerDir(), home, platform: process.platform, env, userHome };
  const server = serverCopyPath(home, process.platform);
  const hook = hookCopyPath(home, process.platform);
  const file = (agent: string) => configFile(agent as never, env, userHome, existsSync, process.platform);

  const grokToml = '# grok\nmodel = "grok-4"\n\n[mcp_servers.other]\ncommand = "x"\n';
  const grokUserHooks = path.join(userHome, '.grok', 'hooks', 'mine.json');
  const piJson = { mcpServers: { other: { command: 'x' } }, autoEnableCodemode: false };
  const hermesYaml = '# hermes\nmodel:\n  default: x # mine\nmcp_servers:\n  other:\n    command: x\nhooks:\n  pre_llm_call:\n    - command: echo hi\n';
  const allowlistFile = path.join(env.HERMES_HOME, 'shell-hooks-allowlist.json');
  const userApproval = { event: 'pre_llm_call', command: 'echo hi', approved_at: '2026-01-01T00:00:00Z' };
  const qwenUserHook = { matcher: 'run_shell_command', hooks: [{ type: 'command', command: 'check.sh' }] };
  const qwenJson = { mcpServers: { other: { command: 'x' } }, hooks: { PreToolUse: [qwenUserHook] } };
  const gooseYaml = 'extensions:\n  developer:\n    enabled: true\n    type: builtin\n    name: developer\nGOOSE_PROVIDER: ollama\n';
  write(file('grok'), grokToml);
  write(grokUserHooks, '{"hooks": {}}');
  write(file('pi'), JSON.stringify(piJson, null, 2));
  write(file('hermes'), hermesYaml);
  write(allowlistFile, JSON.stringify({ approvals: [userApproval] }, null, 2));
  write(file('qwen'), JSON.stringify(qwenJson, null, 2));
  write(file('goose'), gooseYaml);

  const before = await agentsReport(ctx);
  assert.deepEqual(
    before.agents.filter((a) => NEW_AGENTS.includes(a.agent)).map((a) => [a.agent, a.installed, a.registered, a.hooks, a.error]),
    [
      ['grok', true, false, false, undefined],
      ['pi', true, false, null, undefined],
      ['hermes', true, false, false, undefined],
      ['qwen', true, false, false, undefined],
      ['goose', true, false, false, undefined],
    ],
  );

  const report = await registerAgents(ctx, NEW_AGENTS);
  assert.deepEqual(report.errors, []);
  assert.ok(report.agents.every((a) => a.ok && a.registered && a.stable && a.path === server), JSON.stringify(report.agents));
  assert.deepEqual(report.agents.map((a) => a.hooks), [true, null, true, true, true]);

  assert.equal(readFileSync(file('grok'), 'utf8'), `${grokToml}\n[mcp_servers.ide-agent-tabs]\ncommand = "node"\nargs = [${JSON.stringify(server)}]\n`);
  const grokHooks = json(hookConfigFile('grok', env, userHome));
  assert.deepEqual(Object.keys(grokHooks.hooks), ['UserPromptSubmit', 'PreToolUse', 'PostToolUse', 'Notification', 'Stop', 'StopCancelled', 'StopFailure']);
  assert.deepEqual(grokHooks.hooks.Notification, [
    { matcher: 'permission_prompt|idle_prompt', hooks: [{ type: 'command', command: `node "${hook}" grok Notification`, timeout: 5 }] },
  ]);
  assert.deepEqual(grokHooks.hooks.Stop, [{ hooks: [{ type: 'command', command: `node "${hook}" grok Stop`, timeout: 5 }] }]);

  const tabEnv = { IDE_AGENT_TABS_ID: '${IDE_AGENT_TABS_ID}', IDE_AGENT_TABS_AGENT: '${IDE_AGENT_TABS_AGENT}' };
  assert.deepEqual(json(file('pi')), {
    ...piJson,
    mcpServers: { ...piJson.mcpServers, 'ide-agent-tabs': { command: 'node', args: [server], env: tabEnv, timeout: 660, exposure: 'direct' } },
  });

  const hermes = readFileSync(file('hermes'), 'utf8');
  assert.match(hermes, /^# hermes\nmodel:\n {2}default: x # mine\n/);
  const hermesHooks = ['pre_llm_call', 'post_tool_call', 'pre_approval_request', 'post_approval_response', 'pre_verify', 'on_session_end'];
  for (const event of hermesHooks) assert.ok(hermes.includes(`- command: node '${hook}' hermes ${event}\n      timeout: 5\n`), event);
  assert.match(hermes, /ide-agent-tabs:\n {4}command: node\n/);
  assert.match(hermes, /IDE_AGENT_TABS_ID: \$\{IDE_AGENT_TABS_ID\}/);
  assert.match(hermes, /pre_llm_call:\n {4}- command: echo hi\n {4}- command: node /, "the user's hook stays first");
  assert.doesNotMatch(hermes, /hooks_auto_accept/);
  assert.deepEqual(json(allowlistFile).approvals, [userApproval, ...hermesHooks.map((event) => ({ event, command: `node '${hook}' hermes ${event}` }))]);

  const qwen = json(file('qwen'));
  assert.deepEqual(qwen.mcpServers['ide-agent-tabs'], { command: 'node', args: [server], env: tabEnv, timeout: 700_000 });
  assert.deepEqual(Object.keys(qwen.hooks), ['PreToolUse', 'UserPromptSubmit', 'PostToolUse', 'PermissionRequest', 'Notification', 'Stop']);
  assert.deepEqual(qwen.hooks.PreToolUse, [qwenUserHook, { hooks: [{ type: 'command', command: `node "${hook}" qwen PreToolUse`, timeout: 5 }] }]);

  const goose = readFileSync(file('goose'), 'utf8');
  assert.match(goose, /^extensions:\n {2}developer:\n/);
  assert.match(goose, /ide-agent-tabs:\n {4}name: ide-agent-tabs\n {4}type: stdio\n {4}cmd: node\n/);
  assert.match(goose, /timeout: 700\n/);
  const pluginDir = path.join(userHome, '.agents', 'plugins', 'ide-agent-tabs');
  assert.equal(json(path.join(pluginDir, 'plugin.json')).name, 'ide-agent-tabs');
  assert.deepEqual(json(path.join(pluginDir, 'hooks', 'hooks.json')), {
    hooks: {
      UserPromptSubmit: [{ hooks: [{ type: 'command', command: `node '${hook}' goose UserPromptSubmit`, timeout: 5 }] }],
      PostToolUse: [{ hooks: [{ type: 'command', command: `node '${hook}' goose PostToolUse`, timeout: 5 }] }],
      Stop: [{ hooks: [{ type: 'command', command: `node '${hook}' goose Stop`, timeout: 5 }] }],
    },
  });

  const snapshot = NEW_AGENTS.map((a) => readFileSync(file(a), 'utf8'));
  const again = await registerAgents(ctx, NEW_AGENTS);
  assert.deepEqual(again.errors, []);
  assert.deepEqual(NEW_AGENTS.map((a) => readFileSync(file(a), 'utf8')), snapshot, 'registering again changes nothing');
  assert.equal(json(allowlistFile).approvals.length, 1 + hermesHooks.length);

  const removed = await unregisterAgents(ctx, NEW_AGENTS);
  assert.deepEqual(removed.errors, []);
  assert.ok(removed.agents.every((a) => a.ok && !a.registered && !a.hooks));
  assert.equal(readFileSync(file('grok'), 'utf8'), grokToml);
  assert.ok(!existsSync(hookConfigFile('grok', env, userHome)));
  assert.ok(existsSync(grokUserHooks));
  assert.deepEqual(json(file('pi')), piJson);
  assert.equal(readFileSync(file('hermes'), 'utf8'), hermesYaml);
  assert.deepEqual(json(allowlistFile), { approvals: [userApproval] });
  assert.deepEqual(json(file('qwen')), qwenJson);
  assert.equal(readFileSync(file('goose'), 'utf8'), gooseYaml);
  assert.ok(!existsSync(pluginDir));
});

test('a Hermes config whose hooks are not a mapping is left alone', async () => {
  const root = tempDir('iat-hermes-bad-');
  const bin = path.join(root, 'bin');
  const userHome = path.join(root, 'user');
  mkdirSync(bin);
  writeFileSync(path.join(bin, process.platform === 'win32' ? 'hermes.cmd' : 'hermes'), '');
  const env = { PATH: bin, HERMES_HOME: path.join(userHome, '.hermes') };
  const config = path.join(env.HERMES_HOME, 'config.yaml');
  write(config, 'hooks: [1]\n');
  const ctx = { serverDir: makeServerDir(), home: path.join(userHome, '.ide-agent-tabs'), platform: process.platform, env, userHome };
  const report = await registerAgents(ctx, ['hermes']);
  assert.match(report.agents[0]!.error!, /"hooks" isn't a mapping/);
  assert.ok(!existsSync(path.join(env.HERMES_HOME, 'shell-hooks-allowlist.json')));
});

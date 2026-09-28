import assert from 'node:assert/strict';
import { chmodSync, existsSync, mkdirSync, readdirSync, readFileSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import {
  agentsReport,
  configFile,
  copilotEntry,
  entryServerPath,
  opencodeEntry,
  parseCodexGet,
  registerAgents,
  registerArgs,
  samePath,
  stripJsonComments,
  unregisterAgents,
  unregisterArgs,
  withServerEntry,
} from '../src/register.js';
import { refreshServerCopy, serverCopyDir, serverCopyPath, serverHash } from '../src/serverCopy.js';
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
  assert.deepEqual((await refreshServerCopy(source, home)).sort(), ['THIRD_PARTY_NOTICES.txt', 'launch/agent-launch.ps1', 'launch/agent-launch.sh', 'mcp-server.mjs']);
  assert.deepEqual(readdirSync(serverCopyDir(home)).sort(), ['THIRD_PARTY_NOTICES.txt', 'launch', 'mcp-server.mjs']);
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
  } else if (args[1] === 'remove') {
    delete servers[args[2]];
    writeJson(store, servers);
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
      writeFileSync(file, `#!/bin/sh\nexec "${process.execPath}" "$(dirname "$0")/fake-agent.mjs" ${agent} "$@"\n`);
      chmodSync(file, 0o755);
    }
  }
  for (const agent of ['copilot', 'opencode']) writeFileSync(path.join(bin, process.platform === 'win32' ? `${agent}.cmd` : agent), '');
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

  const before = await agentsReport(ctx);
  assert.deepEqual(before.server, { path: server, exists: false, current: false });
  assert.deepEqual(
    before.agents.map((a) => [a.agent, a.installed, a.registered, a.error]),
    [
      ['codex', true, false, undefined],
      ['gemini', true, false, undefined],
      ['copilot', true, false, undefined],
      ['opencode', true, false, undefined],
    ],
  );

  const report = await registerAgents(ctx, ['codex', 'gemini', 'copilot', 'opencode', 'claude']);
  assert.deepEqual(report.errors, ['claude: Claude Code gets the server from the plugin; nothing to register']);
  assert.deepEqual(report.server, { path: server, exists: true, current: true });
  assert.ok(report.agents.every((a) => a.ok && a.registered && a.stable && a.path === server));

  const calls = readFileSync(path.join(bin, 'calls.txt'), 'utf8').trim().split('\n').map((l) => JSON.parse(l) as { agent: string; args: string[]; cwd: string });
  const writes = calls.filter((c) => c.args[1] !== 'get').map((c) => [c.agent, ...c.args]);
  assert.deepEqual(writes, [
    ['codex', 'mcp', 'add', 'ide-agent-tabs', '--', 'node', server],
    ['gemini', 'mcp', 'add', '--scope', 'user', 'ide-agent-tabs', 'node', server],
  ]);
  assert.ok(calls.every((c) => samePath(c.cwd, home, process.platform)));
  assert.deepEqual(JSON.parse(readFileSync(path.join(userHome, '.gemini', 'settings.json'), 'utf8')).mcpServers['ide-agent-tabs'], { command: 'node', args: [server] });

  assert.equal(
    readFileSync(copilotFile, 'utf8'),
    `{\n  "mcpServers": {\n    "github": {\n      "type": "http",\n      "url": "https://example.test/mcp"\n    },\n    "ide-agent-tabs": {\n      "type": "local",\n      "command": "node",\n      "args": [\n        "${server}"\n      ],\n      "env": {},\n      "tools": [\n        "*"\n      ]\n    }\n  }\n}\n`,
  );
  assert.deepEqual(JSON.parse(readFileSync(opencodeFile, 'utf8')), {
    $schema: 'https://opencode.ai/config.json',
    mcp: { 'ide-agent-tabs': { type: 'local', command: ['node', server], enabled: true } },
  });

  const removed = await unregisterAgents(ctx, ['codex', 'gemini', 'copilot', 'opencode']);
  assert.deepEqual(removed.errors, []);
  assert.ok(removed.agents.every((a) => a.ok && !a.registered));
  assert.deepEqual(JSON.parse(readFileSync(copilotFile, 'utf8')), { mcpServers: { github: { type: 'http', url: 'https://example.test/mcp' } } });
  assert.deepEqual(JSON.parse(readFileSync(opencodeFile, 'utf8')).mcp, {});
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
  assert.ok(report.errors.includes('nope: unknown agent; use codex, gemini, copilot, opencode'));
  assert.equal(readFileSync(copilotFile, 'utf8'), '{ "mcpServers": ');
  assert.equal(readFileSync(jsoncFile, 'utf8'), jsonc);
  assert.ok(!existsSync(path.join(path.dirname(jsoncFile), 'opencode.json')));
});

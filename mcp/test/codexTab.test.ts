import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { HOOK_EVENTS } from '../src/messaging/hook.js';
import { BUILTIN_PROFILES, CODEX_TAB_ARGS } from '../src/profiles.js';
import { HOOK_TOOL, SERVER_NAME } from '../src/server.js';
import { tempDir } from './tempDir.js';

const repo = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const HOOK_TIMEOUT_S = 10;

const HOOKS: [event: string, key: string, input: Record<string, string>][] = [
  ['SessionStart', 'session_start', { event: 'SessionStart', session_id: '${session_id}' }],
  ['UserPromptSubmit', 'user_prompt_submit', { event: 'UserPromptSubmit', session_id: '${session_id}', turn_id: '${turn_id}' }],
  ['PostToolUse', 'post_tool_use', { event: 'PostToolUse', session_id: '${session_id}', turn_id: '${turn_id}' }],
  ['PermissionRequest', 'permission_request', { event: 'PermissionRequest', session_id: '${session_id}', turn_id: '${turn_id}' }],
  ['Stop', 'stop', { event: 'Stop', session_id: '${session_id}', turn_id: '${turn_id}' }],
];

// Codex's hook_key for the -c layer: its synthetic source path resolved against "/" or "C:\".
const SESSION_FLAG_SOURCES = ['/<session-flags>/config.toml', 'C:\\<session-flags>\\config.toml'];

function canonical(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonical);
  if (typeof value === 'object' && value !== null) {
    return Object.fromEntries(Object.keys(value).sort().map((k) => [k, canonical((value as Record<string, unknown>)[k])]));
  }
  return value;
}

// codex-rs hooks discovery: hook_hash, then config version_for_toml (SHA-256 of key-sorted compact JSON).
function trustedHash(key: string, input: Record<string, string>): string {
  const identity = { event_name: key, hooks: [{ type: 'mcp_tool', server: SERVER_NAME, tool: HOOK_TOOL, input, timeout: HOOK_TIMEOUT_S }] };
  return `sha256:${createHash('sha256').update(JSON.stringify(canonical(identity))).digest('hex')}`;
}

const table = (o: Record<string, string>) => `{ ${Object.entries(o).map(([k, v]) => `${k} = '${v}'`).join(', ')} }`;

test('the Codex profile launches in-process with its own Agent Tabs server and trusted hooks', () => {
  const codex = BUILTIN_PROFILES.find((p) => p.name === 'codex')!;
  assert.deepEqual(codex.args, CODEX_TAB_ARGS);
  assert.equal(CODEX_TAB_ARGS[0], '--no-daemon');

  const flags = CODEX_TAB_ARGS.slice(1);
  assert.ok(flags.every((a, i) => (i % 2 === 0) === (a === '-c')));
  const overrides = flags.filter((_, i) => i % 2 === 1);
  assert.match(overrides[0]!, /^mcp_servers\.ide-agent-tabs=\{ command = 'node', args = \['-e', '[^']+'\], env_vars = \['IDE_AGENT_TABS_ID', 'IDE_AGENT_TABS_AGENT', 'IDE_AGENT_TABS_HOME'\], tool_timeout_sec = 660 \}$/);

  const hooks = HOOKS.map(
    ([event, , input]) =>
      `hooks.${event}=[{ hooks = [{ type = 'mcp_tool', server = '${SERVER_NAME}', tool = '${HOOK_TOOL}', input = ${table(input)}, timeout = ${HOOK_TIMEOUT_S} }] }]`,
  );
  assert.deepEqual(overrides.slice(1, -1), hooks);

  const state = HOOKS.flatMap(([, key, input]) =>
    SESSION_FLAG_SOURCES.map((source) => `'${source}:${key}:0:0' = { trusted_hash = '${trustedHash(key, input)}' }`),
  );
  assert.equal(overrides.at(-1), `hooks.state={ ${state.join(', ')} }`);
});

test('the hooked events are the ones the hook tool handles', () => {
  assert.deepEqual(HOOKS.map(([event]) => event).sort(), Object.keys(HOOK_EVENTS.codex).sort());
});

test('the server one-liner loads the shared copy from the Agent Tabs home', () => {
  const code = /args = \['-e', '([^']+)'\]/.exec(CODEX_TAB_ARGS[2]!)![1]!;
  const root = tempDir('iat-codex-tab-');
  const fake = `process.stdout.write(import.meta.url);\n`;
  for (const home of [path.join(root, 'custom'), path.join(root, 'user', '.ide-agent-tabs')]) {
    mkdirSync(path.join(home, 'mcp'), { recursive: true });
    writeFileSync(path.join(home, 'mcp', 'mcp-server.mjs'), fake);
  }
  const run = (env: Record<string, string>) =>
    spawnSync(process.execPath, ['-e', code], { env: { ...process.env, HOME: path.join(root, 'user'), USERPROFILE: path.join(root, 'user'), ...env }, encoding: 'utf8', windowsHide: true }).stdout;
  assert.equal(run({ IDE_AGENT_TABS_HOME: path.join(root, 'custom') }), pathToFileURL(path.join(root, 'custom', 'mcp', 'mcp-server.mjs')).href);
  assert.equal(run({ IDE_AGENT_TABS_HOME: '' }), pathToFileURL(path.join(root, 'user', '.ide-agent-tabs', 'mcp', 'mcp-server.mjs')).href);
});

test('no argument can be changed by Windows PowerShell 5.1 or cmd.exe on its way to Codex', () => {
  for (const arg of CODEX_TAB_ARGS) {
    assert.doesNotMatch(arg, /["%!\n]/, arg);
    assert.doesNotMatch(arg, /\\$/, arg);
    if (/[&|<>^()]/.test(arg)) assert.match(arg, /\s/, `${arg} must be quoted, so it needs a space`);
  }
});

test('the VS Code and JetBrains profiles hold the same Codex arguments', () => {
  const vscode = readFileSync(path.join(repo, 'vscode', 'src', 'profiles.ts'), 'utf8');
  const kotlin = readFileSync(path.join(repo, 'jetbrains', 'src', 'main', 'kotlin', 'dev', 'alexk', 'ideagenttabs', 'AgentProfiles.kt'), 'utf8');
  for (const arg of CODEX_TAB_ARGS) {
    assert.ok(vscode.includes(JSON.stringify(arg)), `vscode/src/profiles.ts lacks ${arg}`);
    assert.ok(kotlin.includes(JSON.stringify(arg).replace(/\$/g, '\\$')), `AgentProfiles.kt lacks ${arg}`);
  }
});

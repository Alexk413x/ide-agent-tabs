import { existsSync, promises as fs } from 'node:fs';
import path from 'node:path';
import { ensurePrivateDir, readTextIfExists } from './files.js';
import { isInstalled } from './installed.js';
import { BUILTIN_PROFILES } from './profiles.js';
import {
  copilotHooks,
  hasCodexSettings,
  hasOurHooks,
  hookConfigFile,
  isHookAgent,
  mergeHookSettings,
  withCodexSettings,
  type HookAgent,
} from './hookConfig.js';
import { AGENT_ENV, TAB_ID_ENV } from './profiles.js';
import { hookCopyPath, refreshServerCopy, serverCopyDir, serverCopyPath, serverHash } from './serverCopy.js';
import type { RunResult } from './process.js';
import { cliFailure, findCliOnPath, runCliResult, type SyncContext } from './sync.js';

export const SERVER_NAME = 'ide-agent-tabs';
export const AGENTS = ['codex', 'gemini', 'copilot', 'opencode'] as const;
export type AgentName = (typeof AGENTS)[number];
const CLI_TIMEOUT_MS = 30_000;
const OPENCODE_SCHEMA = 'https://opencode.ai/config.json';

export type RegisterContext = Omit<SyncContext, 'bundleDir'>;

export interface AgentStatus {
  agent: AgentName;
  label: string;
  installed: boolean;
  registered: boolean;
  path: string | null;
  stable: boolean;
  config: string;
  hooks: boolean | null;
  error?: string;
}

export const isAgentName = (name: string): name is AgentName => (AGENTS as readonly string[]).includes(name);

// Codex tabs bring their own hooks, and the shared Codex daemon would run global ones with another tab's
// IDE_AGENT_TABS_ID, so Codex gets none. Registering still removes the ones older versions installed.
export const takesHooks = (agent: AgentName): agent is HookAgent => isHookAgent(agent) && agent !== 'codex';

// The Codex desktop app shares ~/.codex with the CLI, and on Windows, Codex before 0.159 opens a console
// window for each MCP server the app starts.
export const CODEX_WINDOWS_REFUSAL =
  "not registered on Windows: the Codex desktop app reads the same config and would open a console window for each session. Open Codex in an agent tab instead; each Codex tab brings its own Agent Tabs server and hooks";

function profileOf(agent: AgentName) {
  const builtin = BUILTIN_PROFILES.find((p) => p.name === agent);
  return { command: builtin?.command ?? agent, label: builtin?.label ?? 'OpenCode' };
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

export function configFile(agent: AgentName, env: NodeJS.ProcessEnv, userHome: string, exists: (file: string) => boolean = existsSync): string {
  switch (agent) {
    case 'codex':
      return path.join(env.CODEX_HOME || path.join(userHome, '.codex'), 'config.toml');
    case 'gemini':
      return path.join(env.GEMINI_CLI_HOME || userHome, '.gemini', 'settings.json');
    case 'copilot':
      return path.join(env.COPILOT_HOME || path.join(userHome, '.copilot'), 'mcp-config.json');
    case 'opencode': {
      const dir = path.join(env.XDG_CONFIG_HOME || path.join(userHome, '.config'), 'opencode');
      const json = path.join(dir, 'opencode.json');
      const jsonc = path.join(dir, 'opencode.jsonc');
      return !exists(json) && exists(jsonc) ? jsonc : json;
    }
  }
}

export function registerArgs(agent: 'codex' | 'gemini', server: string): string[] {
  return agent === 'codex'
    ? ['mcp', 'add', SERVER_NAME, '--', 'node', server]
    : ['mcp', 'add', '--scope', 'user', SERVER_NAME, 'node', server];
}

export function unregisterArgs(agent: 'codex' | 'gemini'): string[] {
  return agent === 'codex' ? ['mcp', 'remove', SERVER_NAME] : ['mcp', 'remove', '--scope', 'user', SERVER_NAME];
}

// Copilot CLI passes an MCP server only PATH from its environment; the rest must be named here.
export const copilotEntry = (server: string) => ({
  type: 'local',
  command: 'node',
  args: [server],
  env: { [TAB_ID_ENV]: `\${${TAB_ID_ENV}}`, [AGENT_ENV]: `\${${AGENT_ENV}}` },
  tools: ['*'],
});
export const opencodeEntry = (server: string) => ({ type: 'local', command: ['node', server], enabled: true });

export function stripJsonComments(text: string): string {
  let out = '';
  let inString = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i]!;
    if (inString) {
      out += c;
      if (c === '\\') out += text[++i] ?? '';
      else if (c === '"') inString = false;
    } else if (c === '"') {
      inString = true;
      out += c;
    } else if (c === '/' && text[i + 1] === '/') {
      while (i < text.length && text[i] !== '\n') i++;
      out += '\n';
    } else if (c === '/' && text[i + 1] === '*') {
      const close = text.indexOf('*/', i + 2);
      i = close < 0 ? text.length : close + 1;
      out += ' ';
    } else {
      out += c;
    }
  }
  return out;
}

export function parseConfig(text: string, file: string, allowComments: boolean): Record<string, unknown> {
  let json: unknown;
  try {
    json = JSON.parse(allowComments ? stripJsonComments(text) : text);
  } catch (e) {
    throw new Error(`${file} isn't plain JSON (${(e as Error).message}); add the ${SERVER_NAME} entry by hand`);
  }
  if (!isObject(json)) throw new Error(`${file} doesn't hold a JSON object`);
  return json;
}

function detectIndent(text: string): string {
  return text.match(/^([ \t]+)\S/m)?.[1] ?? '  ';
}

export function editJson(
  text: string | undefined,
  file: string,
  change: (root: Record<string, unknown>) => Record<string, unknown>,
): string | undefined {
  const root = text === undefined || text.trim() === '' ? {} : parseConfig(text, file, false);
  const next = change(root);
  if (JSON.stringify(next) === JSON.stringify(root)) return undefined;
  const eol = text?.includes('\r\n') ? '\r\n' : '\n';
  return `${JSON.stringify(next, null, detectIndent(text ?? ''))}\n`.replace(/\n/g, eol);
}

export function withServerEntry(text: string | undefined, file: string, section: string, entry: unknown, skeleton: Record<string, unknown> = {}): string | undefined {
  const blank = text === undefined || text.trim() === '';
  if (blank && entry === undefined) return undefined;
  const root = blank ? { ...skeleton } : parseConfig(text, file, false);
  const current = root[section] ?? {};
  if (!isObject(current)) throw new Error(`${file}: "${section}" isn't an object`);
  const servers = { ...current };
  if (entry === undefined) {
    if (!Object.hasOwn(servers, SERVER_NAME)) return undefined;
    delete servers[SERVER_NAME];
  } else {
    if (JSON.stringify(servers[SERVER_NAME]) === JSON.stringify(entry)) return undefined;
    servers[SERVER_NAME] = entry;
  }
  root[section] = servers;
  const eol = text?.includes('\r\n') ? '\r\n' : '\n';
  return `${JSON.stringify(root, null, detectIndent(text ?? ''))}\n`.replace(/\n/g, eol);
}

export function entryServerPath(entry: unknown): string | undefined {
  if (!isObject(entry)) return undefined;
  const parts = [entry.command, entry.args].flat().filter((p): p is string => typeof p === 'string');
  return parts.find((p) => /\.[cm]?js$/i.test(p)) ?? parts[parts.length - 1];
}

export function samePath(a: string, b: string, platform: NodeJS.Platform): boolean {
  const norm = (p: string) => {
    const forward = p.replace(/\\/g, '/').replace(/\/+/g, '/');
    return platform === 'win32' || platform === 'darwin' ? forward.toLowerCase() : forward;
  };
  return norm(a) === norm(b);
}

export function parseCodexGet(stdout: string): unknown {
  const start = stdout.indexOf('{');
  if (start < 0) throw new Error('codex mcp get printed no JSON');
  const json = JSON.parse(stdout.slice(start)) as { transport?: { command?: unknown; args?: unknown } };
  return { command: json.transport?.command, args: json.transport?.args };
}

const isCodexNotFound = (output: string) => /No MCP server named/i.test(output);

async function writeConfig(file: string, text: string): Promise<void> {
  const target = await fs.realpath(file).catch(() => file);
  await fs.mkdir(path.dirname(target), { recursive: true });
  const mode = ((await fs.stat(target).catch(() => undefined))?.mode ?? 0o600) & 0o777;
  const temp = `${target}.${process.pid}.${Date.now()}.tmp`;
  try {
    await fs.writeFile(temp, text, { mode });
    await fs.rename(temp, target);
  } catch (e) {
    await fs.rm(temp, { force: true });
    throw e;
  }
}

// Agent CLIs also read project config from their working folder, and in the home folder gemini's project settings
// are its user settings, which breaks --scope. Every call runs from the Agent Tabs home, which holds neither.
async function runAgentCli(ctx: RegisterContext, agent: 'codex' | 'gemini', args: string[]): Promise<{ cli: string; result: RunResult }> {
  const cli = findCliOnPath(profileOf(agent).command, ctx);
  if (!cli) throw new Error(`${agent} isn't on PATH as a runnable file`);
  await ensurePrivateDir(ctx.home);
  return { cli, result: await runCliResult(ctx, cli, args, CLI_TIMEOUT_MS, ctx.home) };
}

async function changeWithCli(ctx: RegisterContext, agent: 'codex' | 'gemini', args: string[]): Promise<void> {
  const { cli, result } = await runAgentCli(ctx, agent, args);
  if (result.code !== 0) throw cliFailure(cli, args, result);
}

const codexConfig = (ctx: RegisterContext) => configFile('codex', ctx.env, ctx.userHome);

async function readJsonIfExists(file: string): Promise<Record<string, unknown> | undefined> {
  const text = await readTextIfExists(file);
  return text === undefined || text.trim() === '' ? undefined : parseConfig(text, file, true);
}

async function hooksInstalled(ctx: RegisterContext, agent: HookAgent): Promise<boolean> {
  const hook = hookCopyPath(ctx.home, ctx.platform);
  const file = hookConfigFile(agent, ctx.env, ctx.userHome);
  const root = await readJsonIfExists(file);
  if (agent === 'copilot') return root !== undefined && JSON.stringify(root) === JSON.stringify(copilotHooks(hook));
  return root !== undefined && hasOurHooks(root, agent, hook);
}

async function changeJson(file: string, change: (root: Record<string, unknown>) => Record<string, unknown>): Promise<void> {
  const next = editJson(await readTextIfExists(file), file, change);
  if (next !== undefined) await writeConfig(file, next);
}

async function installCodexSettings(ctx: RegisterContext): Promise<void> {
  const config = codexConfig(ctx);
  const next = withCodexSettings((await readTextIfExists(config)) ?? '', config);
  if (next !== undefined) await writeConfig(config, next);
}

async function installHooks(ctx: RegisterContext, agent: HookAgent): Promise<void> {
  const hook = hookCopyPath(ctx.home, ctx.platform);
  const file = hookConfigFile(agent, ctx.env, ctx.userHome);
  if (agent === 'copilot') return changeJson(file, () => copilotHooks(hook));
  await changeJson(file, (root) => mergeHookSettings(root, file, agent, hook));
}

async function removeHooks(ctx: RegisterContext, agent: HookAgent): Promise<void> {
  const file = hookConfigFile(agent, ctx.env, ctx.userHome);
  if (agent === 'copilot') {
    await fs.rm(file, { force: true });
    return;
  }
  if ((await readTextIfExists(file)) === undefined) return;
  await changeJson(file, (root) => mergeHookSettings(root, file, agent, undefined));
}

const sectionOf = (agent: 'gemini' | 'copilot' | 'opencode') => (agent === 'opencode' ? 'mcp' : 'mcpServers');

async function readEntry(ctx: RegisterContext, agent: AgentName, file: string): Promise<unknown> {
  if (agent === 'codex') {
    const args = ['mcp', 'get', SERVER_NAME, '--json'];
    const { cli, result } = await runAgentCli(ctx, agent, args);
    if (result.code === 0) return parseCodexGet(result.stdout);
    if (isCodexNotFound(result.stderr + result.stdout)) return undefined;
    throw cliFailure(cli, args, result);
  }
  const text = await readTextIfExists(file);
  if (text === undefined || text.trim() === '') return undefined;
  const section = parseConfig(text, file, true)[sectionOf(agent)];
  return isObject(section) ? section[SERVER_NAME] : undefined;
}

export async function agentStatus(ctx: RegisterContext, agent: AgentName): Promise<AgentStatus> {
  const { command, label } = profileOf(agent);
  const config = configFile(agent, ctx.env, ctx.userHome);
  const installed = isInstalled(command, ctx.env.PATH ?? ctx.env.Path ?? '', ctx.platform === 'win32');
  const status: AgentStatus = { agent, label, installed, registered: false, path: null, stable: false, config, hooks: takesHooks(agent) ? false : null };
  if (agent === 'codex' && !installed) return status;
  try {
    if (takesHooks(agent)) status.hooks = await hooksInstalled(ctx, agent);
    const entry = await readEntry(ctx, agent, config);
    if (entry === undefined) return status;
    const registered = entryServerPath(entry) ?? null;
    const settings = agent !== 'codex' || hasCodexSettings(await readTextIfExists(config));
    return {
      ...status,
      registered: true,
      path: registered,
      stable: settings && registered !== null && samePath(registered, serverCopyPath(ctx.home, ctx.platform), ctx.platform),
    };
  } catch (e) {
    return { ...status, error: (e as Error).message };
  }
}

async function editConfig(file: string, agent: 'copilot' | 'opencode', entry: unknown): Promise<void> {
  const skeleton = agent === 'opencode' ? { $schema: OPENCODE_SCHEMA } : {};
  const next = withServerEntry(await readTextIfExists(file), file, sectionOf(agent), entry, skeleton);
  if (next !== undefined) await writeConfig(file, next);
}

async function register(ctx: RegisterContext, agent: AgentName, server: string, file: string): Promise<void> {
  if (agent === 'codex' || agent === 'gemini') await changeWithCli(ctx, agent, registerArgs(agent, server));
  else await editConfig(file, agent, agent === 'copilot' ? copilotEntry(server) : opencodeEntry(server));
  if (agent === 'codex') {
    await installCodexSettings(ctx);
    await removeHooks(ctx, 'codex');
  }
  if (takesHooks(agent)) await installHooks(ctx, agent);
}

async function unregister(ctx: RegisterContext, agent: AgentName, file: string): Promise<void> {
  if (isHookAgent(agent)) await removeHooks(ctx, agent);
  if (agent === 'codex' || agent === 'gemini') await changeWithCli(ctx, agent, unregisterArgs(agent));
  else await editConfig(file, agent, undefined);
}

export async function serverStatus(ctx: RegisterContext) {
  const exists = existsSync(serverCopyDir(ctx.home));
  const [copy, bundled] = await Promise.all([
    exists ? serverHash(serverCopyDir(ctx.home)).catch(() => undefined) : undefined,
    serverHash(ctx.serverDir).catch(() => undefined),
  ]);
  return { path: serverCopyPath(ctx.home, ctx.platform), exists, current: copy !== undefined && copy === bundled };
}

export async function agentsReport(ctx: RegisterContext) {
  return {
    server: await serverStatus(ctx),
    agents: await Promise.all(AGENTS.map((a) => agentStatus(ctx, a))),
  };
}

interface Outcome extends AgentStatus {
  ok: boolean;
}

const outcome = (status: AgentStatus, error?: string): Outcome => ({ ...status, ok: error === undefined, ...(error !== undefined ? { error } : {}) });

function parseAgentNames(names: string[], errors: string[]): AgentName[] {
  const agents: AgentName[] = [];
  for (const name of names) {
    if (name === 'claude') errors.push('claude: Claude Code gets the server from the plugin; nothing to register');
    else if (!isAgentName(name)) errors.push(`${name}: unknown agent; use ${AGENTS.join(', ')}`);
    else if (!agents.includes(name)) agents.push(name);
  }
  return agents;
}

async function registerOne(ctx: RegisterContext, agent: AgentName, server: string, copyError: string | undefined): Promise<Outcome> {
  const before = await agentStatus(ctx, agent);
  if (!existsSync(server)) return outcome(before, copyError ?? `${server} is missing`);
  if (!before.installed) return outcome(before, 'not installed');
  if (agent === 'codex' && ctx.platform === 'win32') return outcome(before, CODEX_WINDOWS_REFUSAL);
  try {
    await register(ctx, agent, server, before.config);
  } catch (e) {
    return outcome(before, (e as Error).message);
  }
  const after = await agentStatus(ctx, agent);
  const missing =
    !after.registered || !after.stable
      ? `${after.config} doesn't hold the ${SERVER_NAME} entry after registering`
      : after.hooks === false
        ? `the Agent Tabs hooks aren't in ${isHookAgent(agent) ? hookConfigFile(agent, ctx.env, ctx.userHome) : after.config} after registering`
        : undefined;
  return outcome(after, after.error ?? missing);
}

async function unregisterOne(ctx: RegisterContext, agent: AgentName): Promise<Outcome> {
  const before = await agentStatus(ctx, agent);
  if (before.error || (!before.registered && !before.hooks)) return outcome(before, before.error);
  try {
    await unregister(ctx, agent, before.config);
  } catch (e) {
    return outcome(before, (e as Error).message);
  }
  const after = await agentStatus(ctx, agent);
  const left = after.registered ? `${after.config} still holds the ${SERVER_NAME} entry` : after.hooks ? 'the Agent Tabs hooks are still installed' : undefined;
  return outcome(after, after.error ?? left);
}

const errorsOf = (outcomes: Outcome[]) => outcomes.filter((o) => o.error !== undefined).map((o) => `${o.agent}: ${o.error}`);

export async function registerAgents(ctx: RegisterContext, names: string[]) {
  const errors: string[] = [];
  const agents = parseAgentNames(names, errors);
  let copyError: string | undefined;
  try {
    await refreshServerCopy(ctx.serverDir, ctx.home);
  } catch (e) {
    copyError = `server copy: ${(e as Error).message}`;
    errors.push(copyError);
  }
  const server = serverCopyPath(ctx.home, ctx.platform);
  const results: Outcome[] = [];
  for (const agent of agents) results.push(await registerOne(ctx, agent, server, copyError));
  return { server: await serverStatus(ctx), agents: results, errors: [...errors, ...errorsOf(results)] };
}

export async function unregisterAgents(ctx: RegisterContext, names: string[]) {
  const errors: string[] = [];
  const results: Outcome[] = [];
  for (const agent of parseAgentNames(names, errors)) results.push(await unregisterOne(ctx, agent));
  return { agents: results, errors: [...errors, ...errorsOf(results)] };
}

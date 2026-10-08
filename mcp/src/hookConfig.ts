import path from 'node:path';
import { HOOK_EVENTS } from './messaging/hook.js';
import { AGENT_ENV, TAB_ID_ENV } from './profiles.js';
import { isScalar, isSeq, type Document } from 'yaml';
import { filterYamlLists, yamlMap } from './yamlConfig.js';

export type HookAgent = 'codex' | 'gemini' | 'copilot' | 'agy' | 'grok' | 'hermes' | 'qwen' | 'goose';
export const HOOK_AGENTS: readonly HookAgent[] = ['codex', 'gemini', 'copilot', 'agy', 'grok', 'hermes', 'qwen', 'goose'];
type SettingsAgent = 'codex' | 'gemini' | 'qwen';
export const AGY_HOOK_GROUP = 'ide-agent-tabs';
// Only tools that read or message: open_tab, close_tab and handoff start or stop agents, and jev_ sends text
// off the machine, so those keep Antigravity CLI's own per-call confirmation.
export const AGY_ALLOW_RULES: readonly string[] = ['send_message', 'read_messages', 'wait_for_message', 'list_sessions', 'list_agents', 'list_tabs'].map(
  (tool) => `mcp(ide-agent-tabs/${tool})`,
);
const AGY_WILDCARD_RULE = 'mcp(ide-agent-tabs/*)';
export const COPILOT_HOOKS_FILE = 'ide-agent-tabs.json';
export const GROK_HOOKS_FILE = 'ide-agent-tabs.json';
export const GOOSE_PLUGIN = 'ide-agent-tabs';
export const CODEX_ENV_VARS = [TAB_ID_ENV, AGENT_ENV, 'IDE_AGENT_TABS_HOME'];
export const CODEX_TOOL_TIMEOUT_S = 660;
const HOOK_TIMEOUT_S = 5;

export const isHookAgent = (agent: string): agent is HookAgent => (HOOK_AGENTS as readonly string[]).includes(agent);

export function hookConfigFile(agent: HookAgent, env: NodeJS.ProcessEnv, userHome: string, platform: NodeJS.Platform = process.platform): string {
  switch (agent) {
    case 'codex':
      return path.join(env.CODEX_HOME || path.join(userHome, '.codex'), 'hooks.json');
    case 'gemini':
      return path.join(env.GEMINI_CLI_HOME || userHome, '.gemini', 'settings.json');
    case 'copilot':
      return path.join(env.COPILOT_HOME || path.join(userHome, '.copilot'), 'hooks', COPILOT_HOOKS_FILE);
    case 'agy':
      return path.join(userHome, '.gemini', 'config', 'hooks.json');
    case 'grok':
      return path.join(grokHome(env, userHome), 'hooks', GROK_HOOKS_FILE);
    case 'hermes':
      return path.join(hermesHome(env, userHome, platform), 'config.yaml');
    case 'qwen':
      return path.join(qwenHome(env, userHome), 'settings.json');
    case 'goose':
      return path.join(goosePluginDir(env, userHome), 'hooks', 'hooks.json');
  }
}

export const grokHome = (env: NodeJS.ProcessEnv, userHome: string) => env.GROK_HOME || path.join(userHome, '.grok');
export const hermesHome = (env: NodeJS.ProcessEnv, userHome: string, platform: NodeJS.Platform) =>
  env.HERMES_HOME || (platform === 'win32' ? path.join(env.LOCALAPPDATA || path.join(userHome, 'AppData', 'Local'), 'hermes') : path.join(userHome, '.hermes'));
export const qwenHome = (env: NodeJS.ProcessEnv, userHome: string) => (env.QWEN_HOME ? path.resolve(env.QWEN_HOME.replace(/^~(?=$|[\\/])/, userHome)) : path.join(userHome, '.qwen'));
export const gooseRoot = (env: NodeJS.ProcessEnv) => (env.GOOSE_PATH_ROOT && path.isAbsolute(env.GOOSE_PATH_ROOT) ? env.GOOSE_PATH_ROOT : undefined);
export const goosePluginDir = (env: NodeJS.ProcessEnv, userHome: string) => path.join(gooseRoot(env) ?? userHome, '.agents', 'plugins', GOOSE_PLUGIN);
export const hermesAllowlistFile = (env: NodeJS.ProcessEnv, userHome: string, platform: NodeJS.Platform) =>
  path.join(hermesHome(env, userHome, platform), 'shell-hooks-allowlist.json');

export const agySettingsFile = (userHome: string) => path.join(userHome, '.gemini', 'antigravity-cli', 'settings.json');

// Codex, Gemini CLI, Qwen Code and Grok Build run a hook command through a shell (cmd.exe or PowerShell on
// Windows), so the path is quoted and may not hold characters any of those shells expands inside double quotes.
export function hookCommand(hook: string, agent: SettingsAgent | 'grok', event: string): string {
  if (/["%$`!\p{Cc}]/u.test(hook)) throw new Error(`can't put the hook path in a shell command: ${hook}`);
  return `node "${hook}" ${agent} ${event}`;
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

const HOOK_PATH_END = /[\\/]agent-hook\.mjs(?:["']|$)/;
const mentionsHook = (part: unknown) => typeof part === 'string' && HOOK_PATH_END.test(part);
const isOurs = (h: unknown) => isObject(h) && [h.command, h.exec, ...(Array.isArray(h.args) ? h.args : [])].some(mentionsHook);

function ourHandler(agent: SettingsAgent, hook: string, event: string): Record<string, unknown> {
  const command = hookCommand(hook, agent, event);
  return agent === 'gemini'
    ? { type: 'command', name: 'ide-agent-tabs', command, timeout: HOOK_TIMEOUT_S * 1000 }
    : { type: 'command', command, timeout: HOOK_TIMEOUT_S };
}

function withoutOurs(hooks: Record<string, unknown>): Record<string, unknown> {
  const kept: Record<string, unknown> = {};
  for (const [event, groups] of Object.entries(hooks)) {
    if (!Array.isArray(groups)) {
      kept[event] = groups;
      continue;
    }
    const left = groups.flatMap((group) => {
      if (!isObject(group) || !Array.isArray(group.hooks)) return [group];
      const handlers = group.hooks.filter((h) => !isOurs(h));
      if (handlers.length === group.hooks.length) return [group];
      return handlers.length ? [{ ...group, hooks: handlers }] : [];
    });
    if (left.length) kept[event] = left;
  }
  return kept;
}

export function mergeHookSettings(root: Record<string, unknown>, file: string, agent: SettingsAgent, hook: string | undefined): Record<string, unknown> {
  const current = root.hooks ?? {};
  if (!isObject(current)) throw new Error(`${file}: "hooks" isn't an object`);
  const hooks = withoutOurs(current);
  if (hook !== undefined) {
    for (const event of Object.keys(HOOK_EVENTS[agent])) {
      hooks[event] = [...(Array.isArray(hooks[event]) ? (hooks[event] as unknown[]) : []), { hooks: [ourHandler(agent, hook, event)] }];
    }
  }
  const next = { ...root };
  if (Object.keys(hooks).length) next.hooks = hooks;
  else delete next.hooks;
  return next;
}

export function hasOurHooks(root: Record<string, unknown>, agent: SettingsAgent, hook: string): boolean {
  const hooks = root.hooks;
  if (!isObject(hooks)) return false;
  return Object.keys(HOOK_EVENTS[agent]).every((event) => {
    const groups = hooks[event];
    const want = hookCommand(hook, agent, event);
    return Array.isArray(groups) && groups.some((g) => isObject(g) && Array.isArray(g.hooks) && g.hooks.some((h) => isObject(h) && h.command === want));
  });
}

export function copilotHooks(hook: string): Record<string, unknown> {
  return {
    version: 1,
    hooks: Object.fromEntries(
      Object.keys(HOOK_EVENTS.copilot).map((event) => [
        event,
        [{ type: 'command', exec: 'node', args: [hook, 'copilot', event], timeoutSec: HOOK_TIMEOUT_S }],
      ]),
    ),
  };
}

// Antigravity CLI hands the command to cmd.exe with its quotes escaped, so a quoted path reaches node with the
// quotes in it; the path goes in bare and may not hold anything cmd.exe splits or expands.
export function agyHookCommand(hook: string, event: string): string {
  if (/[\s"%^&|<>()!\p{Cc}]/u.test(hook)) throw new Error(`can't put the hook path in an Antigravity CLI hook command: ${hook}`);
  return `node ${hook} agy ${event}`;
}

export function agyHooks(hook: string): Record<string, unknown> {
  const handler = (event: string) => ({ type: 'command', command: agyHookCommand(hook, event), timeout: HOOK_TIMEOUT_S });
  return Object.fromEntries(
    Object.keys(HOOK_EVENTS.agy).map((event) => [event, event === 'PostToolUse' ? [{ matcher: '*', hooks: [handler(event)] }] : [handler(event)]]),
  );
}

export function withAgyHooks(root: Record<string, unknown>, hook: string | undefined): Record<string, unknown> {
  const { [AGY_HOOK_GROUP]: _, ...rest } = root;
  return hook === undefined ? rest : { ...rest, [AGY_HOOK_GROUP]: agyHooks(hook) };
}

export const hasAgyHooks = (root: Record<string, unknown>, hook: string) => JSON.stringify(root[AGY_HOOK_GROUP]) === JSON.stringify(agyHooks(hook));

export function withAgyAllowRule(root: Record<string, unknown>, file: string, allow: boolean): Record<string, unknown> {
  if (!allow && !hasAnyAgyRule(root)) return root;
  const permissions = root.permissions ?? {};
  if (!isObject(permissions)) throw new Error(`${file}: "permissions" isn't an object`);
  const rules = permissions.allow ?? [];
  if (!Array.isArray(rules)) throw new Error(`${file}: "permissions.allow" isn't a list`);
  const kept = rules.filter((r) => r !== AGY_WILDCARD_RULE && !AGY_ALLOW_RULES.includes(r));
  return { ...root, permissions: { ...permissions, allow: allow ? [...kept, ...AGY_ALLOW_RULES] : kept } };
}

function hasAnyAgyRule(root: Record<string, unknown>): boolean {
  const allow = isObject(root.permissions) ? root.permissions.allow : undefined;
  return Array.isArray(allow) && allow.some((r) => r === AGY_WILDCARD_RULE || AGY_ALLOW_RULES.includes(r));
}

export function hasAgyAllowRule(root: Record<string, unknown> | undefined): boolean {
  const allow = isObject(root?.permissions) ? root.permissions.allow : undefined;
  return Array.isArray(allow) && (AGY_ALLOW_RULES.every((r) => allow.includes(r)) || allow.includes(AGY_WILDCARD_RULE));
}

const CODEX_TABLE = /^\[\s*mcp_servers\s*\.\s*(?:ide-agent-tabs|"ide-agent-tabs"|'ide-agent-tabs')\s*\]\s*(?:#.*)?$/;

// Codex passes a stdio MCP server only a fixed set of environment variables; env_vars forwards more by name.
// Its default tool timeout of 60 seconds would cut wait_for_message short.
export function withCodexSettings(text: string, file: string): string | undefined {
  const eol = text.includes('\r\n') ? '\r\n' : '\n';
  const lines = text.split(/\r?\n/);
  const start = lines.findIndex((l) => CODEX_TABLE.test(l.trim()));
  if (start < 0) throw new Error(`${file} has no [mcp_servers.ide-agent-tabs] table`);
  let end = lines.findIndex((l, i) => i > start && /^\s*\[/.test(l));
  if (end < 0) end = lines.length;
  const table = lines.slice(start + 1, end);
  const envVars = table.find((l) => /^\s*env_vars\s*=/.test(l));
  if (envVars !== undefined && !CODEX_ENV_VARS.every((name) => envVars.includes(`"${name}"`))) {
    throw new Error(`${file}: [mcp_servers.ide-agent-tabs] already sets env_vars; add ${CODEX_ENV_VARS.join(', ')} to it by hand`);
  }
  const add = [
    ...(envVars === undefined ? [`env_vars = [${CODEX_ENV_VARS.map((n) => `"${n}"`).join(', ')}]`] : []),
    ...(table.some((l) => /^\s*tool_timeout_sec\s*=/.test(l)) ? [] : [`tool_timeout_sec = ${CODEX_TOOL_TIMEOUT_S}`]),
  ];
  if (add.length === 0) return undefined;
  lines.splice(start + 1, 0, ...add);
  return lines.join(eol);
}

export function hasCodexSettings(text: string | undefined): boolean {
  if (text === undefined) return false;
  try {
    return withCodexSettings(text, '') === undefined;
  } catch {
    return false;
  }
}

const GROK_MATCHERS: Record<string, string> = { Notification: 'permission_prompt|idle_prompt' };

export function grokHooks(hook: string): Record<string, unknown> {
  return {
    hooks: Object.fromEntries(
      Object.keys(HOOK_EVENTS.grok).map((event) => [
        event,
        [{ ...(GROK_MATCHERS[event] ? { matcher: GROK_MATCHERS[event] } : {}), hooks: [{ type: 'command', command: hookCommand(hook, 'grok', event), timeout: HOOK_TIMEOUT_S }] }],
      ]),
    ),
  };
}

// Goose runs a hook command with sh -c and Hermes splits one with shlex.split (shell=False); both take a
// single-quoted POSIX word literally, backslashes included.
export const posixQuote = (s: string) => `'${s.replace(/'/g, "'\\''")}'`;

export function posixHookCommand(hook: string, agent: 'goose' | 'hermes', event: string): string {
  if (/\p{Cc}/u.test(hook)) throw new Error(`can't put the hook path in a hook command: ${hook}`);
  return `node ${posixQuote(hook)} ${agent} ${event}`;
}

export const gooseManifest = () => ({ name: GOOSE_PLUGIN, version: '1.0.0', description: 'Agent Tabs session state and message reminders' });

export function gooseHooks(hook: string): Record<string, unknown> {
  return {
    hooks: Object.fromEntries(
      Object.keys(HOOK_EVENTS.goose).map((event) => [event, [{ hooks: [{ type: 'command', command: posixHookCommand(hook, 'goose', event), timeout: HOOK_TIMEOUT_S }] }]]),
    ),
  };
}

export const hermesHookItems = (hook: string) =>
  Object.keys(HOOK_EVENTS.hermes).map((event) => ({ event, command: posixHookCommand(hook, 'hermes', event), timeout: HOOK_TIMEOUT_S }));

const isOurHermesHook = (item: unknown) => isObject(item) && mentionsHook(item.command);

export function withHermesHooks(doc: Document, file: string, hook: string | undefined): void {
  filterYamlLists(doc, 'hooks', file, isOurHermesHook);
  if (hook === undefined) return;
  const hooks = yamlMap(doc, 'hooks', file, true)!;
  for (const { event, command, timeout } of hermesHookItems(hook)) {
    const list = hooks.get(event, true);
    if (list === undefined || list === null || (isScalar(list) && list.value === null)) hooks.set(event, doc.createNode([{ command, timeout }]));
    else if (isSeq(list)) list.add(doc.createNode({ command, timeout }));
    else throw new Error(`${file}: hooks.${event} isn't a list; edit it by hand`);
  }
}

export function hasHermesHooks(root: Record<string, unknown> | undefined, hook: string): boolean {
  const hooks = root?.hooks;
  if (!isObject(hooks)) return false;
  return hermesHookItems(hook).every(({ event, command }) => Array.isArray(hooks[event]) && hooks[event].some((h) => isObject(h) && h.command === command));
}

// Hermes asks before it first runs each (event, command) pair and skips the hook when nobody can answer; the
// allowlist approves exactly these pairs and nothing else.
export function withHermesApprovals(root: Record<string, unknown>, file: string, hook: string | undefined): Record<string, unknown> {
  const approvals = root.approvals ?? [];
  if (!Array.isArray(approvals)) throw new Error(`${file}: "approvals" isn't a list`);
  const kept = approvals.filter((a) => !(isObject(a) && mentionsHook(a.command)));
  const ours = hook === undefined ? [] : hermesHookItems(hook).map(({ event, command }) => ({ event, command }));
  if (hook === undefined && kept.length === approvals.length) return root;
  return { ...root, approvals: [...kept, ...ours] };
}

export function hasHermesApprovals(root: Record<string, unknown> | undefined, hook: string): boolean {
  const approvals = root?.approvals;
  return Array.isArray(approvals) && hermesHookItems(hook).every(({ event, command }) => approvals.some((a) => isObject(a) && a.event === event && a.command === command));
}

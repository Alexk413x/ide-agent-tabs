import path from 'node:path';
import { JEV_OFF, parseJevSettings, type JevSettings } from './jev/settings.js';

export const DEFAULT_AGENT = 'claude';
export const AGENTS_FILE = 'agents.json';
export const CONFIG_FILE = 'config.json';
export const MAX_PROMPT_CHARS = 30_000;
export const MAX_ENTRIES = 64;
export const PLUGIN_ENV_PREFIX = 'IDE_AGENT_TABS_';
export const STARTUP_ENV = 'JEDITERM_SOURCE';
export const TAB_ID_ENV = `${PLUGIN_ENV_PREFIX}ID`;
export const AGENT_ENV = `${PLUGIN_ENV_PREFIX}AGENT`;
export const ALLOW_RESUME_KEY = 'allowResume';
export const IDE_START_TIMEOUT_KEY = 'ideStartTimeoutSec';
export const DEFAULT_IDE_START_TIMEOUT_SEC = 180;
export const MAX_IDE_START_TIMEOUT_SEC = 3600;

const PROFILE_NAME = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;

export type Env = Record<string, string>;

export interface AgentProfile {
  name: string;
  label: string;
  command: string;
  args: string[];
  promptFlag?: string;
  modelFlag?: string;
  env: Env;
  icon?: string;
}

export interface AgentLaunch {
  agent: string;
  command: string;
  args: string[];
  prompt?: string;
  env: Env;
}

export class ConfigError extends Error {}

function profile(name: string, label: string, command: string, modelFlag: string | undefined, promptFlag?: string, args: readonly string[] = []): AgentProfile {
  return { name, label, command, args: [...args], env: {}, ...(promptFlag ? { promptFlag } : {}), ...(modelFlag ? { modelFlag } : {}) };
}

// Codex's shared daemon runs MCP servers and hooks with its own environment and a stale IDE_AGENT_TABS_ID, so a
// Codex tab runs in-process with its own server and trusted hooks. test/codexTab.test.ts checks the IDE copies and hashes.
// No '"' or '%', and a space in any argument with a cmd.exe metacharacter: PowerShell 5.1 and codex.cmd mangle them.
export const CODEX_TAB_ARGS: readonly string[] = Object.freeze([
  "--no-daemon",
  "-c",
  "mcp_servers.ide-agent-tabs={ command = 'python3', args = ['-I', '-S', '-c', '''import os,runpy;h=os.environ.get('IDE_AGENT_TABS_HOME') or os.path.join(os.path.expanduser('~'),'.ide-agent-tabs');runpy.run_path(os.path.join(h,'mcp','py','launch','mcp_server.py'),run_name='__main__')'''], env_vars = ['IDE_AGENT_TABS_ID', 'IDE_AGENT_TABS_AGENT', 'IDE_AGENT_TABS_HOME'], tool_timeout_sec = 660 }",
  "-c",
  "hooks.UserPromptSubmit=[{ hooks = [{ type = 'mcp_tool', server = 'ide-agent-tabs', tool = 'agent_tabs_hook', input = { event = 'UserPromptSubmit', session_id = '${session_id}', turn_id = '${turn_id}' }, timeout = 10 }] }]",
  "-c",
  "hooks.PostToolUse=[{ hooks = [{ type = 'mcp_tool', server = 'ide-agent-tabs', tool = 'agent_tabs_hook', input = { event = 'PostToolUse', session_id = '${session_id}', turn_id = '${turn_id}' }, timeout = 10 }] }]",
  "-c",
  "hooks.PermissionRequest=[{ hooks = [{ type = 'mcp_tool', server = 'ide-agent-tabs', tool = 'agent_tabs_hook', input = { event = 'PermissionRequest', session_id = '${session_id}', turn_id = '${turn_id}' }, timeout = 10 }] }]",
  "-c",
  "hooks.Stop=[{ hooks = [{ type = 'mcp_tool', server = 'ide-agent-tabs', tool = 'agent_tabs_hook', input = { event = 'Stop', session_id = '${session_id}', turn_id = '${turn_id}' }, timeout = 10 }] }]",
  "-c",
  "hooks.Interrupt=[{ hooks = [{ type = 'mcp_tool', server = 'ide-agent-tabs', tool = 'agent_tabs_hook', input = { event = 'Interrupt', session_id = '${session_id}', turn_id = '${turn_id}' }, timeout = 3 }] }]",
  "-c",
  "hooks.state={ '/<session-flags>/config.toml:user_prompt_submit:0:0' = { trusted_hash = 'sha256:aac36b4c0cfafe0f4ae641176bcc1ab25ae590dbe3be9268f7570b55ab4afe89' }, 'C:\\<session-flags>\\config.toml:user_prompt_submit:0:0' = { trusted_hash = 'sha256:aac36b4c0cfafe0f4ae641176bcc1ab25ae590dbe3be9268f7570b55ab4afe89' }, '/<session-flags>/config.toml:post_tool_use:0:0' = { trusted_hash = 'sha256:75aa06c6f44c8918fe729537b56d5f498c931e032f5d89499593b4cd67ba335e' }, 'C:\\<session-flags>\\config.toml:post_tool_use:0:0' = { trusted_hash = 'sha256:75aa06c6f44c8918fe729537b56d5f498c931e032f5d89499593b4cd67ba335e' }, '/<session-flags>/config.toml:permission_request:0:0' = { trusted_hash = 'sha256:5e1483151807db1577272adc730b9ffe56c96d22b1a8fe7c7ff6d6efe42f3626' }, 'C:\\<session-flags>\\config.toml:permission_request:0:0' = { trusted_hash = 'sha256:5e1483151807db1577272adc730b9ffe56c96d22b1a8fe7c7ff6d6efe42f3626' }, '/<session-flags>/config.toml:stop:0:0' = { trusted_hash = 'sha256:a97c883d6b41f88f6879ce99d0d343a7069f3fded56573aaa2b15fc5bbd01c6f' }, 'C:\\<session-flags>\\config.toml:stop:0:0' = { trusted_hash = 'sha256:a97c883d6b41f88f6879ce99d0d343a7069f3fded56573aaa2b15fc5bbd01c6f' }, '/<session-flags>/config.toml:interrupt:0:0' = { trusted_hash = 'sha256:c2704217d5db401ed47f178ff9db1a7be09662f73b3e55753f8600e42bd53165' }, 'C:\\<session-flags>\\config.toml:interrupt:0:0' = { trusted_hash = 'sha256:c2704217d5db401ed47f178ff9db1a7be09662f73b3e55753f8600e42bd53165' } }",
]);

// goose run -s takes the first message from -t and stays interactive, but refuses to start without one.
export const GOOSE_RUN_ARGS: readonly string[] = Object.freeze(['run', '-s']);
export const GOOSE_EMPTY_ARGS: readonly string[] = Object.freeze(['session']);

export const BUILTIN_PROFILES: readonly AgentProfile[] = Object.freeze([
  profile('claude', 'Claude Code', 'claude', '--model'),
  profile('codex', 'Codex', 'codex', '-m', undefined, CODEX_TAB_ARGS),
  profile('agy', 'Antigravity CLI', 'agy', '--model', '-i'),
  profile('copilot', 'Copilot CLI', 'copilot', '--model', '-i'),
  profile('gemini', 'Gemini CLI', 'gemini', '-m', '-i'),
  profile('grok', 'Grok Build', 'grok', '-m'),
  profile('pi', 'Pi', 'pi', '--model'),
  profile('hermes', 'Hermes', 'hermes', '-m', '-q', ['chat']),
  profile('opencode', 'OpenCode', 'opencode', '-m', '--prompt'),
  // A positional prompt makes Qwen Code answer once and exit.
  profile('qwen', 'Qwen Code', 'qwen', '-m', '-i'),
  profile('goose', 'Goose', 'goose', '--model', '-t', GOOSE_RUN_ARGS),
  // Without --local-provider, --oss stops at a picker between LM Studio and Ollama.
  profile('codex-local', 'Codex (local)', 'codex', '-m', undefined, [...CODEX_TAB_ARGS, '--oss', '--local-provider', 'ollama']),
]);

export function launchOf(p: AgentProfile, prompt?: string, callerArgs: string[] = [], callerEnv: Env = {}): AgentLaunch {
  const flag = p.promptFlag !== undefined && prompt !== undefined ? [p.promptFlag] : [];
  return {
    agent: p.name,
    command: p.command,
    args: [...p.args, ...callerArgs, ...flag],
    ...(prompt !== undefined ? { prompt } : {}),
    env: Object.fromEntries([...Object.entries(p.env), ...Object.entries(callerEnv)]),
  };
}

export function isReservedEnv(name: string): boolean {
  const upper = name.toUpperCase();
  return upper.startsWith(PLUGIN_ENV_PREFIX) || upper.startsWith(STARTUP_ENV);
}

const isBlank = (s: string) => s.trim() === '';

export function checkEnv(env: Env, field: string): void {
  const entries = Object.entries(env);
  if (entries.length > MAX_ENTRIES) throw new ConfigError(`${field} exceeds ${MAX_ENTRIES} entries`);
  for (const [name, value] of entries) {
    if (isBlank(name) || /[=\s\0]/u.test(name)) {
      throw new ConfigError(`${field} name is not a valid variable name: '${name}'`);
    }
    if (isReservedEnv(name)) throw new ConfigError(`${field} name ${name} is reserved by the plugin`);
    if (value.length > MAX_PROMPT_CHARS || value.includes('\0')) {
      throw new ConfigError(`${field} ${name} is longer than ${MAX_PROMPT_CHARS} characters or holds a NUL`);
    }
  }
}

function parseJsonObject(text: string, file: string): Record<string, unknown> {
  let json: unknown;
  try {
    json = JSON.parse(text);
  } catch (e) {
    throw new ConfigError(`${file} is not JSON: ${(e as Error).message}`);
  }
  if (!isObject(json)) throw new ConfigError(`${file} must hold a JSON object`);
  return json;
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function field(obj: Record<string, unknown>, key: string): unknown {
  return Object.hasOwn(obj, key) ? obj[key] : undefined;
}

function optString(obj: Record<string, unknown>, name: string, key: string): string | undefined {
  const value = field(obj, key);
  if (value === undefined || value === null) return undefined;
  if (typeof value !== 'string') throw new ConfigError(`${name} must be a string`);
  return value;
}

function optStringList(obj: Record<string, unknown>, name: string, key: string): string[] {
  const value = field(obj, key);
  if (value === undefined || value === null) return [];
  if (!Array.isArray(value) || value.some((v) => typeof v !== 'string')) {
    throw new ConfigError(`${name} must be an array of strings`);
  }
  return value as string[];
}

function optStringMap(obj: Record<string, unknown>, name: string, key: string): Env {
  const value = field(obj, key);
  if (value === undefined || value === null) return {};
  if (!isObject(value)) throw new ConfigError(`${name} must be an object of strings`);
  return Object.fromEntries(
    Object.entries(value).map(([k, v]) => {
      if (typeof v !== 'string') throw new ConfigError(`${name}.${k} must be a string`);
      return [k, v];
    }),
  );
}

export function parseProfiles(text: string): AgentProfile[] {
  const root = parseJsonObject(text, AGENTS_FILE);
  return Object.entries(root).map(([name, value]) => {
    if (!PROFILE_NAME.test(name)) throw new ConfigError(`profile name '${name}' must be letters, digits, '.', '_' or '-'`);
    if (!isObject(value)) throw new ConfigError(`profile ${name} must be an object`);
    const command = optString(value, `${name}.command`, 'command');
    if (command === undefined || isBlank(command) || command.includes('\0')) {
      throw new ConfigError(`profile ${name} needs a command`);
    }
    const args = optStringList(value, `${name}.args`, 'args');
    if (args.length > MAX_ENTRIES) throw new ConfigError(`${name}.args exceeds ${MAX_ENTRIES} entries`);
    if (args.some((a) => a.includes('\0'))) throw new ConfigError(`${name}.args holds a NUL`);
    const promptFlag = optString(value, `${name}.promptFlag`, 'promptFlag');
    if (promptFlag !== undefined && (isBlank(promptFlag) || promptFlag.includes('\0'))) {
      throw new ConfigError(`${name}.promptFlag must not be blank`);
    }
    const modelFlag = optString(value, `${name}.modelFlag`, 'modelFlag');
    if (modelFlag !== undefined && (isBlank(modelFlag) || modelFlag.includes('\0'))) {
      throw new ConfigError(`${name}.modelFlag must not be blank`);
    }
    const env = optStringMap(value, `${name}.env`, 'env');
    checkEnv(env, `${name}.env`);
    const label = optString(value, `${name}.label`, 'label');
    const icon = optString(value, `${name}.icon`, 'icon');
    return {
      name,
      label: label !== undefined && !isBlank(label) ? label : name,
      command,
      args,
      ...(promptFlag !== undefined ? { promptFlag } : {}),
      ...(modelFlag !== undefined ? { modelFlag } : {}),
      env,
      ...(icon !== undefined && !isBlank(icon) ? { icon } : {}),
    };
  });
}

export function mergeProfiles(builtins: readonly AgentProfile[], custom: AgentProfile[]): AgentProfile[] {
  const byName = new Map(custom.map((c) => [c.name, c]));
  return [
    ...builtins.map((b) => byName.get(b.name) ?? b),
    ...custom.filter((c) => !builtins.some((b) => b.name === c.name)),
  ];
}

export function readDefaultAgent(text: string): string | undefined {
  const value = field(parseJsonObject(text, CONFIG_FILE), 'defaultAgent');
  return typeof value === 'string' ? value : undefined;
}

export function readJevSettings(text: string): JevSettings {
  return parseJevSettings(field(parseJsonObject(text, CONFIG_FILE), 'jev'));
}

export type TabRouting = 'project' | 'caller';
export type TerminalWindow = 'last' | 'dedicated';
export type FocusNewTabs = 'auto' | 'always' | 'never';
export type ClaudeMod = 'on' | 'off';
const AUTO = 'auto';

export interface TerminalSettings {
  tabRouting: TabRouting;
  preferredTerminal?: string;
  shell?: string;
  terminalWindow: TerminalWindow;
  launchVia: 'direct' | 'ori';
  focusNewTabs: FocusNewTabs;
  claudeMod: ClaudeMod;
  allowResume: boolean;
  ideStartTimeoutSec: number;
}

export function resolveFocus(setting: FocusNewTabs, requested: boolean | undefined): boolean {
  if (setting === 'auto') return requested ?? false;
  return setting === 'always';
}

function flag(config: Record<string, unknown>, key: string, fallback: boolean, warnings: string[]): boolean {
  const value = field(config, key);
  if (value === undefined || value === null) return fallback;
  if (typeof value === 'boolean') return value;
  warnings.push(`Ignoring ${key} in ${CONFIG_FILE}: it must be true or false`);
  return fallback;
}

function seconds(config: Record<string, unknown>, key: string, fallback: number, max: number, warnings: string[]): number {
  const value = field(config, key);
  if (value === undefined || value === null) return fallback;
  if (typeof value === 'number' && Number.isFinite(value) && value > 0 && value <= max) return value;
  warnings.push(`Ignoring ${key} in ${CONFIG_FILE}: it must be a number of seconds above 0 and at most ${max}`);
  return fallback;
}

function choice<T extends string>(config: Record<string, unknown>, key: string, values: readonly T[], warnings: string[]): T {
  const value = field(config, key);
  if (value === undefined || value === null) return values[0]!;
  if (typeof value === 'string' && (values as readonly string[]).includes(value)) return value as T;
  warnings.push(`Ignoring ${key} in ${CONFIG_FILE}: it must be ${values.map((v) => `"${v}"`).join(' or ')}`);
  return values[0]!;
}

export function readTerminalSettings(config: Record<string, unknown>, warnings: string[]): TerminalSettings {
  const tabRouting = choice(config, 'tabRouting', ['project', 'caller'] as const, warnings);
  const terminalWindow = choice(config, 'terminalWindow', ['last', 'dedicated'] as const, warnings);
  const launchVia = choice(config, 'launchVia', ['direct', 'ori'] as const, warnings);
  const focusNewTabs = choice(config, 'focusNewTabs', ['auto', 'always', 'never'] as const, warnings);
  const claudeMod = choice(config, 'claudeMod', ['on', 'off'] as const, warnings);
  const allowResume = flag(config, ALLOW_RESUME_KEY, true, warnings);
  const ideStartTimeoutSec = seconds(config, IDE_START_TIMEOUT_KEY, DEFAULT_IDE_START_TIMEOUT_SEC, MAX_IDE_START_TIMEOUT_SEC, warnings);
  let preferredTerminal: string | undefined;
  const terminal = field(config, 'terminal');
  if (typeof terminal === 'string') preferredTerminal = !isBlank(terminal) && terminal !== AUTO ? terminal : undefined;
  else if (terminal !== undefined && terminal !== null) warnings.push(`Ignoring terminal in ${CONFIG_FILE}: it must be a string`);
  let shell: string | undefined;
  const shellValue = field(config, 'shell');
  if (typeof shellValue === 'string' && (isBlank(shellValue) || shellValue === AUTO)) shell = undefined;
  else if (typeof shellValue === 'string' && !shellValue.includes('\0') && (path.win32.isAbsolute(shellValue) || path.posix.isAbsolute(shellValue))) {
    shell = shellValue;
  } else if (shellValue !== undefined && shellValue !== null) {
    warnings.push(`Ignoring shell in ${CONFIG_FILE}: it must be "auto" or the absolute path of a shell executable`);
  }
  return { tabRouting, terminalWindow, launchVia, focusNewTabs, claudeMod, allowResume, ideStartTimeoutSec, ...(preferredTerminal ? { preferredTerminal } : {}), ...(shell ? { shell } : {}) };
}

export interface AgentSettings extends TerminalSettings {
  profiles: AgentProfile[];
  defaultAgent: AgentProfile;
  jev: JevSettings;
  warnings: string[];
}
export function resolveSettings(
  agentsText: string | undefined,
  configText: string | undefined,
  agentsPath = AGENTS_FILE,
  configPath = CONFIG_FILE,
): AgentSettings {
  const warnings: string[] = [];
  let profiles: AgentProfile[] = [...BUILTIN_PROFILES];
  if (agentsText !== undefined) {
    try {
      profiles = mergeProfiles(BUILTIN_PROFILES, parseProfiles(agentsText));
    } catch (e) {
      warnings.push(`Ignoring ${agentsPath} and using the built-in agent profiles: ${(e as Error).message}`);
    }
  }
  let configured: string | undefined;
  let terminal: TerminalSettings = { tabRouting: 'project', terminalWindow: 'last', launchVia: 'direct', focusNewTabs: 'auto', claudeMod: 'on', allowResume: true, ideStartTimeoutSec: DEFAULT_IDE_START_TIMEOUT_SEC };
  let jev = JEV_OFF;
  if (configText !== undefined) {
    let readable = true;
    try {
      configured = readDefaultAgent(configText);
      terminal = readTerminalSettings(parseJsonObject(configText, CONFIG_FILE), warnings);
    } catch (e) {
      readable = false;
      warnings.push(`Ignoring ${configPath}: ${(e as Error).message}`);
    }
    if (readable) {
      try {
        jev = readJevSettings(configText);
      } catch (e) {
        warnings.push(`Ignoring jev in ${configPath}, so Jev is off: ${(e as Error).message}`);
      }
    }
  }
  const defaultAgent =
    profiles.find((p) => p.name === configured) ?? profiles.find((p) => p.name === DEFAULT_AGENT)!;
  return { profiles, defaultAgent, ...terminal, jev, warnings };
}

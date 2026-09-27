export const DEFAULT_AGENT = 'claude';
export const AGENTS_FILE = 'agents.json';
export const CONFIG_FILE = 'config.json';
export const MAX_PROMPT_CHARS = 30_000;
export const MAX_ENTRIES = 64;
export const PLUGIN_ENV_PREFIX = 'IDE_AGENT_TABS_';
export const STARTUP_ENV = 'JEDITERM_SOURCE';
export const TAB_ID_ENV = `${PLUGIN_ENV_PREFIX}ID`;
export const AGENT_ENV = `${PLUGIN_ENV_PREFIX}AGENT`;

const PROFILE_NAME = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;

export type Env = Record<string, string>;

export interface AgentProfile {
  name: string;
  label: string;
  command: string;
  args: string[];
  promptFlag?: string;
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

function profile(name: string, label: string, command: string, promptFlag?: string): AgentProfile {
  return { name, label, command, args: [], env: {}, ...(promptFlag ? { promptFlag } : {}) };
}

export const BUILTIN_PROFILES: readonly AgentProfile[] = Object.freeze([
  profile('claude', 'Claude Code', 'claude'),
  profile('codex', 'Codex', 'codex'),
  profile('gemini', 'Gemini CLI', 'gemini', '-i'),
  profile('copilot', 'Copilot CLI', 'copilot', '-i'),
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

export function readPreferredTerminal(text: string): string | undefined {
  const value = field(parseJsonObject(text, CONFIG_FILE), 'terminal');
  return typeof value === 'string' && !isBlank(value) ? value : undefined;
}

export interface AgentSettings {
  profiles: AgentProfile[];
  defaultAgent: AgentProfile;
  preferredTerminal?: string;
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
  let preferredTerminal: string | undefined;
  if (configText !== undefined) {
    try {
      configured = readDefaultAgent(configText);
      preferredTerminal = readPreferredTerminal(configText);
    } catch (e) {
      warnings.push(`Ignoring ${configPath}: ${(e as Error).message}`);
    }
  }
  const defaultAgent =
    profiles.find((p) => p.name === configured) ?? profiles.find((p) => p.name === DEFAULT_AGENT)!;
  return { profiles, defaultAgent, ...(preferredTerminal ? { preferredTerminal } : {}), warnings };
}

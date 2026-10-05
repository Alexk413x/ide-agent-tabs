import * as fs from 'node:fs';
import * as path from 'node:path';
import { BadRequest, checkEnv, isAbsolutePath, MAX_ENTRIES, optString, optStringList, optStringMap, parseObject } from './request';
import { writeAtomically } from './registry';
import {
  Detected,
  DETECTED_FILE,
  DetectedOri,
  LaunchVia,
  parseDetected,
  readSharedSettings,
  SHARED_DEFAULTS,
  SharedSettings,
  withSharedValue,
} from './sharedSettings';

export const DEFAULT_AGENT = 'claude';
export const AGENTS_FILE = 'agents.json';
export const CONFIG_FILE = 'config.json';

const PROFILE_NAME = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;
const WINDOWS_EXTENSIONS = ['.exe', '.cmd', '.bat', '.ps1'];

export interface AgentProfile {
  name: string;
  label: string;
  command: string;
  args: string[];
  promptFlag?: string;
  modelFlag?: string;
  env: Record<string, string>;
  icon?: string;
}

export interface AgentLaunch {
  agent: string;
  command: string;
  args: string[];
  prompt?: string;
  env: Record<string, string>;
  via?: LaunchVia;
}

export function profile(name: string, label: string, command: string, extra: Partial<AgentProfile> = {}): AgentProfile {
  return { name, label, command, args: [], env: {}, ...extra };
}

export function launchOf(
  p: AgentProfile,
  prompt?: string,
  callerArgs: string[] = [],
  callerEnv: Record<string, string> = {},
  model?: string,
): AgentLaunch {
  const flag = prompt !== undefined && p.promptFlag !== undefined ? [p.promptFlag] : [];
  const modelArgs = model !== undefined && p.modelFlag !== undefined ? [p.modelFlag, model] : [];
  return {
    agent: p.name,
    command: p.command,
    args: [...p.args, ...modelArgs, ...callerArgs, ...flag],
    prompt,
    env: { ...p.env, ...callerEnv },
    via: 'direct',
  };
}

export const ORI_COMMAND = 'ori';
export const ORI_PROFILES: readonly string[] = ['claude', 'codex', 'grok', 'hermes', 'opencode', 'pi', 'prime-agent'];
const ORI_CMD_UNSAFE = /[|"%^&<>]/;
const SHIM_EXTENSIONS = ['.exe', '.cmd', '.bat'];

export interface LaunchContext {
  prompt?: string;
  args?: string[];
  env?: Record<string, string>;
  model?: string;
  via?: LaunchVia;
  setting: LaunchVia;
  ori: DetectedOri | null;
  windows: boolean;
  searchPath: string;
}

function isCmdShim(command: string, searchPath: string): boolean {
  const lower = command.toLowerCase();
  if (/\.(cmd|bat)$/.test(lower)) return true;
  if (/\.(exe|com|ps1)$/.test(lower)) return false;
  for (const raw of searchPath.split(path.delimiter)) {
    const dir = raw.trim().replace(/^"+|"+$/g, '');
    if (dir === '') continue;
    const hit = SHIM_EXTENSIONS.find(ext => exists(path.join(dir, command + ext)));
    if (hit !== undefined) return hit !== '.exe';
  }
  return true;
}

function oriRefusal(p: AgentProfile, ctx: LaunchContext, oriArgs: string[]): string | undefined {
  if (ctx.ori === null) return 'Ori is not installed';
  if (!ORI_PROFILES.includes(p.name)) return `Ori does not support ${p.name}`;
  if (!ctx.ori.agents.includes(p.name)) return `Ori does not list ${p.name} as launchable`;
  if (ctx.windows && isCmdShim(p.command, ctx.searchPath) && oriArgs.some(a => ORI_CMD_UNSAFE.test(a))) {
    return 'Ori refuses an argument with | " % ^ & < or > when the agent is a .cmd shim on Windows';
  }
  return undefined;
}

const GOOSE_RUN_ARGS: readonly string[] = ['run', '-s'];
const GOOSE_EMPTY_ARGS: readonly string[] = ['session'];

function withoutPrompt(p: AgentProfile, prompt: string | undefined): AgentProfile {
  const builtinGoose = p.command === 'goose' && p.args.length === GOOSE_RUN_ARGS.length && p.args.every((a, i) => a === GOOSE_RUN_ARGS[i]);
  return prompt === undefined && builtinGoose ? { ...p, args: [...GOOSE_EMPTY_ARGS] } : p;
}

export function planLaunch(profile: AgentProfile, ctx: LaunchContext): AgentLaunch {
  const p = withoutPrompt(profile, ctx.prompt);
  const callerArgs = ctx.args ?? [];
  const callerEnv = ctx.env ?? {};
  if ((ctx.via ?? ctx.setting) === 'ori') {
    const flag = ctx.prompt !== undefined && p.promptFlag !== undefined ? [p.promptFlag] : [];
    const args = [p.name, ...(ctx.model !== undefined ? ['--model', ctx.model] : []), ...p.args, ...callerArgs, ...flag];
    const refusal = oriRefusal(p, ctx, ctx.prompt !== undefined ? [...args, ctx.prompt] : args);
    if (refusal === undefined) {
      return { agent: p.name, command: ORI_COMMAND, args, prompt: ctx.prompt, env: { ...p.env, ...callerEnv }, via: 'ori' };
    }
    if (ctx.via === 'ori') throw new BadRequest(`${p.name} can't launch through Ori: ${refusal}`);
  }
  if (ctx.model !== undefined && p.modelFlag === undefined) {
    throw new BadRequest(`${p.name} has no model option; open it without model, or set modelFlag for it in agents.json`);
  }
  return launchOf(p, ctx.prompt, callerArgs, callerEnv, ctx.model);
}

// Same strings as CODEX_TAB_ARGS in mcp/src/profiles.ts, which explains them; mcp/test/codexTab.test.ts checks both.
export const CODEX_TAB_ARGS: readonly string[] = Object.freeze([
  "--no-daemon",
  "-c",
  "mcp_servers.ide-agent-tabs={ command = 'node', args = ['-e', 'const p=require(`node:path`);import(require(`node:url`).pathToFileURL(p.join(process.env.IDE_AGENT_TABS_HOME||p.join(require(`node:os`).homedir(),`.ide-agent-tabs`),`mcp`,`mcp-server.mjs`)).href)'], env_vars = ['IDE_AGENT_TABS_ID', 'IDE_AGENT_TABS_AGENT', 'IDE_AGENT_TABS_HOME'], tool_timeout_sec = 660 }",
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

export const BUILTIN_PROFILES: readonly AgentProfile[] = Object.freeze([
  profile('claude', 'Claude Code', 'claude', { modelFlag: '--model' }),
  profile('codex', 'Codex', 'codex', { args: [...CODEX_TAB_ARGS], modelFlag: '-m' }),
  profile('agy', 'Antigravity CLI', 'agy', { promptFlag: '-i', modelFlag: '--model' }),
  profile('copilot', 'Copilot CLI', 'copilot', { promptFlag: '-i', modelFlag: '--model' }),
  profile('gemini', 'Gemini CLI', 'gemini', { promptFlag: '-i', modelFlag: '-m' }),
  profile('grok', 'Grok Build', 'grok', { modelFlag: '-m' }),
  profile('pi', 'Pi', 'pi', { modelFlag: '--model' }),
  profile('hermes', 'Hermes', 'hermes', { args: ['chat'], promptFlag: '-q', modelFlag: '-m' }),
  profile('opencode', 'OpenCode', 'opencode', { promptFlag: '--prompt', modelFlag: '-m' }),
  profile('qwen', 'Qwen Code', 'qwen', { promptFlag: '-i', modelFlag: '-m' }),
  profile('goose', 'Goose', 'goose', { args: ['run', '-s'], promptFlag: '-t', modelFlag: '--model' }),
  // Without --local-provider, --oss stops at a picker between LM Studio and Ollama.
  profile('codex-local', 'Codex (local)', 'codex', { args: [...CODEX_TAB_ARGS, '--oss', '--local-provider', 'ollama'], modelFlag: '-m' }),
]);

export function parseProfiles(text: string): AgentProfile[] {
  const root = parseObject(text, AGENTS_FILE);
  return Object.entries(root).map(([name, value]) => {
    if (!PROFILE_NAME.test(name)) throw new BadRequest(`profile name '${name}' must be letters, digits, '.', '_' or '-'`);
    if (value === null || typeof value !== 'object' || Array.isArray(value)) throw new BadRequest(`profile ${name} must be an object`);
    const obj = value as Record<string, unknown>;
    const command = optString(obj, 'command', `${name}.command`);
    if (command === undefined || command.trim() === '' || command.includes('\0')) throw new BadRequest(`profile ${name} needs a command`);
    const args = optStringList(obj, 'args', `${name}.args`);
    if (args.length > MAX_ENTRIES) throw new BadRequest(`${name}.args exceeds ${MAX_ENTRIES} entries`);
    if (args.some(a => a.includes('\0'))) throw new BadRequest(`${name}.args holds a NUL`);
    const promptFlag = optString(obj, 'promptFlag', `${name}.promptFlag`);
    if (promptFlag !== undefined && (promptFlag.trim() === '' || promptFlag.includes('\0'))) {
      throw new BadRequest(`${name}.promptFlag must not be blank`);
    }
    const modelFlag = optString(obj, 'modelFlag', `${name}.modelFlag`);
    if (modelFlag !== undefined && (modelFlag.trim() === '' || modelFlag.includes('\0'))) {
      throw new BadRequest(`${name}.modelFlag must not be blank`);
    }
    const env = optStringMap(obj, 'env', `${name}.env`);
    checkEnv(env, `${name}.env`);
    const label = optString(obj, 'label', `${name}.label`);
    const icon = optString(obj, 'icon', `${name}.icon`);
    return {
      name,
      label: label !== undefined && label.trim() !== '' ? label : name,
      command,
      args,
      promptFlag,
      modelFlag,
      env,
      icon: icon !== undefined && icon.trim() !== '' ? icon : undefined,
    };
  });
}

export function mergeProfiles(builtins: readonly AgentProfile[], custom: AgentProfile[]): AgentProfile[] {
  const byName = new Map(custom.map(c => [c.name, c]));
  const builtinNames = new Set(builtins.map(b => b.name));
  return [...builtins.map(b => byName.get(b.name) ?? b), ...custom.filter(c => !builtinNames.has(c.name))];
}

export function readDefaultAgent(text: string): string | undefined {
  const value = parseObject(text, CONFIG_FILE).defaultAgent;
  return typeof value === 'string' ? value : undefined;
}

export function withDefaultAgent(existing: string | undefined, name: string): string {
  const root = existing === undefined || existing.trim() === '' ? {} : parseObject(existing, CONFIG_FILE);
  root.defaultAgent = name;
  return JSON.stringify(root, null, 2) + '\n';
}

type Stamp = string | undefined;

function stamp(file: string): Stamp {
  try {
    const s = fs.statSync(file);
    return `${s.mtimeMs}:${s.size}`;
  } catch {
    return undefined;
  }
}

export class AgentSettings {
  private profilesCache?: { stamp: Stamp; value: AgentProfile[] };
  private defaultCache?: { stamp: Stamp; value: string | undefined };

  constructor(
    readonly home: string,
    private readonly warn: (message: string) => void,
    private readonly preferred: () => string | undefined = () => undefined,
  ) {}

  private get agentsFile() {
    return path.join(this.home, AGENTS_FILE);
  }

  private get configFile() {
    return path.join(this.home, CONFIG_FILE);
  }

  profiles(): AgentProfile[] {
    const current = stamp(this.agentsFile);
    if (this.profilesCache && this.profilesCache.stamp === current) return this.profilesCache.value;
    let value: AgentProfile[] = [...BUILTIN_PROFILES];
    if (current !== undefined) {
      try {
        value = mergeProfiles(BUILTIN_PROFILES, parseProfiles(fs.readFileSync(this.agentsFile, 'utf8')));
      } catch (e) {
        this.warn(`Ignoring ${this.agentsFile} and using the built-in agent profiles: ${(e as Error).message}`);
      }
    }
    this.profilesCache = { stamp: current, value };
    return value;
  }

  profile(name: string): AgentProfile | undefined {
    return this.profiles().find(p => p.name === name);
  }

  defaultProfile(): AgentProfile {
    const all = this.profiles();
    const preferred = this.preferred();
    const configured = this.sharedDefault();
    return all.find(p => p.name === preferred) ?? all.find(p => p.name === configured) ?? all.find(p => p.name === DEFAULT_AGENT)!;
  }

  setDefaultAgent(name: string): void {
    try {
      let existing: string | undefined;
      try {
        existing = fs.readFileSync(this.configFile, 'utf8');
      } catch {
        existing = undefined;
      }
      writeAtomically(this.configFile, withDefaultAgent(existing, name));
    } catch (e) {
      this.warn(`Could not save the default agent to ${this.configFile}: ${(e as Error).message}`);
    }
  }

  setShared(key: keyof SharedSettings, value: string | boolean): boolean {
    try {
      let existing: string | undefined;
      try {
        existing = fs.readFileSync(this.configFile, 'utf8');
      } catch {
        existing = undefined;
      }
      writeAtomically(this.configFile, withSharedValue(existing, CONFIG_FILE, key, value));
      return true;
    } catch (e) {
      this.warn(`Could not save ${key} to ${this.configFile}: ${(e as Error).message}`);
      return false;
    }
  }

  sharedFound(): Partial<SharedSettings> | undefined {
    let text: string;
    try {
      text = fs.readFileSync(this.configFile, 'utf8');
    } catch {
      return {};
    }
    try {
      return readSharedSettings(text, CONFIG_FILE);
    } catch (e) {
      this.warn(`Ignoring ${this.configFile}: ${(e as Error).message}`);
      return undefined;
    }
  }

  shared(): SharedSettings {
    return { ...SHARED_DEFAULTS, ...this.sharedFound() };
  }

  detected(): Detected {
    const file = path.join(this.home, DETECTED_FILE);
    try {
      return parseDetected(fs.readFileSync(file, 'utf8'));
    } catch {
      return { terminals: [], shells: [], ori: null };
    }
  }

  sharedDefault(): string | undefined {
    const current = stamp(this.configFile);
    if (this.defaultCache && this.defaultCache.stamp === current) return this.defaultCache.value;
    let value: string | undefined;
    if (current !== undefined) {
      try {
        value = readDefaultAgent(fs.readFileSync(this.configFile, 'utf8'));
      } catch (e) {
        this.warn(`Ignoring ${this.configFile}: ${(e as Error).message}`);
      }
    }
    this.defaultCache = { stamp: current, value };
    return value;
  }
}

// lstat, not stat or existsSync: the Microsoft Store pwsh.exe under WindowsApps is an app execution alias
// that stat cannot follow (EACCES), so a following check misses it and the tab falls back to PowerShell 5.1.
function exists(file: string): boolean {
  try {
    fs.lstatSync(file);
    return true;
  } catch {
    return false;
  }
}

export function findOnPath(searchPath: string, executable: string): string | undefined {
  for (const raw of searchPath.split(path.delimiter)) {
    const dir = raw.trim().replace(/^"+|"+$/g, '');
    if (dir === '') continue;
    const candidate = path.join(dir, executable);
    if (exists(candidate)) return candidate;
  }
  return undefined;
}

export function isInstalled(command: string, searchPath: string, isWindows: boolean): boolean {
  const names = isWindows ? [command, ...WINDOWS_EXTENSIONS.map(ext => command + ext)] : [command];
  if (isAbsolutePath(command)) return names.some(exists);
  if (command.includes('/') || command.includes(path.sep)) return false;
  return names.some(name => findOnPath(searchPath, name) !== undefined);
}

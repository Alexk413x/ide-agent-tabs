import * as fs from 'node:fs';
import * as path from 'node:path';
import { BadRequest, checkEnv, isAbsolutePath, MAX_ENTRIES, optString, optStringList, optStringMap, parseObject } from './request';
import { writeAtomically } from './registry';

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
  env: Record<string, string>;
  icon?: string;
}

export interface AgentLaunch {
  agent: string;
  command: string;
  args: string[];
  prompt?: string;
  env: Record<string, string>;
}

export function profile(name: string, label: string, command: string, extra: Partial<AgentProfile> = {}): AgentProfile {
  return { name, label, command, args: [], env: {}, ...extra };
}

export function launchOf(p: AgentProfile, prompt?: string, callerArgs: string[] = [], callerEnv: Record<string, string> = {}): AgentLaunch {
  const flag = prompt !== undefined && p.promptFlag !== undefined ? [p.promptFlag] : [];
  return {
    agent: p.name,
    command: p.command,
    args: [...p.args, ...callerArgs, ...flag],
    prompt,
    env: { ...p.env, ...callerEnv },
  };
}

export const BUILTIN_PROFILES: readonly AgentProfile[] = Object.freeze([
  profile('claude', 'Claude Code', 'claude'),
  profile('codex', 'Codex', 'codex'),
  profile('gemini', 'Gemini CLI', 'gemini', { promptFlag: '-i' }),
  profile('copilot', 'Copilot CLI', 'copilot', { promptFlag: '-i' }),
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
    const configured = this.configuredDefault();
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

  private configuredDefault(): string | undefined {
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

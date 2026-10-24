import * as fs from 'node:fs';
import * as path from 'node:path';
import { readText } from './files';
import { AGENTS_FILE, AgentProfile, BUILTIN_PROFILES, findProfile, mergeProfiles, parseProfiles } from './profiles';
import { writeAtomically } from './registry';
import { parseObject } from './request';
import {
  CONFIG_FILE,
  Detected,
  DETECTED_FILE,
  editConfig,
  parseDetected,
  readSharedSettings,
  SHARED_DEFAULTS,
  SharedSettings,
  SyncedSetting,
  withSharedValue,
} from './sharedSettings';

const DEFAULT_AGENT = 'claude';

export function readDefaultAgent(text: string): string | undefined {
  const value = parseObject(text, CONFIG_FILE).defaultAgent;
  return typeof value === 'string' ? value : undefined;
}

function withDefaultAgent(existing: string | undefined, name: string): string {
  return editConfig(existing, root => {
    root.defaultAgent = name;
  });
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

class StampCache<T> {
  private entry?: { stamp: Stamp; value: T };

  constructor(private readonly read: (stamp: Stamp) => T) {}

  get(file: string): T {
    const current = stamp(file);
    if (this.entry && this.entry.stamp === current) return this.entry.value;
    const value = this.read(current);
    this.entry = { stamp: current, value };
    return value;
  }
}

const isMissing = (e: unknown) => (e as NodeJS.ErrnoException).code === 'ENOENT';

export class AgentSettings {
  private readonly profilesCache = new StampCache<AgentProfile[]>(current => {
    if (current === undefined) return [...BUILTIN_PROFILES];
    try {
      return mergeProfiles(BUILTIN_PROFILES, parseProfiles(fs.readFileSync(this.agentsFile, 'utf8')));
    } catch (e) {
      this.warn(`Ignoring ${this.agentsFile} and using the built-in agent profiles: ${(e as Error).message}`);
      return [...BUILTIN_PROFILES];
    }
  });

  private readonly defaultCache = new StampCache<string | undefined>(current => {
    if (current === undefined) return undefined;
    try {
      return readDefaultAgent(fs.readFileSync(this.configFile, 'utf8'));
    } catch (e) {
      this.warn(`Ignoring ${this.configFile}: ${(e as Error).message}`);
      return undefined;
    }
  });

  private readonly sharedCache = new StampCache<SharedSettings>(() => ({ ...SHARED_DEFAULTS, ...this.sharedFound() }));

  private detectedFailure?: string;

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
    return this.profilesCache.get(this.agentsFile);
  }

  profile(name: string): AgentProfile | undefined {
    return findProfile(this.profiles(), name);
  }

  defaultProfile(): AgentProfile {
    const all = this.profiles();
    const preferred = this.preferred();
    const configured = this.sharedDefault();
    return findProfile(all, preferred) ?? findProfile(all, configured) ?? all.find(p => p.name === DEFAULT_AGENT)!;
  }

  private rewriteConfig(edit: (existing: string | undefined) => string): void {
    writeAtomically(this.configFile, edit(readText(this.configFile)));
  }

  setDefaultAgent(name: string): void {
    try {
      this.rewriteConfig(existing => withDefaultAgent(existing, name));
    } catch (e) {
      this.warn(`Could not save the default agent to ${this.configFile}: ${(e as Error).message}`);
    }
  }

  setShared(key: SyncedSetting, value: string | boolean): boolean {
    try {
      this.rewriteConfig(existing => withSharedValue(existing, key, value));
      return true;
    } catch (e) {
      this.warn(`Could not save ${key} to ${this.configFile}: ${(e as Error).message}`);
      return false;
    }
  }

  sharedFound(): Partial<SharedSettings> | undefined {
    const text = readText(this.configFile);
    if (text === undefined) return {};
    try {
      return readSharedSettings(text);
    } catch (e) {
      this.warn(`Ignoring ${this.configFile}: ${(e as Error).message}`);
      return undefined;
    }
  }

  shared(): SharedSettings {
    return { ...this.sharedCache.get(this.configFile) };
  }

  detected(): Detected {
    const file = path.join(this.home, DETECTED_FILE);
    try {
      const found = parseDetected(fs.readFileSync(file, 'utf8'));
      this.detectedFailure = undefined;
      return found;
    } catch (e) {
      const failure = `Ignoring ${file}: ${(e as Error).message}`;
      if (!isMissing(e) && failure !== this.detectedFailure) this.warn(failure);
      this.detectedFailure = failure;
      return { terminals: [], shells: [], ori: null };
    }
  }

  sharedDefault(): string | undefined {
    return this.defaultCache.get(this.configFile);
  }
}

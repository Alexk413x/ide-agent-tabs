import { statSync } from 'node:fs';
import path from 'node:path';
import { MODEL_PATTERN, type LaunchVia } from './launchPlan.js';
import { checkEnv, ConfigError, MAX_ENTRIES, MAX_PROMPT_CHARS, type Env } from './profiles.js';

export interface OpenInput {
  path: string;
  agent?: string;
  prompt?: string;
  args?: string[];
  env?: Env;
  ide?: string;
  model?: string;
  via?: LaunchVia;
  focus?: boolean;
}

export interface OpenRequest {
  path: string;
  agent?: string;
  prompt?: string;
  args: string[];
  env: Env;
  ide?: string;
  model?: string;
  via?: LaunchVia;
  focus?: boolean;
}

export function validateOpen(input: OpenInput, isDirectory = defaultIsDirectory): OpenRequest {
  const dir = input.path;
  if (dir === undefined || dir.trim() === '') throw new ConfigError('path is required');
  if (!path.isAbsolute(dir)) throw new ConfigError('path must be absolute');
  if (dir.includes('\0') || !isDirectory(dir)) throw new ConfigError(`path is not a directory: ${dir}`);
  const prompt = input.prompt;
  if (prompt !== undefined && prompt.length > MAX_PROMPT_CHARS) {
    throw new ConfigError(`prompt exceeds ${MAX_PROMPT_CHARS} characters`);
  }
  if (prompt?.includes('\0')) throw new ConfigError('prompt holds a NUL');
  const args = input.args ?? [];
  if (args.length > MAX_ENTRIES) throw new ConfigError(`args exceeds ${MAX_ENTRIES} entries`);
  if (args.some((a) => a.length > MAX_PROMPT_CHARS)) throw new ConfigError(`an arg exceeds ${MAX_PROMPT_CHARS} characters`);
  if (args.some((a) => a.includes('\0'))) throw new ConfigError('an arg holds a NUL');
  const env = input.env ?? {};
  checkEnv(env, 'env');
  if (input.agent !== undefined && input.agent.trim() === '') throw new ConfigError('agent must not be blank');
  if (input.ide !== undefined && input.ide.trim() === '') throw new ConfigError('ide must not be blank');
  if (input.model !== undefined && !MODEL_PATTERN.test(input.model)) throw new ConfigError(`model must match ${MODEL_PATTERN.source}`);
  if (input.via !== undefined && input.via !== 'ori' && input.via !== 'direct') throw new ConfigError('via must be "ori" or "direct"');
  if (input.focus !== undefined && typeof input.focus !== 'boolean') throw new ConfigError('focus must be true or false');
  return {
    path: path.normalize(dir),
    ...(input.agent !== undefined ? { agent: input.agent } : {}),
    ...(prompt !== undefined && prompt.trim() !== '' ? { prompt } : {}),
    args,
    env,
    ...(input.ide !== undefined ? { ide: input.ide } : {}),
    ...(input.model !== undefined ? { model: input.model } : {}),
    ...(input.via !== undefined ? { via: input.via } : {}),
    ...(input.focus !== undefined ? { focus: input.focus } : {}),
  };
}

function defaultIsDirectory(p: string): boolean {
  try {
    return statSync(p).isDirectory();
  } catch {
    return false;
  }
}

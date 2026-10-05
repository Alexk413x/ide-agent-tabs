import { promises as fs } from 'node:fs';
import path from 'node:path';
import { isEffort, isModel } from './sessions.js';

export interface ModelInfo {
  model?: string;
  effort?: string;
}

const KEY = /^\s*([A-Za-z0-9_-]+)\s*=\s*(?:"([^"\\]*)"|'([^']*)')\s*(?:#.*)?$/;
const TABLE = /^\s*\[\s*([^\]]+?)\s*\]\s*(?:#.*)?$/;

export function codexHome(env: NodeJS.ProcessEnv): string | undefined {
  if (env.CODEX_HOME) return env.CODEX_HOME;
  const home = env.USERPROFILE || env.HOME;
  return home ? path.join(home, '.codex') : undefined;
}

export function parseCodexConfig(text: string): ModelInfo {
  const tables = new Map<string, Record<string, string>>([['', {}]]);
  let table = '';
  for (const line of text.split(/\r?\n/)) {
    const header = TABLE.exec(line);
    if (header) {
      table = header[1]!.replace(/["']/g, '');
      if (!tables.has(table)) tables.set(table, {});
      continue;
    }
    const kv = KEY.exec(line);
    if (kv) tables.get(table)![kv[1]!] = kv[2] ?? kv[3]!;
  }
  const top = tables.get('')!;
  const chosen = top.profile !== undefined ? { ...top, ...tables.get(`profiles.${top.profile}`) } : top;
  return {
    ...(chosen.model !== undefined && isModel(chosen.model) ? { model: chosen.model } : {}),
    ...(chosen.model_reasoning_effort !== undefined && isEffort(chosen.model_reasoning_effort) ? { effort: chosen.model_reasoning_effort } : {}),
  };
}

export async function readCodexConfig(env: NodeJS.ProcessEnv): Promise<ModelInfo> {
  const dir = codexHome(env);
  if (dir === undefined) return {};
  try {
    return parseCodexConfig(await fs.readFile(path.join(dir, 'config.toml'), 'utf8'));
  } catch {
    return {};
  }
}

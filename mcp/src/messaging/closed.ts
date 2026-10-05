import { promises as fs } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { readTextIfExists, removeStaleFiles, writeAtomically } from '../files.js';
import { BUILTIN_PROFILES } from '../profiles.js';
import { KEEP_MS } from './mailbox.js';
import { isSessionId, VIAS, type PresenceFile, type Via } from './sessions.js';

export const CLOSED_DIR = 'history';
export const CLOSED_KEEP_MS = KEEP_MS;
export const PREVIEW_CHARS = 120;
const TAIL_BYTES = 2 * 1024 * 1024;
const CODEX_DAYS_SEARCHED = 31;
export const CACHE_WINDOWS = ['5m', '1h'] as const;
export type CacheWindow = (typeof CACHE_WINDOWS)[number];

export interface ClosedSession {
  id: string;
  agent: string;
  label: string;
  name: string | null;
  folder: string;
  product: string | null;
  host: string | null;
  model: string | null;
  effort: string | null;
  harness: string;
  via: Via | null;
  startedAt: string | null;
  endedAt: string;
  tokens: number | null;
  cache: CacheWindow | null;
  preview: string | null;
  tab: string;
}

export interface TranscriptDirs {
  claude: string;
  codex: string;
}

export interface Usage {
  found: boolean;
  tokens: number | null;
  cache: CacheWindow | null;
  preview: string | null;
  model?: string;
}

const NO_USAGE: Usage = { found: false, tokens: null, cache: null, preview: null };

export function transcriptDirs(env: NodeJS.ProcessEnv): TranscriptDirs {
  return {
    claude: env.CLAUDE_CONFIG_DIR || path.join(os.homedir(), '.claude'),
    codex: env.CODEX_HOME || path.join(os.homedir(), '.codex'),
  };
}

export const closedPath = (home: string, id: string) => path.join(home, CLOSED_DIR, `${id}.json`);

export function resumableId(p: Pick<PresenceFile, 'agent' | 'owner' | 'threadId'>): string | undefined {
  const id = p.agent === 'codex' || p.agent === 'codex-local' ? (p.threadId ?? p.owner) : p.owner;
  return id !== undefined && isSessionId(id) ? id : undefined;
}

export const labelOf = (agent: string) => BUILTIN_PROFILES.find((p) => p.name === agent)?.label ?? agent;

export function previewOf(text: string): string | null {
  const line = text
    .split(/\r?\n/)
    .map((l) => l.trim())
    .find((l) => l !== '');
  if (line === undefined) return null;
  return line.length > PREVIEW_CHARS ? `${line.slice(0, PREVIEW_CHARS - 1)}…` : line;
}

async function tailLines(file: string): Promise<string[] | undefined> {
  let handle: fs.FileHandle;
  try {
    handle = await fs.open(file, 'r');
  } catch {
    return undefined;
  }
  try {
    const { size } = await handle.stat();
    const length = Math.min(size, TAIL_BYTES);
    const buffer = Buffer.alloc(length);
    await handle.read(buffer, 0, length, size - length);
    const lines = buffer.toString('utf8').split('\n');
    if (length < size) lines.shift();
    return lines.filter((l) => l.trim() !== '');
  } finally {
    await handle.close();
  }
}

function json(line: string): Record<string, unknown> | undefined {
  try {
    const value: unknown = JSON.parse(line);
    return typeof value === 'object' && value !== null && !Array.isArray(value) ? (value as Record<string, unknown>) : undefined;
  } catch {
    return undefined;
  }
}

const obj = (v: unknown): Record<string, unknown> => (typeof v === 'object' && v !== null && !Array.isArray(v) ? (v as Record<string, unknown>) : {});
const num = (v: unknown) => (typeof v === 'number' && Number.isFinite(v) && v >= 0 ? v : 0);

const claudeSlug = (folder: string) => folder.replace(/[^A-Za-z0-9]/g, '-');

async function claudeTranscript(dirs: TranscriptDirs, folder: string, id: string): Promise<string | undefined> {
  const projects = path.join(dirs.claude, 'projects');
  const direct = path.join(projects, claudeSlug(folder), `${id}.jsonl`);
  if (await fs.stat(direct).then((s) => s.isFile(), () => false)) return direct;
  for (const dir of await fs.readdir(projects).catch(() => [] as string[])) {
    const file = path.join(projects, dir, `${id}.jsonl`);
    if (await fs.stat(file).then((s) => s.isFile(), () => false)) return file;
  }
  return undefined;
}

export async function claudeUsage(dirs: TranscriptDirs, folder: string, id: string): Promise<Usage> {
  const file = await claudeTranscript(dirs, folder, id);
  const lines = file === undefined ? undefined : await tailLines(file);
  if (lines === undefined) return NO_USAGE;
  const usage: Usage = { found: true, tokens: null, cache: null, preview: null };
  for (const line of lines.reverse()) {
    const entry = json(line);
    if (entry?.type !== 'assistant' || entry.isSidechain === true) continue;
    const message = obj(entry.message);
    const used = obj(message.usage);
    if (usage.tokens === null && Object.keys(used).length) {
      usage.tokens = num(used.input_tokens) + num(used.cache_creation_input_tokens) + num(used.cache_read_input_tokens);
      if (typeof message.model === 'string') usage.model = message.model;
    }
    const created = obj(used.cache_creation);
    if (usage.cache === null && (num(created.ephemeral_1h_input_tokens) > 0 || num(created.ephemeral_5m_input_tokens) > 0)) {
      usage.cache = num(created.ephemeral_1h_input_tokens) > 0 ? '1h' : '5m';
    }
    if (usage.preview === null && Array.isArray(message.content)) {
      const text = message.content.map(obj).filter((c) => c.type === 'text' && typeof c.text === 'string').map((c) => c.text as string).join('\n');
      usage.preview = previewOf(text);
    }
    if (usage.tokens !== null && usage.cache !== null && usage.preview !== null) break;
  }
  return usage;
}

async function codexRollout(dirs: TranscriptDirs, id: string): Promise<string | undefined> {
  const root = path.join(dirs.codex, 'sessions');
  const desc = async (dir: string) => (await fs.readdir(dir).catch(() => [] as string[])).filter((n) => /^\d+$/.test(n)).sort().reverse();
  let days = 0;
  for (const year of await desc(root)) {
    for (const month of await desc(path.join(root, year))) {
      for (const day of await desc(path.join(root, year, month))) {
        const dir = path.join(root, year, month, day);
        const name = (await fs.readdir(dir).catch(() => [] as string[])).find((n) => n.endsWith(`-${id}.jsonl`));
        if (name !== undefined) return path.join(dir, name);
        if (++days >= CODEX_DAYS_SEARCHED) return undefined;
      }
    }
  }
  return undefined;
}

export async function codexUsage(dirs: TranscriptDirs, id: string): Promise<Usage> {
  const file = await codexRollout(dirs, id);
  const lines = file === undefined ? undefined : await tailLines(file);
  if (lines === undefined) return NO_USAGE;
  const usage: Usage = { found: true, tokens: null, cache: null, preview: null };
  for (const line of lines.reverse()) {
    const payload = obj(json(line)?.payload);
    if (usage.tokens === null && payload.type === 'token_count') {
      const last = obj(obj(payload.info).last_token_usage);
      if (typeof last.input_tokens === 'number') usage.tokens = num(last.input_tokens);
    }
    if (usage.preview === null && payload.type === 'message' && payload.role === 'assistant' && Array.isArray(payload.content)) {
      const text = payload.content.map(obj).filter((c) => c.type === 'output_text' && typeof c.text === 'string').map((c) => c.text as string).join('\n');
      usage.preview = previewOf(text);
    }
    if (usage.tokens !== null && usage.preview !== null) break;
  }
  return usage;
}

export async function usageOf(dirs: TranscriptDirs, agent: string, folder: string, id: string): Promise<Usage> {
  if (agent === 'claude') return claudeUsage(dirs, folder, id);
  if (agent === 'codex' || agent === 'codex-local') return codexUsage(dirs, id);
  return NO_USAGE;
}

export async function closedRecord(p: PresenceFile, endedAt: number, dirs: TranscriptDirs): Promise<ClosedSession | undefined> {
  const id = resumableId(p);
  if (id === undefined || p.agent === undefined || p.path === undefined) return undefined;
  const usage = await usageOf(dirs, p.agent, p.path, id).catch(() => NO_USAGE);
  // Claude Code writes no transcript before the first prompt, and claude --resume can't open a session without one.
  if (p.agent === 'claude' && !usage.found) return undefined;
  const label = labelOf(p.agent);
  return {
    id,
    agent: p.agent,
    label,
    name: p.nativeName ?? null,
    folder: p.path,
    product: p.product ?? null,
    host: p.host ?? null,
    model: p.model ?? usage.model ?? null,
    effort: p.effort ?? null,
    harness: `${label}${p.via === 'ori' ? ' via OpenRouter' : ''}`,
    via: p.via ?? null,
    startedAt: p.startedAt ?? null,
    endedAt: new Date(endedAt).toISOString(),
    tokens: usage.tokens,
    cache: usage.cache,
    preview: usage.preview,
    tab: p.id,
  };
}

export async function recordEnded(home: string, p: PresenceFile, endedAt: number, dirs: TranscriptDirs): Promise<ClosedSession | undefined> {
  const record = await closedRecord(p, endedAt, dirs);
  if (record === undefined) return undefined;
  await writeAtomically(closedPath(home, record.id), `${JSON.stringify(record, null, 2)}\n`);
  await cleanClosed(home, endedAt);
  return record;
}

export async function cleanClosed(home: string, now = Date.now()): Promise<void> {
  await removeStaleFiles(path.join(home, CLOSED_DIR), ['.json', '.tmp'], CLOSED_KEEP_MS, now);
}

const strOrNull = (v: unknown) => (typeof v === 'string' ? v : null);

export function parseClosed(text: string | undefined): ClosedSession | undefined {
  const o = text === undefined ? undefined : json(text);
  if (o === undefined) return undefined;
  if (typeof o.id !== 'string' || !isSessionId(o.id) || typeof o.agent !== 'string' || typeof o.folder !== 'string') return undefined;
  if (typeof o.endedAt !== 'string' || !Number.isFinite(Date.parse(o.endedAt))) return undefined;
  const tokens = typeof o.tokens === 'number' && Number.isSafeInteger(o.tokens) && o.tokens >= 0 ? o.tokens : null;
  return {
    id: o.id,
    agent: o.agent,
    label: typeof o.label === 'string' ? o.label : labelOf(o.agent),
    name: strOrNull(o.name),
    folder: o.folder,
    product: strOrNull(o.product),
    host: strOrNull(o.host),
    model: strOrNull(o.model),
    effort: strOrNull(o.effort),
    harness: typeof o.harness === 'string' ? o.harness : labelOf(o.agent),
    via: VIAS.includes(o.via as Via) ? (o.via as Via) : null,
    startedAt: strOrNull(o.startedAt),
    endedAt: o.endedAt,
    tokens,
    cache: CACHE_WINDOWS.includes(o.cache as CacheWindow) ? (o.cache as CacheWindow) : null,
    preview: strOrNull(o.preview),
    tab: typeof o.tab === 'string' ? o.tab : o.id,
  };
}

export async function readClosed(home: string, now = Date.now()): Promise<ClosedSession[]> {
  await cleanClosed(home, now);
  const dir = path.join(home, CLOSED_DIR);
  const names = (await fs.readdir(dir).catch(() => [] as string[])).filter((n) => n.endsWith('.json'));
  const records = await Promise.all(names.map(async (n) => parseClosed(await readTextIfExists(path.join(dir, n)).catch(() => undefined))));
  return records
    .filter((r): r is ClosedSession => r !== undefined && now - Date.parse(r.endedAt) <= CLOSED_KEEP_MS)
    .sort((a, b) => b.endedAt.localeCompare(a.endedAt) || a.id.localeCompare(b.id));
}

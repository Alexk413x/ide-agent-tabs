import { promises as fs } from 'node:fs';
import path from 'node:path';
import { readTextIfExists, withFileLock, writeAtomically } from '../files.js';
import { isProcessAlive } from '../registry.js';

export const SESSIONS_DIR = 'sessions';
export const STATES = ['idle', 'busy', 'permission', 'waking', 'unknown'] as const;
export type SessionState = (typeof STATES)[number];
export const SESSION_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
const STUB_MAX_AGE_MS = 60 * 60 * 1000;
export const WAKE_TIMEOUT_MS = 20_000;
export const IDLE_SETTLE_MS = 2_000;

export interface PresenceFile {
  id: string;
  agent?: string;
  path?: string;
  pid?: number;
  host?: string;
  startedAt?: string;
  state?: SessionState;
  stateAt?: string;
  nudges?: number;
  threadId?: string;
}

export interface Presence extends PresenceFile {
  agent: string;
  path: string;
  pid: number;
  startedAt: string;
  state: SessionState;
}

export const isSessionId = (id: string) => SESSION_ID.test(id);

export const presencePath = (home: string, id: string) => path.join(home, SESSIONS_DIR, `${id}.json`);

export function safeName(value: string, max = 64): string {
  return value.replace(/[^A-Za-z0-9._-]/g, '').slice(0, max);
}

const CLIENT_AGENTS: [string, string][] = [
  ['claude', 'claude'],
  ['codex', 'codex'],
  ['gemini', 'gemini'],
  ['copilot', 'copilot'],
  ['opencode', 'opencode'],
];

export function agentFromClient(name: string | undefined): string {
  const lower = (name ?? '').toLowerCase();
  const known = CLIENT_AGENTS.find(([part]) => lower.includes(part));
  return known ? known[1] : safeName(name ?? '') || 'unknown';
}

export function parsePresence(text: string | undefined): PresenceFile | undefined {
  if (text === undefined) return undefined;
  let json: unknown;
  try {
    json = JSON.parse(text);
  } catch {
    return undefined;
  }
  if (typeof json !== 'object' || json === null || Array.isArray(json)) return undefined;
  const o = json as Record<string, unknown>;
  if (typeof o.id !== 'string' || !isSessionId(o.id)) return undefined;
  const str = (k: string) => (typeof o[k] === 'string' ? { [k]: o[k] as string } : {});
  const state = STATES.includes(o.state as SessionState) ? { state: o.state as SessionState } : {};
  const pid = typeof o.pid === 'number' && Number.isSafeInteger(o.pid) && o.pid > 0 ? { pid: o.pid } : {};
  const nudges = typeof o.nudges === 'number' && Number.isSafeInteger(o.nudges) && o.nudges >= 0 ? { nudges: o.nudges } : {};
  return {
    id: o.id,
    ...str('agent'),
    ...str('path'),
    ...pid,
    ...str('host'),
    ...str('startedAt'),
    ...state,
    ...str('stateAt'),
    ...nudges,
    ...str('threadId'),
  };
}

export function isComplete(p: PresenceFile): p is Presence {
  return p.pid !== undefined && p.agent !== undefined && p.path !== undefined && p.startedAt !== undefined;
}

export async function readPresence(home: string, id: string): Promise<PresenceFile | undefined> {
  if (!isSessionId(id)) return undefined;
  return parsePresence(await readTextIfExists(presencePath(home, id)).catch(() => undefined));
}

export async function updatePresence(
  home: string,
  id: string,
  change: (current: PresenceFile | undefined) => PresenceFile | undefined,
): Promise<PresenceFile | undefined> {
  const file = presencePath(home, id);
  return withFileLock(file, async () => {
    const current = parsePresence(await readTextIfExists(file));
    const next = change(current);
    if (next === current) return current;
    if (next === undefined) await fs.rm(file, { force: true });
    else await writeAtomically(file, `${JSON.stringify(next, null, 2)}\n`);
    return next;
  });
}

export function withState(p: PresenceFile, state: SessionState, now: number, nudges?: number): PresenceFile {
  return { ...p, state, stateAt: new Date(now).toISOString(), ...(nudges !== undefined ? { nudges } : {}) };
}

export async function liveSessions(
  home: string,
  alive: (pid: number) => boolean = isProcessAlive,
  now = Date.now(),
): Promise<Presence[]> {
  const dir = path.join(home, SESSIONS_DIR);
  let names: string[];
  try {
    names = await fs.readdir(dir);
  } catch {
    return [];
  }
  const sessions: Presence[] = [];
  for (const name of names.filter((n) => n.endsWith('.json')).sort()) {
    const file = path.join(dir, name);
    const text = await readTextIfExists(file).catch(() => null);
    if (text === null || text === undefined) continue;
    const presence = parsePresence(text);
    if (presence && isComplete(presence) && alive(presence.pid)) {
      sessions.push({ ...presence, state: effectiveState(presence, now) });
      continue;
    }
    const stat = await fs.stat(file).catch(() => undefined);
    const dead = presence?.pid !== undefined;
    if (stat && (dead || now - stat.mtimeMs > STUB_MAX_AGE_MS)) await fs.rm(file, { force: true }).catch(() => undefined);
  }
  return sessions;
}

// A wake line that never starts a turn, such as one typed while the agent was still finishing, leaves the
// session waking; after WAKE_TIMEOUT_MS it counts as idle again, so the next send or wait retries.
export function effectiveState(p: { state?: SessionState; stateAt?: string }, now: number): SessionState {
  const state = p.state ?? 'unknown';
  if (state !== 'waking') return state;
  const at = Date.parse(p.stateAt ?? '');
  return Number.isFinite(at) && now - at < WAKE_TIMEOUT_MS ? 'waking' : 'idle';
}

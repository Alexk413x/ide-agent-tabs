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
export const BUSY_STALE_MS = 15 * 60_000;
export const HEARTBEAT_MS = 60_000;
export const PRESENCE_BEATS_MISSED = 5;
export const MOD_STALE_MS = 3 * HEARTBEAT_MS;
export const DRIVERS = ['mod'] as const;
export type Driver = (typeof DRIVERS)[number];
export const VIAS = ['ori', 'direct'] as const;
export type Via = (typeof VIAS)[number];

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
  reminded?: string[];
  threadId?: string;
  owner?: string;
  beatMs?: number;
  inputIdle?: boolean;
  handedOffTo?: string;
  driver?: Driver;
  modBeat?: number;
  nativeName?: string;
  via?: Via;
  project?: string;
  model?: string;
  effort?: string;
  agentType?: string;
  agentColor?: AgentColor;
  product?: string;
}

export interface Presence extends PresenceFile {
  agent: string;
  path: string;
  pid: number;
  startedAt: string;
  state: SessionState;
}

export const isSessionId = (id: string) => SESSION_ID.test(id);
export const isModel = (value: string) => /^[^\x00-\x1f\x7f]{1,128}$/.test(value);
export const isEffort = (value: string) => /^[A-Za-z0-9._-]{1,32}$/.test(value);
export const AGENT_COLORS = ['red', 'blue', 'green', 'yellow', 'purple', 'orange', 'pink', 'cyan'] as const;
export type AgentColor = (typeof AGENT_COLORS)[number];
export const isAgentType = (value: string) => /^[A-Za-z0-9._:-]{1,128}$/.test(value);
export const isAgentColor = (value: string): value is AgentColor => (AGENT_COLORS as readonly string[]).includes(value);

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
  ['antigravity', 'agy'],
  ['grok', 'grok'],
  ['qwen', 'qwen'],
  ['goose', 'goose'],
];

export function agentFromClient(name: string | undefined): string {
  const lower = (name ?? '').toLowerCase();
  if (lower === 'pi') return 'pi';
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
  const beatMs = typeof o.beatMs === 'number' && Number.isSafeInteger(o.beatMs) && o.beatMs > 0 ? { beatMs: o.beatMs } : {};
  const modBeat = typeof o.modBeat === 'number' && Number.isSafeInteger(o.modBeat) && o.modBeat > 0 ? { modBeat: o.modBeat } : {};
  const driver = DRIVERS.includes(o.driver as Driver) ? { driver: o.driver as Driver } : {};
  const via = VIAS.includes(o.via as Via) ? { via: o.via as Via } : {};
  const reminded = Array.isArray(o.reminded) && o.reminded.every((r) => typeof r === 'string') ? { reminded: o.reminded as string[] } : {};
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
    ...reminded,
    ...str('threadId'),
    ...str('owner'),
    ...beatMs,
    ...(typeof o.inputIdle === 'boolean' ? { inputIdle: o.inputIdle } : {}),
    ...str('handedOffTo'),
    ...driver,
    ...modBeat,
    ...str('nativeName'),
    ...via,
    ...str('project'),
    ...str('model'),
    ...str('effort'),
    ...(typeof o.agentType === 'string' && isAgentType(o.agentType) ? { agentType: o.agentType } : {}),
    ...(typeof o.agentColor === 'string' && isAgentColor(o.agentColor) ? { agentColor: o.agentColor } : {}),
    ...str('product'),
  };
}

// The Claude mod delivers mail and reports state in-process; a mod that stopped without cleanup leaves the
// field behind, so it counts only while the mod's heartbeat is fresh, and the classic hooks and wake lines resume.
export function isModDriven(p: Pick<PresenceFile, 'driver' | 'modBeat'>, now: number): boolean {
  if (p.driver !== 'mod' || p.modBeat === undefined) return false;
  const age = now - p.modBeat;
  return age >= -MOD_STALE_MS && age < MOD_STALE_MS;
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
  ended?: (p: PresenceFile, at: number) => Promise<unknown>,
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
    const stat = await fs.stat(file).catch(() => undefined);
    // A pid alone can name a new process once Windows reuses it; a server that beats proves it still runs.
    const silent = presence?.beatMs !== undefined && stat !== undefined && now - stat.mtimeMs > presence.beatMs * PRESENCE_BEATS_MISSED;
    if (presence && isComplete(presence) && !silent && alive(presence.pid)) {
      sessions.push({ ...presence, state: effectiveState(presence, now) });
      continue;
    }
    const dead = presence?.pid !== undefined;
    if (stat && (dead || now - stat.mtimeMs > STUB_MAX_AGE_MS)) {
      if (presence && ended) await ended(presence, Math.min(now, stat.mtimeMs)).catch(() => undefined);
      await fs.rm(file, { force: true }).catch(() => undefined);
    }
  }
  return sessions;
}

// A wake line that never starts a turn, such as one typed while the agent was still finishing, leaves the
// session waking; after WAKE_TIMEOUT_MS it counts as idle again, so the next send or wait retries. A busy turn
// refreshes its state on every tool call, so one silent for BUSY_STALE_MS was interrupted without a turn-end hook.
export function effectiveState(p: { state?: SessionState; stateAt?: string }, now: number): SessionState {
  const state = p.state ?? 'unknown';
  if (state !== 'waking' && state !== 'busy') return state;
  const at = Date.parse(p.stateAt ?? '');
  const elapsed = now - at;
  if (state === 'busy' && !Number.isFinite(at)) return state;
  const limit = state === 'waking' ? WAKE_TIMEOUT_MS : BUSY_STALE_MS;
  return Number.isFinite(at) && elapsed >= 0 && elapsed < limit ? state : 'idle';
}

import { randomBytes } from 'node:crypto';
import { promises as fs } from 'node:fs';
import path from 'node:path';
import { ensurePrivateDir, readTextIfExists, writeAtomically, writeNewPrivateFile } from '../files.js';

export const SERVICE = 'ide-agent-tabs';
export const DEFAULT_PORT = 47828;
export const SERVER_DIR = 'server';
export const TOKEN_FILE = 'token';
export const SERVER_SCRIPT = 'shared-server.mjs';
export const PORT_OPTION_ENV = 'CLAUDE_PLUGIN_OPTION_SERVER_PORT';

export const HEADERS = {
  client: 'x-agent-tabs-client',
  tab: 'x-agent-tabs-tab',
  agent: 'x-agent-tabs-agent',
  pid: 'x-agent-tabs-pid',
  pidStart: 'x-agent-tabs-pid-start',
} as const;

export interface Health {
  service: string;
  version: string;
  pid: number;
  port: number;
  sessions?: number;
  startedAt?: string;
}

export interface ServerState {
  pid: number;
  port: number;
  version: string;
  startedAt: string;
  shutdownToken: string;
}

export const serverDir = (home: string) => path.join(home, SERVER_DIR);
export const tokenPath = (home: string) => path.join(serverDir(home), TOKEN_FILE);
export const statePath = (home: string, port: number) => path.join(serverDir(home), `state-${port}.json`);

export const newToken = () => randomBytes(32).toString('hex');

export function parsePort(value: string | undefined): number | undefined {
  if (value === undefined || !/^\d{1,5}$/.test(value.trim())) return undefined;
  const port = Number(value);
  return port > 0 && port <= 65535 ? port : undefined;
}

export function portFromUrl(value: string | undefined): number | undefined {
  if (!value) return undefined;
  try {
    const url = new URL(value);
    return url.protocol === 'http:' && url.hostname === '127.0.0.1' ? parsePort(url.port) : undefined;
  } catch {
    return undefined;
  }
}

export async function readToken(home: string): Promise<string | undefined> {
  const text = (await readTextIfExists(tokenPath(home)).catch(() => undefined))?.trim();
  return text !== undefined && /^[0-9a-f]{64}$/.test(text) ? text : undefined;
}

export async function ensureToken(home: string): Promise<string> {
  const known = await readToken(home);
  if (known !== undefined) return known;
  await ensurePrivateDir(serverDir(home));
  try {
    await writeNewPrivateFile(tokenPath(home), newToken());
  } catch (e) {
    if ((e as NodeJS.ErrnoException).code !== 'EEXIST') throw e;
  }
  const token = await readToken(home);
  if (token === undefined) throw new Error(`${tokenPath(home)} holds no valid token; delete it and start again`);
  return token;
}

export async function readState(home: string, port: number): Promise<ServerState | undefined> {
  try {
    const json = JSON.parse((await readTextIfExists(statePath(home, port))) ?? '') as Partial<ServerState>;
    if (typeof json.pid !== 'number' || json.port !== port || typeof json.shutdownToken !== 'string' || typeof json.version !== 'string') return undefined;
    return { pid: json.pid, port, version: json.version, startedAt: String(json.startedAt ?? ''), shutdownToken: json.shutdownToken };
  } catch {
    return undefined;
  }
}

export const writeState = (home: string, state: ServerState) => writeAtomically(statePath(home, state.port), `${JSON.stringify(state, null, 2)}\n`);

export async function removeState(home: string, port: number, pid: number): Promise<void> {
  if ((await readState(home, port))?.pid === pid) await fs.rm(statePath(home, port), { force: true });
}

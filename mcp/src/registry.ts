import { promises as fs } from 'node:fs';
import path from 'node:path';

export const PROTOCOL_VERSION = 1;
export const ENDPOINTS_DIR = 'endpoints';

export interface Endpoint {
  id: string;
  file: string;
  ide: string;
  product: string;
  version: string;
  pid: number;
  url: string;
  token: string;
  startedAt: number;
}

export type ParsedEndpoint =
  | { kind: 'endpoint'; endpoint: Endpoint }
  | { kind: 'skip'; reason: string; warn: boolean };

const LOOPBACK_HOSTS = new Set(['127.0.0.1', 'localhost', '[::1]']);

function str(obj: Record<string, unknown>, key: string): string | undefined {
  const v = obj[key];
  return typeof v === 'string' ? v : undefined;
}

export function isLoopbackUrl(url: string): boolean {
  try {
    const u = new URL(url);
    return (u.protocol === 'http:' || u.protocol === 'https:') && LOOPBACK_HOSTS.has(u.hostname);
  } catch {
    return false;
  }
}

export function parseEndpoint(text: string, file: string, startedAt: number): ParsedEndpoint {
  const name = path.basename(file);
  let json: unknown;
  try {
    json = JSON.parse(text);
  } catch {
    return { kind: 'skip', reason: `${name} is not JSON`, warn: true };
  }
  if (typeof json !== 'object' || json === null || Array.isArray(json)) {
    return { kind: 'skip', reason: `${name} must hold a JSON object`, warn: true };
  }
  const obj = json as Record<string, unknown>;
  if (obj.protocol !== PROTOCOL_VERSION) {
    return { kind: 'skip', reason: `${name} uses protocol ${String(obj.protocol)}`, warn: false };
  }
  const ide = str(obj, 'ide');
  const url = str(obj, 'url');
  const token = str(obj, 'token');
  const pid = obj.pid;
  if (!ide || !url || !token || typeof pid !== 'number' || !Number.isSafeInteger(pid) || pid <= 0) {
    return { kind: 'skip', reason: `${name} lacks ide, url, token or pid`, warn: true };
  }
  if (!isLoopbackUrl(url)) return { kind: 'skip', reason: `${name} has a non-loopback url`, warn: true };
  return {
    kind: 'endpoint',
    endpoint: {
      id: name.replace(/\.json$/i, ''),
      file,
      ide,
      product: str(obj, 'product') ?? ide,
      version: str(obj, 'version') ?? '',
      pid,
      url: url.replace(/\/+$/, ''),
      token,
      startedAt,
    },
  };
}

export function isProcessAlive(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch (e) {
    return (e as NodeJS.ErrnoException).code === 'EPERM';
  }
}

export interface Registry {
  endpoints: Endpoint[];
  warnings: string[];
}

export async function readRegistry(home: string, alive: (pid: number) => boolean = isProcessAlive): Promise<Registry> {
  const dir = path.join(home, ENDPOINTS_DIR);
  let names: string[];
  try {
    names = await fs.readdir(dir);
  } catch {
    return { endpoints: [], warnings: [] };
  }
  const endpoints: Endpoint[] = [];
  const warnings: string[] = [];
  for (const name of names.filter((n) => n.endsWith('.json')).sort()) {
    const file = path.join(dir, name);
    let text: string;
    let mtime: number;
    try {
      [text, mtime] = await Promise.all([fs.readFile(file, 'utf8'), fs.stat(file).then((s) => s.mtimeMs)]);
    } catch {
      continue;
    }
    const parsed = parseEndpoint(text, file, mtime);
    if (parsed.kind === 'skip') {
      if (parsed.warn) warnings.push(`Skipping ${parsed.reason}`);
      continue;
    }
    if (!alive(parsed.endpoint.pid)) {
      await fs.rm(file, { force: true }).catch(() => undefined);
      continue;
    }
    endpoints.push(parsed.endpoint);
  }
  return { endpoints, warnings };
}

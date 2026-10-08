import { createHash } from 'node:crypto';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { isProcessAlive } from '../registry.js';
import type { Binding, BoundSession } from './engine.js';
import type { Identity } from './front.js';

const ROOTS_REQUEST = 'agent-tabs-roots';
export const LIVENESS_MS = 15_000;
const MAX_CLIENTS = 1024;

export interface HubDeps {
  bind: (binding: Binding) => Promise<BoundSession>;
  isAlive?: (pid: number) => boolean;
  serverPid?: number;
  log?: (message: string) => void;
}

interface Entry {
  key: string;
  pid: number;
  owned: boolean;
  session: Promise<BoundSession>;
}

export function rootPath(responses: unknown): string | undefined {
  const answer = (responses as Record<string, { roots?: unknown } | undefined> | undefined)?.[ROOTS_REQUEST];
  const roots = Array.isArray(answer?.roots) ? answer.roots : [];
  for (const root of roots) {
    const uri = (root as { uri?: unknown } | null)?.uri;
    if (typeof uri !== 'string') continue;
    try {
      const url = new URL(uri);
      if (url.protocol !== 'file:' || (url.host !== '' && url.host !== 'localhost')) continue;
      const file = fileURLToPath(url);
      if (path.isAbsolute(file)) return file;
    } catch {}
  }
  return undefined;
}

export const sessionKey = (identity: Identity) =>
  identity.pid !== undefined ? `pid:${identity.pid}:${identity.pidStart ?? 0}` : identity.client !== undefined ? `client:${identity.client}` : undefined;

export const derivedId = (key: string) => `s-${createHash('sha256').update(key).digest('hex').slice(0, 12)}`;

export class Hub {
  private readonly entries = new Map<string, Entry>();
  private readonly cwds = new Map<string, string>();
  private timer?: ReturnType<typeof setInterval>;

  constructor(private readonly deps: HubDeps) {}

  get size(): number {
    return this.entries.size;
  }

  private get alive() {
    return this.deps.isAlive ?? isProcessAlive;
  }

  startLiveness(everyMs = LIVENESS_MS): void {
    this.timer = setInterval(() => void this.sweep(), everyMs);
    this.timer.unref();
  }

  async sweep(): Promise<number> {
    const dead = [...this.entries.values()].filter((e) => e.owned && !this.alive(e.pid));
    await Promise.all(dead.map((e) => this.drop(e)));
    return dead.length;
  }

  liveSessions(): number {
    return [...this.entries.values()].filter((e) => e.owned && this.alive(e.pid)).length;
  }

  private async drop(entry: Entry): Promise<void> {
    if (this.entries.get(entry.key) === entry) this.entries.delete(entry.key);
    await entry.session.then((s) => s.end()).catch(() => undefined);
  }

  async endPid(pid: number): Promise<number> {
    const matching = [...this.entries.values()].filter((e) => e.owned && e.pid === pid);
    await Promise.all(matching.map((e) => this.drop(e)));
    return matching.length;
  }

  async call(identity: Identity, params: Record<string, unknown>, signal: AbortSignal): Promise<Record<string, unknown>> {
    const key = sessionKey(identity);
    if (key === undefined) {
      return { isError: true, content: [{ type: 'text', text: 'Agent Tabs: this connection sent no session identity; restart the session so its headers helper runs' }] };
    }
    let entry = this.entries.get(key);
    if (entry === undefined) {
      const cwd = this.cwdOf(identity, params.inputResponses);
      if (cwd === undefined) return { resultType: 'input_required', inputRequests: { [ROOTS_REQUEST]: { method: 'roots/list' } } };
      const owned = identity.pid !== undefined;
      const pid = identity.pid ?? this.deps.serverPid ?? process.pid;
      const binding: Binding = {
        id: derivedId(key),
        ...(identity.tab !== undefined ? { tab: identity.tab } : {}),
        agent: identity.agent ?? 'claude',
        pid,
        ...(identity.pidStart !== undefined ? { pidStart: identity.pidStart } : {}),
        cwd,
      };
      entry = { key, pid, owned, session: this.deps.bind(binding) };
      this.entries.set(key, entry);
      const pending = entry;
      entry.session.catch((e: unknown) => {
        this.deps.log?.(`binding ${key} failed: ${e instanceof Error ? e.message : String(e)}`);
        if (this.entries.get(key) === pending) this.entries.delete(key);
      });
    }
    const session = await entry.session;
    return session.call(params, signal);
  }

  private cwdOf(identity: Identity, responses: unknown): string | undefined {
    const client = identity.client;
    const answered = responses === undefined ? undefined : (rootPath(responses) ?? os.homedir());
    if (answered === undefined) return client === undefined ? undefined : this.cwds.get(client);
    if (client !== undefined) {
      this.cwds.set(client, answered);
      while (this.cwds.size > MAX_CLIENTS) this.cwds.delete(this.cwds.keys().next().value!);
    }
    return answered;
  }

  async close(): Promise<void> {
    if (this.timer) clearInterval(this.timer);
    const all = [...this.entries.values()];
    this.entries.clear();
    await Promise.all(all.map((e) => e.session.then((s) => s.release()).catch(() => undefined)));
  }
}

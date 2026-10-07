import { createHash, randomBytes } from 'node:crypto';
import { promises as fs, readFileSync, rmSync } from 'node:fs';
import { AGENT_ENV, BUILTIN_PROFILES, TAB_ID_ENV } from '../profiles.js';
import { isProcessAlive } from '../registry.js';
import {
  checkMessageId,
  claimBatch,
  cleanMail,
  deliver,
  MailError,
  peekUnread,
  MAX_READ_CHARS,
  MAX_TEXT_CHARS,
  newMessageId,
  putBack,
  releaseSend,
  reserveSend,
  returnStaleClaims,
  sendDigest,
  settleClaim,
  takeBatch,
  unreadDir,
  waitForMessage,
  type Message,
} from './mailbox.js';
import { readCodexConfig } from './codexConfig.js';
import { recordEnded, transcriptDirs, type TranscriptDirs } from './closed.js';
import { runHook } from './hook.js';
import { history, historyCounts, logId, MailIndex, olderThan, previews, textPiece, RECEIVED_LOG, SENT_LOG, writeLog, type Who } from './history.js';
import { UNTRUSTED_NOTICE, wakeLine } from './notice.js';
import {
  agentFromClient,
  effectiveState,
  HEARTBEAT_MS,
  IDLE_SETTLE_MS,
  AGENT_COLORS,
  isAgentColor,
  isAgentType,
  isEffort,
  isModDriven,
  isModel,
  isSessionId,
  liveSessions,
  parsePresence,
  presencePath,
  readPresence,
  updatePresence,
  withState,
  type Presence,
  type PresenceFile,
  type SessionState,
} from './sessions.js';

export const DEFAULT_WAIT_S = 60;
export const MAX_WAIT_S = 600;
// Antigravity CLI ends any MCP tool call after 3 minutes and has no setting to raise that.
export const AGY_MAX_WAIT_S = 170;
const CLEAN_EVERY_MS = 60 * 60 * 1000;
const RESTART_GRACE_MS = 60_000;
export const START_RETRY_DELAYS_MS = [1_000, 3_000, 9_000];
export const REWAKE_EVERY_MS = 15_000;
export const FOLLOW_UP_MS = MAX_WAIT_S * 1000;
const realSleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

export interface Hosts {
  findHost(id: string): Promise<string | undefined>;
  typeInto(id: string, host: string, text: string): Promise<{ ok: true } | { ok: false; reason: string }>;
  describeHost?(host: string): Promise<string | undefined>;
}

export interface MessagingDeps {
  home: string;
  env: NodeJS.ProcessEnv;
  pid: number;
  cwd: string;
  hosts: Hosts;
  isAlive?: (pid: number) => boolean;
  randomId?: () => string;
  now?: () => number;
  sleep?: (ms: number) => Promise<void>;
  rewakeEveryMs?: number;
  heartbeatMs?: number;
  transcripts?: TranscriptDirs;
}

export interface SendInput {
  to: string;
  text: string;
  replyTo?: string;
}

export const MOD_STATES = ['idle', 'busy', 'permission'] as const;
export type ModState = (typeof MOD_STATES)[number];
export const AGENT_ORDER = ['claude', 'codex', 'agy', 'copilot', 'gemini', 'grok', 'pi', 'hermes', 'opencode', 'qwen', 'goose', 'codex-local'];
export const MOD_DELIVERY_NOTE = "the recipient's Agent Tabs mod delivers it in-process once the session is idle";

export interface ModLogInput {
  direction: 'sent' | 'received';
  peer: string;
  text: string;
  id?: string;
  at?: number;
  delivery?: string;
}

export interface ModPresenceInput {
  driver?: boolean;
  nativeName?: string;
  state?: ModState;
  model?: string;
  effort?: string;
  agentType?: string;
  agentColor?: string;
  owner?: string;
}

export interface WaitInput {
  timeout?: number;
  from?: string;
  replyTo?: string;
}

const AGENT_NAME = /^[A-Za-z0-9._-]{1,64}$/;
const THREAD_ID = /^[A-Za-z0-9-]{1,100}$/;
export const CODEX_ID_PREFIX = 'codex-';
const generatedId = () => `s-${randomBytes(6).toString('hex')}`;
const mayBeTab = (id: string) => !id.startsWith('s-') && !id.startsWith(CODEX_ID_PREFIX);
const NATIVE_NAME = /^[^\x00-\x1f\x7f]{1,128}$/;
const ID_PREFIXES = ['s-', CODEX_ID_PREFIX];
const SHORT_ID_CHARS = 4;
const NAME_SLUG_CHARS = 24;
const NAME_SUFFIX_CHARS = 2;
export const SESSION_PREFIX_CHARS = 8;
const agentRank = (agent: string) => {
  const i = AGENT_ORDER.indexOf(agent);
  return i === -1 ? AGENT_ORDER.length : i;
};

export const harnessOf = (agent: string, via?: string) =>
  `${BUILTIN_PROFILES.find((p) => p.name === agent)?.label ?? agent}${via === 'ori' ? ' via OpenRouter' : ''}`;

function idCore(id: string): string {
  const prefix = ID_PREFIXES.find((p) => id.startsWith(p) && id.length > p.length);
  return (prefix !== undefined ? id.slice(prefix.length) : id).replace(/[^A-Za-z0-9]/g, '');
}

export function folderSlug(folder: string): string {
  const base = folder.split(/[\\/]+/).filter((p) => p !== '').at(-1) ?? '';
  const slug = base.toLowerCase().replace(/[^a-z0-9-]/g, '-').slice(0, NAME_SLUG_CHARS).replace(/^-+|-+$/g, '');
  return slug === '' ? 'session' : slug;
}

const suffixPool = (id: string) => `${idCore(id).toLowerCase().replace(/[^0-9a-f]/g, '')}${createHash('sha256').update(id).digest('hex')}`;

export function sessionNames(sessions: readonly { id: string; agent: string; path: string; nativeName?: string }[]): Map<string, string> {
  const names = new Map<string, string>();
  const taken = new Set<string>();
  for (const s of sessions) {
    if (s.agent === 'claude' && s.nativeName !== undefined) {
      names.set(s.id, s.nativeName);
      taken.add(s.nativeName);
    }
  }
  const made = sessions.filter((s) => !names.has(s.id)).map((s) => ({ id: s.id, slug: folderSlug(s.path), pool: suffixPool(s.id) }));
  for (const m of made) {
    const rivals = made.filter((o) => o !== m && o.slug === m.slug);
    const name = (n: number) => `${m.slug}-${m.pool.slice(0, n)}`;
    let n = NAME_SUFFIX_CHARS;
    while (n < m.pool.length && (taken.has(name(n)) || rivals.some((r) => r.pool.slice(0, n) === m.pool.slice(0, n)))) n++;
    names.set(m.id, name(n));
  }
  return names;
}

export function shortNames(sessions: readonly { id: string; agent: string }[]): Map<string, string> {
  const names = new Map<string, string>();
  for (const s of sessions) {
    const core = idCore(s.id);
    const rivals = sessions.filter((o) => o !== s && o.agent === s.agent).map((o) => idCore(o.id));
    let n = Math.min(SHORT_ID_CHARS, core.length);
    while (n < core.length && rivals.some((r) => r.slice(0, n) === core.slice(0, n))) n++;
    const name = `${s.agent}-${core.slice(0, n)}`;
    names.set(s.id, core === '' || rivals.some((r) => r.slice(0, n) === core.slice(0, n)) ? s.id : name);
  }
  return names;
}

interface ReadResult {
  notice?: string;
  messages: ReturnType<typeof shown>[];
  warnings?: string[];
  remaining?: number;
  next?: string;
  unreadable?: number;
  unreadableNote?: string;
}

function shown(m: Message) {
  return { id: m.id, from: m.from, text: m.text, ...(m.replyTo !== undefined ? { replyTo: m.replyTo } : {}), sentAt: m.sentAt };
}

export class Messaging {
  private sessionId: string;
  private isTab: boolean;
  private agent: string;
  private readonly startedAt: string;
  private lastClean = 0;
  private readonly mailIndex = new MailIndex();
  private threadId?: string;
  private ownHost?: Promise<string | undefined>;
  private identified: Promise<void> = Promise.resolve();
  private readonly followUps = new Map<string, ReturnType<typeof setInterval>>();
  private heartbeat?: ReturnType<typeof setInterval>;
  private readonly claims = new Map<string, string[]>();
  private startError?: string;
  private stopped = false;
  registration: Promise<void> = Promise.resolve();

  constructor(private readonly deps: MessagingDeps) {
    const tab = deps.env[TAB_ID_ENV];
    this.isTab = tab !== undefined && isSessionId(tab);
    this.sessionId = this.isTab ? tab! : (deps.randomId ?? generatedId)();
    this.agent = this.agentFromEnv() ?? 'unknown';
    this.startedAt = new Date(this.now()).toISOString();
  }

  // A client that copies variables into a server's config, such as "${IDE_AGENT_TABS_AGENT}", may pass the text
  // unexpanded when the variable is unset.
  private agentFromEnv(): string | undefined {
    const value = this.deps.env[AGENT_ENV];
    return value !== undefined && AGENT_NAME.test(value) ? value : undefined;
  }

  get id(): string {
    return this.sessionId;
  }

  private now(): number {
    return (this.deps.now ?? Date.now)();
  }

  private get alive() {
    return this.deps.isAlive ?? isProcessAlive;
  }

  private readonly ended = (p: PresenceFile, at: number) => recordEnded(this.deps.home, p, at, this.deps.transcripts ?? transcriptDirs(this.deps.env));

  live(): Promise<Presence[]> {
    return liveSessions(this.deps.home, this.alive, this.now(), this.ended);
  }

  async recordEnd(): Promise<void> {
    const own = await readPresence(this.deps.home, this.sessionId);
    if (own?.pid === this.deps.pid) await this.ended(own, this.now());
  }

  private presence(current: PresenceFile | undefined, host = current?.host): PresenceFile {
    return {
      id: this.sessionId,
      agent: this.agent,
      path: this.deps.cwd,
      pid: this.deps.pid,
      ...(host !== undefined ? { host } : {}),
      startedAt: this.startedAt,
      state: current?.state ?? 'unknown',
      ...(current?.stateAt !== undefined ? { stateAt: current.stateAt } : {}),
      ...(current?.nudges !== undefined ? { nudges: current.nudges } : {}),
      ...(current?.inputIdle !== undefined ? { inputIdle: current.inputIdle } : {}),
      ...(this.threadId !== undefined ? { threadId: this.threadId } : {}),
      ...(current?.handedOffTo !== undefined ? { handedOffTo: current.handedOffTo } : {}),
      ...(current?.driver !== undefined ? { driver: current.driver } : {}),
      ...(current?.modBeat !== undefined ? { modBeat: current.modBeat } : {}),
      ...(current?.nativeName !== undefined ? { nativeName: current.nativeName } : {}),
      ...(current?.via !== undefined ? { via: current.via } : {}),
      ...(current?.project !== undefined ? { project: current.project } : {}),
      ...(current?.model !== undefined ? { model: current.model } : {}),
      ...(current?.effort !== undefined ? { effort: current.effort } : {}),
      ...(current?.product !== undefined ? { product: current.product } : {}),
      beatMs: this.beatMs,
    };
  }

  private get beatMs(): number {
    return this.deps.heartbeatMs ?? HEARTBEAT_MS;
  }

  private startHeartbeat(): void {
    this.stopHeartbeat();
    this.heartbeat = setInterval(() => void this.beat().catch(() => undefined), this.beatMs);
    this.heartbeat.unref?.();
  }

  private async beat(): Promise<void> {
    const file = presencePath(this.deps.home, this.sessionId);
    const now = new Date(this.now());
    const touched = await fs.utimes(file, now, now).then(
      () => true,
      () => false,
    );
    if (!touched || (await readPresence(this.deps.home, this.sessionId))?.pid !== this.deps.pid) await this.updateOwn((p) => p);
  }

  stopHeartbeat(): void {
    if (this.heartbeat) clearInterval(this.heartbeat);
    this.heartbeat = undefined;
  }

  async startRegistered(options: { delaysMs?: readonly number[]; log?: (message: string) => void } = {}): Promise<void> {
    const log = options.log ?? (() => undefined);
    const attempt = async () => {
      try {
        await this.start();
        this.startError = undefined;
        return true;
      } catch (e) {
        this.startError = e instanceof Error ? e.message : String(e);
        log(`this session isn't registered: ${this.startError}`);
        return false;
      }
    };
    if (await attempt()) return;
    this.registration = (async () => {
      for (const delay of options.delaysMs ?? START_RETRY_DELAYS_MS) {
        await (this.deps.sleep ?? realSleep)(delay);
        if (this.stopped || (await attempt())) return;
      }
    })();
  }

  private get warnings(): { warnings?: string[] } {
    return this.startError === undefined ? {} : { warnings: [`this session isn't registered: ${this.startError}`] };
  }

  async start(): Promise<void> {
    let taken = false;
    if (this.isTab) {
      // A headless agent started from inside a tab inherits its IDE_AGENT_TABS_ID; the tab's own server keeps the id.
      await updatePresence(this.deps.home, this.sessionId, (current) => {
        if (current?.pid !== undefined && current.pid !== this.deps.pid && this.alive(current.pid)) {
          taken = true;
          return current;
        }
        return this.presence(this.leftByDeadServer(current));
      });
    }
    if (taken) {
      this.isTab = false;
      this.sessionId = (this.deps.randomId ?? generatedId)();
    }
    if (!this.isTab) await updatePresence(this.deps.home, this.sessionId, (current) => this.presence(current));
    if (this.isTab) this.ownHost = this.resolveOwnHost();
    await this.learnCodexDefaults().catch(() => undefined);
    this.startHeartbeat();
    void this.clean().catch(() => undefined);
  }

  // The agent of this tab can set its state just before this server starts, so only a state older than that
  // came from the dead server's agent.
  private leftByDeadServer(current: PresenceFile | undefined): PresenceFile | undefined {
    if (current?.pid === undefined || current.pid === this.deps.pid || this.alive(current.pid)) return current;
    const { nudges: _, driver: _d, modBeat: _b, nativeName: _n, ...rest } = current;
    const at = Date.parse(current.stateAt ?? '');
    const old = !Number.isFinite(at) || at < Date.parse(this.startedAt) - RESTART_GRACE_MS;
    return old && current.state !== 'idle' ? { ...rest, state: 'unknown' } : rest;
  }

  private async resolveOwnHost(): Promise<string | undefined> {
    const host = await this.deps.hosts.findHost(this.sessionId).catch(() => undefined);
    if (host !== undefined) {
      const fields = await this.hostFields(host);
      await this.updateOwn((p) => ({ ...p, ...fields })).catch(() => undefined);
    }
    return host;
  }

  // A host id names one run of an IDE extension or one terminal, so the product label is kept in the
  // presence file: list_sessions still names the IDE after that endpoint is gone.
  private async hostFields(host: string): Promise<Pick<PresenceFile, 'host' | 'product'>> {
    const product = await this.deps.hosts.describeHost?.(host).catch(() => undefined);
    return { host, ...(product !== undefined ? { product } : {}) };
  }

  // A model from open_tab or from a hook payload names what the session runs; the config is only its default.
  private async learnCodexDefaults(): Promise<void> {
    if (this.agent !== 'codex') return;
    const config = await readCodexConfig(this.deps.env);
    if (config.model === undefined && config.effort === undefined) return;
    await this.updateOwn((p) => ({
      ...p,
      ...(p.model === undefined && config.model !== undefined ? { model: config.model } : {}),
      ...(p.effort === undefined && config.effort !== undefined ? { effort: config.effort } : {}),
    }));
  }

  noteThread(threadId: unknown): Promise<void> {
    if (typeof threadId !== 'string' || !THREAD_ID.test(threadId) || threadId === this.threadId) return this.identified;
    this.threadId = threadId;
    this.identified = this.identified.then(() => this.identify(threadId)).catch(() => undefined);
    return this.identified;
  }

  // The shared Codex daemon starts servers with the environment of whatever started the daemon, so its
  // IDE_AGENT_TABS_ID can name another tab or a closed one. A Codex session keeps that id only while the tab is open.
  private async identify(threadId: string): Promise<void> {
    const id = `${CODEX_ID_PREFIX}${threadId}`;
    const tabOpen = this.isTab && (await this.ownHost) !== undefined;
    if (tabOpen || this.sessionId === id) {
      await this.updateOwn((p) => ({ ...p, threadId }));
      return;
    }
    const old = this.sessionId;
    const previous = await readPresence(this.deps.home, old);
    const carried = previous?.pid === this.deps.pid ? previous : undefined;
    let taken = false;
    this.sessionId = id;
    await updatePresence(this.deps.home, id, (current) => {
      if (current?.pid !== undefined && current.pid !== this.deps.pid && this.alive(current.pid)) {
        taken = true;
        return current;
      }
      return this.presence(current ?? carried, current?.host);
    });
    if (taken) {
      this.sessionId = old;
      await this.updateOwn((p) => ({ ...p, threadId }));
      return;
    }
    this.isTab = false;
    await updatePresence(this.deps.home, old, (current) => (current?.pid === this.deps.pid ? undefined : current));
  }

  async hook(event: string, input: Record<string, unknown>): Promise<object | undefined> {
    return runHook({ cli: 'codex', event, input, home: this.deps.home, sessionId: this.sessionId, now: this.now() });
  }

  private updateOwn(change: (p: PresenceFile) => PresenceFile): Promise<unknown> {
    return updatePresence(this.deps.home, this.sessionId, (current) =>
      current === undefined ? change(this.presence(undefined)) : current.pid === this.deps.pid ? change(current) : current,
    );
  }

  async setClient(name: string | undefined): Promise<void> {
    if (this.agentFromEnv() !== undefined) return;
    this.agent = agentFromClient(name);
    await this.updateOwn((p) => ({ ...p, agent: this.agent }));
    await this.learnCodexDefaults().catch(() => undefined);
  }

  stopSync(): void {
    this.stopped = true;
    this.stopHeartbeat();
    const file = presencePath(this.deps.home, this.sessionId);
    try {
      if (parsePresence(readFileSync(file, 'utf8'))?.pid === this.deps.pid) rmSync(file, { force: true });
    } catch {
      return;
    }
  }

  private async clean(): Promise<void> {
    const now = this.now();
    if (now - this.lastClean < CLEAN_EVERY_MS) return;
    this.lastClean = now;
    const live = await liveSessions(this.deps.home, this.alive, now, this.ended);
    await cleanMail(this.deps.home, new Set(live.map((s) => s.id)), now);
  }

  async listSessions() {
    const now = this.now();
    const sessions = await liveSessions(this.deps.home, this.alive, now, this.ended);
    const labels = new Map<string, string | undefined>();
    for (const host of new Set(sessions.flatMap((s) => (s.host !== undefined ? [s.host] : [])))) {
      labels.set(host, await this.deps.hosts.describeHost?.(host).catch(() => undefined));
    }
    const legacy = shortNames(sessions);
    const named = sessionNames(sessions);
    const rows = sessions.map((s) => {
      const native = s.agent === 'claude' && s.nativeName !== undefined && isModDriven(s, now);
      const product = (s.host !== undefined ? labels.get(s.host) : undefined) ?? s.product;
      return {
        name: named.get(s.id)!,
        shortName: named.get(s.id)!,
        legacyName: legacy.get(s.id)!,
        id: s.id,
        session: s.id.slice(0, SESSION_PREFIX_CHARS),
        agent: s.agent,
        route: native ? ('native' as const) : ('agent-tabs' as const),
        ...(s.agent === 'claude' && s.nativeName !== undefined ? { nativeName: s.nativeName } : {}),
        state: s.state,
        ...(s.stateAt !== undefined ? { stateAt: s.stateAt } : {}),
        harness: harnessOf(s.agent, s.via),
        model: s.model ?? null,
        effort: s.effort ?? null,
        agentType: s.agentType ?? null,
        agentColor: s.agentColor ?? null,
        where: product ?? null,
        tab: mayBeTab(s.id) ? s.id : null,
        host: product === undefined ? null : s.project !== undefined ? `${product} (${s.project})` : product,
        ide: s.host ?? null,
        path: s.path,
        folder: s.path,
        ...(s.via !== undefined ? { via: s.via } : {}),
        startedAt: s.startedAt,
        ...(s.handedOffTo !== undefined ? { handedOffTo: s.handedOffTo } : {}),
        self: s.id === this.sessionId,
      };
    });
    rows.sort(
      (a, b) =>
        agentRank(a.agent) - agentRank(b.agent) || a.agent.localeCompare(b.agent) || a.startedAt.localeCompare(b.startedAt) || a.id.localeCompare(b.id),
    );
    return { sessions: rows, ...this.warnings };
  }

  async send(input: SendInput) {
    const { text, replyTo } = input;
    if (input.to === this.sessionId) throw new MailError('to is this session; pick another id from list_sessions');
    if (text.trim() === '') throw new MailError('text is empty');
    if (text.length > MAX_TEXT_CHARS) throw new MailError(`text exceeds ${MAX_TEXT_CHARS} characters`);
    if (replyTo !== undefined) checkMessageId(replyTo, 'replyTo');
    const now = this.now();
    const live = await liveSessions(this.deps.home, this.alive, now, this.ended);
    const named = sessionNames(live);
    const legacy = shortNames(live);
    const recipient =
      live.find((s) => s.id === input.to) ?? live.find((s) => named.get(s.id) === input.to) ?? live.find((s) => legacy.get(s.id) === input.to);
    if (!recipient && !isSessionId(input.to)) throw new MailError(`not a session id: ${input.to}`);
    if (!recipient) {
      const [warning] = this.warnings.warnings ?? [];
      throw new MailError(`no live session with id or name ${input.to}; call list_sessions${warning !== undefined ? `. Warning: ${warning}` : ''}`);
    }
    const to = recipient.id;
    if (to === this.sessionId) throw new MailError('to is this session; pick another id from list_sessions');
    const id = newMessageId();
    const { duplicateOf } = await reserveSend(this.deps.home, this.sessionId, now, { id, to, digest: sendDigest(to, text, replyTo) });
    if (duplicateOf !== undefined) {
      return { id: duplicateOf, to, delivery: 'queued' as const, duplicate: true, note: 'an identical message went to this session less than a minute ago; it was not sent again' };
    }
    const message: Message = {
      id,
      from: { id: this.sessionId, agent: this.agent, path: this.deps.cwd },
      to,
      text,
      ...(replyTo !== undefined ? { replyTo } : {}),
      sentAt: new Date(now).toISOString(),
    };
    try {
      await deliver(this.deps.home, message, now);
    } catch (e) {
      await releaseSend(this.deps.home, this.sessionId, id).catch(() => undefined);
      throw e;
    }
    const wake = await this.wake(recipient, now).catch((e: unknown) => ({ delivery: 'queued' as const, note: String(e) }));
    await writeLog(this.deps.home, this.sessionId, SENT_LOG, {
      id,
      at: message.sentAt,
      route: 'agent-tabs',
      from: message.from,
      to: { id: to, ...(recipient.nativeName !== undefined ? { name: recipient.nativeName } : {}) },
      text,
      ...(replyTo !== undefined ? { replyTo } : {}),
      delivery: wake.delivery,
    }).catch(() => undefined);
    this.followUp(to);
    void this.clean().catch(() => undefined);
    return { id: message.id, to, ...wake, ...this.warnings };
  }

  private async wake(recipient: Presence, now: number): Promise<{ delivery: 'woken' | 'queued'; note?: string }> {
    if (isModDriven(recipient, now)) return { delivery: 'queued', note: MOD_DELIVERY_NOTE };
    // A line typed while the user writes a prompt lands in that prompt; see inputIdleAfter in hook.ts.
    if (effectiveState(recipient, now) !== 'idle' || recipient.inputIdle === false) return { delivery: 'queued' };
    // An agent reports idle when its turn-end hook runs, but it can still be finishing the turn, and a
    // line typed then is lost; typing only after the session stays idle for IDLE_SETTLE_MS avoids that.
    const settle = IDLE_SETTLE_MS - (now - Date.parse(recipient.stateAt ?? ''));
    if (settle > IDLE_SETTLE_MS) return { delivery: 'queued' };
    if (settle > 0) {
      await (this.deps.sleep ?? realSleep)(settle);
      now = this.now();
    }
    let host = recipient.host;
    if (host === undefined && mayBeTab(recipient.id)) host = await this.deps.hosts.findHost(recipient.id);
    if (host === undefined) return { delivery: 'queued' };
    const fields = host === recipient.host ? { host } : await this.hostFields(host);
    let claimed: PresenceFile | undefined;
    await updatePresence(this.deps.home, recipient.id, (current) => {
      if (current?.pid !== recipient.pid || current.stateAt !== recipient.stateAt || effectiveState(current, now) !== 'idle' || current.inputIdle === false) return current;
      claimed = withState({ ...current, ...fields }, 'waking', now);
      return claimed;
    });
    if (!claimed) return { delivery: 'queued' };
    let typed = await this.typeWakeLine(recipient.id, host);
    if (!typed.ok && recipient.host !== undefined && mayBeTab(recipient.id)) {
      const found = await this.deps.hosts.findHost(recipient.id).catch(() => undefined);
      if (found !== undefined && found !== host) {
        const stateAt = claimed.stateAt;
        const refound = await this.hostFields(found);
        await updatePresence(this.deps.home, recipient.id, (current) => (current !== undefined && current.stateAt === stateAt ? { ...current, ...refound } : current));
        typed = await this.typeWakeLine(recipient.id, found);
      }
    }
    if (typed.ok) return { delivery: 'woken' };
    await this.restoreFailedWake(recipient, claimed);
    return { delivery: 'queued', note: `the session was idle, but typing the wake line failed: ${typed.reason}` };
  }

  private async typeWakeLine(id: string, host: string): Promise<Awaited<ReturnType<Hosts['typeInto']>>> {
    try {
      return await this.deps.hosts.typeInto(id, host, wakeLine(this.agent, this.sessionId));
    } catch (error) {
      return { ok: false, reason: String(error) };
    }
  }

  private async restoreFailedWake(recipient: Presence, claimed: PresenceFile): Promise<void> {
    await updatePresence(this.deps.home, recipient.id, (current) =>
      current?.stateAt === claimed.stateAt && current?.state === 'waking'
        ? { ...current, state: recipient.state, ...(recipient.stateAt !== undefined ? { stateAt: recipient.stateAt } : {}) }
        : current,
    );
  }

  private async rewake(peer: string): Promise<boolean> {
    const pending = (await peekUnread(this.deps.home, peer)).some((m) => m.from.id === this.sessionId);
    if (!pending) return false;
    const now = this.now();
    const recipient = (await liveSessions(this.deps.home, this.alive, now, this.ended)).find((s) => s.id === peer);
    if (!recipient) return false;
    await this.wake(recipient, now);
    return true;
  }

  // A wake line can be lost, or the recipient can be in no state that allows one yet, so the sender keeps
  // retrying until the recipient reads the message, ends, or FOLLOW_UP_MS passes.
  private followUp(peer: string): void {
    if (this.followUps.has(peer)) return;
    const until = this.now() + FOLLOW_UP_MS;
    const stop = () => {
      clearInterval(timer);
      this.followUps.delete(peer);
    };
    const timer = setInterval(() => {
      if (this.now() >= until) return stop();
      void this.rewake(peer).then((pending) => pending || stop(), stop);
    }, this.deps.rewakeEveryMs ?? REWAKE_EVERY_MS);
    timer.unref?.();
    this.followUps.set(peer, timer);
  }

  stopFollowUps(): void {
    for (const timer of this.followUps.values()) clearInterval(timer);
    this.followUps.clear();
  }

  private async resetNudges(): Promise<void> {
    await this.updateOwn((p) => (p.nudges ? { ...p, nudges: 0 } : p)).catch(() => undefined);
  }

  async modLog(input: ModLogInput) {
    if (!NATIVE_NAME.test(input.peer)) throw new MailError('peer must be one printable line of at most 128 characters');
    if (input.direction !== 'sent' && input.direction !== 'received') throw new MailError('direction must be sent or received');
    const own = await readPresence(this.deps.home, this.sessionId);
    const self = { id: this.sessionId, agent: this.agent, path: this.deps.cwd, ...(own?.nativeName !== undefined ? { name: own.nativeName } : {}) };
    const peer = { name: input.peer };
    const id = logId(input.id);
    const at = new Date(input.at !== undefined && Number.isFinite(input.at) ? input.at : this.now()).toISOString();
    const text = input.text.slice(0, MAX_TEXT_CHARS);
    const sent = input.direction === 'sent';
    await writeLog(this.deps.home, this.sessionId, sent ? SENT_LOG : RECEIVED_LOG, {
      id,
      at,
      route: 'native',
      from: sent ? self : peer,
      to: sent ? peer : self,
      text,
      ...(input.delivery !== undefined ? { delivery: input.delivery.slice(0, 200) } : {}),
    });
    return { id };
  }

  private historyOf(who: Who) {
    if (who.id !== undefined && !isSessionId(who.id)) throw new MailError(`not a session id: ${who.id}`);
    const names = who.names.filter((n) => NATIVE_NAME.test(n)).slice(0, 8);
    if (who.id === undefined && names.length === 0) throw new MailError('history needs session or names');
    return history(this.deps.home, { ...(who.id !== undefined ? { id: who.id } : {}), names }, this.mailIndex);
  }

  async modHistory(who: Who, before?: string) {
    const items = await this.historyOf(who);
    const pool = olderThan(items, before);
    const messages = previews(pool);
    return { total: items.length, older: pool.length - messages.length, messages };
  }

  async modMessage(who: Who, id: string, offset = 0) {
    const message = (await this.historyOf(who)).find((m) => m.id === id);
    if (message === undefined) return { message: null };
    const start = Math.max(0, Math.min(Math.floor(offset), message.text.length));
    return { message: { ...message, text: '' }, text: textPiece(message.text, start), offset: start, total: message.text.length };
  }

  async sessionFolders(): Promise<string[]> {
    const live = await liveSessions(this.deps.home, this.alive, this.now(), this.ended);
    return [...new Set(live.map((s) => s.path))];
  }

  async hostId(): Promise<string | undefined> {
    return (await readPresence(this.deps.home, this.sessionId).catch(() => undefined))?.host;
  }

  async modCounts(whos: readonly Who[]) {
    const valid = whos.map((who) => ({
      ...(who.id !== undefined && isSessionId(who.id) ? { id: who.id } : {}),
      names: who.names.filter((n) => NATIVE_NAME.test(n)).slice(0, 8),
    }));
    const counts = await historyCounts(this.deps.home, valid, this.mailIndex);
    return { counts: counts.map((n, i) => (valid[i]!.id === undefined && valid[i]!.names.length === 0 ? null : n)) };
  }

  async modPresence(input: ModPresenceInput) {
    if (input.nativeName !== undefined && !NATIVE_NAME.test(input.nativeName)) {
      throw new MailError('nativeName must be one printable line of at most 128 characters');
    }
    if (input.model !== undefined && !isModel(input.model)) throw new MailError('model must be one printable line of at most 128 characters');
    if (input.effort !== undefined && !isEffort(input.effort)) throw new MailError('effort must be at most 32 letters, digits, dots, dashes or underscores');
    if (input.agentType !== undefined && !isAgentType(input.agentType)) throw new MailError('agentType must be at most 128 letters, digits, dots, colons, dashes or underscores');
    if (input.agentColor !== undefined && !isAgentColor(input.agentColor)) throw new MailError(`agentColor must be one of ${AGENT_COLORS.join(', ')}`);
    if (input.owner !== undefined && !isSessionId(input.owner)) throw new MailError(`not a session id: ${input.owner}`);
    const now = this.now();
    const claim = input.driver === true && this.isTab;
    // Only Claude Code is offered the mod tool, and its first call can beat oninitialized's setClient.
    if (this.agent === 'unknown') this.agent = 'claude';
    await this.updateOwn((p) => {
      const { driver: _d, modBeat: _b, nativeName: _n, ...rest } = { ...p, agent: this.agent };
      const kept = input.driver === false ? rest : { ...p, agent: this.agent };
      const stated = input.state !== undefined && input.state !== effectiveState(kept, now) ? withState(kept, input.state as SessionState, now) : kept;
      const driven = input.driver !== false && (claim || p.driver === 'mod');
      return {
        ...stated,
        ...(claim ? { driver: 'mod' as const } : {}),
        ...(driven ? { modBeat: now } : {}),
        ...(input.nativeName !== undefined && input.driver !== false ? { nativeName: input.nativeName } : {}),
        ...(input.model !== undefined ? { model: input.model } : {}),
        ...(input.effort !== undefined ? { effort: input.effort } : {}),
        ...(input.agentType !== undefined ? { agentType: input.agentType } : {}),
        ...(input.agentColor !== undefined && isAgentColor(input.agentColor) ? { agentColor: input.agentColor } : {}),
        ...(input.owner !== undefined ? { owner: input.owner } : {}),
      };
    });
    const own = await readPresence(this.deps.home, this.sessionId);
    return { id: this.sessionId, tab: this.isTab, driver: own?.driver === 'mod', mailbox: unreadDir(this.deps.home, this.sessionId) };
  }

  async modTake() {
    await returnStaleClaims(this.deps.home, this.sessionId, this.now());
    const { messages, names, remaining, unreadable } = await claimBatch(this.deps.home, this.sessionId, Infinity);
    const extra = { ...(remaining ? { remaining } : {}), ...(unreadable ? { unreadable } : {}) };
    if (!messages.length) return { claim: null, messages: [], ...extra };
    const claim = `c-${randomBytes(8).toString('hex')}`;
    this.claims.set(claim, names);
    return { claim, notice: UNTRUSTED_NOTICE, messages: messages.map(shown), ...extra };
  }

  async modSettle(claim: string, op: 'ack' | 'release') {
    const names = this.claims.get(claim);
    if (names === undefined) throw new MailError(`no open claim ${claim}; an unsettled claim returns its messages to unread after two minutes`);
    this.claims.delete(claim);
    const moved = await settleClaim(this.deps.home, this.sessionId, names, op === 'ack' ? 'cur' : 'new');
    if (op === 'ack') await this.resetNudges();
    return { claim, ...(op === 'ack' ? { read: moved } : { released: moved }) };
  }

  async read(signal?: AbortSignal) {
    const { messages, names, remaining, unreadable } = await takeBatch(this.deps.home, this.sessionId, { chars: MAX_READ_CHARS });
    if (signal?.aborted) {
      await putBack(this.deps.home, this.sessionId, names);
      throw new MailError('read_messages was cancelled; the messages stay unread');
    }
    await this.resetNudges();
    void this.clean().catch(() => undefined);
    const result: ReadResult = { ...(messages.length ? { notice: UNTRUSTED_NOTICE } : {}), messages: messages.map(shown), ...this.warnings };
    if (remaining) Object.assign(result, { remaining, next: `${remaining} more unread; call read_messages again` });
    if (unreadable) Object.assign(result, { unreadable, unreadableNote: `${unreadable} mailbox file(s) held no valid message and were set aside` });
    return result;
  }

  async wait(input: WaitInput, signal?: AbortSignal) {
    if (input.from !== undefined && !isSessionId(input.from)) throw new MailError(`from is not a session id: ${input.from}`);
    if (input.replyTo !== undefined) checkMessageId(input.replyTo, 'replyTo');
    const seconds = Math.min(Math.max(input.timeout ?? DEFAULT_WAIT_S, 0), this.agent === 'agy' ? AGY_MAX_WAIT_S : MAX_WAIT_S);
    const filter = { ...(input.from !== undefined ? { from: input.from } : {}), ...(input.replyTo !== undefined ? { replyTo: input.replyTo } : {}) };
    const peer = input.from;
    const retry = peer === undefined ? undefined : setInterval(() => void this.rewake(peer).catch(() => undefined), this.deps.rewakeEveryMs ?? REWAKE_EVERY_MS);
    let message: Message | undefined;
    try {
      message = await waitForMessage(this.deps.home, this.sessionId, filter, seconds * 1000, signal);
    } finally {
      if (retry) clearInterval(retry);
    }
    if (!message) return { message: null, timedOut: true, waitedSeconds: seconds };
    await this.resetNudges();
    return { notice: UNTRUSTED_NOTICE, message: shown(message) };
  }
}

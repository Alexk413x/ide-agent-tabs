import { randomBytes } from 'node:crypto';
import { promises as fs, readFileSync, rmSync } from 'node:fs';
import { AGENT_ENV, TAB_ID_ENV } from '../profiles.js';
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
import { runHook } from './hook.js';
import { UNTRUSTED_NOTICE, wakeLine } from './notice.js';
import {
  agentFromClient,
  effectiveState,
  HEARTBEAT_MS,
  IDLE_SETTLE_MS,
  isModDriven,
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
}

export interface SendInput {
  to: string;
  text: string;
  replyTo?: string;
}

export const MOD_STATES = ['idle', 'busy', 'permission'] as const;
export type ModState = (typeof MOD_STATES)[number];
export const MOD_TAKE_MAX = 10;
export const AGENT_ORDER = ['claude', 'codex', 'agy', 'copilot', 'gemini', 'grok', 'pi', 'hermes', 'opencode', 'qwen', 'goose', 'codex-local'];
export const MOD_DELIVERY_NOTE = "the recipient's Agent Tabs mod delivers it in-process once the session is idle";

export interface ModPresenceInput {
  driver?: boolean;
  nativeName?: string;
  state?: ModState;
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
const agentRank = (agent: string) => {
  const i = AGENT_ORDER.indexOf(agent);
  return i === -1 ? AGENT_ORDER.length : i;
};

interface ReadResult {
  notice?: string;
  messages: ReturnType<typeof shown>[];
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
  private threadId?: string;
  private ownHost?: Promise<string | undefined>;
  private identified: Promise<void> = Promise.resolve();
  private readonly followUps = new Map<string, ReturnType<typeof setInterval>>();
  private heartbeat?: ReturnType<typeof setInterval>;
  private readonly claims = new Map<string, string[]>();

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
    if (host !== undefined) await this.updateOwn((p) => ({ ...p, host })).catch(() => undefined);
    return host;
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
  }

  stopSync(): void {
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
    const live = await liveSessions(this.deps.home, this.alive, now);
    await cleanMail(this.deps.home, new Set(live.map((s) => s.id)), now);
  }

  async listSessions() {
    const now = this.now();
    const sessions = await liveSessions(this.deps.home, this.alive, now);
    const labels = new Map<string, string | undefined>();
    for (const host of new Set(sessions.flatMap((s) => (s.host !== undefined ? [s.host] : [])))) {
      labels.set(host, await this.deps.hosts.describeHost?.(host).catch(() => undefined));
    }
    const rows = sessions.map((s) => {
      const native = s.agent === 'claude' && s.nativeName !== undefined && isModDriven(s, now);
      const product = s.host !== undefined ? (labels.get(s.host) ?? s.host) : undefined;
      return {
        name: native ? s.nativeName! : s.id,
        id: s.id,
        agent: s.agent,
        route: native ? ('native' as const) : ('agent-tabs' as const),
        state: s.state,
        ...(s.stateAt !== undefined ? { stateAt: s.stateAt } : {}),
        tab: mayBeTab(s.id) ? s.id : null,
        host: product === undefined ? null : s.project !== undefined ? `${product} (${s.project})` : product,
        ide: s.host ?? null,
        path: s.path,
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
    return { sessions: rows };
  }

  async send(input: SendInput) {
    const { to, text, replyTo } = input;
    if (to === this.sessionId) throw new MailError('to is this session; pick another id from list_sessions');
    if (!isSessionId(to)) throw new MailError(`not a session id: ${to}`);
    if (text.trim() === '') throw new MailError('text is empty');
    if (text.length > MAX_TEXT_CHARS) throw new MailError(`text exceeds ${MAX_TEXT_CHARS} characters`);
    if (replyTo !== undefined) checkMessageId(replyTo, 'replyTo');
    const now = this.now();
    const recipient = (await liveSessions(this.deps.home, this.alive, now)).find((s) => s.id === to);
    if (!recipient) throw new MailError(`no live session with id ${to}; call list_sessions`);
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
    this.followUp(to);
    void this.clean().catch(() => undefined);
    return { id: message.id, to, ...wake };
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
    let claimed: PresenceFile | undefined;
    await updatePresence(this.deps.home, recipient.id, (current) => {
      if (current?.pid !== recipient.pid || current.stateAt !== recipient.stateAt || effectiveState(current, now) !== 'idle' || current.inputIdle === false) return current;
      claimed = withState({ ...current, host }, 'waking', now);
      return claimed;
    });
    if (!claimed) return { delivery: 'queued' };
    let typed = await this.typeWakeLine(recipient.id, host);
    if (!typed.ok && recipient.host !== undefined && mayBeTab(recipient.id)) {
      const found = await this.deps.hosts.findHost(recipient.id).catch(() => undefined);
      if (found !== undefined && found !== host) {
        const stateAt = claimed.stateAt;
        await updatePresence(this.deps.home, recipient.id, (current) => (current !== undefined && current.stateAt === stateAt ? { ...current, host: found } : current));
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
    const recipient = (await liveSessions(this.deps.home, this.alive, now)).find((s) => s.id === peer);
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

  async modPresence(input: ModPresenceInput) {
    if (input.nativeName !== undefined && !NATIVE_NAME.test(input.nativeName)) {
      throw new MailError('nativeName must be one printable line of at most 128 characters');
    }
    const now = this.now();
    const claim = input.driver === true && this.isTab;
    await this.updateOwn((p) => {
      const { driver: _d, modBeat: _b, nativeName: _n, ...rest } = p;
      const kept = input.driver === false ? rest : p;
      const stated = input.state !== undefined && input.state !== effectiveState(kept, now) ? withState(kept, input.state as SessionState, now) : kept;
      const driven = input.driver !== false && (claim || p.driver === 'mod');
      return {
        ...stated,
        ...(claim ? { driver: 'mod' as const } : {}),
        ...(driven ? { modBeat: now } : {}),
        ...(input.nativeName !== undefined && input.driver !== false ? { nativeName: input.nativeName } : {}),
      };
    });
    const own = await readPresence(this.deps.home, this.sessionId);
    return { id: this.sessionId, tab: this.isTab, driver: own?.driver === 'mod', mailbox: unreadDir(this.deps.home, this.sessionId) };
  }

  async modTake(max = MOD_TAKE_MAX) {
    const count = Math.min(Math.max(Math.trunc(max), 1), MOD_TAKE_MAX);
    await returnStaleClaims(this.deps.home, this.sessionId, this.now());
    const { messages, names, remaining, unreadable } = await claimBatch(this.deps.home, this.sessionId, count);
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
    const result: ReadResult = { ...(messages.length ? { notice: UNTRUSTED_NOTICE } : {}), messages: messages.map(shown) };
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

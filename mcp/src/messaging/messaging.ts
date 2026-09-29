import { randomBytes } from 'node:crypto';
import { readFileSync, rmSync } from 'node:fs';
import { AGENT_ENV, TAB_ID_ENV } from '../profiles.js';
import { isProcessAlive } from '../registry.js';
import {
  checkMessageId,
  cleanMail,
  deliver,
  MailError,
  MAX_TEXT_CHARS,
  newMessageId,
  reserveSend,
  takeMessages,
  waitForMessage,
  type Message,
} from './mailbox.js';
import { UNTRUSTED_NOTICE, wakeLine } from './notice.js';
import {
  agentFromClient,
  isSessionId,
  liveSessions,
  parsePresence,
  presencePath,
  updatePresence,
  withState,
  type Presence,
  type PresenceFile,
} from './sessions.js';

export const DEFAULT_WAIT_S = 60;
export const MAX_WAIT_S = 600;
const CLEAN_EVERY_MS = 60 * 60 * 1000;

export interface Hosts {
  findHost(id: string): Promise<string | undefined>;
  typeInto(id: string, host: string, text: string): Promise<{ ok: true } | { ok: false; reason: string }>;
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
}

export interface SendInput {
  to: string;
  text: string;
  replyTo?: string;
}

export interface WaitInput {
  timeout?: number;
  from?: string;
  replyTo?: string;
}

const AGENT_NAME = /^[A-Za-z0-9._-]{1,64}$/;
const generatedId = () => `s-${randomBytes(6).toString('hex')}`;

function shown(m: Message) {
  return { id: m.id, from: m.from, text: m.text, ...(m.replyTo !== undefined ? { replyTo: m.replyTo } : {}), sentAt: m.sentAt };
}

export class Messaging {
  private sessionId: string;
  private isTab: boolean;
  private agent: string;
  private readonly startedAt: string;
  private lastClean = 0;

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
    };
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
        return this.presence(current);
      });
    }
    if (taken) {
      this.isTab = false;
      this.sessionId = (this.deps.randomId ?? generatedId)();
    }
    if (!this.isTab) await updatePresence(this.deps.home, this.sessionId, (current) => this.presence(current));
    if (this.isTab) void this.resolveOwnHost().catch(() => undefined);
    void this.clean().catch(() => undefined);
  }

  private async resolveOwnHost(): Promise<void> {
    const host = await this.deps.hosts.findHost(this.sessionId);
    if (host !== undefined) await this.updateOwn((p) => ({ ...p, host }));
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
    const sessions = await liveSessions(this.deps.home, this.alive, this.now());
    return {
      sessions: sessions.map((s) => ({
        id: s.id,
        agent: s.agent,
        path: s.path,
        host: s.host ?? null,
        state: s.state,
        ...(s.stateAt !== undefined ? { stateAt: s.stateAt } : {}),
        startedAt: s.startedAt,
        self: s.id === this.sessionId,
      })),
    };
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
    await reserveSend(this.deps.home, this.sessionId, now);
    const message: Message = {
      id: newMessageId(),
      from: { id: this.sessionId, agent: this.agent, path: this.deps.cwd },
      to,
      text,
      ...(replyTo !== undefined ? { replyTo } : {}),
      sentAt: new Date(now).toISOString(),
    };
    await deliver(this.deps.home, message, now);
    const wake = await this.wake(recipient, now).catch((e: unknown) => ({ delivery: 'queued' as const, note: String(e) }));
    void this.clean().catch(() => undefined);
    return { id: message.id, to, ...wake };
  }

  private async wake(recipient: Presence, now: number): Promise<{ delivery: 'woken' | 'queued'; note?: string }> {
    if (recipient.state !== 'idle') return { delivery: 'queued' };
    let host = recipient.host;
    if (host === undefined && !recipient.id.startsWith('s-')) host = await this.deps.hosts.findHost(recipient.id);
    if (host === undefined) return { delivery: 'queued' };
    let claimed: PresenceFile | undefined;
    await updatePresence(this.deps.home, recipient.id, (current) => {
      if (current?.pid !== recipient.pid || current.state !== 'idle') return current;
      claimed = withState({ ...current, host }, 'busy', now);
      return claimed;
    });
    if (!claimed) return { delivery: 'queued' };
    const typed = await this.deps.hosts.typeInto(recipient.id, host, wakeLine(this.agent, this.sessionId));
    if (typed.ok) return { delivery: 'woken' };
    await updatePresence(this.deps.home, recipient.id, (current) =>
      current?.stateAt === claimed!.stateAt && current?.state === 'busy' ? { ...current, state: 'idle', stateAt: recipient.stateAt ?? current.stateAt } : current,
    );
    return { delivery: 'queued', note: `the session was idle, but typing the wake line failed: ${typed.reason}` };
  }

  private async resetNudges(): Promise<void> {
    await this.updateOwn((p) => (p.nudges ? { ...p, nudges: 0 } : p)).catch(() => undefined);
  }

  async read() {
    const messages = await takeMessages(this.deps.home, this.sessionId);
    await this.resetNudges();
    void this.clean().catch(() => undefined);
    return messages.length === 0 ? { messages: [] } : { notice: UNTRUSTED_NOTICE, messages: messages.map(shown) };
  }

  async wait(input: WaitInput, signal?: AbortSignal) {
    if (input.from !== undefined && !isSessionId(input.from)) throw new MailError(`from is not a session id: ${input.from}`);
    if (input.replyTo !== undefined) checkMessageId(input.replyTo, 'replyTo');
    const seconds = Math.min(Math.max(input.timeout ?? DEFAULT_WAIT_S, 0), MAX_WAIT_S);
    const filter = { ...(input.from !== undefined ? { from: input.from } : {}), ...(input.replyTo !== undefined ? { replyTo: input.replyTo } : {}) };
    const message = await waitForMessage(this.deps.home, this.sessionId, filter, seconds * 1000, signal);
    if (!message) return { message: null, timedOut: true, waitedSeconds: seconds };
    await this.resetNudges();
    return { notice: UNTRUSTED_NOTICE, message: shown(message) };
  }
}

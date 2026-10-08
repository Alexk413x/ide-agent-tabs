import { createHash, randomBytes } from 'node:crypto';
import {
  CLEAN_DEADLINE_MS,
  ensureHealthy,
  isConstraint,
  isFull,
  MailError,
  query,
  sameFile,
  StoreBusyError,
  tx,
  type Db,
  type Row,
  type SQLInputValue,
  type StoreOptions,
} from './db.js';
import { isSessionId } from './sessions.js';
import { dataVersion, onWake, removeStaleWakes, signalWake } from './wake.js';

export { MailError };

export const MAX_TEXT_CHARS = 32_000;
export const MAX_SENT_PER_MINUTE = 20;
export const MAX_UNREAD = 50;
export const MAX_READ_CHARS = 40_000;
export const KEEP_MS = 7 * 24 * 60 * 60 * 1000;
export const DEDUPE_MS = 60_000;
export const CLAIM_TIMEOUT_MS = 2 * 60_000;
export const POLL_MS = 1_000;
const MINUTE_MS = 60_000;
const CLOCK_SLACK_MS = 60_000;
const CLEAN_BATCH = 500;
const RECHECK_POLLS = 10;
const MESSAGE_ID = /^m-[0-9a-f]{16}$/;
const CLAIM_ID = /^c-[0-9a-f]{16}$/;

export interface Sender {
  id: string;
  agent: string;
  path: string;
}

export interface Message {
  id: string;
  from: Sender;
  to: string;
  text: string;
  replyTo?: string;
  sentAt: string;
}

export interface MessageFilter {
  from?: string;
  replyTo?: string;
}

export interface Batch {
  messages: Message[];
  ids: string[];
  remaining: number;
  unreadable: number;
}

export interface BatchLimits {
  filter?: MessageFilter;
  count?: number;
  chars?: number;
}

export interface Party {
  id?: string;
  name?: string;
  agent?: string;
  path?: string;
}

export type Route = 'agent-tabs' | 'native';
export type MailState = 'unread' | 'held' | 'read';

export interface StoredMessage {
  id: string;
  route: Route;
  owner?: string;
  direction?: 'sent' | 'received';
  from: Party;
  to: Party;
  text: string;
  replyTo?: string;
  sentAt: string;
  delivery?: string;
  state?: MailState;
}

export const newMessageId = () => `m-${randomBytes(8).toString('hex')}`;
const newClaimId = () => `c-${randomBytes(8).toString('hex')}`;

export function checkMessageId(id: string, field: string): void {
  if (!MESSAGE_ID.test(id)) throw new MailError(`${field} must be a message id such as m-0123456789abcdef`);
}

export const sendDigest = (to: string, text: string, replyTo?: string) =>
  createHash('sha256').update(JSON.stringify([to, replyTo ?? '', text])).digest('hex');

const COLUMNS = 'seq, id, route, owner, direction, from_id, from_name, from_agent, from_path, to_id, to_name, to_agent, to_path, text, reply_to, sent_at, delivery, state';
const PENDING = "(state = 'unread' OR (state = 'held' AND (state_ms <= ? OR state_ms > ?)))";
const pendingArgs = (now: number) => [now - CLAIM_TIMEOUT_MS, now + CLOCK_SLACK_MS];

const str = (v: unknown) => (typeof v === 'string' ? v : undefined);

function toMessage(r: Row): Message {
  const replyTo = str(r.reply_to);
  return {
    id: String(r.id),
    from: { id: String(r.from_id), agent: str(r.from_agent) ?? '', path: str(r.from_path) ?? '' },
    to: String(r.to_id),
    text: String(r.text),
    ...(replyTo !== undefined ? { replyTo } : {}),
    sentAt: String(r.sent_at),
  };
}

function party(r: Row, side: 'from' | 'to'): Party {
  const out: Party = {};
  for (const k of ['id', 'name', 'agent', 'path'] as const) {
    const v = str(r[`${side}_${k}`]);
    if (v !== undefined) out[k] = v;
  }
  return out;
}

function toStored(r: Row): StoredMessage {
  const optional = (key: string, value: unknown) => (typeof value === 'string' ? { [key]: value } : {});
  return {
    id: String(r.id),
    route: r.route as Route,
    ...optional('owner', r.owner),
    ...optional('direction', r.direction),
    from: party(r, 'from'),
    to: party(r, 'to'),
    text: String(r.text),
    ...optional('replyTo', r.reply_to),
    sentAt: String(r.sent_at),
    ...optional('delivery', r.delivery),
    ...optional('state', r.state),
  } as StoredMessage;
}

const count = (db: Db, sql: string, ...args: SQLInputValue[]) => Number(db.prepare(sql).get(...args)!.n);

export interface Outgoing {
  from: Sender & { name?: string };
  to: string;
  toName?: string;
  text: string;
  replyTo?: string;
}

export interface Sent {
  id: string;
  duplicate?: true;
}

function insertChecked(db: Db, out: Outgoing, id: string, digest: string, now: number): Sent {
  const dup = db
    .prepare("SELECT id FROM messages WHERE route = 'agent-tabs' AND from_id = ? AND to_id = ? AND digest = ? AND sent_ms > ? ORDER BY seq DESC LIMIT 1")
    .get(out.from.id, out.to, digest, now - DEDUPE_MS);
  if (dup !== undefined) return { id: String(dup.id), duplicate: true };
  if (count(db, "SELECT count(*) AS n FROM messages WHERE route = 'agent-tabs' AND from_id = ? AND sent_ms > ?", out.from.id, now - MINUTE_MS) >= MAX_SENT_PER_MINUTE) {
    throw new MailError(`this session sent ${MAX_SENT_PER_MINUTE} messages in the last minute; wait before sending more`);
  }
  if (count(db, "SELECT count(*) AS n FROM messages WHERE route = 'agent-tabs' AND to_id = ? AND state = 'unread'", out.to) >= MAX_UNREAD) {
    throw new MailError(`session ${out.to} already has ${MAX_UNREAD} unread messages; wait until it reads them`);
  }
  db.prepare(
    `INSERT INTO messages (id, route, from_id, from_name, from_agent, from_path, to_id, to_name, text, reply_to, sent_at, sent_ms, digest, state, state_ms)
     VALUES (?, 'agent-tabs', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'unread', ?)`,
  ).run(id, out.from.id, out.from.name ?? null, out.from.agent, out.from.path, out.to, out.toName ?? null, out.text, out.replyTo ?? null, new Date(now).toISOString(), now, digest, now);
  return { id };
}

export async function sendMessage(home: string, out: Outgoing, now = Date.now(), options: StoreOptions = {}): Promise<Sent> {
  if (out.text.length > MAX_TEXT_CHARS) throw new MailError(`text exceeds ${MAX_TEXT_CHARS} characters`);
  if (!isSessionId(out.to)) throw new MailError(`not a session id: ${out.to}`);
  const digest = sendDigest(out.to, out.text, out.replyTo);
  for (let attempt = 0; ; attempt++) {
    const id = newMessageId();
    try {
      const sent = await tx(home, (db) => insertChecked(db, out, id, digest, now), { label: 'send', ...options });
      if (!sent.duplicate) await signalWake(home, out.to);
      return sent;
    } catch (e) {
      if (isConstraint(e) && attempt === 0) continue;
      if (isFull(e)) throw new MailError('the disk is full; the message was not sent');
      throw e;
    }
  }
}

export async function setDelivery(home: string, id: string, delivery: string): Promise<void> {
  await query(home, (db) => db.prepare("UPDATE messages SET delivery = ? WHERE route = 'agent-tabs' AND id = ?").run(delivery, id));
}

function returnStale(db: Db, id: string, now: number): void {
  db.prepare("UPDATE messages SET state = 'unread', claim = NULL, state_ms = ? WHERE route = 'agent-tabs' AND to_id = ? AND state = 'held' AND (state_ms <= ? OR state_ms > ?)").run(
    now,
    id,
    ...pendingArgs(now),
  );
}

function unreadRows(db: Db, id: string, filter: MessageFilter): Row[] {
  const where = ["route = 'agent-tabs'", 'to_id = ?', "state = 'unread'"];
  const args: SQLInputValue[] = [id];
  if (filter.from !== undefined) {
    where.push('from_id = ?');
    args.push(filter.from);
  }
  if (filter.replyTo !== undefined) {
    where.push('reply_to = ?');
    args.push(filter.replyTo);
  }
  return db.prepare(`SELECT ${COLUMNS} FROM messages WHERE ${where.join(' AND ')} ORDER BY seq`).all(...args);
}

function pick(rows: Row[], count: number, chars: number): { picked: Row[]; remaining: number } {
  const picked: Row[] = [];
  let size = 0;
  let remaining = 0;
  for (const row of rows) {
    const length = String(row.text).length;
    if (picked.length >= count || (picked.length > 0 && size + length > chars)) {
      remaining++;
      continue;
    }
    size += length;
    picked.push(row);
  }
  return { picked, remaining };
}

export async function takeBatch(home: string, id: string, limits: BatchLimits = {}, now = Date.now(), options: StoreOptions = {}): Promise<Batch> {
  const { filter = {}, count = Infinity, chars = Infinity } = limits;
  return tx(
    home,
    (db) => {
      returnStale(db, id, now);
      const { picked, remaining } = pick(unreadRows(db, id, filter), count, chars);
      const mark = db.prepare("UPDATE messages SET state = 'read', state_ms = ? WHERE seq = ? AND state = 'unread'");
      for (const row of picked) mark.run(now, row.seq as number);
      const messages = picked.map(toMessage);
      return { messages, ids: messages.map((m) => m.id), remaining, unreadable: 0 };
    },
    { label: 'read', ...options },
  );
}

export async function putBack(home: string, id: string, ids: readonly string[], now = Date.now()): Promise<void> {
  if (!ids.length) return;
  await tx(
    home,
    (db) => {
      const back = db.prepare("UPDATE messages SET state = 'unread', state_ms = ? WHERE route = 'agent-tabs' AND to_id = ? AND id = ? AND state = 'read'");
      for (const m of ids) back.run(now, id, m);
    },
    { label: 'put back' },
  );
}

export interface Claimed extends Batch {
  claim: string | null;
}

export async function claimBatch(home: string, id: string, count: number, chars = MAX_READ_CHARS, now = Date.now()): Promise<Claimed> {
  return tx(
    home,
    (db) => {
      returnStale(db, id, now);
      const { picked, remaining } = pick(unreadRows(db, id, {}), count, chars);
      const messages = picked.map(toMessage);
      const claim = picked.length ? newClaimId() : null;
      const hold = db.prepare("UPDATE messages SET state = 'held', claim = ?, state_ms = ? WHERE seq = ? AND state = 'unread'");
      for (const row of picked) hold.run(claim, now, row.seq as number);
      return { claim, messages, ids: messages.map((m) => m.id), remaining, unreadable: 0 };
    },
    { label: 'claim' },
  );
}

export async function settleClaim(home: string, id: string, claim: string, op: 'ack' | 'release', now = Date.now()): Promise<number> {
  if (!CLAIM_ID.test(claim)) return 0;
  return tx(
    home,
    (db) =>
      Number(
        db
          .prepare("UPDATE messages SET state = ?, claim = NULL, state_ms = ? WHERE route = 'agent-tabs' AND to_id = ? AND claim = ? AND state = 'held'")
          .run(op === 'ack' ? 'read' : 'unread', now, id, claim).changes,
      ),
    { label: op },
  );
}

export async function peekUnread(home: string, id: string, now = Date.now(), options: StoreOptions = {}): Promise<Message[]> {
  return query(
    home,
    (db) => db.prepare(`SELECT ${COLUMNS} FROM messages WHERE route = 'agent-tabs' AND to_id = ? AND ${PENDING} ORDER BY seq`).all(id, ...pendingArgs(now)).map(toMessage),
    options,
  );
}

export async function hasUnread(home: string, id: string, filter: MessageFilter = {}, now = Date.now()): Promise<boolean> {
  const where = ["route = 'agent-tabs'", 'to_id = ?', PENDING];
  const args: SQLInputValue[] = [id, ...pendingArgs(now)];
  if (filter.from !== undefined) {
    where.push('from_id = ?');
    args.push(filter.from);
  }
  if (filter.replyTo !== undefined) {
    where.push('reply_to = ?');
    args.push(filter.replyTo);
  }
  return query(home, (db) => db.prepare(`SELECT EXISTS (SELECT 1 FROM messages WHERE ${where.join(' AND ')}) AS hit`).get(...args)!.hit === 1);
}

export const MAX_SENDERS = 20;

export async function unreadSummary(home: string, id: string, now = Date.now()): Promise<{ count: number; senders: string[] }> {
  return query(home, (db) => {
    const n = count(db, `SELECT count(*) AS n FROM messages WHERE route = 'agent-tabs' AND to_id = ? AND ${PENDING}`, id, ...pendingArgs(now));
    const senders = n
      ? db
          .prepare(`SELECT from_id FROM messages WHERE route = 'agent-tabs' AND to_id = ? AND ${PENDING} GROUP BY from_id ORDER BY max(seq) DESC LIMIT ?`)
          .all(id, ...pendingArgs(now), MAX_SENDERS)
          .map((r) => String(r.from_id))
      : [];
    return { count: n, senders };
  });
}

export async function mailTo(home: string, id: string): Promise<Message[]> {
  return query(home, (db) =>
    db.prepare(`SELECT ${COLUMNS} FROM messages WHERE route = 'agent-tabs' AND to_id = ? AND state IN ('unread', 'read') ORDER BY seq`).all(id).map(toMessage),
  );
}

export interface NativeRecord {
  id: string;
  owner: string;
  direction: 'sent' | 'received';
  from: Party;
  to: Party;
  text: string;
  sentAt: string;
  delivery?: string;
}

export async function logNative(home: string, r: NativeRecord): Promise<void> {
  if (r.text.length > MAX_TEXT_CHARS) throw new MailError(`text exceeds ${MAX_TEXT_CHARS} characters`);
  const at = Date.parse(r.sentAt);
  await query(home, (db) =>
    db
      .prepare(
        `INSERT OR IGNORE INTO messages (id, route, owner, direction, from_id, from_name, from_agent, from_path, to_id, to_name, to_agent, to_path, text, sent_at, sent_ms, delivery)
         VALUES (?, 'native', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
      )
      .run(
        r.id,
        r.owner,
        r.direction,
        r.from.id ?? null,
        r.from.name ?? null,
        r.from.agent ?? null,
        r.from.path ?? null,
        r.to.id ?? null,
        r.to.name ?? null,
        r.to.agent ?? null,
        r.to.path ?? null,
        r.text,
        r.sentAt,
        Number.isFinite(at) ? at : Date.now(),
        r.delivery ?? null,
      ),
  );
}

export async function storedFor(home: string, who: { id?: string; names: readonly string[] }): Promise<StoredMessage[]> {
  const names = [...who.names];
  const marks = names.map(() => '?').join(', ');
  const byName = names.length ? ` OR from_name IN (${marks}) OR to_name IN (${marks})` : '';
  const id = who.id ?? null;
  return query(home, (db) =>
    db
      .prepare(`SELECT ${COLUMNS} FROM messages WHERE from_id = ? OR to_id = ? OR owner = ?${byName} ORDER BY seq`)
      .all(id, id, id, ...names, ...names)
      .map(toStored),
  );
}

export interface WaitOptions {
  watch?: boolean;
  pollMs?: number;
  now?: () => number;
}

export async function waitForMessage(
  home: string,
  id: string,
  filter: MessageFilter,
  timeoutMs: number,
  signal?: AbortSignal,
  options: WaitOptions = {},
): Promise<Message | undefined> {
  const now = options.now ?? Date.now;
  const pollMs = options.pollMs ?? POLL_MS;
  const deadline = Date.now() + timeoutMs;
  let dirty = true;
  let wake: () => void = () => {
    dirty = true;
  };
  const stopWatch = options.watch === false ? () => undefined : onWake(home, id, () => wake());
  let version = await dataVersion(home).catch(() => undefined);
  let polls = 0;
  try {
    for (;;) {
      if (dirty) {
        dirty = false;
        if (await hasUnread(home, id, filter, now())) {
          const batch = await takeBatch(home, id, { filter, count: 1 }, now());
          if (batch.messages.length && signal?.aborted) {
            await putBack(home, id, batch.ids, now());
            return undefined;
          }
          if (batch.messages.length) return batch.messages[0];
        }
      }
      const left = deadline - Date.now();
      if (left <= 0 || signal?.aborted) return undefined;
      if (dirty) continue;
      const woken = await new Promise<boolean>((resolve) => {
        const done = (byWake: boolean) => {
          clearTimeout(timer);
          signal?.removeEventListener('abort', aborted);
          wake = () => {
            dirty = true;
          };
          resolve(byWake);
        };
        const aborted = () => done(false);
        const timer = setTimeout(() => done(false), Math.min(left, pollMs));
        signal?.addEventListener('abort', aborted, { once: true });
        wake = () => done(true);
      });
      if (woken) {
        dirty = true;
        continue;
      }
      const current = await dataVersion(home).catch(() => undefined);
      if (current === undefined || current !== version || ++polls % RECHECK_POLLS === 0) dirty = true;
      version = current;
    }
  } finally {
    stopWatch();
  }
}

async function deleteInBatches(home: string, sql: string, args: SQLInputValue[]): Promise<void> {
  for (;;) {
    const changes = await tx(home, (db) => Number(db.prepare(sql).run(...args).changes), { deadlineMs: CLEAN_DEADLINE_MS });
    if (changes < CLEAN_BATCH) return;
  }
}

export async function cleanStore(home: string, live: ReadonlySet<string>, now = Date.now()): Promise<void> {
  try {
    await sameFile(home);
    if (!(await ensureHealthy(home, now))) return;
    const cutoff = now - KEEP_MS;
    await deleteInBatches(
      home,
      `DELETE FROM messages WHERE seq IN (SELECT seq FROM messages WHERE sent_ms < ? AND (route = 'native' OR state = 'read') LIMIT ${CLEAN_BATCH})`,
      [cutoff],
    );
    const boxes = await query(
      home,
      (db) =>
        db
          .prepare(
            `SELECT to_id, max(max(sent_ms, coalesce(state_ms, 0))) AS newest FROM messages
             WHERE route = 'agent-tabs' AND to_id IN (SELECT DISTINCT to_id FROM messages WHERE route = 'agent-tabs' AND state IN ('unread', 'held'))
             GROUP BY to_id`,
          )
          .all(),
      { deadlineMs: CLEAN_DEADLINE_MS },
    );
    for (const box of boxes) {
      const to = String(box.to_id);
      if (live.has(to) || Number(box.newest) >= cutoff) continue;
      await deleteInBatches(
        home,
        `DELETE FROM messages WHERE seq IN (SELECT seq FROM messages WHERE route = 'agent-tabs' AND to_id = ? AND state IN ('unread', 'held') LIMIT ${CLEAN_BATCH})`,
        [to],
      );
    }
    await query(home, (db) => db.prepare('PRAGMA wal_checkpoint(TRUNCATE)').get(), { deadlineMs: CLEAN_DEADLINE_MS });
    await removeStaleWakes(home, live, cutoff);
  } catch (e) {
    if (e instanceof StoreBusyError) return;
    throw e;
  }
}

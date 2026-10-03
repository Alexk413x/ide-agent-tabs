import { createHash, randomBytes } from 'node:crypto';
import { promises as fs, watch, type FSWatcher } from 'node:fs';
import path from 'node:path';
import { ensurePrivateDir, readTextIfExists, withFileLock, writeAtomically } from '../files.js';
import { isSessionId } from './sessions.js';

export const MAIL_DIR = 'mail';
export const MAX_TEXT_CHARS = 32_000;
export const MAX_SENT_PER_MINUTE = 20;
export const MAX_UNREAD = 50;
export const MAX_READ_CHARS = 40_000;
export const KEEP_MS = 7 * 24 * 60 * 60 * 1000;
const TMP_MAX_AGE_MS = 60 * 60 * 1000;
const MINUTE_MS = 60_000;
const POLL_MS = 1_000;
const SENT_FILE = 'sent.json';
const MESSAGE_ID = /^m-[0-9a-f]{16}$/;

export class MailError extends Error {}

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

export const mailboxDir = (home: string, id: string) => path.join(home, MAIL_DIR, id);
const sub = (home: string, id: string, name: 'tmp' | 'new' | 'cur' | 'bad') => path.join(mailboxDir(home, id), name);

export const newMessageId = () => `m-${randomBytes(8).toString('hex')}`;

export function parseMessage(text: string | undefined): Message | undefined {
  if (text === undefined) return undefined;
  try {
    const m = JSON.parse(text) as Partial<Message>;
    const from = m.from as Partial<Sender> | undefined;
    if (
      typeof m.id !== 'string' ||
      typeof m.to !== 'string' ||
      typeof m.text !== 'string' ||
      typeof m.sentAt !== 'string' ||
      typeof from?.id !== 'string' ||
      typeof from.agent !== 'string' ||
      typeof from.path !== 'string' ||
      (m.replyTo !== undefined && typeof m.replyTo !== 'string')
    ) {
      return undefined;
    }
    return {
      id: m.id,
      from: { id: from.id, agent: from.agent, path: from.path },
      to: m.to,
      text: m.text,
      ...(m.replyTo !== undefined ? { replyTo: m.replyTo } : {}),
      sentAt: m.sentAt,
    };
  } catch {
    return undefined;
  }
}

export function checkMessageId(id: string, field: string): void {
  if (!MESSAGE_ID.test(id)) throw new MailError(`${field} must be a message id such as m-0123456789abcdef`);
}

async function ensureMailbox(home: string, id: string): Promise<void> {
  for (const name of ['tmp', 'new', 'cur'] as const) await ensurePrivateDir(sub(home, id, name));
}

export async function unreadNames(home: string, id: string): Promise<string[]> {
  const names = await fs.readdir(sub(home, id, 'new')).catch(() => [] as string[]);
  return names.filter((n) => n.endsWith('.json')).sort();
}

export async function peekUnread(home: string, id: string): Promise<Message[]> {
  const dir = sub(home, id, 'new');
  const messages: Message[] = [];
  for (const name of await unreadNames(home, id)) {
    const message = parseMessage(await readTextIfExists(path.join(dir, name)).catch(() => undefined));
    if (message) messages.push(message);
  }
  return messages;
}

export const DEDUPE_MS = 60_000;

export interface SendRecord {
  id: string;
  to: string;
  digest: string;
}

interface SentEntry extends Partial<SendRecord> {
  at: number;
}

function parseSent(text: string | undefined, now: number): SentEntry[] {
  let json: unknown;
  try {
    json = JSON.parse(text ?? '[]');
  } catch {
    return [];
  }
  if (!Array.isArray(json)) return [];
  const keep = Math.max(MINUTE_MS, DEDUPE_MS);
  return json
    .map((e): SentEntry | undefined => (typeof e === 'number' ? { at: e } : typeof e?.at === 'number' ? (e as SentEntry) : undefined))
    .filter((e): e is SentEntry => e !== undefined && now - e.at < keep);
}

export const sendDigest = (to: string, text: string, replyTo?: string) =>
  createHash('sha256').update(JSON.stringify([to, replyTo ?? '', text])).digest('hex');

export async function reserveSend(home: string, senderId: string, now = Date.now(), record?: SendRecord): Promise<{ duplicateOf?: string }> {
  const file = path.join(mailboxDir(home, senderId), SENT_FILE);
  return withFileLock(file, async () => {
    const entries = parseSent(await readTextIfExists(file), now);
    const same = record && entries.find((e) => e.id !== undefined && e.to === record.to && e.digest === record.digest && now - e.at < DEDUPE_MS);
    if (same) return { duplicateOf: same.id };
    if (entries.filter((e) => now - e.at < MINUTE_MS).length >= MAX_SENT_PER_MINUTE) {
      throw new MailError(`this session sent ${MAX_SENT_PER_MINUTE} messages in the last minute; wait before sending more`);
    }
    await writeAtomically(file, JSON.stringify([...entries, { at: now, ...record }]));
    return {};
  });
}

export async function releaseSend(home: string, senderId: string, id: string): Promise<void> {
  const file = path.join(mailboxDir(home, senderId), SENT_FILE);
  await withFileLock(file, async () => {
    const entries = parseSent(await readTextIfExists(file), Date.now());
    const left = entries.filter((e) => e.id !== id);
    if (left.length !== entries.length) await writeAtomically(file, JSON.stringify(left));
  });
}

export async function deliver(home: string, message: Message, now = Date.now()): Promise<void> {
  if (message.text.length > MAX_TEXT_CHARS) throw new MailError(`text exceeds ${MAX_TEXT_CHARS} characters`);
  if (!isSessionId(message.to)) throw new MailError(`not a session id: ${message.to}`);
  await ensureMailbox(home, message.to);
  const name = `${now}-${message.id}.json`;
  const temp = path.join(sub(home, message.to, 'tmp'), name);
  await withFileLock(path.join(mailboxDir(home, message.to), 'deliver'), async () => {
    if ((await unreadNames(home, message.to)).length >= MAX_UNREAD) {
      throw new MailError(`session ${message.to} already has ${MAX_UNREAD} unread messages; wait until it reads them`);
    }
    await fs.writeFile(temp, JSON.stringify(message, null, 2), { mode: 0o600, flag: 'wx' });
    try {
      await fs.rename(temp, path.join(sub(home, message.to, 'new'), name));
    } catch (e) {
      await fs.rm(temp, { force: true });
      throw e;
    }
  });
}

const RETRY_CODES = new Set(['EPERM', 'EBUSY', 'EACCES']);

async function move(home: string, id: string, name: string, from: 'new' | 'cur', to: 'new' | 'cur' | 'bad'): Promise<boolean> {
  await ensurePrivateDir(sub(home, id, to));
  const source = path.join(sub(home, id, from), name);
  const target = path.join(sub(home, id, to), name);
  for (let attempt = 0; ; attempt++) {
    try {
      await fs.rename(source, target);
      const now = new Date();
      await fs.utimes(target, now, now).catch(() => undefined);
      return true;
    } catch (e) {
      const code = (e as NodeJS.ErrnoException).code ?? '';
      if (code === 'ENOENT') return false;
      // Windows refuses a rename while another process has the file open, such as a hook reading it.
      if (!RETRY_CODES.has(code) || attempt >= 5) throw e;
      await new Promise((r) => setTimeout(r, 20));
    }
  }
}

const matches = (m: Message, filter: MessageFilter) =>
  (filter.from === undefined || m.from.id === filter.from) && (filter.replyTo === undefined || m.replyTo === filter.replyTo);

export interface Batch {
  messages: Message[];
  names: string[];
  remaining: number;
  unreadable: number;
}

export interface BatchLimits {
  filter?: MessageFilter;
  count?: number;
  chars?: number;
}

// Node on Windows renames through an open handle, so two readers can both "move" one file. The lock makes
// sure only one of them returns it.
export async function takeBatch(home: string, id: string, limits: BatchLimits = {}): Promise<Batch> {
  const { filter = {}, count = Infinity, chars = Infinity } = limits;
  const batch: Batch = { messages: [], names: [], remaining: 0, unreadable: 0 };
  await ensureMailbox(home, id);
  if ((await unreadNames(home, id)).length === 0) return batch;
  return withFileLock(path.join(mailboxDir(home, id), 'read'), async () => {
    let size = 0;
    for (const name of await unreadNames(home, id)) {
      let text: string | undefined;
      try {
        text = await readTextIfExists(path.join(sub(home, id, 'new'), name));
      } catch {
        continue;
      }
      if (text === undefined) continue;
      const message = parseMessage(text);
      if (!message) {
        if (await move(home, id, name, 'new', 'bad')) batch.unreadable++;
        continue;
      }
      if (!matches(message, filter)) continue;
      const full = batch.messages.length >= count || (batch.messages.length > 0 && size + message.text.length > chars);
      if (full) {
        batch.remaining++;
        continue;
      }
      if (!(await move(home, id, name, 'new', 'cur'))) continue;
      size += message.text.length;
      batch.messages.push(message);
      batch.names.push(name);
    }
    return batch;
  });
}

export async function takeMessages(home: string, id: string, filter: MessageFilter = {}, limit = Infinity): Promise<Message[]> {
  return (await takeBatch(home, id, { filter, count: limit })).messages;
}

export async function putBack(home: string, id: string, names: string[]): Promise<void> {
  for (const name of names) await move(home, id, name, 'cur', 'new');
}

export async function waitForMessage(
  home: string,
  id: string,
  filter: MessageFilter,
  timeoutMs: number,
  signal?: AbortSignal,
): Promise<Message | undefined> {
  await ensureMailbox(home, id);
  const deadline = Date.now() + timeoutMs;
  let dirty = false;
  let wake = () => {
    dirty = true;
  };
  let watcher: FSWatcher | undefined;
  try {
    watcher = watch(sub(home, id, 'new'), () => wake());
    watcher.on('error', () => undefined);
  } catch {
    watcher = undefined;
  }
  try {
    for (;;) {
      dirty = false;
      const { messages, names } = await takeBatch(home, id, { filter, count: 1 });
      if (messages.length && signal?.aborted) {
        await putBack(home, id, names);
        return undefined;
      }
      if (messages.length) return messages[0];
      const left = deadline - Date.now();
      if (left <= 0 || signal?.aborted) return undefined;
      if (dirty) continue;
      await new Promise<void>((resolve) => {
        const done = () => {
          clearTimeout(timer);
          signal?.removeEventListener('abort', done);
          wake = () => {
            dirty = true;
          };
          resolve();
        };
        const timer = setTimeout(done, Math.min(left, POLL_MS));
        signal?.addEventListener('abort', done, { once: true });
        wake = done;
      });
    }
  } finally {
    watcher?.close();
  }
}

async function newestMtime(dir: string): Promise<number> {
  let newest = (await fs.stat(dir).catch(() => undefined))?.mtimeMs ?? 0;
  for (const name of ['tmp', 'new', 'cur', 'bad', SENT_FILE]) {
    const file = path.join(dir, name);
    newest = Math.max(newest, (await fs.stat(file).catch(() => undefined))?.mtimeMs ?? 0);
    for (const child of await fs.readdir(file).catch(() => [] as string[])) {
      newest = Math.max(newest, (await fs.stat(path.join(file, child)).catch(() => undefined))?.mtimeMs ?? 0);
    }
  }
  return newest;
}

async function removeOlder(dir: string, maxAgeMs: number, now: number): Promise<void> {
  for (const name of await fs.readdir(dir).catch(() => [] as string[])) {
    const file = path.join(dir, name);
    const stat = await fs.stat(file).catch(() => undefined);
    if (stat?.isFile() && now - stat.mtimeMs > maxAgeMs) await fs.rm(file, { force: true }).catch(() => undefined);
  }
}

export async function cleanMail(home: string, liveIds: Set<string>, now = Date.now()): Promise<void> {
  const root = path.join(home, MAIL_DIR);
  for (const id of await fs.readdir(root).catch(() => [] as string[])) {
    const dir = path.join(root, id);
    if (!liveIds.has(id) && now - (await newestMtime(dir)) > KEEP_MS) {
      await fs.rm(dir, { recursive: true, force: true }).catch(() => undefined);
      continue;
    }
    await removeOlder(path.join(dir, 'cur'), KEEP_MS, now);
    await removeOlder(path.join(dir, 'bad'), KEEP_MS, now);
    await removeOlder(path.join(dir, 'tmp'), TMP_MAX_AGE_MS, now);
  }
}

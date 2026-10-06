import { promises as fs } from 'node:fs';
import path from 'node:path';
import { readTextIfExists, writeNewPrivateFile } from '../files.js';
import { MAIL_DIR, mailboxDir, newMessageId, parseMessage } from './mailbox.js';
import { isSessionId } from './sessions.js';

export const SENT_LOG = 'sent-log';
export const RECEIVED_LOG = 'received-log';
export const HISTORY_MAX = 500;
// Claude Code replaces an MCP result over its output limit (about 25,000 tokens) with an error, so the mod's
// history reply stays well under it: message texts cut to a preview, and only the newest that fit.
export const HISTORY_REPLY_CHARS = 60_000;
export const PREVIEW_CHARS = 200;
export const PIECE_CHARS = 50_000;

export function textPiece(text: string, offset: number, budget = PIECE_CHARS): string {
  let n = Math.min(budget, Math.max(0, text.length - offset));
  while (n > 1 && JSON.stringify(text.slice(offset, offset + n)).length > budget) n = Math.floor(n / 2);
  const last = text.charCodeAt(offset + n - 1);
  if (n > 1 && offset + n < text.length && last >= 0xd800 && last <= 0xdbff) n--;
  return text.slice(offset, offset + n);
}

export function olderThan(items: readonly HistoryItem[], before: string | undefined): readonly HistoryItem[] {
  if (before === undefined) return items;
  const at = items.findIndex((m) => m.id === before);
  if (at !== -1) return items.slice(0, at);
  return items.filter((m) => m.at < before);
}
const NATIVE_SAME_MS = 120_000;
const MAILBOX_STATES = [
  ['new', 'unread'],
  ['held', 'delivering'],
  ['cur', 'read'],
] as const;

export type Route = 'agent-tabs' | 'native';

export interface Party {
  id?: string;
  name?: string;
  agent?: string;
  path?: string;
}

export interface LogRecord {
  id: string;
  at: string;
  route: Route;
  from: Party;
  to: Party;
  text: string;
  replyTo?: string;
  delivery?: string;
  status?: 'unread' | 'delivering' | 'read';
}

export interface HistoryItem extends LogRecord {
  direction: 'sent' | 'received';
  peer: Party;
}

export interface Who {
  id?: string;
  names: string[];
}

function party(value: unknown): Party | undefined {
  if (typeof value !== 'object' || value === null) return undefined;
  const o = value as Record<string, unknown>;
  const out: Party = {};
  for (const k of ['id', 'name', 'agent', 'path'] as const) if (typeof o[k] === 'string') out[k] = o[k] as string;
  return out;
}

export function parseRecord(text: string | undefined): LogRecord | undefined {
  if (text === undefined) return undefined;
  try {
    const o = JSON.parse(text) as Record<string, unknown>;
    const from = party(o.from);
    const to = party(o.to);
    if (typeof o.id !== 'string' || typeof o.at !== 'string' || typeof o.text !== 'string' || !from || !to) return undefined;
    if (o.route !== 'agent-tabs' && o.route !== 'native') return undefined;
    const status = ['unread', 'delivering', 'read'].includes(o.status as string) ? { status: o.status as LogRecord['status'] } : {};
    return {
      id: o.id,
      at: o.at,
      route: o.route,
      from,
      to,
      text: o.text,
      ...(typeof o.replyTo === 'string' ? { replyTo: o.replyTo } : {}),
      ...(typeof o.delivery === 'string' ? { delivery: o.delivery } : {}),
      ...status,
    };
  } catch {
    return undefined;
  }
}

export async function writeLog(home: string, owner: string, folder: typeof SENT_LOG | typeof RECEIVED_LOG, record: LogRecord): Promise<void> {
  if (!isSessionId(owner)) return;
  const file = path.join(mailboxDir(home, owner), folder, `${Date.parse(record.at)}-${record.id}.json`);
  await writeNewPrivateFile(file, JSON.stringify(record, null, 2));
}

export const logId = (id: string | undefined) => (id !== undefined && /^m-[0-9a-f]{16}$/.test(id) ? id : newMessageId());

async function jsonFiles(dir: string): Promise<string[]> {
  const names = await fs.readdir(dir).catch(() => [] as string[]);
  return names.filter((n) => n.endsWith('.json')).map((n) => path.join(dir, n));
}

const matches = (who: Who, p: Party) => (who.id !== undefined && p.id === who.id) || (p.name !== undefined && who.names.includes(p.name));

function sameNative(a: HistoryItem, b: HistoryItem): boolean {
  return a.route === 'native' && b.route === 'native' && a.direction === b.direction && a.text === b.text && Math.abs(Date.parse(a.at) - Date.parse(b.at)) < NATIVE_SAME_MS;
}

type Source = typeof SENT_LOG | typeof RECEIVED_LOG | (typeof MAILBOX_STATES)[number][1];

interface Collected {
  owner: string;
  source: Source;
  record: LogRecord;
}

// Log and mailbox files are written once under their name (a status change moves the file to another
// folder), so a parsed file is kept by its path, and a refresh reads only the files it hasn't seen.
export class MailIndex {
  private files = new Map<string, Collected>();

  async collect(home: string): Promise<Collected[]> {
    const root = path.join(home, MAIL_DIR);
    const seen = new Map<string, Collected>();
    const folders: [string, Source, 'log' | 'mailbox'][] = [
      [SENT_LOG, SENT_LOG, 'log'],
      [RECEIVED_LOG, RECEIVED_LOG, 'log'],
      ...MAILBOX_STATES.map(([folder, status]): [string, Source, 'mailbox'] => [folder, status, 'mailbox']),
    ];
    for (const owner of (await fs.readdir(root).catch(() => [] as string[])).filter(isSessionId)) {
      for (const [folder, source, kind] of folders) {
        for (const file of await jsonFiles(path.join(root, owner, folder))) {
          const known = this.files.get(file);
          if (known !== undefined) {
            seen.set(file, known);
            continue;
          }
          const text = await readTextIfExists(file).catch(() => undefined);
          const record = kind === 'log' ? parseRecord(text) : mailboxRecord(text, source);
          if (record !== undefined) seen.set(file, { owner, source, record });
        }
      }
    }
    this.files = seen;
    return [...seen.values()];
  }
}

function mailboxRecord(text: string | undefined, status: Source): LogRecord | undefined {
  const m = parseMessage(text);
  if (m === undefined) return undefined;
  return {
    id: m.id,
    at: m.sentAt,
    route: 'agent-tabs',
    from: m.from,
    to: { id: m.to },
    text: m.text,
    ...(m.replyTo !== undefined ? { replyTo: m.replyTo } : {}),
    status: status as NonNullable<LogRecord['status']>,
  };
}

function select(collected: readonly Collected[], who: Who): HistoryItem[] {
  const items: HistoryItem[] = [];
  const add = (record: LogRecord, direction: 'sent' | 'received') =>
    items.push({ ...record, direction, peer: direction === 'sent' ? record.to : record.from });
  for (const { owner, source, record: r } of collected) {
    const own = owner === who.id;
    if (source === SENT_LOG) {
      if (own || matches(who, r.from)) add(r, 'sent');
      else if (matches(who, r.to)) add(r, 'received');
    } else if (source === RECEIVED_LOG) {
      if (own || matches(who, r.to)) add(r, 'received');
      else if (matches(who, r.from)) add(r, 'sent');
    } else if (own) add(r, 'received');
    else if (matches(who, r.from)) add(r, 'sent');
  }
  const byId = new Map<string, HistoryItem>();
  for (const item of items) {
    const seen = byId.get(item.id);
    if (seen === undefined) {
      byId.set(item.id, item);
      continue;
    }
    // A sent-log entry and the recipient's mailbox file are one message: the log knows the delivery, the mailbox the status.
    const [log, box] = seen.status === undefined ? [seen, item] : [item, seen];
    byId.set(item.id, { ...box, ...log, ...(box.status !== undefined ? { status: box.status } : {}) });
  }
  const unique: HistoryItem[] = [];
  for (const item of [...byId.values()].sort((a, b) => a.at.localeCompare(b.at) || a.id.localeCompare(b.id))) {
    if (!unique.some((u) => sameNative(u, item))) unique.push(item);
  }
  return unique.slice(-HISTORY_MAX);
}

// Reads only: nothing moves between new/, held/ and cur/, so a message stays unread for its session.
export async function history(home: string, who: Who, index = new MailIndex()): Promise<HistoryItem[]> {
  return select(await index.collect(home), who);
}

export function previews(items: readonly HistoryItem[], budget = HISTORY_REPLY_CHARS - 1_000): (HistoryItem & { textLength: number })[] {
  const out: (HistoryItem & { textLength: number })[] = [];
  let used = 0;
  for (let i = items.length - 1; i >= 0; i--) {
    const item = items[i]!;
    const preview = { ...item, text: item.text.slice(0, PREVIEW_CHARS), textLength: item.text.length };
    used += JSON.stringify(preview).length + 1;
    if (used > budget) break;
    out.unshift(preview);
  }
  return out;
}

export async function historyCounts(home: string, whos: readonly Who[], index = new MailIndex()): Promise<number[]> {
  const collected = await index.collect(home);
  return whos.map((who) => select(collected, who).length);
}

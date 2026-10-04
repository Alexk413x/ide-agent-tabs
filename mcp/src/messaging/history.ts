import { promises as fs } from 'node:fs';
import path from 'node:path';
import { readTextIfExists, writeNewPrivateFile } from '../files.js';
import { MAIL_DIR, mailboxDir, newMessageId, parseMessage } from './mailbox.js';
import { isSessionId } from './sessions.js';

export const SENT_LOG = 'sent-log';
export const RECEIVED_LOG = 'received-log';
export const HISTORY_MAX = 500;
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

async function readAll<T>(dir: string, parse: (text: string | undefined) => T | undefined): Promise<T[]> {
  const out: T[] = [];
  for (const file of await jsonFiles(dir)) {
    const value = parse(await readTextIfExists(file).catch(() => undefined));
    if (value !== undefined) out.push(value);
  }
  return out;
}

const matches = (who: Who, p: Party) => (who.id !== undefined && p.id === who.id) || (p.name !== undefined && who.names.includes(p.name));

function sameNative(a: HistoryItem, b: HistoryItem): boolean {
  return a.route === 'native' && b.route === 'native' && a.direction === b.direction && a.text === b.text && Math.abs(Date.parse(a.at) - Date.parse(b.at)) < NATIVE_SAME_MS;
}

// Reads only: nothing moves between new/, held/ and cur/, so a message stays unread for its session.
export async function history(home: string, who: Who): Promise<HistoryItem[]> {
  const root = path.join(home, MAIL_DIR);
  const items: HistoryItem[] = [];
  const add = (record: LogRecord, direction: 'sent' | 'received') =>
    items.push({ ...record, direction, peer: direction === 'sent' ? record.to : record.from });
  for (const owner of (await fs.readdir(root).catch(() => [] as string[])).filter(isSessionId)) {
    const own = owner === who.id;
    for (const r of await readAll(path.join(root, owner, SENT_LOG), parseRecord)) {
      if (own || matches(who, r.from)) add(r, 'sent');
      else if (matches(who, r.to)) add(r, 'received');
    }
    for (const r of await readAll(path.join(root, owner, RECEIVED_LOG), parseRecord)) {
      if (own || matches(who, r.to)) add(r, 'received');
      else if (matches(who, r.from)) add(r, 'sent');
    }
    for (const [folder, status] of MAILBOX_STATES) {
      for (const m of await readAll(path.join(root, owner, folder), parseMessage)) {
        const record: LogRecord = {
          id: m.id,
          at: m.sentAt,
          route: 'agent-tabs',
          from: m.from,
          to: { id: m.to },
          text: m.text,
          ...(m.replyTo !== undefined ? { replyTo: m.replyTo } : {}),
          status,
        };
        if (own) add(record, 'received');
        else if (matches(who, m.from)) add(record, 'sent');
      }
    }
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

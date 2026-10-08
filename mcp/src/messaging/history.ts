import { newMessageId, storedFor, type MailState, type Party, type Route, type StoredMessage } from './store.js';

export type { Party, Route };

export const HISTORY_MAX = 500;
// Claude Code replaces an MCP result over its output limit (about 25,000 tokens) with an error. JSON of ids,
// times and hex tokenizes at about 2.3 characters per token, so a history reply stays near 30,000 characters.
export const HISTORY_REPLY_CHARS = 30_000;
export const HISTORY_BATCH = 50;
export const PREVIEW_CHARS = 200;
export const PIECE_CHARS = 30_000;

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
const STATUS: Record<MailState, NonNullable<LogRecord['status']>> = { unread: 'unread', held: 'delivering', read: 'read' };

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

export const logId = (id: string | undefined) => (id !== undefined && /^m-[0-9a-f]{16}$/.test(id) ? id : newMessageId());

const matches = (who: Who, p: Party) => (who.id !== undefined && p.id === who.id) || (p.name !== undefined && who.names.includes(p.name));

function sameNative(a: HistoryItem, b: HistoryItem): boolean {
  return a.route === 'native' && b.route === 'native' && a.direction === b.direction && a.text === b.text && Math.abs(Date.parse(a.at) - Date.parse(b.at)) < NATIVE_SAME_MS;
}

function record(m: StoredMessage): LogRecord {
  return {
    id: m.id,
    at: m.sentAt,
    route: m.route,
    from: m.from,
    to: m.to,
    text: m.text,
    ...(m.replyTo !== undefined ? { replyTo: m.replyTo } : {}),
    ...(m.delivery !== undefined ? { delivery: m.delivery } : {}),
    ...(m.state !== undefined ? { status: STATUS[m.state] } : {}),
  };
}

function select(stored: readonly StoredMessage[], who: Who): HistoryItem[] {
  const items: HistoryItem[] = [];
  const add = (r: LogRecord, direction: 'sent' | 'received') => items.push({ ...r, direction, peer: direction === 'sent' ? r.to : r.from });
  for (const m of stored) {
    const r = record(m);
    const owner = m.route === 'native' ? m.owner : m.from.id;
    const own = owner !== undefined && owner === who.id;
    if (m.direction === 'received') {
      if (own || matches(who, r.to)) add(r, 'received');
      else if (matches(who, r.from)) add(r, 'sent');
    } else if (own || matches(who, r.from)) add(r, 'sent');
    else if (matches(who, r.to)) add(r, 'received');
  }
  const byId = new Map<string, HistoryItem>();
  for (const item of items) if (!byId.has(item.id)) byId.set(item.id, item);
  const unique: HistoryItem[] = [];
  for (const item of [...byId.values()].sort((a, b) => a.at.localeCompare(b.at) || a.id.localeCompare(b.id))) {
    if (!unique.some((u) => sameNative(u, item))) unique.push(item);
  }
  return unique.slice(-HISTORY_MAX);
}

// Reads only: no message changes state, so a message stays unread for its session.
export async function history(home: string, who: Who): Promise<HistoryItem[]> {
  return select(await storedFor(home, who), who);
}

export function previews(items: readonly HistoryItem[], budget = HISTORY_REPLY_CHARS - 1_000): (HistoryItem & { textLength: number })[] {
  const out: (HistoryItem & { textLength: number })[] = [];
  let used = 0;
  for (let i = items.length - 1; i >= 0 && out.length < HISTORY_BATCH; i--) {
    const item = items[i]!;
    const preview = { ...item, text: item.text.slice(0, PREVIEW_CHARS), textLength: item.text.length };
    used += JSON.stringify(preview).length + 1;
    if (used > budget) break;
    out.unshift(preview);
  }
  return out;
}

export async function historyCounts(home: string, whos: readonly Who[]): Promise<number[]> {
  return Promise.all(whos.map(async (who) => (who.id === undefined && who.names.length === 0 ? 0 : (await history(home, who)).length)));
}

import { readFileSync, writeFileSync } from 'node:fs';
import { withFileLock } from '../src/files.js';
import { closeAllDbs, query } from '../src/messaging/db.js';
import { runHook } from '../src/messaging/hook.js';
import { liveSessions, readPresence, updatePresence, type PresenceFile } from '../src/messaging/sessions.js';
import {
  claimBatch,
  logNative,
  mailTo,
  peekUnread,
  sendMessage,
  settleClaim,
  storedFor,
  takeBatch,
  unreadSummary,
  waitForMessage,
  type NativeRecord,
  type Outgoing,
} from '../src/messaging/store.js';

const [mode = '', json = '{}'] = process.argv.slice(2);
const args = JSON.parse(json) as Record<string, unknown>;
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

function go(): Promise<void> {
  process.stdout.write('ready\n');
  return new Promise((resolve) => {
    process.stdin.once('data', () => resolve());
  });
}

const done = (result: unknown) => process.stdout.write(`${JSON.stringify(result)}\n`, () => process.exit(0));

async function lockCount() {
  const file = String(args.file);
  const rounds = Number(args.rounds);
  await go();
  for (let i = 0; i < rounds; i++) {
    await withFileLock(file, async () => {
      const n = Number(readFileSync(file, 'utf8') || '0');
      await sleep(1);
      writeFileSync(file, String(n + 1));
    });
  }
  done({ rounds });
}

async function lockOnce() {
  const file = String(args.file);
  await go();
  const started = Date.now();
  try {
    await withFileLock(file, async () => undefined, { timeoutMs: Number(args.timeoutMs ?? 15_000) });
    done({ ok: true, ms: Date.now() - started });
  } catch (e) {
    done({ ok: false, error: e instanceof Error ? e.message : String(e), ms: Date.now() - started });
  }
}

async function lockHold() {
  const file = String(args.file);
  await withFileLock(file, async () => {
    process.stdout.write('locked\n');
    await new Promise((resolve) => process.stdin.once('data', resolve));
  });
  done({ released: true });
}

type Op = Record<string, unknown> & { op: string };
const num = (v: unknown) => (v === undefined || v === null ? undefined : Number(v));

async function runOp(home: string, o: Op): Promise<unknown> {
  const id = String(o.id ?? '');
  switch (o.op) {
    case 'send':
      return sendMessage(home, o.out as Outgoing, num(o.now));
    case 'take':
      return takeBatch(home, id, { ...(o.filter ? { filter: o.filter as { from?: string } } : {}), ...(o.count !== undefined ? { count: Number(o.count) } : {}) }, num(o.now));
    case 'claim':
      return claimBatch(home, id, Number(o.count ?? Infinity), undefined, num(o.now));
    case 'settle':
      return settleClaim(home, id, String(o.claim), o.how === 'ack' ? 'ack' : 'release', num(o.now));
    case 'peek':
      return peekUnread(home, id, num(o.now));
    case 'summary':
      return unreadSummary(home, id, num(o.now));
    case 'mailTo':
      return mailTo(home, id);
    case 'logNative':
      return logNative(home, o.record as NativeRecord);
    case 'stored':
      return storedFor(home, { ...(o.id !== undefined && o.id !== null ? { id } : {}), names: (o.names as string[]) ?? [] });
    case 'presenceWrite':
      return updatePresence(home, id, () => o.presence as PresenceFile);
    case 'presenceRead':
      return (await readPresence(home, id)) ?? null;
    case 'presenceText':
      return readFileSync(`${home}/sessions/${id}.json`, 'utf8');
    case 'live':
      return liveSessions(home, undefined, num(o.now));
    case 'schema':
      return query(home, (db) => ({
        master: db.sql.prepare('SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY name').all(),
        userVersion: Number(db.sql.prepare('PRAGMA user_version').get()!.user_version),
        journalMode: String(db.sql.prepare('PRAGMA journal_mode').get()!.journal_mode),
      }));
    case 'hook':
      return (await runHook({ cli: String(o.cli), event: String(o.event), input: (o.input as Record<string, unknown>) ?? {}, home, sessionId: id, ...(o.now !== undefined ? { now: Number(o.now) } : {}) })) ?? null;
    case 'rows':
      return query(home, (db) => db.sql.prepare('SELECT * FROM messages ORDER BY seq').all());
    default:
      throw new Error(`unknown op ${o.op}`);
  }
}

async function store() {
  const home = String(args.home);
  const results: unknown[] = [];
  for (const o of args.ops as Op[]) {
    try {
      results.push({ ok: (await runOp(home, o)) ?? null });
    } catch (e) {
      results.push({ error: e instanceof Error ? e.message : String(e) });
    }
  }
  await closeAllDbs();
  done({ results });
}

async function waitOnce() {
  const home = String(args.home);
  await query(home, () => undefined);
  await go();
  const message = await waitForMessage(home, String(args.id), {}, Number(args.timeoutMs ?? 10_000), undefined, { watch: args.watch !== false });
  const returned = Date.now();
  done({ message: message ?? null, returned });
}

const modes: Record<string, () => Promise<void>> = { lockCount, lockOnce, lockHold, store, waitOnce };
await modes[mode]!();

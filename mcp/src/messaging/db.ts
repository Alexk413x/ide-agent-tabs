import { promises as fs, statfsSync, statSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import type { DatabaseSync, SQLInputValue, SQLOutputValue, StatementSync } from 'node:sqlite';
import { ensurePrivateDir, withFileLock } from '../files.js';

export const DB_FILE = 'messages.db';
export const WAKE_DIR = 'wake';
export const FILE_MAILBOX_DIR = 'mail';
export const SCHEMA_VERSION = 1;
export const BUSY_TIMEOUT_MS = 200;
export const SHARED_BUSY_TIMEOUT_MS = 25;
export const TOOL_DEADLINE_MS = 5_000;
export const HOOK_DEADLINE_MS = 1_000;
export const CLEAN_DEADLINE_MS = 1_000;
const JOURNAL_SIZE_LIMIT = 4 * 1024 * 1024;
const MIN_NODE = '22.13';

const SQLITE_BUSY = 5;
const SQLITE_IOERR = 10;
const SQLITE_CORRUPT = 11;
const SQLITE_FULL = 13;
const SQLITE_CONSTRAINT = 19;
const SQLITE_NOTADB = 26;

export class MailError extends Error {}

export class StoreBusyError extends MailError {
  constructor() {
    super('the message store is busy; try again');
  }
}

export const CORRUPT_MESSAGE =
  'the message store is corrupt; Agent Tabs moves it aside and starts a new one at the next start or hourly cleanup, and unread messages in it can be lost';

export function sqliteCode(e: unknown): number | undefined {
  const code = (e as { errcode?: unknown } | null)?.errcode;
  return typeof code === 'number' ? code & 0xff : undefined;
}

export const isBusy = (e: unknown) => sqliteCode(e) === SQLITE_BUSY;
export const isFull = (e: unknown) => sqliteCode(e) === SQLITE_FULL;
export const isConstraint = (e: unknown) => sqliteCode(e) === SQLITE_CONSTRAINT;
// Windows reports SQLITE_IOERR when processes open a new database at once and race to set up its WAL files.
const isOpenRace = (e: unknown) => isBusy(e) || sqliteCode(e) === SQLITE_IOERR;
export const isCorrupt = (e: unknown) => sqliteCode(e) === SQLITE_CORRUPT || sqliteCode(e) === SQLITE_NOTADB;

// Migrations stay additive (new tables, columns with defaults, indexes): an older build keeps using a database
// that a newer build migrated, with no version check.
const MIGRATIONS: readonly string[] = [
  `CREATE TABLE messages (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    id         TEXT    NOT NULL CHECK (id GLOB 'm-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'),
    route      TEXT    NOT NULL CHECK (route IN ('agent-tabs', 'native')),
    owner      TEXT,
    direction  TEXT    CHECK (direction IN ('sent', 'received')),
    from_id    TEXT, from_name TEXT, from_agent TEXT, from_path TEXT,
    to_id      TEXT, to_name   TEXT, to_agent   TEXT, to_path   TEXT,
    text       TEXT    NOT NULL CHECK (length(text) <= 32000),
    reply_to   TEXT,
    sent_at    TEXT    NOT NULL,
    sent_ms    INTEGER NOT NULL,
    digest     TEXT,
    delivery   TEXT,
    state      TEXT    CHECK (state IN ('unread', 'held', 'read')),
    claim      TEXT,
    state_ms   INTEGER,
    CHECK ((route = 'agent-tabs') = (state IS NOT NULL)),
    CHECK ((route = 'native') = (owner IS NOT NULL AND direction IS NOT NULL))
  );
  CREATE UNIQUE INDEX messages_id_mail   ON messages(id) WHERE route = 'agent-tabs';
  CREATE UNIQUE INDEX messages_id_native ON messages(owner, id) WHERE route = 'native';
  CREATE INDEX messages_inbox   ON messages(to_id, state, seq) WHERE route = 'agent-tabs';
  CREATE INDEX messages_outbox  ON messages(from_id, sent_ms);
  CREATE INDEX messages_to      ON messages(to_id, sent_ms);
  CREATE INDEX messages_names   ON messages(from_name, to_name);
  CREATE INDEX messages_owner   ON messages(owner, sent_ms) WHERE route = 'native';
  CREATE INDEX messages_reply   ON messages(reply_to) WHERE reply_to IS NOT NULL;
  CREATE INDEX messages_age     ON messages(sent_ms);
  CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);`,
];

type Driver = typeof import('node:sqlite');

type EmitWarning = (warning: string | Error, ...rest: unknown[]) => void;

export function isSqliteWarning(warning: string | Error, typeOrOptions?: unknown): boolean {
  const type =
    typeof warning !== 'string'
      ? warning.name
      : typeof typeOrOptions === 'string'
        ? typeOrOptions
        : (typeOrOptions as { type?: unknown } | undefined)?.type;
  const text = typeof warning === 'string' ? warning : warning.message;
  return type === 'ExperimentalWarning' && /sqlite/i.test(text);
}

export async function withoutSqliteWarning<T>(load: () => Promise<T>): Promise<T> {
  const emit = process.emitWarning as EmitWarning;
  const filtered: EmitWarning = (warning, ...rest) => {
    if (!isSqliteWarning(warning, rest[0])) emit.call(process, warning, ...rest);
  };
  process.emitWarning = filtered as typeof process.emitWarning;
  try {
    return await load();
  } finally {
    if (process.emitWarning === (filtered as typeof process.emitWarning)) process.emitWarning = emit as typeof process.emitWarning;
  }
}

let driver: Promise<Driver> | undefined;

export function loadDriver(): Promise<Driver> {
  driver ??= withoutSqliteWarning(() => import('node:sqlite')).catch(() => {
    throw new MailError(`messaging needs Node.js ${MIN_NODE} or later; this is ${process.version}`);
  });
  return driver;
}

const NETWORK_FS_TYPES = new Set([0x6969, 0xff534d42, 0xfe534d42, 0x01021997]);

export function networkHomeReason(
  home: string,
  platform: NodeJS.Platform = process.platform,
  fsType: (dir: string) => number = (dir) => statfsSync(dir).type,
): string | undefined {
  const advice = `the Agent Tabs home ${home} is on a network file system, where the message store can't lock; set IDE_AGENT_TABS_HOME to a local folder`;
  if (platform === 'win32') {
    const p = home.replace(/\//g, '\\');
    return /^\\\\\?\\UNC\\/i.test(p) || /^\\\\[^?.\\]/.test(p) ? advice : undefined;
  }
  if (platform !== 'linux') return undefined;
  let type: number;
  try {
    type = fsType(home);
  } catch {
    return undefined;
  }
  return NETWORK_FS_TYPES.has(type) ? advice : undefined;
}

export interface Db {
  readonly home: string;
  readonly file: string;
  readonly sql: DatabaseSync;
  readonly ino: number;
  prepare(text: string): StatementSync;
}

export type Row = Record<string, SQLOutputValue>;
export type { SQLInputValue };

export const dbPath = (home: string) => path.join(home, DB_FILE);
export const wakeDir = (home: string) => path.join(home, WAKE_DIR);

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));
const jitter = () => 10 + Math.floor(Math.random() * 41);

let busyTimeoutMs = BUSY_TIMEOUT_MS;
const connections = new Map<string, Promise<Db>>();
export const openStats = { opened: 0 };

export function setBusyTimeout(ms: number): void {
  busyTimeoutMs = ms;
  for (const pending of connections.values()) void pending.then((db) => db.sql.exec(`PRAGMA busy_timeout=${ms}`)).catch(() => undefined);
}

async function retryBusy<T>(work: () => T, deadline: number, label?: string, retryable: (e: unknown) => boolean = isBusy): Promise<T> {
  for (;;) {
    try {
      return work();
    } catch (e) {
      if (!retryable(e)) throw e;
      if (Date.now() + 10 >= deadline) {
        if (label !== undefined) process.stderr.write(`ide-agent-tabs: ${label} gave up waiting for the message store lock\n`);
        throw new StoreBusyError();
      }
      await sleep(jitter());
    }
  }
}

function runTx<T>(sql: DatabaseSync, work: () => T): T {
  sql.exec('BEGIN IMMEDIATE');
  try {
    const result = work();
    if (typeof (result as { then?: unknown } | null)?.then === 'function') {
      (result as Promise<unknown>).then(undefined, () => undefined);
      throw new TypeError('a message store transaction must be synchronous; its work returned a promise');
    }
    sql.exec('COMMIT');
    return result;
  } catch (e) {
    try {
      sql.exec('ROLLBACK');
    } catch {}
    throw e;
  }
}

function userVersion(sql: DatabaseSync): number {
  return Number(sql.prepare('PRAGMA user_version').get()?.user_version ?? 0);
}

function setup(sql: DatabaseSync): void {
  sql.exec(`PRAGMA busy_timeout=${busyTimeoutMs}`);
  if (String(sql.prepare('PRAGMA journal_mode').get()?.journal_mode).toLowerCase() !== 'wal') sql.exec('PRAGMA journal_mode=WAL');
  sql.exec('PRAGMA synchronous=NORMAL');
  sql.exec(`PRAGMA journal_size_limit=${JOURNAL_SIZE_LIMIT}`);
  if (userVersion(sql) >= SCHEMA_VERSION) return;
  runTx(sql, () => {
    const from = userVersion(sql);
    if (from >= SCHEMA_VERSION) return;
    for (const step of MIGRATIONS.slice(from, SCHEMA_VERSION)) sql.exec(step);
    sql.exec(`PRAGMA user_version=${SCHEMA_VERSION}`);
  });
}

async function connect(home: string, file: string, deadline: number): Promise<Db> {
  const reason = networkHomeReason(home);
  if (reason !== undefined) throw new MailError(reason);
  const { DatabaseSync } = await loadDriver();
  await ensurePrivateDir(home);
  await ensurePrivateDir(wakeDir(home));
  writeFileSync(file, '', { flag: 'a', mode: 0o600 });
  const fresh = statSync(file).size === 0;
  const sql = new DatabaseSync(file);
  openStats.opened++;
  const ready = () => retryBusy(() => setup(sql), deadline, 'open', isOpenRace);
  try {
    await (fresh ? withFileLock(`${file}.init`, ready) : ready());
  } catch (e) {
    sql.close();
    throw isCorrupt(e) ? new MailError(CORRUPT_MESSAGE) : e;
  }
  await fs.rm(path.join(home, FILE_MAILBOX_DIR), { recursive: true, force: true, maxRetries: 3 }).catch(() => undefined);
  const statements = new Map<string, StatementSync>();
  const db: Db = {
    home,
    file,
    sql,
    ino: statSync(file).ino,
    prepare(text) {
      let statement = statements.get(text);
      if (statement === undefined) {
        statement = sql.prepare(text);
        statements.set(text, statement);
      }
      return statement;
    },
  };
  return db;
}

export function openDb(home: string, deadlineMs = TOOL_DEADLINE_MS): Promise<Db> {
  const file = path.resolve(dbPath(home));
  const known = connections.get(file);
  if (known !== undefined) return known;
  const pending = connect(path.dirname(file), file, Date.now() + deadlineMs);
  connections.set(file, pending);
  pending.catch(() => {
    if (connections.get(file) === pending) connections.delete(file);
  });
  return pending;
}

export function closeDb(home: string): void {
  const file = path.resolve(dbPath(home));
  const pending = connections.get(file);
  connections.delete(file);
  void pending?.then((db) => db.sql.close()).catch(() => undefined);
}

export async function closeAllDbs(): Promise<void> {
  const all = [...connections.values()];
  connections.clear();
  for (const pending of all) {
    try {
      (await pending).sql.close();
    } catch {}
  }
}

export interface StoreOptions {
  deadlineMs?: number;
  label?: string;
}

const writeTurns = new WeakMap<Db, Promise<unknown>>();

// Each busy wait blocks the event loop for the busy timeout (65 ms for 25 ms on Windows, whose sleeps round
// up), so a process that serves many sessions takes write transactions one at a time instead of letting
// every waiting session block in turn.
function inWriteTurn<T>(db: Db, deadline: number, work: () => Promise<T>): Promise<T> {
  const turn = (writeTurns.get(db) ?? Promise.resolve()).then(() => (Date.now() >= deadline ? Promise.reject(new StoreBusyError()) : work()));
  writeTurns.set(db, turn.catch(() => undefined));
  return turn;
}

async function guarded<T>(home: string, options: StoreOptions, work: (db: Db) => T, inTx: boolean): Promise<T> {
  const deadline = Date.now() + (options.deadlineMs ?? TOOL_DEADLINE_MS);
  const db = await openDb(home, options.deadlineMs);
  try {
    return inTx
      ? await inWriteTurn(db, deadline, () => retryBusy(() => runTx(db.sql, () => work(db)), deadline, options.label))
      : await retryBusy(() => work(db), deadline, options.label);
  } catch (e) {
    if (isCorrupt(e)) {
      closeDb(home);
      throw new MailError(CORRUPT_MESSAGE);
    }
    throw e;
  }
}

export function tx<T>(home: string, work: (db: Db) => T, options: StoreOptions = {}): Promise<T> {
  return guarded(home, options, work, true);
}

export function query<T>(home: string, work: (db: Db) => T, options: StoreOptions = {}): Promise<T> {
  return guarded(home, options, work, false);
}

export async function quickCheck(home: string): Promise<boolean> {
  try {
    return await query(home, (db) => db.prepare('PRAGMA quick_check').all().every((r) => Object.values(r)[0] === 'ok'), { deadlineMs: CLEAN_DEADLINE_MS });
  } catch (e) {
    if (e instanceof StoreBusyError) return true;
    if (e instanceof MailError && e.message === CORRUPT_MESSAGE) return false;
    throw e;
  }
}

export async function sameFile(home: string): Promise<boolean> {
  const file = path.resolve(dbPath(home));
  const pending = connections.get(file);
  if (pending === undefined) return true;
  const db = await pending.catch(() => undefined);
  const ino = (await fs.stat(file).catch(() => undefined))?.ino;
  if (db !== undefined && ino === db.ino) return true;
  closeDb(home);
  return false;
}

const RENAME_REFUSED = new Set(['EPERM', 'EBUSY', 'EACCES']);

export async function setAsideCorrupt(home: string, now = Date.now()): Promise<void> {
  closeDb(home);
  const file = dbPath(home);
  await withFileLock(`${file}.recover`, async () => {
    const { DatabaseSync } = await loadDriver();
    try {
      const broken = new DatabaseSync(file);
      try {
        broken.exec(`VACUUM INTO '${path.join(home, `messages.recovered-${now}.db`).replace(/'/g, "''")}'`);
      } finally {
        broken.close();
      }
    } catch {}
    for (const suffix of ['-shm', '-wal', '']) {
      try {
        await fs.rename(`${file}${suffix}`, path.join(home, `messages.corrupt-${now}.db${suffix}`));
      } catch (e) {
        const code = (e as NodeJS.ErrnoException).code ?? '';
        if (code === 'ENOENT') continue;
        if (RENAME_REFUSED.has(code)) throw new MailError(`${CORRUPT_MESSAGE}; another process still has it open`);
        throw e;
      }
    }
  });
}

export async function ensureHealthy(home: string, now = Date.now()): Promise<boolean> {
  if (await quickCheck(home)) return true;
  await setAsideCorrupt(home, now);
  return false;
}

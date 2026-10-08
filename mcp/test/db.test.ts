import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { existsSync, readdirSync, statSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import {
  closeDb,
  dbPath,
  ensureHealthy,
  isSqliteWarning,
  MailError,
  networkHomeReason,
  openDb,
  openStats,
  query,
  SCHEMA_VERSION,
  StoreBusyError,
  tx,
  wakeDir,
  withoutSqliteWarning,
} from '../src/messaging/db.js';
import { tempDir } from './tempDir.js';

const count = (home: string) => query(home, (db) => Number(db.prepare('SELECT count(*) AS n FROM meta').get()!.n));

test('the first open creates an owner-only database in WAL mode at the current schema, and a wake folder', async () => {
  const home = tempDir('iat-db-');
  const db = await openDb(home);
  assert.equal(db.file, path.resolve(dbPath(home)));
  if (process.platform !== 'win32') assert.equal(statSync(db.file).mode & 0o777, 0o600);
  assert.equal(db.sql.prepare('PRAGMA journal_mode').get()!.journal_mode, 'wal');
  assert.equal(db.sql.prepare('PRAGMA user_version').get()!.user_version, SCHEMA_VERSION);
  assert.equal(db.sql.prepare('PRAGMA synchronous').get()!.synchronous, 1);
  assert.ok(existsSync(wakeDir(home)));
});

test('a process holds one connection per database, however many callers open it at once', async () => {
  const home = tempDir('iat-db-');
  const before = openStats.opened;
  const [a, b, c] = await Promise.all([openDb(home), openDb(home), openDb(path.join(home, '.'))]);
  assert.equal(openStats.opened - before, 1);
  assert.ok(a === b && b === c);
});

test('a transaction commits its work, and rolls it back when the work throws', async () => {
  const home = tempDir('iat-db-');
  await tx(home, (db) => db.prepare("INSERT INTO meta (key, value) VALUES ('a', '1')").run());
  await assert.rejects(
    tx(home, (db) => {
      db.prepare("INSERT INTO meta (key, value) VALUES ('b', '2')").run();
      throw new Error('stop');
    }),
    /stop/,
  );
  assert.equal(await count(home), 1);
});

test('a transaction refuses async work and keeps none of its writes', async () => {
  const home = tempDir('iat-db-');
  await assert.rejects(
    tx(home, async (db) => {
      db.prepare("INSERT INTO meta (key, value) VALUES ('a', '1')").run();
    }),
    /must be synchronous/,
  );
  assert.equal(await count(home), 0);
});

function holdLock(home: string): Promise<{ release: () => Promise<void> }> {
  const script = `const { DatabaseSync } = await import('node:sqlite');
const db = new DatabaseSync(${JSON.stringify(dbPath(home))});
db.exec('BEGIN IMMEDIATE');
db.exec("INSERT INTO meta (key, value) VALUES ('held', 'x')");
process.stdout.write('locked');
setInterval(() => {}, 1000);`;
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, ['--no-warnings', '--input-type=module', '-e', script], { stdio: ['ignore', 'pipe', 'inherit'] });
    child.on('error', reject);
    child.stdout.once('data', () =>
      resolve({
        release: () =>
          new Promise<void>((done) => {
            child.once('close', () => done());
            child.kill();
          }),
      }),
    );
  });
}

test('a busy store is retried until the deadline, then reported busy; a killed holder leaves no row behind', async () => {
  const home = tempDir('iat-db-');
  await openDb(home);
  const holder = await holdLock(home);
  const started = Date.now();
  await assert.rejects(
    tx(home, (db) => db.prepare("INSERT INTO meta (key, value) VALUES ('a', '1')").run(), { deadlineMs: 400 }),
    (e) => e instanceof StoreBusyError && /busy; try again/.test(e.message),
  );
  const waited = Date.now() - started;
  assert.ok(waited >= 300 && waited < 2_000, `waited ${waited} ms`);
  await holder.release();
  const after = Date.now();
  await tx(home, (db) => db.prepare("INSERT INTO meta (key, value) VALUES ('a', '1')").run());
  assert.ok(Date.now() - after < 1_000);
  assert.deepEqual(await query(home, (db) => db.prepare('SELECT key FROM meta').all().map((r) => r.key)), ['a']);
});

test('the warning filter drops only the SQLite experimental warning', async () => {
  const seen: string[] = [];
  const listen = (w: Error) => seen.push(`${w.name}: ${w.message}`);
  process.on('warning', listen);
  try {
    await withoutSqliteWarning(async () => {
      process.emitWarning('SQLite is an experimental feature and might change at any time', 'ExperimentalWarning');
      process.emitWarning('Fetch is an experimental feature', 'ExperimentalWarning');
      process.emitWarning('SQLite said something else', { type: 'DeprecationWarning' } as never);
    });
    process.emitWarning('SQLite is an experimental feature and might change at any time', 'ExperimentalWarning');
    await new Promise((r) => setImmediate(r));
  } finally {
    process.off('warning', listen);
  }
  assert.deepEqual(seen, [
    'ExperimentalWarning: Fetch is an experimental feature',
    'DeprecationWarning: SQLite said something else',
    'ExperimentalWarning: SQLite is an experimental feature and might change at any time',
  ]);
  assert.ok(isSqliteWarning(Object.assign(new Error('SQLite is an experimental feature'), { name: 'ExperimentalWarning' })));
});

test('a home on a network file system is refused with advice to use a local folder', () => {
  assert.match(networkHomeReason('\\\\server\\share\\.ide-agent-tabs', 'win32')!, /network file system.*IDE_AGENT_TABS_HOME/);
  assert.ok(networkHomeReason('//server/share/x', 'win32'));
  assert.ok(networkHomeReason('\\\\?\\UNC\\server\\share\\x', 'win32'));
  assert.equal(networkHomeReason('C:\\Users\\you\\.ide-agent-tabs', 'win32'), undefined);
  assert.equal(networkHomeReason('\\\\?\\C:\\Users\\you', 'win32'), undefined);
  for (const type of [0x6969, 0xff534d42, 0xfe534d42, 0x01021997]) assert.ok(networkHomeReason('/home/you', 'linux', () => type));
  assert.equal(networkHomeReason('/home/you', 'linux', () => 0xef53), undefined);
  assert.equal(networkHomeReason('/home/you', 'linux', () => {
    throw new Error('ENOSYS');
  }), undefined);
  assert.equal(networkHomeReason('/Volumes/share', 'darwin', () => 0x6969), undefined);
});

test('a database from a newer build is used as it is, without migrating', async () => {
  const home = tempDir('iat-db-');
  const db = await openDb(home);
  db.sql.exec(`PRAGMA user_version=${SCHEMA_VERSION + 4}`);
  closeDb(home);
  await new Promise((r) => setImmediate(r));
  await tx(home, (d) => d.prepare("INSERT INTO meta (key, value) VALUES ('a', '1')").run());
  assert.equal(await query(home, (d) => Number(d.prepare('PRAGMA user_version').get()!.user_version)), SCHEMA_VERSION + 4);
});

test('a corrupt database fails with a clear error, and the health check moves it aside for a fresh one', async () => {
  const home = tempDir('iat-db-');
  writeFileSync(dbPath(home), 'this is not a database'.repeat(200));
  await assert.rejects(count(home), (e) => e instanceof MailError && /corrupt/.test(e.message));
  assert.equal(await ensureHealthy(home, 1234), false);
  assert.ok(readdirSync(home).includes('messages.corrupt-1234.db'));
  assert.equal(await count(home), 0);
  assert.equal(await ensureHealthy(home), true);
});

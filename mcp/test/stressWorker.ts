import { existsSync } from 'node:fs';
import { monitorEventLoopDelay } from 'node:perf_hooks';
import { openDb, setBusyTimeout, SHARED_BUSY_TIMEOUT_MS } from '../src/messaging/db.js';
import { claimBatch, newMessageId, sendMessage, settleClaim, takeBatch, waitForMessage, type Outgoing } from '../src/messaging/store.js';

const [mode = '', json = '{}'] = process.argv.slice(2);
const args = JSON.parse(json) as Record<string, unknown>;
const home = String(args.home);
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
const out = (from: string, to: string, text: string): Outgoing => ({ from: { id: from, agent: 'codex', path: `/w/${from}` }, to, text });

function go(): Promise<void> {
  process.stdout.write('ready\n');
  return new Promise((resolve) => {
    process.stdin.once('data', () => resolve());
  });
}

const done = (result: unknown) => process.stdout.write(`${JSON.stringify(result)}\n`, () => process.exit(0));

async function mesh() {
  const me = String(args.me);
  const peers = args.peers as string[];
  const count = Number(args.count);
  const expected = Number(args.expected);
  const sent: { id: string; to: string }[] = [];
  const read: { id: string; from: string; to: string }[] = [];
  const pairs: number[] = [];
  const take = async () => {
    for (const m of (await takeBatch(home, me)).messages) read.push({ id: m.id, from: m.from.id, to: m.to });
  };
  await openDb(home);
  await go();
  for (let k = 0; k < count; k++) {
    const started = performance.now();
    const to = peers[k % peers.length]!;
    sent.push({ id: (await sendMessage(home, out(me, to, `${me} to ${to} #${k}`))).id, to });
    await take();
    pairs.push(performance.now() - started);
  }
  const deadline = Date.now() + 60_000;
  while (read.length < expected && Date.now() < deadline) {
    await take();
    if (read.length < expected) await sleep(20);
  }
  done({ sent, read, pairs });
}

async function readLoop() {
  const target = String(args.target);
  const stop = String(args.stop);
  const ids: string[] = [];
  await openDb(home);
  await go();
  for (;;) {
    const stopping = existsSync(stop);
    const batch = await takeBatch(home, target, { count: 2 });
    ids.push(...batch.ids);
    if (stopping && batch.ids.length === 0) break;
    if (batch.ids.length === 0) await sleep(5);
  }
  done({ ids });
}

async function claimLoop() {
  const target = String(args.target);
  const stop = String(args.stop);
  const ids: string[] = [];
  await openDb(home);
  await go();
  for (;;) {
    const stopping = existsSync(stop);
    const claimed = await claimBatch(home, target, 3);
    if (claimed.claim !== null) {
      await settleClaim(home, target, claimed.claim, 'ack');
      ids.push(...claimed.ids);
    }
    if (stopping && claimed.claim === null) break;
    if (claimed.claim === null) await sleep(5);
  }
  done({ ids });
}

async function send() {
  const from = String(args.from);
  const targets = args.targets as string[];
  const texts = args.texts as string[];
  const at = args.now === undefined ? undefined : Number(args.now);
  const results: { id?: string; duplicate?: boolean; error?: string; ms: number }[] = [];
  await openDb(home);
  await go();
  for (let i = 0; i < texts.length; i++) {
    const started = performance.now();
    try {
      const sent = await sendMessage(home, out(from, targets[i % targets.length]!, texts[i]!), at ?? Date.now());
      results.push({ id: sent.id, ...(sent.duplicate ? { duplicate: true } : {}), ms: performance.now() - started });
    } catch (e) {
      results.push({ error: e instanceof Error ? e.message : String(e), ms: performance.now() - started });
    }
  }
  done({ results });
}

async function holdLock() {
  const db = await openDb(home);
  db.sql.exec('BEGIN IMMEDIATE');
  const now = Date.now();
  db.sql
    .prepare(
      "INSERT INTO messages (id, route, from_id, from_agent, from_path, to_id, text, sent_at, sent_ms, state, state_ms) VALUES (?, 'agent-tabs', 'tab-dead', 'codex', '/', 'tab-b', 'never committed', ?, ?, 'unread', ?)",
    )
    .run(newMessageId(), new Date(now).toISOString(), now, now);
  process.stdout.write('locked\n');
  setInterval(() => undefined, 1_000);
}

async function open() {
  await go();
  const db = await openDb(home);
  done({ version: Number(db.sql.prepare('PRAGMA user_version').get()!.user_version) });
}

async function wakeSend() {
  const to = String(args.to);
  const from = String(args.from);
  const rounds = Number(args.rounds);
  await openDb(home);
  const commits: number[] = [];
  for (let i = 0; i < rounds; i++) {
    await go();
    await sleep(Number(args.delayMs));
    await sendMessage(home, out(from, to, `wake ${String(args.tag)} ${i}`));
    commits.push(Date.now());
    process.stdout.write(`sent ${Date.now()}\n`);
  }
  done({ commits });
}

async function shared() {
  setBusyTimeout(SHARED_BUSY_TIMEOUT_MS);
  const ids = args.sessions as string[];
  const internal = Number(args.internal);
  const expected = Number(args.expected);
  const delay = monitorEventLoopDelay({ resolution: 10 });
  const sends: number[] = [];
  const received = new Map<string, { id: string; from: string }[]>(ids.map((id) => [id, []]));
  await openDb(home);
  await go();
  delay.enable();
  const deadline = Date.now() + 90_000;
  const session = async (me: string, index: number) => {
    const waiting = (async () => {
      while (received.get(me)!.length < expected && Date.now() < deadline) {
        const m = await waitForMessage(home, me, {}, Math.max(1, deadline - Date.now()));
        if (m) received.get(me)!.push({ id: m.id, from: m.from.id });
      }
    })();
    for (let k = 1; k <= internal; k++) {
      const to = ids[(index + k) % ids.length]!;
      const started = performance.now();
      await sendMessage(home, out(me, to, `${me} to ${to} #${k}`));
      sends.push(performance.now() - started);
    }
    await waiting;
  };
  await Promise.all(ids.map((id, i) => session(id, i)));
  delay.disable();
  done({ received: Object.fromEntries(received), sends, loopP99Ms: delay.percentile(99) / 1e6, loopMaxMs: delay.max / 1e6 });
}

const modes: Record<string, () => Promise<void>> = { mesh, readLoop, claimLoop, send, holdLock, open, wakeSend, shared };
await modes[mode]!();

import { readFileSync, writeFileSync } from 'node:fs';
import { withFileLock } from '../src/files.js';

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

const modes: Record<string, () => Promise<void>> = { lockCount, lockOnce, lockHold };
await modes[mode]!();

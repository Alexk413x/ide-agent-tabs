import { randomBytes } from 'node:crypto';
import { promises as fs } from 'node:fs';
import path from 'node:path';
import { isProcessAlive } from './registry.js';

export async function ensurePrivateDir(dir: string): Promise<void> {
  await fs.mkdir(dir, { recursive: true, mode: 0o700 });
  if (process.platform !== 'win32') await fs.chmod(dir, 0o700);
}

export async function writeNewPrivateFile(file: string, content: string | Buffer): Promise<void> {
  await ensurePrivateDir(path.dirname(file));
  await fs.writeFile(file, content, { flag: 'wx', mode: 0o600 });
}

const RENAME_BUSY = new Set(['EPERM', 'EACCES', 'EBUSY']);

// Windows refuses to rename over a file while another process has it open, such as a reader of the same
// file; the reader closes it within milliseconds.
async function renameRetrying(from: string, to: string): Promise<void> {
  for (let attempt = 0; ; attempt++) {
    try {
      return await fs.rename(from, to);
    } catch (e) {
      const code = (e as NodeJS.ErrnoException).code ?? '';
      if (process.platform !== 'win32' || !RENAME_BUSY.has(code) || attempt >= 20) throw e;
      await new Promise((r) => setTimeout(r, 25));
    }
  }
}

export async function writeAtomically(file: string, content: string | Buffer): Promise<void> {
  await ensurePrivateDir(path.dirname(file));
  const temp = `${file}.${process.pid}.${Date.now()}.tmp`;
  try {
    await fs.writeFile(temp, content, { mode: 0o600 });
    await renameRetrying(temp, file);
  } catch (e) {
    await fs.rm(temp, { force: true });
    throw e;
  }
}

export async function readTextIfExists(file: string): Promise<string | undefined> {
  try {
    return await fs.readFile(file, 'utf8');
  } catch (e) {
    if ((e as NodeJS.ErrnoException).code === 'ENOENT') return undefined;
    throw e;
  }
}

export async function removeStaleFiles(dir: string, suffixes: string[], maxAgeMs: number, now = Date.now()): Promise<void> {
  let names: string[];
  try {
    names = await fs.readdir(dir);
  } catch {
    return;
  }
  await Promise.all(
    names
      .filter((n) => suffixes.some((s) => n.endsWith(s)))
      .map(async (n) => {
        const file = path.join(dir, n);
        const stat = await fs.stat(file).catch(() => undefined);
        if (stat && now - stat.mtimeMs > maxAgeMs) await fs.rm(file, { force: true }).catch(() => undefined);
      }),
  );
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

export const LOCK_STALE_MS = 10_000;
export const LOCK_WAIT_MS = 15_000;

export interface LockOptions {
  timeoutMs?: number;
}

function lockOwner(text: string | undefined): number | undefined {
  const pid = Number(/^(\d+) /.exec(text ?? '')?.[1]);
  return Number.isSafeInteger(pid) && pid > 0 ? pid : undefined;
}

async function isAbandoned(lock: string): Promise<string | undefined> {
  const [text, stat] = await Promise.all([readTextIfExists(lock).catch(() => undefined), fs.stat(lock).catch(() => undefined)]);
  if (text === undefined || !stat) return undefined;
  const owner = lockOwner(text);
  const dead = owner !== undefined && !isProcessAlive(owner);
  return dead || Date.now() - stat.mtimeMs > LOCK_STALE_MS ? text : undefined;
}

async function removeIfStill(lock: string, text: string): Promise<void> {
  if ((await readTextIfExists(lock).catch(() => undefined)) === text) await fs.rm(lock, { force: true }).catch(() => undefined);
}

export async function withFileLock<T>(file: string, work: () => Promise<T>, options: LockOptions = {}): Promise<T> {
  const lock = `${file}.lock`;
  const token = `${process.pid} ${randomBytes(8).toString('hex')}`;
  await ensurePrivateDir(path.dirname(file));
  const deadline = Date.now() + (options.timeoutMs ?? LOCK_WAIT_MS);
  for (;;) {
    try {
      await fs.writeFile(lock, token, { flag: 'wx', mode: 0o600 });
      break;
    } catch (e) {
      const code = (e as NodeJS.ErrnoException).code;
      // Windows answers EPERM, not EEXIST, while another process's delete of the lock is still pending.
      if (code !== 'EEXIST' && !(process.platform === 'win32' && code === 'EPERM')) throw e;
      const abandoned = await isAbandoned(lock);
      if (abandoned !== undefined) {
        await removeIfStill(lock, abandoned);
        continue;
      }
      if (Date.now() > deadline) throw new Error(`timed out waiting for ${lock}`);
      await sleep(25);
    }
  }
  try {
    return await work();
  } finally {
    await removeIfStill(lock, token);
  }
}

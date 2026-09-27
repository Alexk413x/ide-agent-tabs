import { promises as fs } from 'node:fs';
import path from 'node:path';

export async function ensurePrivateDir(dir: string): Promise<void> {
  await fs.mkdir(dir, { recursive: true, mode: 0o700 });
  if (process.platform !== 'win32') await fs.chmod(dir, 0o700);
}

export async function writeNewPrivateFile(file: string, content: string | Buffer): Promise<void> {
  await ensurePrivateDir(path.dirname(file));
  await fs.writeFile(file, content, { flag: 'wx', mode: 0o600 });
}

export async function writeAtomically(file: string, content: string): Promise<void> {
  await ensurePrivateDir(path.dirname(file));
  const temp = `${file}.${process.pid}.${Date.now()}.tmp`;
  try {
    await fs.writeFile(temp, content, { mode: 0o600 });
    await fs.rename(temp, file);
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

export async function withFileLock<T>(file: string, work: () => Promise<T>, timeoutMs = 5_000): Promise<T> {
  const lock = `${file}.lock`;
  await ensurePrivateDir(path.dirname(file));
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    try {
      const handle = await fs.open(lock, 'wx', 0o600);
      await handle.close();
      break;
    } catch (e) {
      if ((e as NodeJS.ErrnoException).code !== 'EEXIST') throw e;
      const stat = await fs.stat(lock).catch(() => undefined);
      if (stat && Date.now() - stat.mtimeMs > 10_000) {
        await fs.rm(lock, { force: true });
        continue;
      }
      if (Date.now() > deadline) throw new Error(`timed out waiting for ${lock}`);
      await sleep(25);
    }
  }
  try {
    return await work();
  } finally {
    await fs.rm(lock, { force: true });
  }
}

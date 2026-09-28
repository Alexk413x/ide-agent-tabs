import { createHash } from 'node:crypto';
import { promises as fs } from 'node:fs';
import path from 'node:path';
import { writeAtomically } from './files.js';

export const SERVER_FILE = 'mcp-server.mjs';
export const NOTICES_FILE = 'THIRD_PARTY_NOTICES.txt';
export const LAUNCH_DIR = 'launch';
const IN_USE = new Set(['EBUSY', 'EPERM', 'EACCES']);

export const serverCopyDir = (home: string) => path.join(home, 'mcp');

export function serverCopyPath(home: string, platform: NodeJS.Platform = process.platform): string {
  const file = path.join(serverCopyDir(home), SERVER_FILE);
  return platform === 'win32' ? file.replace(/\\/g, '/') : file;
}

export async function serverFiles(dir: string): Promise<string[]> {
  const launch = (await fs.readdir(path.join(dir, LAUNCH_DIR))).filter((n) => !n.endsWith('.tmp')).sort();
  return [SERVER_FILE, NOTICES_FILE, ...launch.map((n) => `${LAUNCH_DIR}/${n}`)];
}

export async function serverHash(dir: string): Promise<string> {
  const hash = createHash('sha256');
  for (const rel of await serverFiles(dir)) {
    hash.update(`${rel}\0`);
    hash.update(await fs.readFile(path.join(dir, rel)));
    hash.update('\0');
  }
  return hash.digest('hex').slice(0, 32);
}

export async function refreshServerCopy(sourceDir: string, home: string): Promise<string[]> {
  const dest = serverCopyDir(home);
  const files = await serverFiles(sourceDir);
  const changed: string[] = [];
  // The server file goes last, so a server that starts mid-refresh finds launch scripts at least as new as itself.
  for (const rel of [...files.filter((f) => f !== SERVER_FILE), SERVER_FILE]) {
    const content = await fs.readFile(path.join(sourceDir, rel));
    const target = path.join(dest, rel);
    const current = await fs.readFile(target).catch(() => undefined);
    if (current?.equals(content)) continue;
    try {
      await writeAtomically(target, content);
    } catch (e) {
      const code = (e as NodeJS.ErrnoException).code;
      if (code && IN_USE.has(code)) throw new Error(`${target} is in use (${code}); the next refresh tries again`);
      throw e;
    }
    changed.push(rel);
  }
  const keep = new Set(files);
  for (const name of await fs.readdir(path.join(dest, LAUNCH_DIR))) {
    if (!keep.has(`${LAUNCH_DIR}/${name}`) && !name.endsWith('.tmp')) {
      await fs.rm(path.join(dest, LAUNCH_DIR, name), { force: true });
      changed.push(`${LAUNCH_DIR}/${name}`);
    }
  }
  return changed;
}

import { createHash } from 'node:crypto';
import { promises as fs } from 'node:fs';
import path from 'node:path';
import { writeAtomically } from './files.js';
import { compareVersions } from './version.js';

export const SERVER_FILE = 'mcp-server.mjs';
export const NOTICES_FILE = 'THIRD_PARTY_NOTICES.txt';
export const HOOK_FILE = 'agent-hook.mjs';
export const LAUNCH_DIR = 'launch';
export const VERSION_FILE = 'version.json';
const IN_USE = new Set(['EBUSY', 'EPERM', 'EACCES']);

export const serverCopyDir = (home: string) => path.join(home, 'mcp');

function copyPath(home: string, name: string, platform: NodeJS.Platform): string {
  const file = path.join(serverCopyDir(home), name);
  return platform === 'win32' ? file.replace(/\\/g, '/') : file;
}

export const serverCopyPath = (home: string, platform: NodeJS.Platform = process.platform) => copyPath(home, SERVER_FILE, platform);
export const hookCopyPath = (home: string, platform: NodeJS.Platform = process.platform) => copyPath(home, HOOK_FILE, platform);

export async function serverFiles(dir: string): Promise<string[]> {
  const launch = (await fs.readdir(path.join(dir, LAUNCH_DIR))).filter((n) => !n.endsWith('.tmp')).sort();
  return [SERVER_FILE, HOOK_FILE, NOTICES_FILE, ...launch.map((n) => `${LAUNCH_DIR}/${n}`)];
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

async function readVersion(file: string): Promise<string | undefined> {
  try {
    const version = JSON.parse(await fs.readFile(file, 'utf8')).version;
    return typeof version === 'string' ? version : undefined;
  } catch {
    return undefined;
  }
}

export const pluginVersion = (sourceDir: string) => readVersion(path.join(sourceDir, '..', '.claude-plugin', 'plugin.json'));
export const copyVersion = (home: string) => readVersion(path.join(serverCopyDir(home), VERSION_FILE));

export async function refreshServerCopy(sourceDir: string, home: string): Promise<string[]> {
  const dest = serverCopyDir(home);
  const version = await pluginVersion(sourceDir);
  const installed = await copyVersion(home);
  // Every Claude Code install on this machine shares the copy, so an older plugin never replaces a newer one.
  if (version !== undefined && installed !== undefined && compareVersions(installed, version) > 0) return [];
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
  if (version !== undefined && version !== installed) {
    await writeAtomically(path.join(dest, VERSION_FILE), `${JSON.stringify({ version })}\n`);
    changed.push(VERSION_FILE);
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

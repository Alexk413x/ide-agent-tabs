import { promises as fs, watch, type FSWatcher } from 'node:fs';
import path from 'node:path';
import { query, wakeDir } from './db.js';

type Listener = () => void;

interface Folder {
  watcher?: FSWatcher;
  listeners: Map<string, Set<Listener>>;
}

const folders = new Map<string, Folder>();

export const wakePath = (home: string, id: string) => path.join(wakeDir(home), id);

const folderKey = (home: string) => path.resolve(wakeDir(home));

function dispatch(folder: Folder, name: string | null): void {
  const targets = name === null ? [...folder.listeners.values()] : [folder.listeners.get(name)].filter((s) => s !== undefined);
  for (const set of targets) for (const listener of [...set]) listener();
}

export function notifyLocal(home: string, id: string): void {
  const folder = folders.get(folderKey(home));
  if (folder !== undefined) dispatch(folder, id);
}

export async function signalWake(home: string, id: string): Promise<void> {
  notifyLocal(home, id);
  await fs.writeFile(wakePath(home, id), String(Date.now()), { mode: 0o600 }).catch(() => undefined);
}

function startWatch(key: string, folder: Folder): void {
  try {
    const watcher = watch(key, (_event, name) => dispatch(folder, typeof name === 'string' ? name : null));
    watcher.on('error', () => {
      watcher.close();
      if (folder.watcher === watcher) folder.watcher = undefined;
    });
    watcher.unref();
    folder.watcher = watcher;
  } catch {
    folder.watcher = undefined;
  }
}

export function onWake(home: string, id: string, listener: Listener): () => void {
  const key = folderKey(home);
  let folder = folders.get(key);
  if (folder === undefined) {
    folder = { listeners: new Map() };
    folders.set(key, folder);
  }
  if (folder.watcher === undefined) startWatch(key, folder);
  let set = folder.listeners.get(id);
  if (set === undefined) {
    set = new Set();
    folder.listeners.set(id, set);
  }
  set.add(listener);
  const own = folder;
  return () => {
    set.delete(listener);
    if (set.size === 0) own.listeners.delete(id);
    if (own.listeners.size === 0) {
      own.watcher?.close();
      if (folders.get(key) === own) folders.delete(key);
    }
  };
}

export async function dataVersion(home: string): Promise<number> {
  return query(home, (db) => Number(db.prepare('PRAGMA data_version').get()!.data_version));
}

export async function removeStaleWakes(home: string, live: ReadonlySet<string>, cutoffMs: number): Promise<void> {
  const dir = wakeDir(home);
  for (const name of await fs.readdir(dir).catch(() => [] as string[])) {
    if (live.has(name)) continue;
    const file = path.join(dir, name);
    const stat = await fs.stat(file).catch(() => undefined);
    if (stat?.isFile() && stat.mtimeMs < cutoffMs) await fs.rm(file, { force: true }).catch(() => undefined);
  }
}

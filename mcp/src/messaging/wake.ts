import { promises as fs, watch, type FSWatcher } from 'node:fs';
import path from 'node:path';
import { query, wakeDir } from './db.js';

type Listener = () => void;

interface Folder {
  watcher?: FSWatcher;
  poller?: ReturnType<typeof setInterval>;
  listeners: Map<string, Set<Listener>>;
}

const folders = new Map<string, Folder>();
export const SHARED_POLL_MS = 100;
let sharedPollMs: number | undefined;

// On Windows, fs.watch on a folder that many sessions signal stalls a process's event loop for 50-150 ms at a
// time, so the shared server polls the store's data_version once for all its waiters instead.
export function pollInsteadOfWatch(pollMs: number | undefined): void {
  sharedPollMs = pollMs;
}

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
  const local = folders.get(folderKey(home))?.listeners.has(id) === true;
  notifyLocal(home, id);
  if (local && sharedPollMs !== undefined) return;
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

function startPoll(home: string, folder: Folder, pollMs: number): void {
  let version: number | undefined;
  let busy = false;
  folder.poller = setInterval(() => {
    if (busy) return;
    busy = true;
    void dataVersion(home)
      .then((current) => {
        if (version !== undefined && current !== version) dispatch(folder, null);
        version = current;
      })
      .catch(() => undefined)
      .finally(() => (busy = false));
  }, pollMs);
  folder.poller.unref();
}

export function onWake(home: string, id: string, listener: Listener): () => void {
  const key = folderKey(home);
  let folder = folders.get(key);
  if (folder === undefined) {
    folder = { listeners: new Map() };
    folders.set(key, folder);
  }
  if (sharedPollMs !== undefined) {
    if (folder.poller === undefined) startPoll(home, folder, sharedPollMs);
  } else if (folder.watcher === undefined) startWatch(key, folder);
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
      if (own.poller !== undefined) clearInterval(own.poller);
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

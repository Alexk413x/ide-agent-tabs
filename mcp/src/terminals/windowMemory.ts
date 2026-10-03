import path from 'node:path';
import { readTextIfExists, withFileLock, writeAtomically } from '../files.js';

export const WINDOWS_FILE = 'terminal-windows.json';
export const DEDICATED_NAME = 'agent-tabs';

export interface RememberedWindow {
  id: string;
  socket?: string;
}

function parse(text: string | undefined): Record<string, RememberedWindow> {
  if (text === undefined) return {};
  try {
    const json: unknown = JSON.parse(text);
    if (typeof json !== 'object' || json === null || Array.isArray(json)) return {};
    return Object.fromEntries(
      Object.entries(json).filter(
        ([, w]) => typeof w?.id === 'string' && (w.socket === undefined || typeof w.socket === 'string'),
      ),
    ) as Record<string, RememberedWindow>;
  } catch {
    return {};
  }
}

export async function readWindow(home: string, terminal: string): Promise<RememberedWindow | undefined> {
  return parse(await readTextIfExists(path.join(home, WINDOWS_FILE)).catch(() => undefined))[terminal];
}

export async function rememberWindow(home: string, terminal: string, window: RememberedWindow): Promise<void> {
  const file = path.join(home, WINDOWS_FILE);
  await withFileLock(file, async () => {
    const all = parse(await readTextIfExists(file));
    all[terminal] = window;
    await writeAtomically(file, `${JSON.stringify(all, null, 2)}\n`);
  });
}

import path from 'node:path';
import { readTextIfExists, withFileLock, writeAtomically } from './files.js';
import type { TerminalTab } from './terminals/types.js';

export const TABS_FILE = 'terminal-tabs.json';

export function parseTabs(text: string | undefined): TerminalTab[] {
  if (text === undefined || text.trim() === '') return [];
  try {
    const json = JSON.parse(text) as { tabs?: unknown };
    if (!Array.isArray(json.tabs)) return [];
    return json.tabs.filter(
      (t): t is TerminalTab =>
        typeof t === 'object' &&
        t !== null &&
        typeof t.id === 'string' &&
        typeof t.terminal === 'string' &&
        typeof t.agent === 'string' &&
        typeof t.path === 'string' &&
        typeof t.createdAt === 'number',
    );
  } catch {
    return [];
  }
}

export class TabStore {
  readonly file: string;

  constructor(home: string) {
    this.file = path.join(home, TABS_FILE);
  }

  async read(): Promise<TerminalTab[]> {
    return parseTabs(await readTextIfExists(this.file));
  }

  async update(change: (tabs: TerminalTab[]) => TerminalTab[]): Promise<TerminalTab[]> {
    return withFileLock(this.file, async () => {
      const next = change(await this.read());
      await writeAtomically(this.file, `${JSON.stringify({ tabs: next }, null, 2)}\n`);
      return next;
    });
  }

  add(tab: TerminalTab): Promise<TerminalTab[]> {
    return this.update((tabs) => [...tabs.filter((t) => t.id !== tab.id), tab]);
  }

  remove(ids: Set<string>): Promise<TerminalTab[]> {
    return this.update((tabs) => tabs.filter((t) => !ids.has(t.id)));
  }
}

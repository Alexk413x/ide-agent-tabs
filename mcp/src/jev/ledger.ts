import { promises as fs } from 'node:fs';
import path from 'node:path';
import { ensurePrivateDir, readTextIfExists } from '../files.js';

export interface LedgerEntry {
  at: string;
  tool: string;
  agent: string | null;
  tab: string | null;
  model: string;
  questions: number;
  input_tokens: number;
  ok: boolean;
  status?: number | string;
}

export const ledgerPath = (home: string) => path.join(home, 'jev', 'ledger.jsonl');

export async function appendLedger(home: string, entry: LedgerEntry): Promise<void> {
  const file = ledgerPath(home);
  await ensurePrivateDir(path.dirname(file));
  await fs.appendFile(file, `${JSON.stringify(entry)}\n`, { mode: 0o600 });
}

export function costUsd(inputTokens: number, pricePerMillionInput: number): number {
  return Number(((inputTokens * pricePerMillionInput) / 1e6).toFixed(8));
}

function localDay(d: Date): string {
  return `${d.getFullYear()}-${d.getMonth() + 1}-${d.getDate()}`;
}

function entriesOf(text: string): LedgerEntry[] {
  return text.split('\n').flatMap((line) => {
    if (line.trim() === '') return [];
    try {
      const e = JSON.parse(line) as LedgerEntry;
      return typeof e.at === 'string' && typeof e.ok === 'boolean' ? [e] : [];
    } catch {
      return [];
    }
  });
}

export async function summarizeLedger(home: string, pricePerMillionInput: number, now = new Date()) {
  const entries = entriesOf((await readTextIfExists(ledgerPath(home))) ?? '');
  const today = entries.filter((e) => localDay(new Date(e.at)) === localDay(now));
  const inputTokens = today.reduce((sum, e) => sum + (Number.isFinite(e.input_tokens) ? e.input_tokens : 0), 0);
  return {
    last_model: entries.filter((e) => e.ok).at(-1)?.model ?? null,
    today: {
      calls: today.length,
      failed: today.filter((e) => !e.ok).length,
      input_tokens: inputTokens,
      cost_usd: costUsd(inputTokens, pricePerMillionInput),
    },
  };
}

import { MODEL_PATTERN } from './launchPlan.js';
import { readClosed, resumableId, type ClosedSession } from './messaging/closed.js';
import type { PresenceFile } from './messaging/sessions.js';
import { ALLOW_RESUME_KEY, CONFIG_FILE, type AgentSettings } from './profiles.js';
import type { OpenInput } from './request.js';
import { ToolError } from './service.js';

export const CACHE_MS = 5 * 60_000;
export const LONG_CACHE_MS = 60 * 60_000;
export const MAX_CHEAP_TOKENS = 50_000;
export const CHEAP_NOTE = 'likely cached: about 10% of normal input cost';
const MIN_ID_PREFIX = 4;

export const RESUME_ARGS: Record<string, (id: string) => string[]> = {
  claude: (id) => ['--resume', id],
  codex: (id) => ['resume', id],
  'codex-local': (id) => ['resume', id],
  agy: (id) => ['--conversation', id],
};

export interface ResumeInput {
  id: string;
  ide?: string;
  model?: string;
  focus?: boolean;
  confirm?: boolean;
}

export interface ResumeDeps {
  home: string;
  settings: () => Promise<AgentSettings>;
  openTab: (input: OpenInput) => Promise<Record<string, unknown>>;
  liveHost: (host: string | null, product: string | null) => Promise<string | undefined>;
  live: () => Promise<PresenceFile[]>;
  now?: () => number;
}

export function ago(ms: number): string {
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 60) return `${s}s ago`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.round(m / 60);
  if (h < 48) return `${h}h ago`;
  return `${Math.round(h / 24)}d ago`;
}

export function sizeText(tokens: number | null): string {
  if (tokens === null) return 'unknown size';
  if (tokens < 1000) return `${tokens} tokens`;
  if (tokens < 1_000_000) return `${Math.round(tokens / 1000)}k tokens`;
  return `${(tokens / 1_000_000).toFixed(1)}M tokens`;
}

const shortSize = (tokens: number | null) => (tokens === null ? '—' : sizeText(tokens).replace(' tokens', ''));
const nameOf = (r: ClosedSession) => r.name ?? `${r.agent}-${r.id.replace(/[^A-Za-z0-9]/g, '').slice(0, 4)}`;

export function cacheWindowMs(r: Pick<ClosedSession, 'cache'>): number {
  return r.cache === '1h' ? LONG_CACHE_MS : CACHE_MS;
}

export interface CostCheck {
  cheap: boolean;
  reasons: string[];
  withinCache: boolean;
}

export function costCheck(r: Pick<ClosedSession, 'cache' | 'tokens' | 'model'>, ageMs: number, model: string | undefined): CostCheck {
  const window = cacheWindowMs(r);
  const withinCache = ageMs <= window;
  const reasons: string[] = [];
  if (!withinCache) reasons.push(`it ended past the ${window === LONG_CACHE_MS ? '1-hour' : '5-minute'} prompt cache window`);
  if (model !== undefined && model !== r.model) reasons.push(`model ${model} differs from the session's ${r.model ?? 'unknown model'}, so no cache applies`);
  if (r.tokens !== null && r.tokens > MAX_CHEAP_TOKENS) reasons.push(`it holds over ${MAX_CHEAP_TOKENS.toLocaleString('en-US')} tokens`);
  if (r.tokens === null && ageMs > CACHE_MS) reasons.push('its size is unknown and it ended over 5 minutes ago');
  return { cheap: reasons.length === 0, reasons, withinCache };
}

export function closedListing(records: readonly ClosedSession[], now: number): string {
  if (!records.length) return 'No closed session in the last 7 days.';
  const header = ['NAME', 'AGENT', 'ENDED', 'SIZE', 'MODEL', 'WHERE', 'ID'];
  const cells = (r: ClosedSession) => [nameOf(r), r.harness, ago(now - Date.parse(r.endedAt)), shortSize(r.tokens), r.model ?? '—', r.product ?? '—', r.id.slice(0, 8)];
  const rows = records.map(cells);
  const widths = header.map((h, i) => Math.max(h.length, ...rows.map((c) => c[i]!.length)));
  const line = (c: string[]) => `  ${c.map((v, i) => v.padEnd(widths[i]!)).join('  ')}`.trimEnd();
  const folders: string[] = [];
  for (const r of records) if (!folders.includes(r.folder)) folders.push(r.folder);
  const groups = folders.map((f) => [f, ...records.filter((r) => r.folder === f).map((r) => line(cells(r)))].join('\n'));
  return `${line(header)}\n${groups.join('\n\n')}`;
}

export class Resumes {
  constructor(private readonly deps: ResumeDeps) {}

  private now(): number {
    return (this.deps.now ?? Date.now)();
  }

  private async closed(): Promise<ClosedSession[]> {
    const [records, live] = await Promise.all([readClosed(this.deps.home, this.now()), this.deps.live().catch(() => [] as PresenceFile[])]);
    const running = new Set(live.map(resumableId).filter((id) => id !== undefined));
    return records.filter((r) => !running.has(r.id));
  }

  async list() {
    const now = this.now();
    const records = await this.closed();
    return {
      listing: closedListing(records, now),
      sessions: records.map((r) => ({
        id: r.id,
        name: nameOf(r),
        agent: r.agent,
        harness: r.harness,
        folder: r.folder,
        endedAt: r.endedAt,
        ended: ago(now - Date.parse(r.endedAt)),
        tokens: r.tokens,
        size: sizeText(r.tokens),
        model: r.model,
        effort: r.effort,
        where: r.product,
        preview: r.preview,
        resumable: RESUME_ARGS[r.agent] !== undefined,
      })),
    };
  }

  private async find(id: string): Promise<ClosedSession> {
    const records = await this.closed();
    const exact = records.find((r) => r.id === id);
    if (exact) return exact;
    const matches = id.length >= MIN_ID_PREFIX ? records.filter((r) => r.id.startsWith(id)) : [];
    if (matches.length === 1) return matches[0]!;
    if (matches.length > 1) throw new ToolError(`${id} matches ${matches.length} closed sessions; pass more of the id from closed_sessions`);
    throw new ToolError(`no closed session with id ${id} in the last 7 days; call closed_sessions for the ids`);
  }

  async resume(input: ResumeInput) {
    const settings = await this.deps.settings();
    if (!settings.allowResume) {
      throw new ToolError(
        `resuming closed sessions is off ("Allow resuming closed sessions", ${ALLOW_RESUME_KEY} in ~/.ide-agent-tabs/${CONFIG_FILE}). Turn it on in the IDE settings, or start a fresh session with handoff.`,
      );
    }
    const r = await this.find(input.id);
    const args = RESUME_ARGS[r.agent];
    if (args === undefined) {
      throw new ToolError(`${r.label} has no resume option that Agent Tabs knows, so session ${r.id.slice(0, 8)} can't be reopened; use handoff to start a fresh ${r.label} session with a brief.`);
    }
    const now = this.now();
    const ageMs = now - Date.parse(r.endedAt);
    const recorded = r.model !== null && MODEL_PATTERN.test(r.model) ? r.model : undefined;
    const model = input.model ?? recorded;
    const check = costCheck(r, ageMs, input.model);
    const facts = { id: r.id, agent: r.agent, folder: r.folder, tokens: r.tokens, size: sizeText(r.tokens), age: ago(ageMs), endedAt: r.endedAt };
    if (!check.cheap && input.confirm !== true) {
      const cached = check.withinCache && check.reasons.length === 1 && r.tokens !== null && r.tokens > MAX_CHEAP_TOKENS ? ' The prompt cache may still hold part of it, but not reliably.' : '';
      return {
        resumed: false,
        needsConfirm: true,
        ...facts,
        reasons: check.reasons,
        message:
          `Not resumed: ${check.reasons.join('; ')}. Resuming makes ${r.label} re-read the full history, ${sizeText(r.tokens)}, at full input price. It ended ${ago(ageMs)}.${cached} ` +
          'Handoff is the cheaper option: a fresh session that starts from a short brief. Ask the user which they want, and call resume_tab again with confirm: true only after they agree to the cost.',
      };
    }
    const ide = input.ide ?? (await this.deps.liveHost(r.host, r.product).catch(() => undefined));
    const opened = await this.deps.openTab({
      path: r.folder,
      agent: r.agent,
      args: args(r.id),
      ...(model !== undefined ? { model } : {}),
      ...(r.via !== null ? { via: r.via } : {}),
      ...(ide !== undefined ? { ide } : {}),
      ...(input.focus !== undefined ? { focus: input.focus } : {}),
    });
    return {
      resumed: true,
      ...facts,
      tab: opened.id,
      ide: opened.ide,
      product: opened.product,
      ...(model !== undefined ? { model } : {}),
      cost: check.cheap ? CHEAP_NOTE : `the user confirmed: the full history, ${sizeText(r.tokens)}, is re-read at full input price`,
      ...(opened.note !== undefined ? { note: opened.note } : {}),
    };
  }
}

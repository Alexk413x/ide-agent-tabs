import assert from 'node:assert/strict';
import { existsSync, mkdirSync, readFileSync, statSync, utimesSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js';
import { CLOSED_DIR, CLOSED_KEEP_MS, closedPath, codexUsage, readClosed, recordEnded, type ClosedSession, type TranscriptDirs } from '../src/messaging/closed.js';
import { Messaging } from '../src/messaging/messaging.js';
import { liveSessions, presencePath, type PresenceFile } from '../src/messaging/sessions.js';
import { resolveSettings } from '../src/profiles.js';
import type { OpenInput } from '../src/request.js';
import { CHEAP_NOTE, closedListing, costCheck, Resumes, type ResumeDeps } from '../src/resume.js';
import { createServer } from '../src/server.js';
import { Service } from '../src/service.js';
import type { TerminalDriver } from '../src/terminals/types.js';
import { tempDir } from './tempDir.js';

const CLAUDE_ID = '0b5d2c1e-1111-4222-8333-444455556666';
const CODEX_ID = '01a10626-3892-7963-938c-a326a5769d94';
const AGY_ID = '7f00aa11-2222-4333-8444-555566667777';
const NOW = Date.parse('2026-10-04T12:00:00.000Z');
const MINUTE = 60_000;

function dirsIn(root: string): TranscriptDirs {
  return { claude: path.join(root, 'claude'), codex: path.join(root, 'codex') };
}

function writeClaudeTranscript(dirs: TranscriptDirs, folder: string, id: string, turns: { text: string; usage: Record<string, unknown> }[]) {
  const dir = path.join(dirs.claude, 'projects', folder.replace(/[^A-Za-z0-9]/g, '-'));
  mkdirSync(dir, { recursive: true });
  const lines = [
    JSON.stringify({ type: 'user', message: { role: 'user', content: 'secret user prompt' } }),
    ...turns.map((t) => JSON.stringify({ type: 'assistant', isSidechain: false, message: { model: 'claude-opus-5-5', role: 'assistant', content: [{ type: 'text', text: t.text }], usage: t.usage } })),
    JSON.stringify({ type: 'assistant', isSidechain: true, message: { role: 'assistant', content: [{ type: 'text', text: 'subagent' }], usage: { input_tokens: 999999 } } }),
  ];
  writeFileSync(path.join(dir, `${id}.jsonl`), `${lines.join('\n')}\n`);
}

const presence = (over: Partial<PresenceFile> = {}): PresenceFile => ({
  id: 'tab-claude-1',
  agent: 'claude',
  path: '/work/app',
  pid: 4242,
  startedAt: '2026-10-04T10:00:00.000Z',
  owner: CLAUDE_ID,
  model: 'claude-opus-5-5',
  effort: 'high',
  product: 'IntelliJ IDEA',
  host: 'jetbrains-1',
  nativeName: 'parser-fix',
  ...over,
});

function record(over: Partial<ClosedSession> = {}): ClosedSession {
  return {
    id: CLAUDE_ID,
    agent: 'claude',
    label: 'Claude Code',
    name: 'parser-fix',
    folder: '/work/app',
    product: 'IntelliJ IDEA',
    host: 'jetbrains-1',
    model: 'claude-opus-5-5',
    effort: 'high',
    harness: 'Claude Code',
    via: null,
    startedAt: '2026-10-04T10:00:00.000Z',
    endedAt: new Date(NOW - 2 * MINUTE).toISOString(),
    tokens: 20_000,
    cache: '5m',
    preview: 'Done.',
    tab: 'tab-claude-1',
    ...over,
  };
}

function saveRecord(home: string, r: ClosedSession) {
  mkdirSync(path.join(home, CLOSED_DIR), { recursive: true });
  writeFileSync(closedPath(home, r.id), JSON.stringify(r));
}

function resumes(home: string, over: Partial<ResumeDeps> & { config?: object } = {}) {
  const opened: OpenInput[] = [];
  const r = new Resumes({
    home,
    settings: async () => resolveSettings(undefined, over.config === undefined ? undefined : JSON.stringify(over.config)),
    openTab: async (input) => {
      opened.push(input);
      return { id: 'tab-new', ide: input.ide ?? 'auto-ide', product: 'IntelliJ IDEA', agent: input.agent };
    },
    liveHost: async (host) => (host === 'jetbrains-1' ? 'jetbrains-1' : undefined),
    live: async () => [],
    now: () => NOW,
    ...over,
  });
  return { r, opened };
}

test('a Claude session that ends leaves an owner-only record with the size and preview of its last turn, and no transcript text', async () => {
  const root = tempDir('iat-closed-');
  const home = path.join(root, 'home');
  const dirs = dirsIn(root);
  const long = `${'x'.repeat(200)}\nsecond line`;
  writeClaudeTranscript(dirs, '/work/app', CLAUDE_ID, [
    { text: 'first answer', usage: { input_tokens: 5, cache_creation_input_tokens: 100, cache_read_input_tokens: 1000, cache_creation: { ephemeral_1h_input_tokens: 100, ephemeral_5m_input_tokens: 0 } } },
    { text: long, usage: { input_tokens: 2, cache_creation_input_tokens: 0, cache_read_input_tokens: 41_000, cache_creation: { ephemeral_1h_input_tokens: 0, ephemeral_5m_input_tokens: 0 } } },
  ]);
  const written = await recordEnded(home, presence(), NOW, dirs);
  assert.ok(written);
  const saved = JSON.parse(readFileSync(closedPath(home, CLAUDE_ID), 'utf8')) as ClosedSession;
  assert.equal(saved.id, CLAUDE_ID);
  assert.equal(saved.agent, 'claude');
  assert.equal(saved.label, 'Claude Code');
  assert.equal(saved.folder, '/work/app');
  assert.equal(saved.product, 'IntelliJ IDEA');
  assert.equal(saved.model, 'claude-opus-5-5');
  assert.equal(saved.effort, 'high');
  assert.equal(saved.harness, 'Claude Code');
  assert.equal(saved.startedAt, '2026-10-04T10:00:00.000Z');
  assert.equal(saved.endedAt, new Date(NOW).toISOString());
  assert.equal(saved.tokens, 41_002);
  assert.equal(saved.cache, '1h', 'the newest turn that wrote the cache shows the 1-hour cache');
  assert.equal(saved.preview, `${'x'.repeat(119)}…`);
  assert.equal(saved.tab, 'tab-claude-1');
  const text = readFileSync(closedPath(home, CLAUDE_ID), 'utf8');
  assert.doesNotMatch(text, /secret user prompt|second line|first answer/);
  if (process.platform !== 'win32') {
    assert.equal(statSync(closedPath(home, CLAUDE_ID)).mode & 0o777, 0o600);
    assert.equal(statSync(path.join(home, CLOSED_DIR)).mode & 0o777, 0o700);
  }
});

test('a session without a resumable id, or a Claude session without a transcript, leaves no record', async () => {
  const root = tempDir('iat-closed-none-');
  const home = path.join(root, 'home');
  const dirs = dirsIn(root);
  assert.equal(await recordEnded(home, presence({ owner: undefined }), NOW, dirs), undefined);
  assert.equal(await recordEnded(home, presence(), NOW, dirs), undefined);
  const agy = await recordEnded(home, presence({ id: 'tab-agy', agent: 'agy', owner: AGY_ID, model: undefined }), NOW, dirs);
  assert.equal(agy?.id, AGY_ID);
  assert.equal(agy?.tokens, null);
  assert.equal(agy?.preview, null);
});

test('a Codex record takes the thread id and the last token count and answer from the rollout', async () => {
  const root = tempDir('iat-closed-codex-');
  const dirs = dirsIn(root);
  const day = path.join(dirs.codex, 'sessions', '2026', '10', '04');
  mkdirSync(day, { recursive: true });
  writeFileSync(
    path.join(day, `rollout-2026-10-04T02-02-10-${CODEX_ID}.jsonl`),
    [
      { type: 'session_meta', payload: { id: CODEX_ID } },
      { type: 'event_msg', payload: { type: 'token_count', info: { last_token_usage: { input_tokens: 12_000 } } } },
      { type: 'response_item', payload: { type: 'message', role: 'assistant', content: [{ type: 'output_text', text: 'Tests pass.\nDetails follow.' }] } },
      { type: 'event_msg', payload: { type: 'token_count', info: { last_token_usage: { input_tokens: 37_637 } } } },
    ]
      .map((l) => JSON.stringify(l))
      .join('\n'),
  );
  const usage = await codexUsage(dirs, CODEX_ID);
  assert.deepEqual(usage, { found: true, tokens: 37_637, cache: null, preview: 'Tests pass.' });
  const home = path.join(root, 'home');
  const saved = await recordEnded(home, presence({ id: 'codex-x', agent: 'codex', owner: 'other', threadId: CODEX_ID, model: 'gpt-5.5' }), NOW, dirs);
  assert.equal(saved?.id, CODEX_ID);
  assert.equal(saved?.tokens, 37_637);
});

test('liveSessions records a dead session as ended when its presence file last changed, and a server records its own end', async () => {
  const root = tempDir('iat-closed-live-');
  const home = path.join(root, 'home');
  const dirs = dirsIn(root);
  writeClaudeTranscript(dirs, '/work/app', CLAUDE_ID, [{ text: 'ok', usage: { input_tokens: 10 } }]);
  mkdirSync(path.join(home, 'sessions'), { recursive: true });
  writeFileSync(presencePath(home, 'tab-claude-1'), JSON.stringify(presence({ state: 'idle' })));
  const beat = new Date(NOW - 3 * MINUTE);
  utimesSync(presencePath(home, 'tab-claude-1'), beat, beat);
  const seen: [string, number][] = [];
  const live = await liveSessions(home, () => false, NOW, async (p, at) => {
    seen.push([p.id, at]);
    return recordEnded(home, p, at, dirs);
  });
  assert.deepEqual(live, []);
  assert.deepEqual(seen, [['tab-claude-1', beat.getTime()]]);
  assert.equal(JSON.parse(readFileSync(closedPath(home, CLAUDE_ID), 'utf8')).endedAt, beat.toISOString());

  const env = { IDE_AGENT_TABS_ID: 'tab-codex-2', IDE_AGENT_TABS_AGENT: 'codex' };
  const hosts = { findHost: async () => undefined, typeInto: async () => ({ ok: true as const }) };
  const m = new Messaging({ home, env, pid: 777, cwd: '/work/other', hosts, isAlive: () => true, now: () => NOW, transcripts: dirs });
  await m.start();
  await m.noteThread(CODEX_ID);
  await m.recordEnd();
  m.stopSync();
  const own = JSON.parse(readFileSync(closedPath(home, CODEX_ID), 'utf8')) as ClosedSession;
  assert.equal(own.agent, 'codex');
  assert.equal(own.folder, '/work/other');
  assert.equal(own.tokens, null);
});

test('the mod reports the Claude session id as the owner', async () => {
  const home = tempDir('iat-closed-mod-');
  const hosts = { findHost: async () => undefined, typeInto: async () => ({ ok: true as const }) };
  const m = new Messaging({ home, env: { IDE_AGENT_TABS_AGENT: 'claude' }, pid: 778, cwd: '/w', hosts, isAlive: () => true, randomId: () => 's-000000000001' });
  await m.start();
  try {
    await m.modPresence({ owner: CLAUDE_ID });
    assert.equal(JSON.parse(readFileSync(presencePath(home, 's-000000000001'), 'utf8')).owner, CLAUDE_ID);
    await assert.rejects(m.modPresence({ owner: 'not an id' }), /not a session id/);
  } finally {
    m.stopSync();
  }
});

test('close_tab records the session it ends', async () => {
  const root = tempDir('iat-closed-close-');
  const home = path.join(root, 'home');
  const dirs = dirsIn(root);
  writeClaudeTranscript(dirs, '/work/app', CLAUDE_ID, [{ text: 'bye', usage: { input_tokens: 7 } }]);
  const driver: TerminalDriver = {
    name: 'fake-term',
    label: 'Fake Terminal',
    capabilities: { open: 'tab', list: 'yes', close: 'yes' },
    available: async () => true,
    open: async (_ctx, spec) => ({ id: spec.id, terminal: 'fake-term', agent: spec.agent, path: spec.cwd, createdAt: Date.now() }),
    alive: async (_ctx, tabs) => new Set(tabs.map((t) => t.id)),
    close: async () => undefined,
  };
  const service = new Service({ home, scriptsDir: home, platform: 'linux', env: { PATH: '' }, callIde: async () => ({}), drivers: [driver], newId: () => 'tab-claude-1', transcripts: dirs });
  await service.openTab({ path: root, ide: 'fake-term' });
  writeFileSync(presencePath(home, 'tab-claude-1'), JSON.stringify(presence({ path: '/work/app' })));
  await service.closeTab('tab-claude-1');
  assert.equal(JSON.parse(readFileSync(closedPath(home, CLAUDE_ID), 'utf8')).preview, 'bye');
});

test('records are kept for 7 days', async () => {
  const home = tempDir('iat-closed-keep-');
  saveRecord(home, record());
  saveRecord(home, record({ id: CODEX_ID, agent: 'codex', endedAt: new Date(NOW - CLOSED_KEEP_MS - MINUTE).toISOString() }));
  saveRecord(home, record({ id: AGY_ID, agent: 'agy' }));
  const old = new Date(NOW - CLOSED_KEEP_MS - MINUTE);
  utimesSync(closedPath(home, AGY_ID), old, old);
  const kept = await readClosed(home, NOW);
  assert.deepEqual(kept.map((r) => r.id), [CLAUDE_ID]);
  assert.equal(existsSync(closedPath(home, AGY_ID)), false, 'a file older than 7 days is deleted');
});

test('closed_sessions lists records newest first, grouped by folder, one aligned line each, without live sessions', async () => {
  const home = tempDir('iat-closed-list-');
  saveRecord(home, record({ endedAt: new Date(NOW - 2 * MINUTE).toISOString() }));
  saveRecord(home, record({ id: CODEX_ID, agent: 'codex', label: 'Codex', harness: 'Codex', name: null, folder: '/work/api', model: 'gpt-5.5', product: 'Windows Terminal', tokens: null, endedAt: new Date(NOW - 90 * MINUTE).toISOString() }));
  saveRecord(home, record({ id: AGY_ID, agent: 'agy', label: 'Antigravity CLI', harness: 'Antigravity CLI', name: null, tokens: 1_234_567, endedAt: new Date(NOW - 3 * 24 * 60 * MINUTE).toISOString() }));
  const { r } = resumes(home);
  const { listing, sessions } = await r.list();
  assert.deepEqual(sessions.map((s) => s.id), [CLAUDE_ID, CODEX_ID, AGY_ID]);
  assert.equal(
    listing,
    [
      '  NAME        AGENT            ENDED   SIZE  MODEL            WHERE             ID',
      '/work/app',
      '  parser-fix  Claude Code      2m ago  20k   claude-opus-5-5  IntelliJ IDEA     0b5d2c1e',
      '  agy-7f00    Antigravity CLI  3d ago  1.2M  claude-opus-5-5  IntelliJ IDEA     7f00aa11',
      '',
      '/work/api',
      '  codex-01a1  Codex            2h ago  —     gpt-5.5          Windows Terminal  01a10626',
    ].join('\n'),
  );
  assert.equal(sessions[0]!.size, '20k tokens');
  assert.equal(sessions[1]!.resumable, true);

  const live = resumes(home, { live: async () => [{ id: 'tab-x', agent: 'claude', owner: CLAUDE_ID }] });
  assert.deepEqual((await live.r.list()).sessions.map((s) => s.id), [CODEX_ID, AGY_ID]);
  assert.equal(closedListing([], NOW), 'No closed session in the last 7 days.');
});

test('resume_tab uses each agent resume command in the record folder, IDE and model', async () => {
  const home = tempDir('iat-resume-');
  saveRecord(home, record());
  saveRecord(home, record({ id: CODEX_ID, agent: 'codex', label: 'Codex', model: 'gpt-5.5', host: 'windows-terminal', product: 'Windows Terminal' }));
  saveRecord(home, record({ id: AGY_ID, agent: 'agy', label: 'Antigravity CLI', model: 'Gemini 3 Pro', host: null, product: null, tokens: 3_000 }));
  const { r, opened } = resumes(home);

  const claude = await r.resume({ id: CLAUDE_ID.slice(0, 8) });
  assert.equal(claude.resumed, true);
  assert.equal(claude.size, '20k tokens');
  assert.equal(claude.age, '2m ago');
  assert.deepEqual(opened.at(-1), { path: '/work/app', agent: 'claude', args: ['--resume', CLAUDE_ID], model: 'claude-opus-5-5', ide: 'jetbrains-1' });

  await r.resume({ id: CODEX_ID });
  assert.deepEqual(opened.at(-1), { path: '/work/app', agent: 'codex', args: ['resume', CODEX_ID], model: 'gpt-5.5' }, 'a host that is gone routes by folder');

  await r.resume({ id: AGY_ID, focus: true });
  assert.deepEqual(opened.at(-1), { path: '/work/app', agent: 'agy', args: ['--conversation', AGY_ID], focus: true }, 'a model name the flag cannot take is left out');

  await r.resume({ id: CLAUDE_ID, ide: 'wezterm', model: 'claude-opus-5-5', focus: false });
  assert.deepEqual(opened.at(-1), { path: '/work/app', agent: 'claude', args: ['--resume', CLAUDE_ID], model: 'claude-opus-5-5', ide: 'wezterm', focus: false });
});

test('resume_tab refuses an agent without a known resume option and points to handoff', async () => {
  const home = tempDir('iat-resume-other-');
  saveRecord(home, record({ id: 'gem-1234', agent: 'gemini', label: 'Gemini CLI' }));
  const { r, opened } = resumes(home);
  await assert.rejects(r.resume({ id: 'gem-1234' }), /Gemini CLI has no resume option.*handoff/);
  await assert.rejects(r.resume({ id: 'nope-0000' }), /no closed session with id nope-0000/);
  assert.equal(opened.length, 0);
});

test('the cost guard resumes a likely cached session and asks before a full-price one', async () => {
  const home = tempDir('iat-resume-cost-');
  const { r, opened } = resumes(home);
  const cases: [string, Partial<ClosedSession>, { model?: string }, boolean, RegExp?][] = [
    ['within 5 minutes, same model', { endedAt: new Date(NOW - 4 * MINUTE).toISOString() }, {}, true],
    ['past 5 minutes', { endedAt: new Date(NOW - 6 * MINUTE).toISOString() }, {}, false, /5-minute prompt cache window/],
    ['1-hour cache at 30 minutes', { cache: '1h', endedAt: new Date(NOW - 30 * MINUTE).toISOString() }, {}, true],
    ['1-hour cache past an hour', { cache: '1h', endedAt: new Date(NOW - 61 * MINUTE).toISOString() }, {}, false, /1-hour prompt cache window/],
    ['another model', {}, { model: 'claude-sonnet-5' }, false, /model claude-sonnet-5 differs/],
    ['over 50,000 tokens', { tokens: 50_001 }, {}, false, /over 50,000 tokens/],
    ['unknown size within 5 minutes', { tokens: null }, {}, true],
    ['unknown size past 5 minutes', { tokens: null, cache: '1h', endedAt: new Date(NOW - 10 * MINUTE).toISOString() }, {}, false, /size is unknown/],
  ];
  for (const [name, over, input, cheap, reason] of cases) {
    saveRecord(home, record(over));
    const before = opened.length;
    const result = await r.resume({ id: CLAUDE_ID, ...input });
    assert.equal(result.resumed, cheap, name);
    assert.ok(typeof result.size === 'string' && typeof result.age === 'string', `${name}: size and age`);
    if (cheap) {
      assert.equal((result as { cost: string }).cost, CHEAP_NOTE, name);
      assert.doesNotMatch(CHEAP_NOTE, /free/);
      assert.equal(opened.length, before + 1, name);
      continue;
    }
    const refusal = result as { needsConfirm: boolean; message: string; reasons: string[] };
    assert.equal(refusal.needsConfirm, true, name);
    assert.match(refusal.reasons.join('; '), reason!, name);
    assert.match(refusal.message, /full history.*full input price/, name);
    assert.match(refusal.message, /Handoff is the cheaper option/, name);
    assert.match(refusal.message, /confirm: true only after they agree/, name);
    assert.equal(opened.length, before, `${name}: nothing opens`);
    const confirmed = await r.resume({ id: CLAUDE_ID, ...input, confirm: true });
    assert.equal(confirmed.resumed, true, `${name}: confirm opens it`);
    assert.match((confirmed as { cost: string }).cost, /full input price/);
  }
  assert.equal(costCheck({ cache: null, tokens: 50_000, model: 'm' }, 5 * MINUTE, undefined).cheap, true);
});

test('with allowResume off, resume_tab refuses and says how to turn it on', async () => {
  const home = tempDir('iat-resume-off-');
  saveRecord(home, record());
  const { r, opened } = resumes(home, { config: { allowResume: false } });
  await assert.rejects(r.resume({ id: CLAUDE_ID }), /resuming closed sessions is off.*Allow resuming closed sessions.*allowResume/);
  assert.equal(opened.length, 0);
  assert.equal(resolveSettings(undefined, undefined).allowResume, true);
  const bad = resolveSettings(undefined, JSON.stringify({ allowResume: 'no' }));
  assert.equal(bad.allowResume, true);
  assert.match(bad.warnings.join(' '), /allowResume .*true or false/);
});

test('the server lists closed_sessions and resume_tab only with resumes, and resume_tab opens a terminal tab with the resume flag', async () => {
  const root = tempDir('iat-resume-srv-');
  const home = path.join(root, 'home');
  const opened: string[][] = [];
  const driver: TerminalDriver = {
    name: 'fake-term',
    label: 'Fake Terminal',
    capabilities: { open: 'tab', list: 'yes', close: 'yes' },
    available: async () => true,
    open: async (_ctx, spec) => (opened.push([spec.command, ...spec.args]), { id: spec.id, terminal: 'fake-term', agent: spec.agent, path: spec.cwd, createdAt: Date.now() }),
    alive: async () => new Set(),
    close: async () => undefined,
  };
  const service = new Service({ home, scriptsDir: home, platform: 'linux', env: { PATH: '' }, callIde: async () => ({}), drivers: [driver], newId: () => 'tab-resumed' });
  saveRecord(home, record({ folder: root, host: 'fake-term', product: 'Fake Terminal', endedAt: new Date(Date.now() - MINUTE).toISOString() }));
  const r = new Resumes({ home, settings: () => service.settings(), openTab: (i) => service.openTab(i), liveHost: (h, p) => service.liveHost(h, p), live: async () => [] });
  const [clientSide, serverSide] = InMemoryTransport.createLinkedPair();
  await createServer(service, undefined, undefined, undefined, r).connect(serverSide);
  const client = new Client({ name: 'claude-code', version: '1.0.0' });
  await client.connect(clientSide);
  const names = (await client.listTools()).tools.map((t) => t.name);
  assert.ok(names.includes('closed_sessions') && names.includes('resume_tab'));
  const call = async (name: string, args: Record<string, unknown> = {}) => {
    const result = (await client.callTool({ name, arguments: args })) as { isError?: boolean; content: { text: string }[] };
    return JSON.parse(result.content[0]!.text);
  };
  assert.match((await call('closed_sessions')).listing, /parser-fix/);
  const resumed = await call('resume_tab', { id: CLAUDE_ID });
  assert.equal(resumed.resumed, true);
  assert.equal(resumed.ide, 'fake-term');
  assert.deepEqual(opened, [['claude', '--model', 'claude-opus-5-5', '--resume', CLAUDE_ID]]);
});

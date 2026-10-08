import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import http from 'node:http';
import type { AddressInfo } from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { APIError } from '@typesafe-ai/sdk';
import { z } from 'zod';
import { toJevError } from '../src/jev/client.js';
import { decodeBlob, lookUpKey, pickWindowsCredential, type CommandRunner } from '../src/jev/key.js';
import { costUsd, ledgerPath, summarizeLedger } from '../src/jev/ledger.js';
import { Jev } from '../src/jev/service.js';
import { parseJevSettings } from '../src/jev/settings.js';
import { JEV_TOOLS } from '../src/jev/tools.js';

export const JEV_FIXTURES_FILE = fileURLToPath(new URL('../tests/fixtures/jev.json', import.meta.url));

const KEY = 'tsk-fixture-key-0123456789';
const HOME_MARK = '<home>';

const SETTINGS_INPUTS: unknown[] = [
  null,
  { enabled: true },
  { enabled: false, sure: 1, pricePerMillionInput: 0 },
  { enabled: true, sure: 0.5, tiers: { claude: 'Hard work', 'codex:gpt-5': 'Quick fixes', 'a.b_c-d': 'x' }, pricePerMillionInput: 1.25 },
  'on',
  [],
  { enabled: 'yes' },
  { sure: 0 },
  { sure: 1.5 },
  { sure: '0.9' },
  { pricePerMillionInput: -1 },
  { pricePerMillionInput: '1' },
  { tiers: [] },
  { tiers: 'x' },
  { tiers: { 'bad name': 'x' } },
  { tiers: { '-lead': 'x' } },
  { tiers: { [`a${'b'.repeat(64)}`]: 'x' } },
  { tiers: { [`a${'b'.repeat(63)}`]: 'x' } },
  { tiers: { 'codex:': 'x' } },
  { tiers: { 'codex:a:b': 'x' } },
  { tiers: { [`codex:${'m'.repeat(128)}`]: 'x' } },
  { tiers: { [`codex:${'m'.repeat(129)}`]: 'x' } },
  { tiers: { 'codex:a\u00a0b': 'x' } },
  { tiers: { codex: '' } },
  { tiers: { codex: ' \u3000\ufeff ' } },
  { tiers: { codex: 5 } },
  { tiers: null, enabled: true },
];

const COSTS: [number, number][] = [
  [0, 0.042],
  [1, 0.042],
  [1234, 0.042],
  [5, 0.1],
  [125, 0.001],
  [999_999, 0.042],
  [1e9, 1.25],
  [3, 0.0000005],
  [7, 0.000000125],
  [1, 0.000000005],
];

const BLOBS: string[] = [
  Buffer.from('tsk-abc', 'utf16le').toString('base64'),
  Buffer.from('tsk-abc', 'utf8').toString('base64'),
  Buffer.from('  tsk-abc\r\n', 'utf8').toString('base64'),
  Buffer.from('tsk-abcd', 'utf8').toString('base64'),
  Buffer.from('\ufefftsk-abc', 'utf16le').toString('base64'),
  Buffer.from('\ufefftsk-abc', 'utf8').toString('base64'),
  Buffer.from('café', 'utf16le').toString('base64'),
  Buffer.from([0xff, 0xfe, 0xfd]).toString('base64'),
  Buffer.from('   ', 'utf8').toString('base64'),
  '',
];

const CREDENTIAL_SETS = [
  [{ target: 'typesafe', user: 'api_key', blob: Buffer.from('k1', 'utf16le').toString('base64') }],
  [{ target: 'typesafe', user: 'someone', blob: Buffer.from('k1', 'utf16le').toString('base64') }],
  [
    { target: 'typesafe', user: 'someone', blob: Buffer.from('k1', 'utf16le').toString('base64') },
    { target: 'api_key@typesafe', user: null, blob: Buffer.from('k2', 'utf8').toString('base64') },
  ],
  [
    { target: 'typesafe', user: 'api_key', blob: Buffer.from('k1', 'utf8').toString('base64') },
    { target: 'api_key@typesafe', user: 'x', blob: Buffer.from('k2', 'utf8').toString('base64') },
  ],
  [],
];

const VALIDATION: [string, unknown][] = [
  ['jev_choose', {}],
  ['jev_choose', { instruction: '', options: [] }],
  ['jev_choose', { instruction: 5, options: [{ id: 'a' }], no_match: 'x' }],
  ['jev_choose', [1]],
  ['jev_choose', null],
  ['jev_choose', { instruction: 'i', options: [{ id: 'a', description: 'd', extra: 1 }], state: { b: 1, a: [2] }, extra: true }],
  ['jev_choose', { instruction: 'i', options: [{ id: 'a', description: 'd' }], state: null }],
  ['jev_choose', { instruction: 'i', options: [{ id: 'x'.repeat(129), description: 'd' }] }],
  ['jev_choose', { instruction: 'i', options: [{ id: '😀'.repeat(64), description: 'd' }] }],
  ['jev_choose', { instruction: 'i', options: [{ id: '😀'.repeat(65), description: 'd' }] }],
  ['jev_choose', { instruction: 'i', options: Array.from({ length: 255 }, (_, i) => ({ id: `o${i}`, description: 'd' })) }],
  ['jev_choose', { instruction: 'i', options: Array.from({ length: 3 }, () => ({ id: '' })) }],
  ['jev_check', { state: 5, conditions: [{ id: '', question: 'q' }] }],
  ['jev_check', { state: true, conditions: [] }],
  ['jev_check', { state: 'x', conditions: [{ id: 'a', question: '' }, { id: 'b' }] }],
  [
    'jev_ask',
    {
      state: null,
      questions: { 'a b': { instructions: 'x' }, '1': { type: 'choice' }, z: 5, s: { type: 'score', criteria: [5, null] }, n: { type: 'noul', criteria: { true: 3 } } },
    },
  ],
  ['jev_ask', { state: 's', questions: { q: { type: 'noul', criteria: 5 }, r: { type: 'score', criteria: {} }, t: { type: 'choice', criteria: [] } } }],
  ['jev_ask', { state: 's', questions: [] }],
  ['jev_ask', { state: 's', questions: { q: null, '': { type: 'bogus' }, $x: { type: 7 } } }],
  [
    'jev_ask',
    {
      questions: { q: { criteria: { false: null, true: 'yes', other: 1 }, extra: 2, type: 'noul', instructions: null } },
      state: [1, { b: 2 }],
    },
  ],
  ['jev_ask', { state: 's', questions: { c: { type: 'choice', criteria: { b: 'B', a: null, '2': ['x'] }, instructions: { k: 1 } } } }],
  ['jev_rank', { query: 'q', items: [{ id: 'a', text: 't' }], top: 0.5 }],
  ['jev_rank', { query: 'q', items: [{ id: 'a', text: 't' }], top: -2 }],
  ['jev_rank', { query: 'q', items: [{ id: 'a', text: 't' }], top: 0 }],
  ['jev_rank', { query: 'q', items: [{ id: 'a', text: 't' }], top: '2' }],
  ['jev_rank', { query: 'q', items: [{ id: 'a', text: 't' }], top: 1.0 }],
  ['jev_rank', { query: 'q', items: [{ id: 'a', text: 1 }], top: 3 }],
  ['jev_rank', { query: 'q', items: [] }],
  ['jev_route', {}],
  ['jev_route', { task: 'x', extra: 1 }],
  ['jev_status', { anything: 1 }],
];

const DESCRIBE: [number, unknown][] = [
  [401, { error: 'bad key' }],
  [403, undefined],
  [400, { detail: [{ loc: ['body', 'questions', 'q', 0], msg: 'field required' }, { msg: 'second' }, { loc: 'x', msg: 'third' }, 5] }],
  [400, { detail: [{ nope: 1 }] }],
  [422, { detail: 'state: Field required' }],
  [422, { detail: { message: 'nested detail' } }],
  [422, { error: { message: 'nested error' } }],
  [422, { message: 'plain message' }],
  [422, { error: '' }],
  [422, 'text body'],
  [422, ''],
  [422, undefined],
  [422, { other: 'x'.repeat(250) }],
  [422, 'y'.repeat(201)],
  [422, [1, 2]],
  [429, { error: 'slow down' }],
  [500, undefined],
  [503, { error: `upstream has ${KEY}` }],
  [529, { error: 'busy' }],
  [418, 'teapot'],
  [418, null],
  [404, { error: '404 not found' }],
];

interface Canned {
  status: number;
  body: string;
  headers?: Record<string, string>;
}

interface ToolCase {
  name: string;
  tool: string;
  input: unknown;
  settings?: Record<string, unknown>;
  profiles?: { name: string; installed: boolean }[];
  response?: Canned;
  noKey?: boolean;
}

const ok = (answers: unknown, extra: Record<string, unknown> = {}): Canned => ({
  status: 200,
  body: JSON.stringify({ model: 'jev-1.2', answers, usage: { input_tokens: 321 }, ...extra }),
  headers: { 'content-type': 'application/json' },
});

const pick = (choice: string, probabilities: Record<string, number>, confidence: number | undefined = 0.7) =>
  ok({ pick: { type: 'choice', choice, ...(confidence === undefined ? {} : { confidence }), probabilities } });

const noul = (values: Record<string, number>) => ok(Object.fromEntries(Object.entries(values).map(([k, v]) => [k, { type: 'noul', noul: v }])));

const options = [
  { id: 'src/login.ts', description: 'The login handler' },
  { id: 'src/db.ts', description: 'Database access' },
];

const TOOL_CASES: ToolCase[] = [
  { name: 'status fresh', tool: 'jev_status', input: {}, settings: { tiers: { 'codex:b': 'B', claude: 'A', 'Zed': 'Z' }, sure: 0.9 } },
  { name: 'status no key', tool: 'jev_status', input: {}, noKey: true },
  {
    name: 'ask mixed',
    tool: 'jev_ask',
    input: {
      state: { diff: '+ a\n- b', '2': 'two', '1': 'one' },
      questions: {
        safe: { type: 'noul', instructions: '  Is it safe? ', criteria: { true: 'Safe', false: null } },
        '10': { type: 'score', instructions: null, criteria: ['bad', 'ok', { level: 'good' }] },
        '2': { type: 'choice', criteria: { b: 'B', a: 'A', '1': null }, extra: 'dropped' },
      },
    },
    response: ok({ safe: { type: 'noul', noul: 0.25 }, '2': { type: 'choice', choice: 'a', confidence: 0.6, probabilities: { a: 0.6, b: 0.3, '1': 0.1 } } }),
  },
  { name: 'ask null state', tool: 'jev_ask', input: { state: '', questions: { q: { type: 'noul' } } }, response: ok({ q: { type: 'noul', noul: 1 } }, { usage: { input_tokens: 0 } }) },
  { name: 'ask one choice option', tool: 'jev_ask', input: { state: 's', questions: { q: { type: 'choice', criteria: { a: 'A' } } } } },
  { name: 'ask eleven levels', tool: 'jev_ask', input: { state: 's', questions: { q: { type: 'score', criteria: Array.from({ length: 11 }, (_, i) => `l${i}`) } } } },
  { name: 'ask one level', tool: 'jev_ask', input: { state: 's', questions: { b: { type: 'noul' }, a: { type: 'score', criteria: ['x'] } } } },
  { name: 'ask no questions', tool: 'jev_ask', input: { state: 's', questions: {} } },
  { name: 'ask too large', tool: 'jev_ask', input: { state: { $repeat: ['😀', 100_000] }, questions: { q: { type: 'noul' } } } },
  { name: 'ask no usage', tool: 'jev_ask', input: { state: 's', questions: { q: { type: 'noul' } } }, response: { status: 200, body: '{"model":"jev-x","answers":{"q":{"type":"noul","noul":0.5}}}' } },
  {
    name: 'choose basic',
    tool: 'jev_choose',
    input: { instruction: ' Which file handles login? ', options, state: { error: 'login fails' } },
    response: pick('src/login.ts', { 'src/login.ts': 0.9, 'src/db.ts': 0.07, none: 0.03 }, 0.9),
  },
  {
    name: 'choose at threshold',
    tool: 'jev_choose',
    input: { instruction: 'Pick', options, no_match: false },
    settings: { sure: 0.6 },
    response: pick('src/db.ts', { 'src/login.ts': 0.4, 'src/db.ts': 0.6 }),
  },
  { name: 'choose none', tool: 'jev_choose', input: { instruction: 'Pick', options }, response: pick('none', { 'src/login.ts': 0.1, 'src/db.ts': 0.1, none: 0.8 }) },
  {
    name: 'choose numeric ids and missing probabilities',
    tool: 'jev_choose',
    input: { instruction: 'Pick', options: [{ id: 'b', description: 'B' }, { id: '2', description: 'Two' }, { id: '1', description: 'One' }], no_match: false },
    response: pick('2', { '2': 0.5, b: 0.5 }, undefined),
  },
  {
    name: 'choose tie keeps order',
    tool: 'jev_choose',
    input: { instruction: 'Pick', options: [{ id: 'x', description: 'X' }, { id: 'y', description: 'Y' }, { id: 'z', description: 'Z' }], no_match: false },
    response: pick('y', { x: 0.3, y: 0.4, z: 0.3 }),
  },
  { name: 'choose reserved none', tool: 'jev_choose', input: { instruction: 'Pick', options: [{ id: 'none', description: 'N' }] } },
  { name: 'choose reserved none allowed', tool: 'jev_choose', input: { instruction: 'Pick', options: [{ id: 'none', description: 'N' }, { id: 'a', description: 'A' }], no_match: false }, response: pick('none', { none: 0.9, a: 0.1 }) },
  { name: 'choose duplicate ids', tool: 'jev_choose', input: { instruction: 'Pick', options: [{ id: 'a', description: 'A' }, { id: 'a', description: 'B' }] } },
  { name: 'choose one option no match', tool: 'jev_choose', input: { instruction: 'Pick', options: [{ id: 'a', description: 'A' }], no_match: false } },
  { name: 'choose wrong answer type', tool: 'jev_choose', input: { instruction: 'Pick', options }, response: ok({ pick: { type: 'noul', noul: 0.5 } }) },
  { name: 'choose answer missing', tool: 'jev_choose', input: { instruction: 'Pick', options }, response: ok({}) },
  {
    name: 'check basic',
    tool: 'jev_check',
    input: { state: 'diff text', conditions: [{ id: 'secrets', question: 'Does it add a secret?' }, { id: '3', question: ' Does it delete tests? ' }] },
    response: noul({ secrets: 0.02, '3': 0.75 }),
  },
  { name: 'check duplicate', tool: 'jev_check', input: { state: 'x', conditions: [{ id: 'a', question: 'q' }, { id: 'a', question: 'r' }] } },
  { name: 'check missing answer', tool: 'jev_check', input: { state: 'x', conditions: [{ id: 'a', question: 'q' }, { id: 'b', question: 'r' }] }, response: noul({ a: 0.5 }) },
  {
    name: 'rank basic',
    tool: 'jev_rank',
    input: {
      query: 'login bugs',
      items: [
        { id: 'z', text: 'unrelated' },
        { id: '😀', text: 'emoji' },
        { id: '\uffff', text: 'high' },
        { id: '10', text: 'ten' },
        { id: '9', text: 'nine' },
        { id: 'a"b', text: 'quote' },
      ],
      top: 4,
    },
    response: noul({ z: 0.1, '😀': 0.5, '\uffff': 0.5, '10': 0.9, '9': 0.2, 'a"b': 0.95 }),
  },
  { name: 'rank no top', tool: 'jev_rank', input: { query: 'q', items: [{ id: 'b', text: 'B' }, { id: 'a', text: 'A' }] }, response: noul({ a: 0.4, b: 0.4 }) },
  { name: 'rank too many', tool: 'jev_rank', input: { query: 'q', items: Array.from({ length: 256 }, (_, i) => ({ id: `i${i}`, text: 't' })) } },
  { name: 'rank duplicate', tool: 'jev_rank', input: { query: 'q', items: [{ id: 'a', text: 'A' }, { id: 'a', text: 'B' }] } },
  { name: 'route no tiers', tool: 'jev_route', input: { task: 'Fix a typo' } },
  {
    name: 'route none installed',
    tool: 'jev_route',
    input: { task: 'Fix a typo' },
    settings: { tiers: { gemini: 'G', 'qwen:max': 'Q' } },
    profiles: [{ name: 'gemini', installed: false }, { name: 'claude', installed: true }],
  },
  {
    name: 'route one usable',
    tool: 'jev_route',
    input: { task: 'Fix a typo' },
    settings: { tiers: { gemini: 'G', 'claude:haiku': 'Small edits' } },
    profiles: [{ name: 'gemini', installed: false }, { name: 'claude', installed: true }],
  },
  {
    name: 'route pick',
    tool: 'jev_route',
    input: { task: 'Refactor the auth module across 30 files.' },
    settings: { tiers: { 'codex:gpt-5': 'Quick fixes', 'claude:opus': 'Large refactors', gemini: 'G' } },
    profiles: [{ name: 'gemini', installed: false }, { name: 'claude', installed: true }, { name: 'codex', installed: true }],
    response: pick('claude:opus', { 'claude:opus': 0.8, 'codex:gpt-5': 0.2 }, 0.8),
  },
  { name: 'http 401', tool: 'jev_check', input: { state: 'x', conditions: [{ id: 'a', question: 'q' }] }, response: { status: 401, body: '{"error":"invalid key"}', headers: { 'content-type': 'application/json' } } },
  { name: 'http 403 empty', tool: 'jev_check', input: { state: 'x', conditions: [{ id: 'a', question: 'q' }] }, response: { status: 403, body: '' } },
  {
    name: 'http 422 detail',
    tool: 'jev_check',
    input: { state: 'x', conditions: [{ id: 'a', question: 'q' }] },
    response: { status: 422, body: JSON.stringify({ detail: [{ loc: ['body', 'state'], msg: 'Field required' }] }), headers: { 'content-type': 'application/json' } },
  },
  { name: 'http 400 text', tool: 'jev_check', input: { state: 'x', conditions: [{ id: 'a', question: 'q' }] }, response: { status: 400, body: `bad request for ${KEY}` } },
  { name: 'http 429 retried', tool: 'jev_check', input: { state: 'x', conditions: [{ id: 'a', question: 'q' }] }, response: { status: 429, body: '{"error":"slow"}', headers: { 'retry-after-ms': '0' } } },
  { name: 'http 503 retried', tool: 'jev_check', input: { state: 'x', conditions: [{ id: 'a', question: 'q' }] }, response: { status: 503, body: '', headers: { 'retry-after': '0' } } },
  { name: 'http 529', tool: 'jev_check', input: { state: 'x', conditions: [{ id: 'a', question: 'q' }] }, response: { status: 529, body: '{"error":"busy"}', headers: { 'retry-after-ms': '0' } } },
  { name: 'http 404 not retried', tool: 'jev_check', input: { state: 'x', conditions: [{ id: 'a', question: 'q' }] }, response: { status: 404, body: '{"message":"no route"}' } },
];

function expand(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(expand);
  if (typeof value !== 'object' || value === null) return value;
  const repeat = (value as { $repeat?: [string, number] }).$repeat;
  if (repeat) return repeat[0].repeat(repeat[1]);
  return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, expand(v)]));
}

function credentialRunner(stdout: string): CommandRunner {
  return async () => ({ code: 0, stdout, stderr: '' });
}

interface Seen {
  method: string;
  path: string;
  headers: Record<string, string | undefined>;
  body: string;
}

const SEEN_HEADERS = ['authorization', 'accept', 'content-type', 'user-agent', 'x-typesafe-sdk', 'x-typesafe-retry-count'];

async function stub(): Promise<{ url: string; set: (c?: Canned) => void; seen: Seen[]; close: () => Promise<void> }> {
  let canned: Canned | undefined;
  const seen: Seen[] = [];
  const server = http.createServer((req, res) => {
    let body = '';
    req.setEncoding('utf8');
    req.on('data', (c: string) => (body += c));
    req.on('end', () => {
      seen.push({ method: req.method ?? '', path: req.url ?? '', headers: Object.fromEntries(SEEN_HEADERS.map((h) => [h, req.headers[h] as string | undefined])), body });
      const reply = canned ?? { status: 500, body: 'no canned response' };
      res.writeHead(reply.status, reply.headers ?? {});
      res.end(reply.body);
    });
  });
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  const { port } = server.address() as AddressInfo;
  return {
    url: `http://127.0.0.1:${port}`,
    set: (c) => {
      canned = c;
      seen.length = 0;
    },
    seen,
    close: () => new Promise<void>((resolve) => server.close(() => resolve())),
  };
}

function masked(text: string, home: string): string {
  const escaped = (s: string) => JSON.stringify(s).slice(1, -1);
  return [home + path.sep, escaped(home + path.sep)]
    .reduce((t, prefix) => t.split(prefix).join(`${HOME_MARK}/`), text)
    .split(escaped(home))
    .join(HOME_MARK)
    .split(home)
    .join(HOME_MARK);
}

async function toolCases(): Promise<unknown[]> {
  const server = await stub();
  const out: unknown[] = [];
  try {
    for (const c of TOOL_CASES) {
      const home = mkdtempSync(path.join(os.tmpdir(), 'iat-jevfx-'));
      try {
        server.set(c.response);
        const settings = parseJevSettings({ enabled: true, ...c.settings });
        const env: NodeJS.ProcessEnv = { TYPESAFE_BASE_URL: server.url, IDE_AGENT_TABS_AGENT: 'codex', IDE_AGENT_TABS_ID: 'tab-1', ...(c.noKey ? {} : { TYPESAFE_API_KEY: KEY }) };
        const jev = new Jev({
          settings,
          home,
          env,
          platform: 'linux',
          profiles: async () => c.profiles ?? [],
          runCommand: async () => ({ code: 1, stdout: '', stderr: '' }),
        });
        const tool = JEV_TOOLS.find((t) => t.name === c.tool)!;
        const parsed = z.object(tool.inputSchema).parse(expand(c.input));
        let result: Record<string, unknown>;
        try {
          result = { output: masked(JSON.stringify(await tool.run(jev, parsed), null, 2), home) };
        } catch (e) {
          const err = e as Error & { status?: unknown };
          result = { error: masked(err.message, home), ...(err.status === undefined ? {} : { status: err.status }) };
        }
        let ledger: unknown[] = [];
        try {
          ledger = readFileSync(ledgerPath(home), 'utf8')
            .trim()
            .split('\n')
            .map((l) => {
              const { at: _at, ...rest } = JSON.parse(l) as Record<string, unknown>;
              return rest;
            });
        } catch {}
        out.push({
          name: c.name,
          tool: c.tool,
          input: c.input,
          settings: c.settings ?? {},
          profiles: c.profiles ?? [],
          noKey: c.noKey === true,
          response: c.response ?? null,
          requests: server.seen.map((s) => ({ ...s })),
          ...result,
          ledger,
        });
      } finally {
        rmSync(home, { recursive: true, force: true });
      }
    }
  } finally {
    await server.close();
  }
  return out;
}

async function keyCases(): Promise<unknown[]> {
  const out: unknown[] = [];
  for (const entries of CREDENTIAL_SETS) {
    const r = await lookUpKey({ env: {}, platform: 'win32', runCommand: credentialRunner(JSON.stringify(entries)) });
    out.push({ platform: 'win32', entries, found: r.found ?? null, missing: r.missing ?? null });
  }
  for (const [platform, stdout, code] of [
    ['darwin', 'tsk-mac\n', 0],
    ['darwin', '', 0],
    ['linux', '  tsk-linux  ', 0],
    ['linux', 'tsk-linux', 1],
  ] as const) {
    const r = await lookUpKey({ env: {}, platform, runCommand: async () => ({ code, stdout, stderr: '' }) });
    out.push({ platform, stdout, code, found: r.found ?? null, missing: r.missing ?? null });
  }
  const enoent = Object.assign(new Error('spawn secret-tool ENOENT'), { code: 'ENOENT' });
  const r = await lookUpKey({ env: {}, platform: 'linux', runCommand: async () => Promise.reject(enoent) });
  out.push({ platform: 'linux', enoent: true, found: r.found ?? null, missing: r.missing ?? null });
  const fromEnv = await lookUpKey({ env: { TYPESAFE_API_KEY: '  tsk-env \n' }, platform: 'linux' });
  out.push({ platform: 'linux', env: '  tsk-env \n', found: fromEnv.found ?? null, missing: fromEnv.missing ?? null });
  return out;
}

async function ledgerCases(): Promise<unknown> {
  const home = mkdtempSync(path.join(os.tmpdir(), 'iat-jevfx-'));
  try {
    const lines = [
      JSON.stringify({ at: '2026-10-08T11:00:00.000Z', tool: 'jev_check', model: 'jev-1', input_tokens: 100, ok: true }),
      JSON.stringify({ at: '2026-10-08T12:30:00.000Z', tool: 'jev_check', model: 'jev-2', input_tokens: 50.5, ok: true }),
      JSON.stringify({ at: '2026-10-08T12:40:00.000Z', tool: 'jev_ask', model: 'jev-latest', input_tokens: 0, ok: false, status: 429 }),
      JSON.stringify({ at: '2026-10-08T12:45:00.000Z', tool: 'jev_ask', input_tokens: '7', ok: true }),
      JSON.stringify({ at: '2026-10-07T12:00:00.000Z', tool: 'jev_ask', model: 'old', input_tokens: 1000, ok: true }),
      JSON.stringify({ at: 'not a date', ok: true, model: 'undated', input_tokens: 5 }),
      JSON.stringify({ at: 5, ok: true }),
      JSON.stringify({ at: '2026-10-08T11:30:00.000Z', ok: 'yes' }),
      '[1,2]',
      'null',
      'not json',
      '   ',
      '',
    ];
    mkdirSync(path.dirname(ledgerPath(home)), { recursive: true });
    writeFileSync(ledgerPath(home), `${lines.join('\r\n')}\n`);
    const now = new Date('2026-10-08T12:00:00.000Z');
    return { text: `${lines.join('\r\n')}\n`, now: now.getTime(), price: 0.042, summary: await summarizeLedger(home, 0.042, now) };
  } finally {
    rmSync(home, { recursive: true, force: true });
  }
}

export async function jevFixturesText(): Promise<string> {
  const data = {
    key: KEY,
    settings: SETTINGS_INPUTS.map((input) => {
      try {
        return { input, settings: parseJevSettings(input) };
      } catch (e) {
        return { input, error: (e as Error).message };
      }
    }),
    costs: COSTS.map(([tokens, price]) => ({ tokens, price, cost: costUsd(tokens, price) })),
    blobs: BLOBS.map((blob) => ({ blob, key: decodeBlob(Buffer.from(blob, 'base64')) ?? null })),
    credentials: CREDENTIAL_SETS.map((entries) => ({ entries, key: pickWindowsCredential(entries) ?? null })),
    keys: await keyCases(),
    validation: VALIDATION.map(([name, input]) => {
      const tool = JEV_TOOLS.find((t) => t.name === name)!;
      const parsed = z.object(tool.inputSchema).safeParse(input);
      return parsed.success ? { tool: name, input, parsed: parsed.data } : { tool: name, input, error: z.prettifyError(parsed.error) };
    }),
    describe: DESCRIBE.map(([status, body]) => ({
      status,
      body: body === undefined ? { undefined: true } : { value: body },
      message: toJevError(APIError.fromResponse(status, body, new Headers()), KEY).message,
    })),
    tools: await toolCases(),
    ledger: await ledgerCases(),
  };
  return `${JSON.stringify(data, null, 2)}\n`;
}

if (process.argv[1] !== undefined && fileURLToPath(import.meta.url) === process.argv[1]) {
  mkdirSync(path.dirname(JEV_FIXTURES_FILE), { recursive: true });
  writeFileSync(JEV_FIXTURES_FILE, await jevFixturesText());
  console.log(`Wrote ${JEV_FIXTURES_FILE}`);
}

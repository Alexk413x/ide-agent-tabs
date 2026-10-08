import { mkdirSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { effectiveState, parsePresence } from '../src/messaging/sessions.js';
import { sendDigest } from '../src/messaging/store.js';

export const FIXTURES_FILE = fileURLToPath(new URL('../tests/fixtures/js.json', import.meta.url));

const JSON_INPUTS = [
  'null',
  'true',
  'false',
  '0',
  '-0',
  '1',
  '-1',
  '1.0',
  '1.5',
  '0.1',
  '100',
  '1e20',
  '1e21',
  '1.5e21',
  '0.000001',
  '1e-7',
  '-1.25e-10',
  '5e-324',
  '1.7976931348623157e308',
  '9007199254740991',
  '9007199254740993',
  '12345678901234567890',
  '123456789.125',
  '""',
  '"plain"',
  '"quote \\" backslash \\\\ slash /"',
  '"\\b\\f\\n\\r\\t"',
  '"\\u0000\\u0001\\u001f\\u007f\\u0080"',
  '"\\u2028\\u2029"',
  '"caf\\u00e9 \\u4e2d\\u6587"',
  '"\\ud83d\\ude00 emoji"',
  '"\\ud800"',
  '"\\udc00x"',
  '"a\\ud83d"',
  '"\\ude00\\ud83d"',
  '[]',
  '{}',
  '[[]]',
  '[{}]',
  '[1, "two", null, true, [3, {"four": 4}]]',
  '{"b": 1, "a": 2, "10": 3, "2": 4, "-1": 5, "01": 6, "4294967295": 7, "4294967294": 8, "1.5": 9, "0": 10}',
  '{"nested": {"empty": {}, "list": [], "deep": [[1, [2]], {"x": null}]}}',
  '{"id": "tab-1", "pid": 42, "startedAt": "2026-10-08T12:00:00.000Z", "reminded": ["m-0123456789abcdef"]}',
];

const DIGESTS: [string, string, string | undefined][] = [
  ['tab-b', 'hello', undefined],
  ['tab-b', 'hello', 'm-0123456789abcdef'],
  ['tab-b', '', ''],
  ['tab-c', 'line\nbreak "quoted" café 😀', undefined],
  ['tab-c', 'lone \ud800 surrogate', undefined],
  ['tab-c', '\u0000\u001f ', undefined],
];

const TIMES = [0, 1, 999, 1_000, 1_759_900_000_123, 1_791_460_800_000, -1, 253_402_300_799_999];

const DATE_STRINGS = [
  '2026-10-08T12:34:56.789Z',
  '2026-10-08T12:34:56Z',
  '2026-10-08T12:34Z',
  '2026-10-08T12:34:56.7Z',
  '2026-10-08T12:34:56.78912Z',
  '2026-10-08',
  '2026-10',
  '2026',
  '2026-10-08T12:34:56.789+02:00',
  '2026-10-08T12:34:56.789-05:30',
  '1970-01-01T00:00:00.000Z',
  '2026-02-30T00:00:00Z',
  '2026-10-08T24:00:00Z',
  '2026-10-08T25:00:00Z',
  'garbage',
  '',
];

const UTF16_STRINGS = ['', 'abc', 'café', '😀', 'a😀b', '\ud800', '中文😀😀'];
const SLICES: [string, number, number][] = [
  ['a😀b', 0, 2],
  ['a😀b', 2, 4],
  ['a😀b', 1, 3],
  ['😀😀', 1, 3],
  ['abc', 0, 200],
  ['abc', 5, 9],
];

const PRESENCE_TEXTS = [
  '{"id": "tab-1", "agent": "codex", "path": "/w", "pid": 42, "startedAt": "2026-10-08T12:00:00.000Z"}',
  '{"id": "tab-1", "pid": 42.0, "nudges": 1.5, "beatMs": 0, "modBeat": 1759900000123, "state": "busy", "driver": "mod", "via": "ori"}',
  '{"id": "tab-1", "pid": -3, "state": "asleep", "driver": "hooks", "inputIdle": true, "reminded": ["a", 1]}',
  '{"id": "tab-1", "pid": true, "pidStart": 0, "agentType": "bad type", "agentColor": "teal", "mail": 2}',
  '{"id": "tab-1", "agentType": "general-purpose", "agentColor": "cyan", "reminded": [], "unknown": 1}',
  '{"id": "-bad"}',
  '{"agent": "codex"}',
  '[1]',
  'null',
  'not json',
  '{"id": "tab-1", "pid": 9007199254740992}',
];

const STATES: [{ state?: string; stateAt?: string }, number][] = [
  [{}, 0],
  [{ state: 'idle' }, 0],
  [{ state: 'busy' }, 1_000],
  [{ state: 'busy', stateAt: 'never' }, 1_000],
  [{ state: 'busy', stateAt: '1970-01-01T00:00:00.000Z' }, 15 * 60_000 - 1],
  [{ state: 'busy', stateAt: '1970-01-01T00:00:00.000Z' }, 15 * 60_000],
  [{ state: 'waking', stateAt: '1970-01-01T00:00:00.000Z' }, 19_999],
  [{ state: 'waking', stateAt: '1970-01-01T00:00:00.000Z' }, 20_000],
  [{ state: 'waking', stateAt: '1970-01-01T00:00:01.000Z' }, 0],
  [{ state: 'waking' }, 0],
];

export function fixturesText(): string {
  const data = {
    stringify: JSON_INPUTS.map((input) => {
      const value: unknown = JSON.parse(input);
      return { input, compact: JSON.stringify(value), pretty: JSON.stringify(value, null, 2) };
    }),
    digests: DIGESTS.map(([to, text, replyTo]) => ({ to, text, ...(replyTo !== undefined ? { replyTo } : {}), digest: sendDigest(to, text, replyTo) })),
    isoTimes: TIMES.map((ms) => ({ ms, iso: new Date(ms).toISOString() })),
    dateParse: DATE_STRINGS.map((text) => {
      const ms = Date.parse(text);
      return { text, ms: Number.isNaN(ms) ? null : ms };
    }),
    utf16Length: UTF16_STRINGS.map((text) => ({ text, length: text.length })),
    utf16Slice: SLICES.map(([text, start, end]) => ({ text, start, end, slice: text.slice(start, end) })),
    presence: PRESENCE_TEXTS.map((text) => ({ text, parsed: parsePresence(text) ?? null })),
    effectiveState: STATES.map(([presence, now]) => ({ presence, now, state: effectiveState(presence as Parameters<typeof effectiveState>[0], now) })),
  };
  return `${JSON.stringify(data, null, 2)}\n`;
}

if (process.argv[1] !== undefined && fileURLToPath(import.meta.url) === process.argv[1]) {
  mkdirSync(path.dirname(FIXTURES_FILE), { recursive: true });
  writeFileSync(FIXTURES_FILE, fixturesText());
  console.log(`Wrote ${FIXTURES_FILE}`);
}

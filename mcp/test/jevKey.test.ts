import assert from 'node:assert/strict';
import { test } from 'node:test';
import { decodeBlob, KeyStore, lookUpKey, type CommandRunner } from '../src/jev/key.js';

const KEY = 'ts-live-looking-key-9876';

interface Call {
  command: string;
  args: string[];
}

function runner(answers: Record<string, { code?: number; stdout?: string } | 'missing'>, calls: Call[] = []): CommandRunner {
  return async (command, args) => {
    calls.push({ command, args });
    const a = answers[command];
    if (a === undefined || a === 'missing') throw Object.assign(new Error(`spawn ${command} ENOENT`), { code: 'ENOENT' });
    return { code: a.code ?? 0, stdout: a.stdout ?? '', stderr: '' };
  };
}

const utf16 = (s: string) => Buffer.from(s, 'utf16le').toString('base64');
const utf8 = (s: string) => Buffer.from(s, 'utf8').toString('base64');
const stored = (entries: object[]) => ({ stdout: JSON.stringify(entries) });

test('TYPESAFE_API_KEY wins, and the credential store is not asked', async () => {
  const calls: Call[] = [];
  const r = await lookUpKey({ env: { TYPESAFE_API_KEY: ` ${KEY} ` }, platform: 'win32', runCommand: runner({}, calls) });
  assert.deepEqual(r.found, { key: KEY, source: 'env' });
  assert.equal(calls.length, 0);
});

test('Windows reads the typesafe credential with a fixed encoded script and decodes a UTF-16LE blob', async () => {
  const calls: Call[] = [];
  const r = await lookUpKey({
    env: {},
    platform: 'win32',
    runCommand: runner({ pwsh: stored([{ target: 'typesafe', user: 'api_key', blob: utf16(KEY) }]) }, calls),
  });
  assert.deepEqual(r.found, { key: KEY, source: 'credential-store' });
  assert.equal(calls.length, 1);
  const [, , flag, encoded] = calls[0]!.args;
  assert.deepEqual(calls[0]!.args.slice(0, 3), ['-NoProfile', '-NonInteractive', '-EncodedCommand']);
  assert.equal(flag, '-EncodedCommand');
  const script = Buffer.from(encoded!, 'base64').toString('utf16le');
  assert.match(script, /CredReadW/);
  assert.match(script, /'typesafe', 'api_key@typesafe'/);
});

test('Windows skips a typesafe credential with another user name and falls back to api_key@typesafe', async () => {
  const other = { target: 'typesafe', user: 'someone', blob: utf16('wrong-key-000') };
  const compound = { target: 'api_key@typesafe', user: 'api_key', blob: utf16(KEY) };
  const r = await lookUpKey({ env: {}, platform: 'win32', runCommand: runner({ pwsh: stored([other, compound]) }) });
  assert.deepEqual(r.found, { key: KEY, source: 'credential-store' });

  const none = await lookUpKey({ env: {}, platform: 'win32', runCommand: runner({ pwsh: stored([other]) }) });
  assert.equal(none.found, undefined);
  assert.ok(!none.missing!.includes('wrong-key-000'));
});

test('Windows uses Windows PowerShell when pwsh is missing, and reads a UTF-8 blob', async () => {
  const calls: Call[] = [];
  const r = await lookUpKey({
    env: {},
    platform: 'win32',
    runCommand: runner({ pwsh: 'missing', 'powershell.exe': stored([{ target: 'typesafe', user: 'api_key', blob: utf8(KEY) }]) }, calls),
  });
  assert.deepEqual(r.found, { key: KEY, source: 'credential-store' });
  assert.deepEqual(calls.map((c) => c.command), ['pwsh', 'powershell.exe']);
});

test('decodeBlob prefers UTF-16LE and falls back to UTF-8', () => {
  assert.equal(decodeBlob(Buffer.from(KEY, 'utf16le')), KEY);
  assert.equal(decodeBlob(Buffer.from('abcd', 'utf8')), 'abcd');
  assert.equal(decodeBlob(Buffer.from('abc', 'utf8')), 'abc');
  assert.equal(decodeBlob(Buffer.alloc(0)), undefined);
});

test('macOS and Linux ask security and secret-tool for service typesafe, account api_key', async () => {
  const calls: Call[] = [];
  const mac = await lookUpKey({ env: {}, platform: 'darwin', runCommand: runner({ security: { stdout: `${KEY}\n` } }, calls) });
  assert.deepEqual(mac.found, { key: KEY, source: 'credential-store' });
  const linux = await lookUpKey({ env: {}, platform: 'linux', runCommand: runner({ 'secret-tool': { stdout: KEY } }, calls) });
  assert.deepEqual(linux.found, { key: KEY, source: 'credential-store' });
  assert.deepEqual(calls, [
    { command: 'security', args: ['find-generic-password', '-s', 'typesafe', '-a', 'api_key', '-w'] },
    { command: 'secret-tool', args: ['lookup', 'service', 'typesafe', 'username', 'api_key'] },
  ]);
});

test('a missing key names where the server looked, and never echoes store output', async () => {
  const win = await lookUpKey({ env: {}, platform: 'win32', runCommand: runner({ pwsh: { code: 1, stdout: KEY }, 'powershell.exe': 'missing' }) });
  assert.equal(win.found, undefined);
  assert.match(win.missing!, /TYPESAFE_API_KEY/);
  assert.match(win.missing!, /Windows Credential Manager/);
  assert.match(win.missing!, /api_key@typesafe/);
  assert.match(win.missing!, /powershell\.exe is not installed/);
  assert.ok(!win.missing!.includes(KEY));

  const linux = await lookUpKey({ env: {}, platform: 'linux', runCommand: runner({ 'secret-tool': { code: 1 } }) });
  assert.match(linux.missing!, /Secret Service keyring \(service typesafe, username api_key\)/);
  const mac = await lookUpKey({ env: {}, platform: 'darwin', runCommand: runner({}) });
  assert.match(mac.missing!, /macOS keychain.*security is not installed/);
});

test('KeyStore looks the key up once and keeps it, but retries while it is missing', async () => {
  const calls: Call[] = [];
  const answers: Record<string, { stdout?: string; code?: number }> = { 'secret-tool': { code: 1 } };
  const store = new KeyStore({ env: {}, platform: 'linux', runCommand: runner(answers, calls) });
  assert.equal((await store.lookUp()).found, undefined);
  answers['secret-tool'] = { stdout: KEY };
  assert.equal((await store.lookUp()).found?.key, KEY);
  assert.equal((await store.lookUp()).found?.key, KEY);
  assert.equal(calls.length, 2);
});

import assert from 'node:assert/strict';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { test } from 'node:test';
import {
  BadRequest,
  closestBase,
  isAbsolutePath,
  isReservedEnv,
  MAX_ENTRIES,
  MAX_PROMPT_CHARS,
  openRequestOf,
  parseCloseId,
  parseEmpty,
  parseOpenRequest,
} from '../request';

const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'iat-request-'));
const isWindows = process.platform === 'win32';

function body(fields: Record<string, unknown>) {
  return JSON.stringify({ path: dir, ...fields });
}

test('parses path and prompt', () => {
  const request = parseOpenRequest(body({ prompt: 'hello' }));
  assert.equal(request.path, dir);
  assert.equal(request.prompt, 'hello');
});

test('normalizes the path and drops a trailing separator', () => {
  assert.equal(parseOpenRequest(JSON.stringify({ path: dir + path.sep })).path, dir);
  assert.equal(parseOpenRequest(JSON.stringify({ path: path.join(dir, 'x', '..') })).path, dir);
});

test('blank prompt means no prompt', () => {
  assert.equal(parseOpenRequest(body({})).prompt, undefined);
  assert.equal(parseOpenRequest(body({ prompt: '  ' })).prompt, undefined);
  assert.equal(parseOpenRequest(body({ prompt: null })).prompt, undefined);
});

test('rejects bad bodies', () => {
  for (const bad of ['', 'not json', '[]', 'null', '"x"', '{"prompt":"x"}', '{"path":1}', body({ prompt: {} })]) {
    assert.throws(() => parseOpenRequest(bad), BadRequest, bad);
  }
});

test('rejects relative and missing directories', () => {
  assert.throws(() => openRequestOf('relative\\dir'), BadRequest);
  assert.throws(() => openRequestOf('relative/dir'), BadRequest);
  assert.throws(() => openRequestOf(path.join(dir, 'absent')), BadRequest);
  assert.throws(() => openRequestOf(`${dir}\0x`), BadRequest);
  const file = path.join(dir, 'f.txt');
  fs.writeFileSync(file, 'x');
  assert.throws(() => openRequestOf(file), BadRequest);
});

test('absolute means rooted with a drive or UNC share on Windows', () => {
  assert.ok(isAbsolutePath('C:\\work', true));
  assert.ok(isAbsolutePath('c:/work', true));
  assert.ok(isAbsolutePath('\\\\server\\share', true));
  assert.ok(!isAbsolutePath('\\work', true));
  assert.ok(!isAbsolutePath('C:work', true));
  assert.ok(!isAbsolutePath('/work', true));
  assert.ok(isAbsolutePath('/work', false));
  assert.ok(!isAbsolutePath('work', false));
});

test('rejects an oversized prompt', () => {
  assert.throws(() => openRequestOf(dir, 'x'.repeat(MAX_PROMPT_CHARS + 1)), BadRequest);
  assert.equal(openRequestOf(dir, 'x'.repeat(MAX_PROMPT_CHARS)).prompt?.length, MAX_PROMPT_CHARS);
});

test('parses args and env', () => {
  const request = parseOpenRequest(body({ args: ['--plugin-dir', 'C:\\a b'], env: { FOO: 'x y', EMPTY: '' } }));
  assert.deepEqual(request.args, ['--plugin-dir', 'C:\\a b']);
  assert.deepEqual(request.env, { FOO: 'x y', EMPTY: '' });
});

test('parses an optional agent', () => {
  assert.equal(parseOpenRequest(body({ agent: 'codex' })).agent, 'codex');
  assert.equal(parseOpenRequest(body({})).agent, undefined);
  assert.equal(parseOpenRequest(body({ agent: null })).agent, undefined);
  for (const agent of [1, '  ', ['codex']]) {
    assert.throws(() => parseOpenRequest(body({ agent })), BadRequest, JSON.stringify(agent));
  }
});

test('args and env are optional', () => {
  const request = parseOpenRequest(body({}));
  assert.deepEqual(request.args, []);
  assert.deepEqual(request.env, {});
  assert.deepEqual(parseOpenRequest(body({ args: null, env: null })).args, []);
});

test('rejects bad args and env', () => {
  const bad: Record<string, unknown>[] = [
    { args: '--x' },
    { args: [1] },
    { args: [['x']] },
    { args: ['a\0b'] },
    { env: ['A'] },
    { env: { A: 1 } },
    { env: { 'A=B': 'x' } },
    { env: { 'A B': 'x' } },
    { env: { '': 'x' } },
    { env: { JEDITERM_SOURCE: 'x' } },
    { env: { ide_agent_tabs_id: 'x' } },
    { env: { IDE_AGENT_TABS_ARG_0: 'x' } },
    { env: { JEDITERM_SOURCE_ARGS: 'x' } },
    { env: { A: 'x'.repeat(MAX_PROMPT_CHARS + 1) } },
  ];
  for (const fields of bad) {
    assert.throws(() => parseOpenRequest(body(fields)), BadRequest, JSON.stringify(fields));
  }
  assert.throws(() => openRequestOf(dir, undefined, Array(MAX_ENTRIES + 1).fill('x')), BadRequest);
  assert.throws(() => openRequestOf(dir, undefined, ['x'.repeat(MAX_PROMPT_CHARS + 1)]), BadRequest);
  assert.throws(() => openRequestOf(dir, undefined, [], { A: 'x\0y' }), BadRequest);
  const many = Object.fromEntries(Array.from({ length: MAX_ENTRIES + 1 }, (_, i) => [`V${i}`, 'x']));
  assert.throws(() => openRequestOf(dir, undefined, [], many), BadRequest);
  openRequestOf(dir, undefined, Array(MAX_ENTRIES).fill('x'));
});

test('close takes a string id', () => {
  assert.equal(parseCloseId('{"id":"abc"}'), 'abc');
  for (const bad of ['', '{}', '{"id":""}', '{"id":"  "}', '{"id":7}', '[]', 'nope']) {
    assert.throws(() => parseCloseId(bad), BadRequest, bad);
  }
});

test('list accepts an empty body or an object', () => {
  parseEmpty('');
  parseEmpty('{}');
  assert.throws(() => parseEmpty('[]'), BadRequest);
  assert.throws(() => parseEmpty('nope'), BadRequest);
});

test('plugin variables and the startup variables are reserved', () => {
  for (const name of ['JEDITERM_SOURCE', 'jediterm_source_args', 'IDE_AGENT_TABS_ID', 'IDE_AGENT_TABS_ARG_0', 'ide_agent_tabs_argc']) {
    assert.ok(isReservedEnv(name), name);
  }
  assert.ok(!isReservedEnv('CLAUDE_CODE_USE_BEDROCK'));
});

test('closest base picks the deepest containing folder', () => {
  const a = path.join(dir, 'work');
  const b = path.join(a, 'repo');
  const c = path.join(dir, 'other');
  assert.equal(closestBase(path.join(b, 'sub'), [a, b, c]), 1);
  assert.equal(closestBase(path.join(a, 'x'), [a, b, undefined]), 0);
  assert.equal(closestBase(b, [a, b]), 1);
  assert.equal(closestBase(path.join(dir, 'none'), [a, b, c]), undefined);
  assert.equal(closestBase(`${a}-sibling`, [a]), undefined);
});

test('closest base ignores case on Windows', () => {
  assert.equal(closestBase('c:\\WORK\\Repo', ['C:\\work', 'C:\\work\\repo'], true), 1);
  assert.equal(closestBase('C:\\work\\x', ['C:\\'], true), 0);
  assert.equal(closestBase('/Work/repo', ['/work'], false), undefined);
  if (isWindows) assert.equal(closestBase('c:\\WORK', ['C:\\work\\']), 0);
});

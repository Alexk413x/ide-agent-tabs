import assert from 'node:assert/strict';
import { existsSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';
import type { RunResult } from '../src/process.js';
import { run } from '../src/process.js';
import { parsePosixSpec, type LaunchSpec } from '../src/spec.js';
import { defaultTerminalName, TERMINAL_DRIVERS } from '../src/terminals/index.js';
import {
  classifyOsascriptError,
  CLOSE_SCRIPT,
  createIterm2,
  INPUT_SCRIPT,
  iterm2,
  iterm2Command,
  iterm2Locations,
  LIST_SCRIPT,
  OPEN_SCRIPT,
  parseSessionId,
  type Iterm2Deps,
} from '../src/terminals/iterm2.js';
import type { TerminalContext } from '../src/terminals/types.js';
import { tempDir } from './tempDir.js';

const GUID = '4B5E6F70-1A2B-4C3D-8E9F-0123456789AB';
const ctx: TerminalContext = { home: '/Users/u/.ide-agent-tabs', scriptsDir: '/p/dist/launch', pathVar: '', env: { SHELL: '/bin/zsh' } };
const spec: LaunchSpec = { id: 'tab-1', agent: 'claude', cwd: '/Users/u/my app', command: 'claude', args: ['--x'], prompt: 'hi', env: { FOO: 'bar' } };
const ok = (stdout: string): RunResult => ({ code: 0, stdout, stderr: '' });
const fail = (stderr: string): RunResult => ({ code: 1, stdout: '', stderr });

function fake(answers: RunResult[], overrides: Partial<Iterm2Deps> = {}) {
  const calls: { script: string; args: string[] }[] = [];
  const written = new Map<string, Buffer>();
  const removed: string[] = [];
  const driver = createIterm2({
    platform: 'darwin',
    findApp: () => '/Applications/iTerm.app',
    osascript: async (script, args) => {
      calls.push({ script, args });
      const answer = answers.shift();
      if (!answer) throw new Error('unexpected osascript call');
      return answer;
    },
    writeSpec: async (file, content) => void written.set(file, content),
    removeSpec: async (file) => void removed.push(file),
    ...overrides,
  });
  return { driver, calls, written, removed };
}

const tab = { id: 'tab-1', terminal: 'iterm2', agent: 'claude', path: '/w', createdAt: 0, terminalId: GUID };

test('iTerm2 is registered and comes after Ghostty in the macOS order', () => {
  assert.ok(TERMINAL_DRIVERS.includes(iterm2));
  assert.deepEqual(iterm2.capabilities, { open: 'tab', list: 'yes', close: 'yes' });
  assert.equal(defaultTerminalName('darwin', ['tmux', 'iterm2', 'kitty']), 'iterm2');
  assert.equal(defaultTerminalName('darwin', ['tmux', 'iterm2', 'ghostty']), 'ghostty');
  assert.equal(defaultTerminalName('linux', ['iterm2']), undefined);
  assert.deepEqual(iterm2Locations('/Users/u'), ['/Applications/iTerm.app', '/Users/u/Applications/iTerm.app']);
});

test('iTerm2 is available only on macOS with the app installed', async () => {
  assert.equal(await fake([]).driver.available(ctx), true);
  assert.equal(await fake([], { platform: 'linux' }).driver.available(ctx), false);
  assert.equal(await fake([], { findApp: () => undefined }).driver.available(ctx), false);
});

test('iTerm2 opens a tab with a single-quoted argv-mode command and gets the session GUID', async () => {
  const { driver, calls, written, removed } = fake([ok(`${GUID}\n`)]);
  const opened = await driver.open(ctx, spec, 'Claude\nCode');
  assert.deepEqual(opened, { id: 'tab-1', terminal: 'iterm2', agent: 'claude', path: '/Users/u/my app', createdAt: opened.createdAt, terminalId: GUID });
  assert.equal(calls.length, 1);
  assert.equal(calls[0]!.script, OPEN_SCRIPT);
  assert.deepEqual(calls[0]!.args, [
    "'/bin/zsh' '-l' '-i' '-c' 'IDE_AGENT_TABS_SPEC=$2; export IDE_AGENT_TABS_SPEC; . \"$1\"; exec /bin/zsh -l -i' 'agent-tabs' '/p/dist/launch/agent-launch.sh' '/Users/u/.ide-agent-tabs/launch/tab-1.spec'",
    'Claude Code',
  ]);
  const file = '/Users/u/.ide-agent-tabs/launch/tab-1.spec';
  assert.deepEqual(parsePosixSpec(written.get(file)!), spec);
  assert.deepEqual(removed, []);
});

test('iTerm2 opens fish tabs with the fish launcher', async () => {
  const { driver, calls } = fake([ok(GUID)]);
  await driver.open({ ...ctx, env: { SHELL: '/opt/homebrew/bin/fish' } }, spec, 't');
  assert.match(calls[0]!.args[0]!, /^'\/opt\/homebrew\/bin\/fish' '-l' '-i' '-c' 'set -gx IDE_AGENT_TABS_SPEC \$argv\[2\]; source "\$argv\[1\]"; exec \/opt\/homebrew\/bin\/fish -l -i' '\/p\/dist\/launch\/agent-launch.fish' /);
});

test('every value reaches osascript as argv, never in the script source', async () => {
  const hostile = 'x" & (do shell script "id") & " ';
  const { driver, calls } = fake([ok(GUID), ok('sent\n'), ok('closed\n')]);
  await driver.open(ctx, { ...spec, cwd: `/w/${hostile}`, prompt: hostile, args: [hostile] }, hostile);
  await driver.input!(ctx, tab, 'Agent Tabs: new message from codex 01234567. Call read_messages.');
  await driver.close(ctx, tab);
  assert.deepEqual(calls.map((c) => c.script), [OPEN_SCRIPT, INPUT_SCRIPT, CLOSE_SCRIPT]);
  for (const script of [OPEN_SCRIPT, LIST_SCRIPT, INPUT_SCRIPT, CLOSE_SCRIPT]) {
    assert.match(script, /^on run argv\n/);
    assert.ok(!script.includes('do shell script'));
    assert.ok(!script.includes(GUID));
  }
  assert.ok(!calls[0]!.args[0]!.includes('do shell script'));
  assert.deepEqual(calls[1]!.args, [GUID, 'Agent Tabs: new message from codex 01234567. Call read_messages.']);
  assert.deepEqual(calls[2]!.args, [GUID]);
});

test("iTerm2 refuses launcher and spec paths that hold ', \\, $ or a control character", async () => {
  for (const bad of ["/Users/o'brien", '/Users/a\\(id)', '/Users/$$USER$$', '/Users/a\nb']) {
    const { driver, calls, written } = fake([]);
    await assert.rejects(driver.open({ ...ctx, home: bad }, spec, 't'), /iTerm2 can't start a path|control character/);
    await assert.rejects(driver.open({ ...ctx, scriptsDir: bad }, spec, 't'), /iTerm2 can't start a path|control character/);
    assert.equal(calls.length, 0);
    assert.equal(written.size, 0);
  }
  assert.throws(() => iterm2Command(["it's"]), /'/);
  assert.throws(() => iterm2Command(['a\\b']), /\\/);
  assert.equal(iterm2Command(['/bin/zsh', 'a b', '$1"']), "'/bin/zsh' 'a b' '$1\"'");
});

test('a folder with quotes is fine, because the launcher changes to it', async () => {
  const { driver, calls } = fake([ok(GUID)]);
  await driver.open(ctx, { ...spec, cwd: "/Users/u/o'brien\\x" }, 't');
  assert.ok(!calls[0]!.args.join(' ').includes('brien'));
});

test('iTerm2 lists open sessions by GUID and never launches iTerm2 to do it', async () => {
  const other = { ...tab, id: 'tab-2', terminalId: 'DEAD' };
  const { driver, calls } = fake([ok(`AAA\n${GUID}\nBBB\n`)]);
  assert.deepEqual([...(await driver.alive(ctx, [tab, other, { ...tab, id: 'tab-3', terminalId: undefined }]))], ['tab-1']);
  assert.deepEqual(calls, [{ script: LIST_SCRIPT, args: [] }]);
  assert.match(LIST_SCRIPT, /if application id "com\.googlecode\.iterm2" is not running then return ""/);
  assert.deepEqual([...(await fake([]).driver.alive(ctx, []))], []);
});

test('iTerm2 closes a session by GUID, and reports one that is gone', async () => {
  assert.match(CLOSE_SCRIPT, /if \(unique ID of s\) is sessionId then\n\t+close \(contents of s\)/);
  const { driver, calls } = fake([ok('closed\n'), ok('missing\n')]);
  await driver.close(ctx, tab);
  await assert.rejects(driver.close(ctx, tab), /no session .*already closed/);
  assert.deepEqual(calls.map((c) => c.args), [[GUID], [GUID]]);
  await assert.rejects(fake([]).driver.close(ctx, { ...tab, terminalId: undefined }), /no iTerm2 session id/);
});

test('iTerm2 types the line without a newline, waits 200 ms, then sends Enter on its own', async () => {
  assert.match(INPUT_SCRIPT, /write text wakeLine newline false\n\t+delay 0\.2\n\t+write text ""\n/);
  const { driver, calls } = fake([ok('missing')]);
  await assert.rejects(driver.input!(ctx, tab, 'hi'), /no session/);
  assert.equal(calls.length, 1);
  for (const bad of ['two\nlines', 'x'.repeat(501), 'esc\u001b[2J']) {
    const f = fake([]);
    await assert.rejects(f.driver.input!(ctx, tab, bad), /one line/);
    assert.equal(f.calls.length, 0);
  }
});

test('a denied Automation permission names the setting and takes iTerm2 out of the terminal choice', async () => {
  const { driver, removed } = fake([fail('execution error: Not authorized to send Apple events to iTerm. (-1743)')]);
  await assert.rejects(driver.open(ctx, spec, 't'), /System Settings > Privacy & Security > Automation/);
  assert.deepEqual(removed, ['/Users/u/.ide-agent-tabs/launch/tab-1.spec']);
  assert.equal(await driver.available(ctx), false);
  await assert.rejects(driver.open(ctx, spec, 't'), /denied permission/);
  const available = [...((await driver.available(ctx)) ? ['iterm2'] : []), 'kitty', 'tmux'];
  assert.equal(defaultTerminalName('darwin', available), 'kitty');
});

test('an iTerm2 that macOS cannot find is reported as not installed and skipped', async () => {
  const { driver } = fake([fail("execution error: Can't find application id \"com.googlecode.iterm2\". (-10814)")]);
  await assert.rejects(driver.open(ctx, spec, 't'), /iTerm2 is not installed/);
  assert.equal(await driver.available(ctx), false);
});

test('any other AppleScript failure is reported like Ghostty and keeps iTerm2 available', async () => {
  const { driver, removed } = fake([fail("execution error: iTerm got an error: Can't get current window. (-1728)"), ok('garbage answer with spaces')]);
  await assert.rejects(driver.open(ctx, spec, 't'), /^Error: osascript failed: execution error: iTerm got an error/);
  assert.equal(await driver.available(ctx), true);
  await assert.rejects(driver.open(ctx, spec, 't'), /unexpected answer from iTerm2/);
  assert.equal(removed.length, 2);
  assert.equal(classifyOsascriptError('AppleEvent timed out. (-1712)'), 'timeout');
  assert.equal(classifyOsascriptError('syntax error'), 'other');
  assert.equal(parseSessionId(` ${GUID}\n`), GUID);
});

test('iTerm2 refuses to open off macOS', async () => {
  await assert.rejects(fake([], { platform: 'win32' }).driver.open(ctx, spec, 't'), /macOS only/);
});

const appInstalled = process.platform === 'darwin' && iterm2Locations(os.homedir()).some((p) => existsSync(p));

test('the iTerm2 scripts compile against the installed iTerm2', { skip: !appInstalled && 'needs macOS with iTerm2' }, async () => {
  const out = path.join(tempDir('iat-iterm2-'), 'x.scpt');
  for (const script of [OPEN_SCRIPT, LIST_SCRIPT, CLOSE_SCRIPT, INPUT_SCRIPT]) {
    const result = await run('/usr/bin/osacompile', ['-o', out, '-'], { input: script });
    assert.equal(result.code, 0, result.stderr);
  }
});

test(
  'a live iTerm2 tab opens, lists, takes input and closes',
  { skip: (!appInstalled || process.env.IDE_AGENT_TABS_LIVE_ITERM2 !== '1') && 'set IDE_AGENT_TABS_LIVE_ITERM2=1 on macOS with iTerm2' },
  async () => {
    const home = tempDir('iat-iterm2-live-');
    const scriptsDir = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', 'launch');
    const live: TerminalContext = { home, scriptsDir, pathVar: process.env.PATH ?? '', env: process.env };
    const opened = await iterm2.open(live, { id: `live-${Date.now()}`, agent: 'cat', cwd: home, command: '/bin/cat', args: [], env: {} }, 'Agent Tabs live test');
    assert.ok((await iterm2.alive(live, [opened])).has(opened.id));
    await iterm2.input!(live, opened, 'Agent Tabs live test line');
    await iterm2.close(live, opened);
    assert.ok(!(await iterm2.alive(live, [opened])).has(opened.id));
  },
);

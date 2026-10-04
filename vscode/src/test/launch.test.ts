import assert from 'node:assert/strict';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { test } from 'node:test';
import {
  AGENT_ENV,
  ARG_COUNT_ENV,
  ARG_ENV_PREFIX,
  ARGS_ENV,
  COMMAND_ENV,
  editorLocation,
  fishQuote,
  launchScripts,
  PROMPT_ENV,
  revivedTabs,
  shellKind,
  TAB_ID_ENV,
  terminalEnv,
  unixShell,
  windowsShell,
} from '../launch';
import { launchOf, profile } from '../profiles';

export const scriptDir = path.resolve(__dirname, '..', '..', 'resources', 'launch');
const scripts = launchScripts('/ext/resources/launch');
const claude = profile('claude', 'Claude Code', 'claude');

test('shell kind comes from the file name', () => {
  assert.equal(shellKind('/bin/zsh'), 'posix');
  assert.equal(shellKind('/usr/local/bin/bash'), 'posix');
  assert.equal(shellKind('/opt/homebrew/bin/fish'), 'fish');
  assert.equal(shellKind('/usr/local/bin/pwsh'), 'powershell');
  assert.equal(shellKind('C:\\Program Files\\PowerShell\\7\\pwsh.exe'), 'powershell');
  assert.equal(shellKind('powershell.exe'), 'powershell');
  assert.equal(shellKind('/bin/tcsh'), undefined);
});

test('unix shell uses the login shell and falls back to bash', () => {
  assert.equal(unixShell('/bin/zsh', true, scripts).path, '/bin/zsh');
  assert.equal(unixShell('/usr/bin/fish', false, scripts).kind, 'fish');
  for (const other of [undefined, '', '/bin/tcsh', '/bin/sh']) {
    const shell = unixShell(other, false, scripts);
    assert.equal(shell.path, '/bin/bash', String(other));
    assert.equal(shell.kind, 'posix', String(other));
  }
});

test('bash and zsh source the launcher, then exec an interactive shell; login shells on macOS', () => {
  assert.deepEqual(unixShell('/bin/zsh', true, scripts).args, [
    '-l',
    '-i',
    '-c',
    '. "$1"; exec "$0" -l -i',
    '/bin/zsh',
    scripts.posix,
  ]);
  assert.deepEqual(unixShell('/bin/bash', false, scripts).args, ['-i', '-c', '. "$1"; exec "$0" -i', '/bin/bash', scripts.posix]);
});

test('fish sources the launcher through its init command', () => {
  assert.deepEqual(unixShell('/usr/bin/fish', true, scripts).args, ['-l', '-i', '-C', `source ${fishQuote(scripts.fish)}`]);
  assert.equal(fishQuote("/a b/it's\\x"), "'/a b/it\\'s\\\\x'");
});

test('PowerShell runs the launcher file and stays open', () => {
  assert.deepEqual(unixShell('/usr/local/bin/pwsh', true, scripts), {
    path: '/usr/local/bin/pwsh',
    kind: 'powershell',
    args: ['-NoLogo', '-NoExit', '-File', scripts.powershell],
  });
  const win = windowsShell('', scripts);
  assert.equal(win.path, 'powershell.exe');
  assert.deepEqual(win.args, ['-NoLogo', '-NoExit', '-ExecutionPolicy', 'Bypass', '-File', scripts.powershell]);
});

test('Windows prefers pwsh on PATH, then Windows PowerShell', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'iat-shell-'));
  const a = path.join(dir, 'a');
  const b = path.join(dir, 'b');
  fs.mkdirSync(a);
  fs.mkdirSync(b);
  fs.writeFileSync(path.join(a, 'powershell.exe'), '');
  assert.equal(windowsShell([a, b].join(path.delimiter), scripts).path, path.join(a, 'powershell.exe'));
  fs.writeFileSync(path.join(b, 'pwsh.exe'), '');
  assert.equal(windowsShell([a, b].join(path.delimiter), scripts).path, path.join(b, 'pwsh.exe'));
});

test('caller text reaches only environment variables, never the shell arguments', () => {
  const nasty = profile('x', 'X', 'C:\\a b\\x;y.exe', { args: ['$(whoami)'], promptFlag: '-p' });
  const launch = launchOf(nasty, "'; Remove-Item C:\\ #", ['`t']);
  const env = terminalEnv('powershell', launch, 'tab-9');
  assert.deepEqual(env, {
    [TAB_ID_ENV]: 'tab-9',
    [AGENT_ENV]: 'x',
    [COMMAND_ENV]: 'C:\\a b\\x;y.exe',
    [ARGS_ENV]: '["$(whoami)","`t","-p"]',
    [PROMPT_ENV]: "'; Remove-Item C:\\ #",
  });
  for (const shell of [windowsShell('', scripts), unixShell('/bin/zsh', true, scripts), unixShell('/usr/bin/fish', false, scripts)]) {
    for (const arg of shell.args) {
      for (const text of ['whoami', 'Remove-Item', 'x;y', '`t']) assert.ok(!arg.includes(text), arg);
    }
  }
});

test('PowerShell env holds the command only when there are no args or prompt', () => {
  assert.deepEqual(terminalEnv('powershell', launchOf(claude), 'tab-1'), {
    [TAB_ID_ENV]: 'tab-1',
    [AGENT_ENV]: 'claude',
    [COMMAND_ENV]: 'claude',
  });
});

test('bash, zsh and fish get the arg count and each arg in its own variable', () => {
  const gemini = profile('gemini', 'Gemini CLI', 'gemini', { promptFlag: '-i' });
  assert.deepEqual(terminalEnv('posix', launchOf(gemini, 'hi', ['--a', ''], { FOO: 'bar' }), 'tab-1'), {
    FOO: 'bar',
    [TAB_ID_ENV]: 'tab-1',
    [AGENT_ENV]: 'gemini',
    [COMMAND_ENV]: 'gemini',
    [PROMPT_ENV]: 'hi',
    [ARG_COUNT_ENV]: '3',
    [`${ARG_ENV_PREFIX}0`]: '--a',
    [`${ARG_ENV_PREFIX}1`]: '',
    [`${ARG_ENV_PREFIX}2`]: '-i',
  });
});

test('reserved variables inherited from the editor are unset in the tab', () => {
  const inherited = { PATH: '/bin', IDE_AGENT_TABS_ARGC: '2', ide_agent_tabs_prompt: 'old', IDE_AGENT_TABS_ID: 'parent', JEDITERM_SOURCE: 'x' };
  const env = terminalEnv('posix', launchOf(claude), 'tab-5', inherited);
  assert.equal(env[TAB_ID_ENV], 'tab-5');
  assert.equal(env.IDE_AGENT_TABS_ARGC, null);
  assert.equal(env.ide_agent_tabs_prompt, null);
  assert.equal(env.JEDITERM_SOURCE, null);
  assert.equal(env.PATH, undefined);
  assert.equal(Object.keys(env).includes('ide_agent_tabs_id'), false);
});

test('launch scripts ship with LF line endings', () => {
  for (const name of ['agent.sh', 'agent.fish', 'agent.ps1']) {
    const text = fs.readFileSync(path.join(scriptDir, name), 'utf8');
    assert.ok(!text.includes('\r'), name);
    assert.ok(text.includes('IDE_AGENT_TABS_COMMAND'), name);
  }
});

test('an editor terminal keeps focus in the current editor unless focus is asked for', () => {
  assert.deepEqual(editorLocation(-1, false), { viewColumn: -1, preserveFocus: true });
  assert.deepEqual(editorLocation(-1, true), { viewColumn: -1, preserveFocus: false });
});

test('terminals that survive an extension host restart come back as tabs from their env', () => {
  const env = terminalEnv('powershell', launchOf(claude), 'tab-7');
  const panel = { name: 'panel', creationOptions: { name: 'pwsh' } };
  const pty = { name: 'pty', creationOptions: { name: 'Task', pty: {} } };
  const agentTab = { name: 'agent', creationOptions: { name: 'Claude Code', cwd: 'C:\work\app', env } };
  const uriCwd = { name: 'uri', creationOptions: { cwd: { fsPath: '/home/a/b' }, env: { [TAB_ID_ENV]: 'tab-8', [AGENT_ENV]: 'codex' } } };
  assert.deepEqual(revivedTabs([panel, pty, agentTab, uriCwd], () => false), [
    { id: 'tab-7', agent: 'claude', path: 'C:\work\app', terminal: agentTab },
    { id: 'tab-8', agent: 'codex', path: '/home/a/b', terminal: uriCwd },
  ]);
});

test('known tabs and a second terminal with the same tab id are not revived', () => {
  const env = { [TAB_ID_ENV]: 'tab-9', [AGENT_ENV]: 'gemini' };
  const first = { creationOptions: { cwd: '/a', env } };
  const copy = { creationOptions: { cwd: '/b', env } };
  const known = { creationOptions: { cwd: '/c', env: { [TAB_ID_ENV]: 'tab-10' } } };
  const revived = revivedTabs([first, copy, known], id => id === 'tab-10');
  assert.deepEqual(revived.map(t => [t.id, t.path]), [['tab-9', '/a']]);
  assert.deepEqual(revivedTabs([first], (_, terminal) => terminal === first), []);
});

test('a terminal with a tab id but no agent or folder is still revived', () => {
  const bare = { creationOptions: { env: { [TAB_ID_ENV]: 'tab-11', [AGENT_ENV]: null } } };
  assert.deepEqual(revivedTabs([bare], () => false), [{ id: 'tab-11', agent: '', path: '', terminal: bare }]);
});

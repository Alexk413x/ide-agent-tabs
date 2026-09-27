import assert from 'node:assert/strict';
import { mkdirSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import { tempDir } from './tempDir.js';
import {
  appleScriptString,
  closeScript,
  listScript,
  loginShell,
  openScript,
  parseOpenResult,
  surfaceCommand,
} from '../src/terminals/ghostty.js';
import { findPowerShell, parseTasklist, wtArgs, wtEnvironment, wtTitle } from '../src/terminals/windowsTerminal.js';

test('Windows Terminal gets only fixed strings and our own paths', () => {
  assert.deepEqual(
    wtArgs({ title: 'Claude Code', shell: 'C:\\pwsh.exe', launcher: 'C:\\p d\\agent-launch.ps1', spec: 'C:\\h\\launch\\t.json' }),
    ['-w', '0', 'new-tab', '--title', 'Claude Code', 'C:\\pwsh.exe', '-NoLogo', '-NoExit', '-ExecutionPolicy', 'Bypass', '-File', 'C:\\p d\\agent-launch.ps1', 'C:\\h\\launch\\t.json'],
  );
  assert.throws(() => wtArgs({ title: 'x', shell: 'C:\\pwsh.exe', launcher: 'C:\\a;b\\l.ps1', spec: 'C:\\s.json' }), /';'/);
  assert.equal(wtTitle('A; B\nC'), 'A  B C');
  assert.equal(wtTitle(';'), 'Agent');
});

test('a Windows Terminal started by the server does not inherit the calling session', () => {
  assert.deepEqual(
    wtEnvironment({ PATH: 'p', ClaudeCode: '1', CLAUDE_CODE_SSE_PORT: '1', CLAUDE_CODE_USE_BEDROCK: '1', IDE_AGENT_TABS_ID: 't', IDE_AGENT_TABS_HOME: 'h' }),
    { PATH: 'p', CLAUDE_CODE_USE_BEDROCK: '1', IDE_AGENT_TABS_HOME: 'h' },
  );
});

test('prefers pwsh, then Windows PowerShell', () => {
  const dir = tempDir('iat-ps-');
  const a = path.join(dir, 'a');
  const b = path.join(dir, 'b');
  mkdirSync(a);
  mkdirSync(b);
  writeFileSync(path.join(a, 'powershell.exe'), '');
  assert.equal(findPowerShell(a), path.join(a, 'powershell.exe'));
  writeFileSync(path.join(b, 'pwsh.exe'), '');
  assert.equal(findPowerShell([a, b].join(path.delimiter)), path.join(b, 'pwsh.exe'));
  assert.equal(findPowerShell(''), 'powershell.exe');
});

test('reads process images from tasklist CSV', () => {
  const images = parseTasklist('"System Idle Process","0","Services","0","8 K"\r\n"pwsh.exe","4242","Console","1","90,000 K"\r\n\r\n');
  assert.equal(images.get(4242), 'pwsh.exe');
  assert.equal(images.get(0), 'system idle process');
  assert.equal(images.size, 2);
});

test('Ghostty uses the login shell when it is bash, zsh or fish, else zsh', () => {
  assert.deepEqual(loginShell('/opt/homebrew/bin/fish'), { path: '/opt/homebrew/bin/fish', kind: 'fish' });
  assert.deepEqual(loginShell('/bin/bash'), { path: '/bin/bash', kind: 'posix' });
  assert.deepEqual(loginShell('/usr/local/bin/nu'), { path: '/bin/zsh', kind: 'posix' });
  assert.deepEqual(loginShell("/bin/zsh'; rm -rf ~"), { path: '/bin/zsh', kind: 'posix' });
  assert.deepEqual(loginShell(undefined), { path: '/bin/zsh', kind: 'posix' });
});

test('the Ghostty surface command is fixed apart from a checked shell path', () => {
  assert.equal(
    surfaceCommand({ path: '/bin/zsh', kind: 'posix' }),
    `/bin/zsh -l -i -c '. "$IDE_AGENT_TABS_LAUNCHER"; exec /bin/zsh -l -i'`,
  );
  assert.equal(
    surfaceCommand({ path: '/opt/homebrew/bin/fish', kind: 'fish' }),
    `/opt/homebrew/bin/fish -l -i -c 'source "$IDE_AGENT_TABS_LAUNCHER"; exec /opt/homebrew/bin/fish -l -i'`,
  );
  assert.throws(() => surfaceCommand({ path: '/bin/my shell', kind: 'posix' }));
});

test('AppleScript string literals escape backslashes and quotes and refuse control characters', () => {
  assert.equal(appleScriptString('a"b\\c'), '"a\\"b\\\\c"');
  assert.equal(appleScriptString('/Users/J Doe/é'), '"/Users/J Doe/é"');
  assert.throws(() => appleScriptString('a\nb'));
  assert.throws(() => appleScriptString('a\rb'));
});

test('the Ghostty open script holds only the command and our paths', () => {
  const script = openScript(`/bin/zsh -l -i -c '. "$IDE_AGENT_TABS_LAUNCHER"; exec /bin/zsh -l -i'`, {
    IDE_AGENT_TABS_LAUNCHER: '/Users/a "q"/dist/launch/agent-launch.sh',
    IDE_AGENT_TABS_SPEC: '/Users/a/.ide-agent-tabs/launch/t.spec',
  });
  assert.equal(
    script,
    [
      'tell application "Ghostty"',
      '\tset cfg to new surface configuration',
      `\tset command of cfg to "/bin/zsh -l -i -c '. \\"$IDE_AGENT_TABS_LAUNCHER\\"; exec /bin/zsh -l -i'"`,
      '\tset environment variables of cfg to {"IDE_AGENT_TABS_LAUNCHER=/Users/a \\"q\\"/dist/launch/agent-launch.sh", "IDE_AGENT_TABS_SPEC=/Users/a/.ide-agent-tabs/launch/t.spec"}',
      '\tif (count of windows) > 0 then',
      '\t\tset t to new tab in front window with configuration cfg',
      '\telse',
      '\t\tset w to new window with configuration cfg',
      '\t\tset t to selected tab of w',
      '\tend if',
      '\treturn (id of t as text) & linefeed & (id of (focused terminal of t) as text)',
      'end tell',
      '',
    ].join('\n'),
  );
});

test('the Ghostty list and close scripts never launch Ghostty and quote the id', () => {
  assert.match(listScript(), /^if application "Ghostty" is not running then return ""/);
  const close = closeScript('ABC-"1"');
  assert.match(close, /^if application "Ghostty" is not running then return "missing"/);
  assert.ok(close.includes('is "ABC-\\"1\\"" then'));
  assert.deepEqual(parseOpenResult('tab-1\nterm-2\n'), { tabId: 'tab-1', terminalId: 'term-2' });
  assert.throws(() => parseOpenResult('only-one'));
});

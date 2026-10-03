import assert from 'node:assert/strict';
import { mkdirSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import { tempDir } from './tempDir.js';
import {
  appleScriptString,
  closeScript,
  ghosttyCapabilities,
  ghosttyLinuxArgs,
  ghosttyLinuxLocations,
  inputScript,
  listScript,
  openScript,
  parseOpenResult,
} from '../src/terminals/ghostty.js';
import { defaultTerminalName } from '../src/terminals/index.js';
import { isShellName, pidTabsAlive, terminalEnvironment } from '../src/terminals/processes.js';
import { argvModeCommand, checkArgvPaths, loginShell, surfaceArgv, surfaceCommand, tabTitle } from '../src/terminals/shell.js';
import { findPowerShell, parseTasklist, powerShellArgv, wtArgs, wtTitle } from '../src/terminals/windowsTerminal.js';

test('Windows Terminal gets only fixed strings and our own paths', () => {
  assert.deepEqual(
    wtArgs({ title: 'Claude Code', shell: 'C:\\pwsh.exe', launcher: 'C:\\p d\\agent-launch.ps1', spec: 'C:\\h\\launch\\t.json' }),
    ['-w', '0', 'new-tab', '--title', 'Claude Code', 'C:\\pwsh.exe', '-NoLogo', '-NoExit', '-ExecutionPolicy', 'Bypass', '-File', 'C:\\p d\\agent-launch.ps1', 'C:\\h\\launch\\t.json'],
  );
  assert.throws(() => wtArgs({ title: 'x', shell: 'C:\\pwsh.exe', launcher: 'C:\\a;b\\l.ps1', spec: 'C:\\s.json' }), /';'/);
  assert.equal(wtTitle('A; B\nC'), 'A B C');
  assert.equal(wtTitle(';'), 'Agent');
});

test('a Windows Terminal started by the server does not inherit the calling session', () => {
  assert.deepEqual(
    terminalEnvironment({
      PATH: 'p',
      ClaudeCode: '1',
      CLAUDE_CODE_SSE_PORT: '1',
      CLAUDE_CODE_USE_BEDROCK: '1',
      IDE_AGENT_TABS_ID: 't',
      IDE_AGENT_TABS_HOME: 'h',
      CODEX_SANDBOX: 'seatbelt',
      CODEX_SANDBOX_NETWORK_DISABLED: '1',
      CODEX_HOME: 'c',
      GEMINI_CLI: '1',
      OPENCODE_SESSION_ID: 's',
      ANTIGRAVITY_CLI_ALIAS: 'agy',
      COPILOT_HOME: 'g',
    }),
    { PATH: 'p', CLAUDE_CODE_USE_BEDROCK: '1', IDE_AGENT_TABS_HOME: 'h', CODEX_HOME: 'c', COPILOT_HOME: 'g' },
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

test('a tab uses the login shell when it is bash, zsh or fish, else zsh on macOS and bash on Linux', () => {
  assert.deepEqual(loginShell('/opt/homebrew/bin/fish', 'darwin'), { path: '/opt/homebrew/bin/fish', kind: 'fish' });
  assert.deepEqual(loginShell('/bin/bash', 'darwin'), { path: '/bin/bash', kind: 'posix' });
  assert.deepEqual(loginShell('/usr/local/bin/nu', 'darwin'), { path: '/bin/zsh', kind: 'posix' });
  assert.deepEqual(loginShell("/bin/zsh'; rm -rf ~", 'darwin'), { path: '/bin/zsh', kind: 'posix' });
  assert.deepEqual(loginShell(undefined, 'darwin'), { path: '/bin/zsh', kind: 'posix' });
  assert.deepEqual(loginShell(undefined, 'linux'), { path: '/bin/bash', kind: 'posix' });
  assert.deepEqual(loginShell('/usr/bin/zsh', 'linux'), { path: '/usr/bin/zsh', kind: 'posix' });
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

test('env mode runs the login shell with a fixed script', () => {
  assert.deepEqual(surfaceArgv({ path: '/usr/bin/bash', kind: 'posix' }), [
    '/usr/bin/bash', '-l', '-i', '-c', '. "$IDE_AGENT_TABS_LAUNCHER"; exec /usr/bin/bash -l -i',
  ]);
  assert.throws(() => surfaceArgv({ path: 'bash', kind: 'posix' }), /unsafe shell path/);
});

test('argv mode keeps the launcher and spec out of the script the shell interprets', () => {
  const launcher = "/opt/a b/it's $(x)/agent-launch.sh";
  const spec = '/home/u/.ide-agent-tabs/launch/t.spec';
  assert.deepEqual(argvModeCommand({ path: '/bin/zsh', kind: 'posix' }, launcher, spec), [
    '/bin/zsh', '-l', '-i', '-c',
    'IDE_AGENT_TABS_SPEC=$2; export IDE_AGENT_TABS_SPEC; . "$1"; exec /bin/zsh -l -i',
    'agent-tabs', launcher, spec,
  ]);
  assert.deepEqual(argvModeCommand({ path: '/usr/bin/fish', kind: 'fish' }, launcher, spec), [
    '/usr/bin/fish', '-l', '-i', '-c',
    'set -gx IDE_AGENT_TABS_SPEC $argv[2]; source "$argv[1]"; exec /usr/bin/fish -l -i',
    launcher, spec,
  ]);
  assert.throws(() => argvModeCommand({ path: '/bin/zsh', kind: 'posix' }, '/a\nb', spec), /control character/);
});

test('paths with control characters or a refused character are rejected, and titles are cleaned', () => {
  checkArgvPaths('X', ['/a b/c;d']);
  assert.throws(() => checkArgvPaths('X', ['/a\tb']), /X can't start a path that holds a control character/);
  assert.throws(() => checkArgvPaths('X', ['/a;b'], ';'), /holds ';'/);
  assert.equal(tabTitle(' Claude\u0007 Code\u202e '), 'Claude Code');
  assert.equal(tabTitle('\n\t'), 'Agent');
  assert.equal([...tabTitle('é'.repeat(100))].length, 40);
});

test('PowerShell tabs get the same argv in every terminal', () => {
  assert.deepEqual(powerShellArgv('C:\\pwsh.exe', 'C:\\l.ps1', 'C:\\s.json'), [
    'C:\\pwsh.exe', '-NoLogo', '-NoExit', '-ExecutionPolicy', 'Bypass', '-File', 'C:\\l.ps1', 'C:\\s.json',
  ]);
});

test('Ghostty opens a tab over AppleScript on macOS and a new process on Linux', () => {
  assert.deepEqual(ghosttyCapabilities('darwin'), { open: 'tab', list: 'yes', close: 'yes' });
  assert.deepEqual(ghosttyCapabilities('linux'), { open: 'window', list: 'tracked', close: 'best-effort' });
  assert.deepEqual(ghosttyLinuxArgs('/home/u/my app', { path: '/bin/bash', kind: 'posix' }), [
    '--gtk-single-instance=false',
    '--working-directory=/home/u/my app',
    '--confirm-close-surface=false',
    '--wait-after-command=false',
    '-e', '/bin/bash', '-l', '-i', '-c', '. "$IDE_AGENT_TABS_LAUNCHER"; exec /bin/bash -l -i',
  ]);
  assert.throws(() => ghosttyLinuxArgs('/home/u/a\nb', { path: '/bin/bash', kind: 'posix' }), /control character/);
});

test('Ghostty on Linux is looked for in its install folders after PATH', () => {
  assert.deepEqual(ghosttyLinuxLocations('/home/u'), ['/usr/bin/ghostty', '/usr/local/bin/ghostty', '/home/u/.local/bin/ghostty', '/snap/bin/ghostty']);
});

test('the default terminal is the first available one in the platform order', () => {
  assert.equal(defaultTerminalName('win32', ['wezterm', 'windows-terminal']), 'windows-terminal');
  assert.equal(defaultTerminalName('win32', ['wezterm', 'tmux']), 'wezterm');
  assert.equal(defaultTerminalName('darwin', ['tmux', 'wezterm', 'kitty']), 'kitty');
  assert.equal(defaultTerminalName('linux', ['tmux']), 'tmux');
  assert.equal(defaultTerminalName('linux', ['windows-terminal']), undefined);
  assert.equal(defaultTerminalName('freebsd', ['tmux']), undefined);
});

test('a pid-tracked tab is a running shell, and counts as open during start-up until its pid file exists', async () => {
  assert.ok(isShellName('bash\n'));
  assert.ok(isShellName('-zsh'));
  assert.ok(isShellName('/opt/homebrew/bin/fish'));
  assert.ok(!isShellName('node'));
  const dir = tempDir('iat-pid-');
  const ended = path.join(dir, 'ended.pid');
  writeFileSync(ended, '999999999\n');
  const missing = path.join(dir, 'missing.pid');
  const base = { terminal: 'ghostty', agent: 'a', path: '/' };
  const now = Date.now();
  const alive = await pidTabsAlive(
    [
      { ...base, id: 'starting', createdAt: now, pidFile: missing },
      { ...base, id: 'stale', createdAt: now - 120_000, pidFile: missing },
      { ...base, id: 'ended', createdAt: now, pidFile: ended },
    ],
    now,
  );
  assert.deepEqual([...alive], ['starting']);
});

test('Ghostty types the line into the recorded terminal, then presses Enter as a key event', () => {
  assert.equal(
    inputScript('A1B2-C3', 'Agent Tabs: new message from codex 01234567. Call read_messages.'),
    [
      'tell application "Ghostty"',
      '\tset t to terminal id "A1B2-C3"',
      '\tinput text "Agent Tabs: new message from codex 01234567. Call read_messages." to t',
      '\tdelay 0.2',
      '\tsend key "enter" to t',
      'end tell',
      '',
    ].join('\n'),
  );
  assert.equal(inputScript('x"y', 'hi').split('\n')[1], '\tset t to terminal id "x\\"y"');
  assert.throws(() => inputScript('x', 'a\rb'), /one line/);
});

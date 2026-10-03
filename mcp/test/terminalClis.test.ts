import assert from 'node:assert/strict';
import { utimesSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import { tempDir } from './tempDir.js';
import {
  findKittySockets,
  kittyAddresses,
  kittyLaunchArgs,
  kittySocketDir,
  kittyInputCalls,
  kittySpawnArgs,
  parseKittyOsWindows,
  parseKittyWindowId,
  planKittyPlace,
  parseKittyWindows,
} from '../src/terminals/kitty.js';
import {
  isNoServerError,
  parseTmuxSessions,
  parseTmuxWindow,
  parseTmuxWindowList,
  planDedicatedTmuxTarget,
  planTmuxTarget,
  tmuxInputArgs,
  tmuxOpenArgs,
  tmuxTitle,
} from '../src/terminals/tmux.js';
import {
  findGuiSockets,
  isNoGuiError,
  parsePaneId,
  parseWeztermPanes,
  parseWeztermPaneWindows,
  planWeztermTargets,
  weztermCliArgs,
  weztermInputArgs,
  weztermLocations,
  weztermRuntimeDir,
  weztermSpawnArgs,
  weztermStartArgs,
} from '../src/terminals/wezterm.js';
import type { TerminalTab } from '../src/terminals/types.js';

const argv = ['/bin/zsh', '-l', '-i', '-c', 'script', 'agent-tabs', '/l.sh', '/s.spec'];

test('WezTerm spawns and starts panes with the folder and our argv only', () => {
  assert.deepEqual(weztermSpawnArgs('/w/my app', argv), ['spawn', '--cwd', '/w/my app', '--', ...argv]);
  assert.deepEqual(weztermCliArgs(['list']), ['cli', '--no-auto-start', 'list']);
  assert.deepEqual(weztermStartArgs('C:\\w\\a;b', argv), ['start', '--cwd', 'C:\\w\\a;b', '--', ...argv]);
  assert.throws(() => weztermSpawnArgs('/w/a\rb', argv), /control character/);
});

test('WezTerm pane ids come from cli spawn and cli list', () => {
  assert.equal(parsePaneId('42\n'), '42');
  assert.throws(() => parsePaneId('Error: nope'));
  const list = JSON.stringify([
    { window_id: 0, tab_id: 0, pane_id: 3, cwd: 'file:///home/u/', title: 'zsh' },
    { window_id: 0, tab_id: 1, pane_id: 7, cwd: 'file:///w/', title: 'claude' },
    { window_id: 1 },
  ]);
  assert.deepEqual(parseWeztermPanes(list), ['3', '7']);
  assert.deepEqual(parseWeztermPanes('[]'), []);
  assert.throws(() => parseWeztermPanes('{}'));
});

test('WezTerm reports no GUI when cli cannot connect', () => {
  assert.ok(isNoGuiError('Error: failed to connect to Socket("/run/user/1000/wezterm/gui-sock-1")'));
  assert.ok(isNoGuiError('while connecting to the mux: No such file or directory'));
  assert.ok(!isNoGuiError('error: unexpected argument --foo'));
});

test('WezTerm is looked for in its install folders', () => {
  assert.deepEqual(weztermLocations('win32', 'C:\\Users\\u', 'C:\\Program Files'), ['C:\\Program Files\\WezTerm\\wezterm.exe']);
  assert.deepEqual(weztermLocations('darwin', '/Users/u', undefined), [
    '/Applications/WezTerm.app/Contents/MacOS/wezterm',
    '/Users/u/Applications/WezTerm.app/Contents/MacOS/wezterm',
  ]);
  assert.deepEqual(weztermLocations('linux', '/home/u', undefined), [
    '/usr/bin/wezterm',
    '/usr/local/bin/wezterm',
    '/home/u/.local/bin/wezterm',
    '/home/linuxbrew/.linuxbrew/bin/wezterm',
  ]);
});

test('WezTerm GUI sockets are found in its runtime folder, only for running GUIs, newest first', () => {
  assert.equal(weztermRuntimeDir('linux', { XDG_RUNTIME_DIR: '/run/user/1000' }, '/home/u'), '/run/user/1000/wezterm');
  assert.equal(weztermRuntimeDir('darwin', {}, '/Users/u'), path.join('/Users/u', '.local', 'share', 'wezterm'));
  const dir = tempDir('iat-wezterm-');
  for (const [name, age] of [['gui-sock-100', 300], ['gui-sock-200', 100], ['gui-sock-300', 0], ['sock', 0], ['gui-sock-x', 0]] as const) {
    const file = path.join(dir, name);
    writeFileSync(file, '');
    const t = Date.now() / 1000 - age;
    utimesSync(file, t, t);
  }
  const running = (pid: number) => pid !== 300;
  assert.deepEqual(findGuiSockets(dir, running), [path.join(dir, 'gui-sock-200'), path.join(dir, 'gui-sock-100')]);
  assert.deepEqual(findGuiSockets(path.join(dir, 'missing'), running), []);
});

test('kitty sockets are found by name and newest first', () => {
  const dir = tempDir('iat-kitty-');
  for (const [name, age] of [['kitty-agent-tabs-100', 300], ['kitty-agent-tabs-200', 100], ['kitty-agent-tabs-x', 0], ['other-1', 0]] as const) {
    const file = path.join(dir, name);
    writeFileSync(file, '');
    const t = Date.now() / 1000 - age;
    utimesSync(file, t, t);
  }
  assert.deepEqual(findKittySockets(dir), [`unix:${path.join(dir, 'kitty-agent-tabs-200')}`, `unix:${path.join(dir, 'kitty-agent-tabs-100')}`]);
  assert.deepEqual(findKittySockets(path.join(dir, 'missing')), []);
  assert.deepEqual(kittyAddresses('linux', { XDG_RUNTIME_DIR: dir, KITTY_LISTEN_ON: 'unix:/tmp/k-1' }), ['unix:/tmp/k-1']);
  assert.equal(kittyAddresses('linux', { XDG_RUNTIME_DIR: dir, KITTY_LISTEN_ON: 'tcp:localhost:5000' }).length, 2);
  assert.equal(kittySocketDir('linux', { TMPDIR: '/tmp' }), undefined);
  assert.equal(kittySocketDir('darwin', { TMPDIR: '/var/folders/x/T/' }), '/var/folders/x/T/');
  assert.equal(kittySocketDir('win32', {}), undefined);
});

test('kitty launches a tab with our paths in --env and a cleaned title', () => {
  assert.deepEqual(
    kittyLaunchArgs({ address: 'unix:/run/user/1/kitty-agent-tabs-9', cwd: '/w/app', title: 'Claude\nCode', launcher: '/d/agent-launch.sh', spec: '/h/t.spec', argv }),
    [
      '@', '--to', 'unix:/run/user/1/kitty-agent-tabs-9', 'launch', '--type=tab', '--cwd', '/w/app',
      '--env', 'IDE_AGENT_TABS_LAUNCHER=/d/agent-launch.sh',
      '--env', 'IDE_AGENT_TABS_SPEC=/h/t.spec',
      '--tab-title', 'Claude Code',
      '--', ...argv,
    ],
  );
  assert.throws(() => kittyLaunchArgs({ address: 'unix:/k', cwd: '/w', title: 't', launcher: '/l\n', spec: '/s', argv }), /control character/);
  assert.deepEqual(kittySpawnArgs('/w/app', argv), ['--directory', '/w/app', ...argv]);
});

test('kitty window ids come from launch and ls', () => {
  assert.equal(parseKittyWindowId('17\n'), '17');
  assert.throws(() => parseKittyWindowId('Error'));
  const ls = JSON.stringify([
    { id: 1, tabs: [{ id: 1, title: 'a', windows: [{ id: 1, pid: 10, cwd: '/' }, { id: 4 }] }, { id: 2, windows: [{ id: 9 }] }] },
    { id: 2, tabs: [] },
  ]);
  assert.deepEqual([...parseKittyWindows(ls)].sort(), ['1', '4', '9']);
  assert.throws(() => parseKittyWindows('{}'));
});

test('tmux opens in the most recently attached session, else in the agents session', () => {
  const sessions = parseTmuxSessions('0 1700000000 $0 work\n1 1700000100 $1 main\n1 1700000200 $2 my session\n0  $3 agents\nbad line\n');
  assert.deepEqual(sessions.map((s) => s.id), ['$0', '$1', '$2', '$3']);
  assert.equal(sessions[2]!.name, 'my session');
  assert.deepEqual(planTmuxTarget(sessions), { session: '$2', detached: false });
  assert.deepEqual(planTmuxTarget(sessions.filter((s) => s.attached === 0)), { session: '$3', detached: true });
  assert.deepEqual(planTmuxTarget(sessions.slice(0, 1)), { newSession: 'agents' });
  assert.deepEqual(planTmuxTarget([]), { newSession: 'agents' });
});

test('tmux gets our paths through env without -e, a title without # or ;, and no folder', () => {
  const o = { title: 'Cl#{pane_pid}aude;', launcher: '/d/agent-launch.sh', spec: '/h/t.spec', argv };
  const tail = ['-n', 'Cl {pane_pid}aude', '--', '/usr/bin/env', 'IDE_AGENT_TABS_LAUNCHER=/d/agent-launch.sh', 'IDE_AGENT_TABS_SPEC=/h/t.spec', ...argv];
  assert.deepEqual(tmuxOpenArgs({ session: '$2', detached: false }, o), ['new-window', '-P', '-F', '#{window_id} #{session_id} #{pid} #{socket_path}', '-t', '$2:', ...tail]);
  assert.deepEqual(tmuxOpenArgs({ newSession: 'agents' }, o), ['new-session', '-d', '-s', 'agents', '-P', '-F', '#{window_id} #{session_id} #{pid} #{socket_path}', ...tail]);
  assert.ok(!tmuxOpenArgs({ newSession: 'agents' }, o).includes('-e'), 'new-session -e needs tmux 3.2');
  assert.throws(() => tmuxOpenArgs({ newSession: 'agents' }, { ...o, spec: '/h/a;b.spec' }), /';'/);
  assert.equal(tmuxTitle('#;'), 'Agent');
});

test('tmux window records hold the window id, server pid and socket', () => {
  assert.deepEqual(parseTmuxWindow('@12 $2 4242 /tmp/tmux-501/my default\n'), { windowId: '@12', sessionId: '$2', serverPid: 4242, socket: '/tmp/tmux-501/my default' });
  assert.throws(() => parseTmuxWindow('12 $2 4242 /tmp/s'));
  assert.throws(() => parseTmuxWindow('@12 $2 x /tmp/s'));
  const list = parseTmuxWindowList('4242 @1\n4242 @12\n\n');
  assert.equal(list.serverPid, 4242);
  assert.deepEqual([...list.windows], ['@1', '@12']);
  assert.ok(isNoServerError('no server running on /tmp/tmux-501/default'));
  assert.ok(isNoServerError('error connecting to /tmp/tmux-501/default (No such file or directory)'));
  assert.ok(!isNoServerError('unknown flag -e'));
});

test('typing a line sends the text and a separate Enter, with no bracketed paste', () => {
  const line = 'Agent Tabs: new message from codex 01234567. Call read_messages.';
  assert.deepEqual(tmuxInputArgs('/tmp/tmux-1000/default', '@12', line), [
    ['-S', '/tmp/tmux-1000/default', 'send-keys', '-t', '@12', '-l', '--', line],
    ['-S', '/tmp/tmux-1000/default', 'send-keys', '-t', '@12', 'Enter'],
  ]);
  assert.throws(() => tmuxInputArgs('/s', '@1', 'ends;'), /';'/);
  assert.deepEqual(weztermInputArgs('7', line), [
    ['send-text', '--pane-id', '7', '--no-paste', '--', line],
    ['send-text', '--pane-id', '7', '--no-paste', '--', '\r'],
  ]);
  const kittyArgs = ['@', '--to', 'unix:/run/k-1', 'send-text', '--match', 'id:5', '--stdin'];
  assert.deepEqual(kittyInputCalls('unix:/run/k-1', '5', line), [
    { args: kittyArgs, input: line },
    { args: kittyArgs, input: '\r' },
  ]);
  for (const bad of ['two\nlines', 'x'.repeat(501), 'esc\u001b[2J']) {
    assert.throws(() => tmuxInputArgs('/s', '@1', bad), /one line/);
    assert.throws(() => weztermInputArgs('1', bad), /one line/);
    assert.throws(() => kittyInputCalls('unix:/k', '1', bad), /one line/);
  }
});

const nearTab = (over: Partial<TerminalTab>): TerminalTab => ({ id: 'caller', terminal: 'x', agent: 'claude', path: '/w', createdAt: 0, ...over });

test('WezTerm spawns in the caller pane\'s window, a remembered window, or a new one it remembers', () => {
  assert.deepEqual(weztermSpawnArgs('/w', argv, { paneId: '7' }), ['spawn', '--pane-id', '7', '--cwd', '/w', '--', ...argv]);
  assert.deepEqual(weztermSpawnArgs('/w', argv, { windowId: '2' }), ['spawn', '--window-id', '2', '--cwd', '/w', '--', ...argv]);
  assert.deepEqual(weztermSpawnArgs('/w', argv, { newWindow: true }), ['spawn', '--new-window', '--cwd', '/w', '--', ...argv]);
  assert.throws(() => weztermSpawnArgs('/w', argv, { windowId: '2 --x' }), /not a WezTerm id/);

  const panes = new Map([['7', '1'], ['8', '2']]);
  assert.deepEqual(planWeztermTargets(undefined, undefined, undefined, undefined), [{ remember: false }]);
  assert.deepEqual(planWeztermTargets({ window: 'last' }, undefined, { id: '2', socket: '/g' }, panes), [{ remember: false }]);
  assert.deepEqual(planWeztermTargets({ window: 'dedicated' }, undefined, { id: '2', socket: '/g' }, panes), [
    { socket: '/g', place: { windowId: '2' }, remember: false },
    { place: { newWindow: true }, remember: true },
  ]);
  assert.deepEqual(planWeztermTargets({ window: 'dedicated' }, undefined, { id: '9', socket: '/g' }, panes), [{ place: { newWindow: true }, remember: true }], 'a closed window is made again');
  assert.deepEqual(planWeztermTargets({ window: 'dedicated' }, undefined, { id: '2', socket: '/g' }, undefined), [{ place: { newWindow: true }, remember: true }]);
  const near = nearTab({ terminal: 'wezterm', terminalId: '7', socket: '/n' });
  assert.deepEqual(planWeztermTargets({ window: 'last', near }, panes, undefined, undefined), [
    { socket: '/n', place: { paneId: '7' }, remember: false },
    { remember: false },
  ]);
  assert.deepEqual(planWeztermTargets({ window: 'last', near }, new Map(), undefined, undefined), [{ remember: false }], 'a caller pane that is gone falls back');

  const list = JSON.stringify([{ window_id: 0, pane_id: 3 }, { window_id: 1, pane_id: 7 }, { pane_id: 9 }]);
  assert.deepEqual([...parseWeztermPaneWindows(list)], [['3', '0'], ['7', '1'], ['9', '']]);
});

test('kitty opens a tab next to the caller, in a remembered OS window, or in a new OS window it remembers', () => {
  const o = { address: 'unix:/k', cwd: '/w', title: 't', launcher: '/l.sh', spec: '/s.spec', argv };
  assert.deepEqual(kittyLaunchArgs({ ...o, place: { windowId: '12' } }).slice(3, 7), ['launch', '--type=tab', '--match', 'window_id:12']);
  assert.deepEqual(kittyLaunchArgs({ ...o, place: { osWindow: true } }).slice(3, 6), ['launch', '--type=os-window', '--cwd']);
  assert.deepEqual(kittyLaunchArgs(o).slice(3, 6), ['launch', '--type=tab', '--cwd']);
  assert.throws(() => kittyLaunchArgs({ ...o, place: { windowId: 'recent:0' } }), /not a kitty window id/);

  const osWindows = new Map([['1', ['10', '11']], ['2', []]]);
  assert.deepEqual(planKittyPlace(undefined, 'unix:/k', undefined, undefined, undefined), { address: 'unix:/k' });
  assert.deepEqual(planKittyPlace({ window: 'dedicated' }, 'unix:/k', undefined, { id: '1', socket: 'unix:/r' }, osWindows), { address: 'unix:/r', place: { windowId: '10' } });
  assert.deepEqual(planKittyPlace({ window: 'dedicated' }, 'unix:/k', undefined, { id: '2', socket: 'unix:/r' }, osWindows), { address: 'unix:/k', place: { osWindow: true } });
  assert.deepEqual(planKittyPlace({ window: 'dedicated' }, 'unix:/k', undefined, undefined, undefined), { address: 'unix:/k', place: { osWindow: true } });
  const near = nearTab({ terminal: 'kitty', terminalId: '11', socket: 'unix:/n' });
  assert.deepEqual(planKittyPlace({ window: 'dedicated', near }, 'unix:/k', new Set(['11']), undefined, undefined), { address: 'unix:/n', place: { windowId: '11' } });
  assert.deepEqual(planKittyPlace({ window: 'last', near }, 'unix:/k', new Set(), undefined, undefined), { address: 'unix:/k' });

  const ls = JSON.stringify([{ id: 1, tabs: [{ windows: [{ id: 10 }, { id: 11 }] }] }, { id: 2, tabs: [{ windows: [{ id: 20 }] }] }]);
  assert.deepEqual([...parseKittyOsWindows(ls)], [['1', ['10', '11']], ['2', ['20']]]);
  assert.deepEqual([...parseKittyWindows(ls)], ['10', '11', '20']);
});

test('tmux uses a dedicated agent-tabs session, or opens next to the caller\'s window on its server', () => {
  const sessions = [
    { attached: 1, lastAttached: 5, id: '$1', name: 'work' },
    { attached: 0, lastAttached: 0, id: '$4', name: 'agent-tabs' },
  ];
  assert.deepEqual(planDedicatedTmuxTarget(sessions), { session: '$4', detached: true });
  assert.deepEqual(planDedicatedTmuxTarget([{ ...sessions[1]!, attached: 1 }]), { session: '$4', detached: false });
  assert.deepEqual(planDedicatedTmuxTarget(sessions.slice(0, 1)), { newSession: 'agent-tabs' });
  assert.deepEqual(planTmuxTarget(sessions), { session: '$1', detached: false }, 'the last mode keeps its plan');
  const o = { title: 't', launcher: '/d/agent-launch.sh', spec: '/h/t.spec', argv };
  assert.deepEqual(tmuxOpenArgs({ after: '@7', socket: '/tmp/tmux-1/default' }, o).slice(0, 9), [
    '-S', '/tmp/tmux-1/default', 'new-window', '-a', '-t', '@7', '-P', '-F', '#{window_id} #{session_id} #{pid} #{socket_path}',
  ]);
});

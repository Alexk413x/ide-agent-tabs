import { promises as fs, existsSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { writeNewPrivateFile } from '../files.js';
import { run, type RunResult } from '../process.js';
import { checkPosixEnvNames, posixSpec, type LaunchSpec } from '../spec.js';
import { argvModeCommand, checkArgvPaths, checkInputLine, launcherName, loginShell, tabTitle } from './shell.js';
import type { TerminalCapabilities, TerminalDriver, TerminalTab } from './types.js';

export const ITERM2 = 'iterm2';
export const ITERM2_BUNDLE_ID = 'com.googlecode.iterm2';
const CAPABILITIES: TerminalCapabilities = { open: 'tab', list: 'yes', close: 'yes' };
const APP = `application id "${ITERM2_BUNDLE_ID}"`;

// iTerm2 evaluates a tab's command as an interpolated string, where \( starts an expression, then splits it
// shell-style. Our paths must not hold ', \ or $, and each word is single-quoted.
const COMMAND_REFUSED = "'\\$";

export function iterm2Locations(home: string): string[] {
  return ['/Applications/iTerm.app', path.posix.join(home, 'Applications', 'iTerm.app')];
}

export function iterm2Command(argv: string[]): string {
  return argv
    .map((word) => {
      if ([...word].some((c) => c === "'" || c === '\\' || /\p{Cc}/u.test(c))) {
        throw new Error(`iTerm2 can't run a command word that holds ', \\ or a control character: ${JSON.stringify(word)}`);
      }
      return `'${word}'`;
    })
    .join(' ');
}

// Every value reaches AppleScript through osascript's argv, read by `on run argv`, never through the script
// source, so these scripts are constants.
export const OPEN_SCRIPT = [
  'on run argv',
  '\tset agentCommand to item 1 of argv',
  '\tset agentTitle to item 2 of argv',
  `\ttell ${APP}`,
  '\t\tset w to missing value',
  '\t\tif (count of windows) > 0 then set w to current window',
  '\t\tif w is missing value then',
  '\t\t\tset w to (create window with default profile command agentCommand)',
  '\t\t\tset s to current session of current tab of w',
  '\t\telse',
  '\t\t\ttell w to set t to (create tab with default profile command agentCommand)',
  '\t\t\tset s to current session of t',
  '\t\tend if',
  '\t\tset name of s to agentTitle',
  '\t\treturn unique ID of s',
  '\tend tell',
  'end run',
  '',
].join('\n');

export const LIST_SCRIPT = [
  'on run argv',
  `\tif ${APP} is not running then return ""`,
  '\tset out to ""',
  `\ttell ${APP}`,
  '\t\trepeat with w in windows',
  '\t\t\trepeat with t in tabs of w',
  '\t\t\t\trepeat with s in sessions of t',
  '\t\t\t\t\tset out to out & (unique ID of s) & linefeed',
  '\t\t\t\tend repeat',
  '\t\t\tend repeat',
  '\t\tend repeat',
  '\tend tell',
  '\treturn out',
  'end run',
  '',
].join('\n');

function findSessionScript(action: string[], done: string): string {
  return [
    'on run argv',
    '\tset sessionId to item 1 of argv',
    `\tif ${APP} is not running then return "missing"`,
    `\ttell ${APP}`,
    '\t\trepeat with w in windows',
    '\t\t\trepeat with t in tabs of w',
    '\t\t\t\trepeat with s in sessions of t',
    '\t\t\t\t\tif (unique ID of s) is sessionId then',
    ...action.map((line) => `\t\t\t\t\t\t${line}`),
    `\t\t\t\t\t\treturn "${done}"`,
    '\t\t\t\t\tend if',
    '\t\t\t\tend repeat',
    '\t\t\tend repeat',
    '\t\tend repeat',
    '\tend tell',
    '\treturn "missing"',
    'end run',
    '',
  ].join('\n');
}

export const CLOSE_SCRIPT = findSessionScript(['close (contents of s)'], 'closed');

// write text sends raw bytes to the pty, and `newline false` holds back the CR, so Enter goes separately
// after the same 200 ms the other terminals wait.
export const INPUT_SCRIPT = findSessionScript(
  [
    'set wakeLine to item 2 of argv',
    'tell (contents of s)',
    '\twrite text wakeLine newline false',
    '\tdelay 0.2',
    '\twrite text ""',
    'end tell',
  ],
  'sent',
);

export function parseSessionId(stdout: string): string {
  const id = stdout.trim();
  if (!/^[A-Za-z0-9._:-]{1,128}$/.test(id)) throw new Error(`unexpected answer from iTerm2: ${id}`);
  return id;
}

export function parseSessionList(stdout: string): Set<string> {
  return new Set(stdout.split(/\r?\n/).filter((l) => l !== ''));
}

const DENIED = /\(-1743\)|not authori[sz]ed to send apple events/i;
const NOT_INSTALLED = /\(-10814\)|can[’'`]?t find application|unable to find application/i;
const TIMED_OUT = /\(-1712\)/;

export type OsascriptFailure = 'denied' | 'not-installed' | 'timeout' | 'other';

export function classifyOsascriptError(stderr: string): OsascriptFailure {
  if (DENIED.test(stderr)) return 'denied';
  if (NOT_INSTALLED.test(stderr)) return 'not-installed';
  if (TIMED_OUT.test(stderr)) return 'timeout';
  return 'other';
}

export function osascriptErrorMessage(failure: OsascriptFailure, stderr: string): string {
  switch (failure) {
    case 'denied':
      return 'macOS denied permission to control iTerm2. Allow the app that runs this agent to control iTerm in System Settings > Privacy & Security > Automation, then start a new session. Until then, open_tab skips iTerm2.';
    case 'not-installed':
      return `iTerm2 is not installed, or macOS can't find it (${stderr}). Until the server restarts, open_tab skips iTerm2.`;
    case 'timeout':
      return `iTerm2 did not answer in time; if macOS shows a prompt to allow control of iTerm, answer it and try again (${stderr})`;
    case 'other':
      return `osascript failed: ${stderr}`;
  }
}

export type OsascriptRunner = (script: string, args: string[]) => Promise<RunResult>;

export interface Iterm2Deps {
  platform: NodeJS.Platform;
  findApp: () => string | undefined;
  osascript: OsascriptRunner;
  writeSpec: (file: string, content: Buffer) => Promise<void>;
  removeSpec: (file: string) => Promise<void>;
}

const defaultDeps: Iterm2Deps = {
  platform: process.platform,
  findApp: () => iterm2Locations(os.homedir()).find((p) => existsSync(p)),
  osascript: (script, args) => run('/usr/bin/osascript', ['-', ...args], { input: script, timeoutMs: 30_000 }),
  writeSpec: writeNewPrivateFile,
  removeSpec: (file) => fs.rm(file, { force: true }),
};

export function createIterm2(overrides: Partial<Iterm2Deps> = {}): TerminalDriver {
  const deps = { ...defaultDeps, ...overrides };
  let blocked: string | undefined;

  async function osascript(script: string, args: string[]): Promise<string> {
    const result = await deps.osascript(script, args);
    if (result.code === 0) return result.stdout;
    const stderr = result.stderr.trim();
    const failure = classifyOsascriptError(stderr);
    const message = osascriptErrorMessage(failure, stderr);
    if (failure === 'denied' || failure === 'not-installed') blocked = message;
    throw new Error(message);
  }

  function sessionId(tab: TerminalTab): string {
    if (!tab.terminalId) throw new Error(`tab ${tab.id} has no iTerm2 session id`);
    return tab.terminalId;
  }

  return {
    name: ITERM2,
    label: 'iTerm2',
    capabilities: CAPABILITIES,

    async available() {
      return deps.platform === 'darwin' && blocked === undefined && deps.findApp() !== undefined;
    },

    async open(ctx, spec: LaunchSpec, title) {
      if (deps.platform !== 'darwin') throw new Error('iTerm2 runs on macOS only');
      if (blocked) throw new Error(blocked);
      checkPosixEnvNames(spec.env);
      const shell = loginShell(ctx.env.SHELL, deps.platform);
      const specFile = path.posix.join(ctx.home, 'launch', `${spec.id}.spec`);
      const launcher = path.posix.join(ctx.scriptsDir, launcherName(shell));
      checkArgvPaths('iTerm2', [launcher, specFile], COMMAND_REFUSED);
      const command = iterm2Command(argvModeCommand(shell, launcher, specFile));
      await deps.writeSpec(specFile, posixSpec(spec));
      let id: string;
      try {
        id = parseSessionId(await osascript(OPEN_SCRIPT, [command, tabTitle(title)]));
      } catch (e) {
        await deps.removeSpec(specFile);
        throw e;
      }
      return { id: spec.id, terminal: ITERM2, agent: spec.agent, path: spec.cwd, createdAt: Date.now(), terminalId: id };
    },

    async alive(_ctx, tabs) {
      const tracked = tabs.filter((t) => t.terminalId);
      if (tracked.length === 0) return new Set();
      const ids = parseSessionList(await osascript(LIST_SCRIPT, []));
      return new Set(tracked.filter((t) => ids.has(t.terminalId!)).map((t) => t.id));
    },

    async close(_ctx, tab: TerminalTab) {
      const id = sessionId(tab);
      if ((await osascript(CLOSE_SCRIPT, [id])).trim() !== 'closed') {
        throw new Error(`iTerm2 has no session ${id}; the tab is already closed`);
      }
    },

    async input(_ctx, tab: TerminalTab, text) {
      checkInputLine(text);
      const id = sessionId(tab);
      if ((await osascript(INPUT_SCRIPT, [id, text])).trim() !== 'sent') throw new Error(`iTerm2 has no session ${id}`);
    },
  };
}

export const iterm2 = createIterm2();

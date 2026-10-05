import { promises as fs, existsSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { writeNewPrivateFile } from '../files.js';
import { run } from '../process.js';
import { checkPosixEnvNames, posixSpec, type LaunchSpec } from '../spec.js';
import { findExecutable, GUI_SETTLE_MS, hangUp, pidTabsAlive, startDetached, terminalEnvironment } from './processes.js';
import { checkArgvPaths, checkInputLine, launcherName, loginShell, surfaceArgv, surfaceCommand, type LoginShell } from './shell.js';
import type { OpenedTab, OpenOptions, TerminalCapabilities, TerminalContext, TerminalDriver, TerminalTab } from './types.js';
import { readWindow, rememberWindow, type RememberedWindow } from './windowMemory.js';

export const GHOSTTY = 'ghostty';

export function ghosttyLinuxLocations(home: string): string[] {
  return ['/usr/bin/ghostty', '/usr/local/bin/ghostty', path.posix.join(home, '.local', 'bin', 'ghostty'), '/snap/bin/ghostty'];
}

function findGhosttyOnLinux(ctx: TerminalContext): string | undefined {
  return findExecutable(ctx.pathVar, 'ghostty', ghosttyLinuxLocations(os.homedir()));
}

export function ghosttyCapabilities(platform: NodeJS.Platform): TerminalCapabilities {
  return platform === 'linux'
    ? { open: 'window', list: 'tracked', close: 'best-effort' }
    : { open: 'tab', list: 'yes', close: 'yes' };
}

// Ghostty on Linux can't open a tab in a running instance from outside (ghostty#12136), so each agent gets a
// new Ghostty process with one window, tracked through its shell's pid.
export function ghosttyLinuxArgs(cwd: string, shell: LoginShell): string[] {
  checkArgvPaths('Ghostty', [cwd]);
  return [
    '--gtk-single-instance=false',
    `--working-directory=${cwd}`,
    '--confirm-close-surface=false',
    '--wait-after-command=false',
    '-e',
    ...surfaceArgv(shell),
  ];
}

export function appleScriptString(value: string): string {
  if (/[\p{Cc}]/u.test(value)) throw new Error('AppleScript strings here must not hold control characters');
  return `"${value.replace(/\\/g, '\\\\').replace(/"/g, '\\"')}"`;
}

export type GhosttyPlace = { nearTab: string } | { dedicated: string | undefined };

function findWindowLines(place: GhosttyPlace): string[] {
  if ('nearTab' in place) {
    return [
      '\trepeat with cw in windows',
      '\t\trepeat with ct in tabs of cw',
      `\t\t\tif (id of ct as text) is ${appleScriptString(place.nearTab)} then set w to (contents of cw)`,
      '\t\tend repeat',
      '\tend repeat',
      '\tif w is missing value and (count of windows) > 0 then set w to front window',
    ];
  }
  if (place.dedicated === undefined) return [];
  return [
    '\trepeat with cw in windows',
    `\t\tif (id of cw as text) is ${appleScriptString(place.dedicated)} then set w to (contents of cw)`,
    '\tend repeat',
  ];
}

export function openScript(command: string, env: Record<string, string>, place?: GhosttyPlace, keepFocus = false): string {
  const vars = Object.entries(env).map(([k, v]) => appleScriptString(`${k}=${v}`));
  const head = [
    'tell application "Ghostty"',
    '\tset cfg to new surface configuration',
    `\tset command of cfg to ${appleScriptString(command)}`,
    `\tset environment variables of cfg to {${vars.join(', ')}}`,
  ];
  const newTabIn = (window: string) =>
    keepFocus
      ? [`\t\tset previousTab to selected tab of ${window}`, `\t\tset t to new tab in ${window} with configuration cfg`, '\t\tselect tab previousTab']
      : [`\t\tset t to new tab in ${window} with configuration cfg`];
  if (!place) {
    return [
      ...head,
      '\tif (count of windows) > 0 then',
      ...newTabIn('front window'),
      '\telse',
      '\t\tset w to new window with configuration cfg',
      '\t\tset t to selected tab of w',
      '\tend if',
      '\treturn (id of t as text) & linefeed & (id of (focused terminal of t) as text)',
      'end tell',
      '',
    ].join('\n');
  }
  return [
    ...head,
    '\tset w to missing value',
    ...findWindowLines(place),
    '\tif w is not missing value then',
    ...newTabIn('w'),
    '\telse',
    '\t\tset w to new window with configuration cfg',
    '\t\tset t to selected tab of w',
    '\tend if',
    '\treturn (id of t as text) & linefeed & (id of (focused terminal of t) as text) & linefeed & (id of w as text)',
    'end tell',
    '',
  ].join('\n');
}

export function ghosttyPlace(options: OpenOptions | undefined, remembered: RememberedWindow | undefined): GhosttyPlace | undefined {
  if (options?.near?.terminal === GHOSTTY && options.near.terminalTabId) return { nearTab: options.near.terminalTabId };
  if (options?.window === 'dedicated') return { dedicated: remembered?.id };
  return undefined;
}

export function listScript(): string {
  return [
    'if application "Ghostty" is not running then return ""',
    'set out to ""',
    'tell application "Ghostty"',
    '\trepeat with term in terminals',
    '\t\tset out to out & (id of term as text) & linefeed',
    '\tend repeat',
    'end tell',
    'return out',
    '',
  ].join('\n');
}

export function closeScript(terminalId: string): string {
  return [
    'if application "Ghostty" is not running then return "missing"',
    'tell application "Ghostty"',
    '\trepeat with term in terminals',
    `\t\tif (id of term as text) is ${appleScriptString(terminalId)} then`,
    '\t\t\tclose (contents of term)',
    '\t\t\treturn "closed"',
    '\t\tend if',
    '\tend repeat',
    'end tell',
    'return "missing"',
    '',
  ].join('\n');
}

// input text arrives as a paste when the program turned on bracketed paste, so Enter is a separate key event.
export function inputScript(terminalId: string, text: string): string {
  checkInputLine(text);
  return [
    'tell application "Ghostty"',
    `\tset t to terminal id ${appleScriptString(terminalId)}`,
    `\tinput text ${appleScriptString(text)} to t`,
    '\tdelay 0.2',
    '\tsend key "enter" to t',
    'end tell',
    '',
  ].join('\n');
}

export function parseOpenResult(stdout: string): { tabId: string; terminalId: string; windowId?: string } {
  const [tabId, terminalId, windowId] = stdout.trim().split(/\r?\n/);
  if (!tabId || !terminalId) throw new Error(`unexpected answer from Ghostty: ${stdout.trim()}`);
  return { tabId, terminalId, ...(windowId ? { windowId } : {}) };
}

async function osascript(script: string): Promise<string> {
  const result = await run('/usr/bin/osascript', ['-'], { input: script, timeoutMs: 30_000 });
  if (result.code !== 0) throw new Error(`osascript failed: ${result.stderr.trim()}`);
  return result.stdout;
}

function ghosttyApp(home: string): string | undefined {
  return ['/Applications/Ghostty.app', path.join(home, 'Applications', 'Ghostty.app')].find((p) => existsSync(p));
}

async function openOnLinux(ctx: TerminalContext, spec: LaunchSpec, shell: LoginShell, specFile: string, launcher: string): Promise<OpenedTab> {
  const exe = findGhosttyOnLinux(ctx);
  if (!exe) throw new Error('ghostty was not found');
  const pidFile = path.join(path.dirname(specFile), `${spec.id}.pid`);
  const args = ghosttyLinuxArgs(spec.cwd, shell);
  const env = { ...terminalEnvironment(ctx.env), IDE_AGENT_TABS_LAUNCHER: launcher, IDE_AGENT_TABS_SPEC: specFile };
  await writeNewPrivateFile(specFile, posixSpec({ ...spec, pidFile }));
  try {
    await startDetached(exe, args, env, GUI_SETTLE_MS);
  } catch (e) {
    await fs.rm(specFile, { force: true });
    throw e;
  }
  return { id: spec.id, terminal: GHOSTTY, agent: spec.agent, path: spec.cwd, createdAt: Date.now(), pidFile };
}

export const ghostty: TerminalDriver = {
  name: GHOSTTY,
  label: 'Ghostty',
  capabilities: ghosttyCapabilities(process.platform),

  async available(ctx) {
    if (process.platform === 'linux') return findGhosttyOnLinux(ctx) !== undefined;
    return process.platform === 'darwin' && ghosttyApp(os.homedir()) !== undefined;
  },

  async open(ctx, spec: LaunchSpec, _title, options) {
    checkPosixEnvNames(spec.env);
    const shell = loginShell(ctx.env.SHELL, process.platform);
    const specFile = path.join(ctx.home, 'launch', `${spec.id}.spec`);
    const launcher = path.join(ctx.scriptsDir, launcherName(shell));
    if (process.platform === 'linux') return openOnLinux(ctx, spec, shell, specFile, launcher);
    const dedicated = options?.window === 'dedicated' && !options.near;
    const remembered = dedicated ? await readWindow(ctx.home, GHOSTTY).catch(() => undefined) : undefined;
    const script = openScript(
      surfaceCommand(shell),
      { IDE_AGENT_TABS_LAUNCHER: launcher, IDE_AGENT_TABS_SPEC: specFile },
      ghosttyPlace(options, remembered),
      options?.focus === false,
    );
    await writeNewPrivateFile(specFile, posixSpec(spec));
    let ids: { tabId: string; terminalId: string; windowId?: string };
    try {
      ids = parseOpenResult(await osascript(script));
    } catch (e) {
      await fs.rm(specFile, { force: true });
      throw e;
    }
    if (dedicated && ids.windowId !== undefined && ids.windowId !== remembered?.id) {
      await rememberWindow(ctx.home, GHOSTTY, { id: ids.windowId }).catch(() => undefined);
    }
    return {
      id: spec.id,
      terminal: GHOSTTY,
      agent: spec.agent,
      path: spec.cwd,
      createdAt: Date.now(),
      terminalTabId: ids.tabId,
      terminalId: ids.terminalId,
      ...(ids.windowId !== undefined ? { window: ids.windowId } : {}),
    };
  },

  async alive(_ctx, tabs) {
    const alive = await pidTabsAlive(tabs.filter((t) => t.pidFile));
    const scripted = tabs.filter((t) => t.terminalId);
    if (scripted.length === 0) return alive;
    const ids = new Set((await osascript(listScript())).split(/\r?\n/).filter((l) => l !== ''));
    for (const t of scripted) if (ids.has(t.terminalId!)) alive.add(t.id);
    return alive;
  },

  async close(_ctx, tab: TerminalTab) {
    if (tab.pidFile) return hangUp(tab);
    if (!tab.terminalId) throw new Error(`tab ${tab.id} has no Ghostty terminal id`);
    if ((await osascript(closeScript(tab.terminalId))).trim() !== 'closed') {
      throw new Error(`Ghostty has no terminal ${tab.terminalId}; the tab is already closed`);
    }
  },

  async input(_ctx, tab: TerminalTab, text) {
    if (tab.pidFile || !tab.terminalId) throw new Error("Ghostty on Linux can't take input from outside");
    await osascript(inputScript(tab.terminalId, text));
  },
};

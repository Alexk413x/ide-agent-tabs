import { promises as fs, existsSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { writeNewPrivateFile } from '../files.js';
import { findOnPath } from '../installed.js';
import { run } from '../process.js';
import { checkPosixEnvNames, posixSpec, type LaunchSpec } from '../spec.js';
import { GUI_SETTLE_MS, hangUp, pidTabsAlive, startDetached, terminalEnvironment } from './processes.js';
import { checkArgvPaths, launcherName, loginShell, surfaceArgv, surfaceCommand, type LoginShell } from './shell.js';
import type { OpenedTab, TerminalCapabilities, TerminalContext, TerminalDriver, TerminalTab } from './types.js';

export const GHOSTTY = 'ghostty';

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

export function openScript(command: string, env: Record<string, string>): string {
  const vars = Object.entries(env).map(([k, v]) => appleScriptString(`${k}=${v}`));
  return [
    'tell application "Ghostty"',
    '\tset cfg to new surface configuration',
    `\tset command of cfg to ${appleScriptString(command)}`,
    `\tset environment variables of cfg to {${vars.join(', ')}}`,
    '\tif (count of windows) > 0 then',
    '\t\tset t to new tab in front window with configuration cfg',
    '\telse',
    '\t\tset w to new window with configuration cfg',
    '\t\tset t to selected tab of w',
    '\tend if',
    '\treturn (id of t as text) & linefeed & (id of (focused terminal of t) as text)',
    'end tell',
    '',
  ].join('\n');
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

export function parseOpenResult(stdout: string): { tabId: string; terminalId: string } {
  const [tabId, terminalId] = stdout.trim().split(/\r?\n/);
  if (!tabId || !terminalId) throw new Error(`unexpected answer from Ghostty: ${stdout.trim()}`);
  return { tabId, terminalId };
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
  const exe = findOnPath(ctx.pathVar, 'ghostty');
  if (!exe) throw new Error('ghostty was not found on PATH');
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
    if (process.platform === 'linux') return findOnPath(ctx.pathVar, 'ghostty') !== undefined;
    return process.platform === 'darwin' && ghosttyApp(os.homedir()) !== undefined;
  },

  async open(ctx, spec: LaunchSpec) {
    checkPosixEnvNames(spec.env);
    const shell = loginShell(ctx.env.SHELL, process.platform);
    const specFile = path.join(ctx.home, 'launch', `${spec.id}.spec`);
    const launcher = path.join(ctx.scriptsDir, launcherName(shell));
    if (process.platform === 'linux') return openOnLinux(ctx, spec, shell, specFile, launcher);
    const script = openScript(surfaceCommand(shell), {
      IDE_AGENT_TABS_LAUNCHER: launcher,
      IDE_AGENT_TABS_SPEC: specFile,
    });
    await writeNewPrivateFile(specFile, posixSpec(spec));
    let ids: { tabId: string; terminalId: string };
    try {
      ids = parseOpenResult(await osascript(script));
    } catch (e) {
      await fs.rm(specFile, { force: true });
      throw e;
    }
    return {
      id: spec.id,
      terminal: GHOSTTY,
      agent: spec.agent,
      path: spec.cwd,
      createdAt: Date.now(),
      terminalTabId: ids.tabId,
      terminalId: ids.terminalId,
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
};

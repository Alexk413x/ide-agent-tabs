import { promises as fs, existsSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { writeNewPrivateFile } from '../files.js';
import { run } from '../process.js';
import { checkPosixEnvNames, posixSpec, type LaunchSpec } from '../spec.js';
import type { TerminalDriver, TerminalTab } from './types.js';

export const GHOSTTY = 'ghostty';
export const LAUNCHER_SH = 'agent-launch.sh';
export const LAUNCHER_FISH = 'agent-launch.fish';
const DEFAULT_SHELL = '/bin/zsh';
const SAFE_PATH = /^\/[A-Za-z0-9._/+-]+$/;

export interface LoginShell {
  path: string;
  kind: 'posix' | 'fish';
}

export function loginShell(shellEnv: string | undefined): LoginShell {
  if (shellEnv && SAFE_PATH.test(shellEnv)) {
    const name = path.posix.basename(shellEnv);
    if (name === 'bash' || name === 'zsh') return { path: shellEnv, kind: 'posix' };
    if (name === 'fish') return { path: shellEnv, kind: 'fish' };
  }
  return { path: DEFAULT_SHELL, kind: 'posix' };
}

// Ghostty runs a surface's command through /bin/sh -c. The shell path is checked against SAFE_PATH, and
// everything else the launch needs reaches it through the surface's environment variables.
export function surfaceCommand(shell: LoginShell): string {
  if (!SAFE_PATH.test(shell.path)) throw new Error(`unsafe shell path: ${shell.path}`);
  const inner =
    shell.kind === 'fish'
      ? `source "$IDE_AGENT_TABS_LAUNCHER"; exec ${shell.path} -l -i`
      : `. "$IDE_AGENT_TABS_LAUNCHER"; exec ${shell.path} -l -i`;
  return `${shell.path} -l -i -c '${inner}'`;
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

export const ghostty: TerminalDriver = {
  name: GHOSTTY,
  label: 'Ghostty',
  capabilities: { open: 'tab', list: 'yes', close: 'yes' },

  async available() {
    return process.platform === 'darwin' && ghosttyApp(os.homedir()) !== undefined;
  },

  async open(ctx, spec: LaunchSpec) {
    checkPosixEnvNames(spec.env);
    const shell = loginShell(ctx.env.SHELL);
    const specFile = path.join(ctx.home, 'launch', `${spec.id}.spec`);
    const launcher = path.join(ctx.scriptsDir, shell.kind === 'fish' ? LAUNCHER_FISH : LAUNCHER_SH);
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
    const ids = new Set((await osascript(listScript())).split(/\r?\n/).filter((l) => l !== ''));
    return new Set(tabs.filter((t) => t.terminalId && ids.has(t.terminalId)).map((t) => t.id));
  },

  async close(_ctx, tab: TerminalTab) {
    if (!tab.terminalId) throw new Error(`tab ${tab.id} has no Ghostty terminal id`);
    if ((await osascript(closeScript(tab.terminalId))).trim() !== 'closed') {
      throw new Error(`Ghostty has no terminal ${tab.terminalId}; the tab is already closed`);
    }
  },
};

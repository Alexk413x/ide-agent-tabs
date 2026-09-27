import * as path from 'node:path';
import type { AgentLaunch } from './profiles';
import { findOnPath } from './profiles';
import { isReservedEnv, PLUGIN_ENV_PREFIX } from './request';

export const TAB_ID_ENV = `${PLUGIN_ENV_PREFIX}ID`;
export const AGENT_ENV = `${PLUGIN_ENV_PREFIX}AGENT`;
export const COMMAND_ENV = `${PLUGIN_ENV_PREFIX}COMMAND`;
export const ARGS_ENV = `${PLUGIN_ENV_PREFIX}ARGS`;
export const ARG_COUNT_ENV = `${PLUGIN_ENV_PREFIX}ARGC`;
export const ARG_ENV_PREFIX = `${PLUGIN_ENV_PREFIX}ARG_`;
export const PROMPT_ENV = `${PLUGIN_ENV_PREFIX}PROMPT`;

export type ShellKind = 'powershell' | 'posix' | 'fish';

export interface LaunchScripts {
  powershell: string;
  posix: string;
  fish: string;
}

export function launchScripts(dir: string): LaunchScripts {
  return {
    powershell: path.join(dir, 'agent.ps1'),
    posix: path.join(dir, 'agent.sh'),
    fish: path.join(dir, 'agent.fish'),
  };
}

export interface ShellPlan {
  path: string;
  kind: ShellKind;
  args: string[];
}

export function shellKind(shell: string): ShellKind | undefined {
  const name = shell.split(/[\\/]/).pop()!.toLowerCase().replace(/\.exe$/, '');
  if (name === 'pwsh' || name === 'powershell') return 'powershell';
  if (name === 'bash' || name === 'zsh') return 'posix';
  if (name === 'fish') return 'fish';
  return undefined;
}

export function powerShellArgs(script: string, isWindows: boolean): string[] {
  return ['-NoLogo', '-NoExit', ...(isWindows ? ['-ExecutionPolicy', 'Bypass'] : []), '-File', script];
}

export function windowsShell(searchPath: string, scripts: LaunchScripts): ShellPlan {
  const shell = findOnPath(searchPath, 'pwsh.exe') ?? findOnPath(searchPath, 'powershell.exe') ?? 'powershell.exe';
  return { path: shell, kind: 'powershell', args: powerShellArgs(scripts.powershell, true) };
}

export function fishQuote(text: string): string {
  return `'${text.replace(/[\\']/g, '\\$&')}'`;
}

// The launcher path travels as $1 and the shell's own path as $0, so neither is parsed as shell code.
// After the agent exits, exec replaces the -c shell with a plain interactive one, so the tab stays usable.
export function unixShell(loginShell: string | undefined, isMac: boolean, scripts: LaunchScripts): ShellPlan {
  const flags = isMac ? ['-l', '-i'] : ['-i'];
  const kind = loginShell ? shellKind(loginShell) : undefined;
  const shell = kind ? loginShell! : '/bin/bash';
  switch (kind) {
    case 'powershell':
      return { path: shell, kind, args: powerShellArgs(scripts.powershell, false) };
    case 'fish':
      return { path: shell, kind, args: [...flags, '-C', `source ${fishQuote(scripts.fish)}`] };
    default:
      return { path: shell, kind: 'posix', args: [...flags, '-c', `. "$1"; exec "$0" ${flags.join(' ')}`, shell, scripts.posix] };
  }
}

export function terminalEnv(
  kind: ShellKind,
  launch: AgentLaunch,
  tabId: string,
  inherited: NodeJS.ProcessEnv = {},
): Record<string, string | null> {
  const env: Record<string, string | null> = { ...launch.env };
  env[TAB_ID_ENV] = tabId;
  env[AGENT_ENV] = launch.agent;
  env[COMMAND_ENV] = launch.command;
  if (launch.args.length > 0) {
    if (kind === 'powershell') {
      env[ARGS_ENV] = JSON.stringify(launch.args);
    } else {
      env[ARG_COUNT_ENV] = String(launch.args.length);
      launch.args.forEach((arg, i) => (env[`${ARG_ENV_PREFIX}${i}`] = arg));
    }
  }
  if (launch.prompt !== undefined) env[PROMPT_ENV] = launch.prompt;
  const own = new Set(Object.keys(env).map(k => k.toUpperCase()));
  for (const name of Object.keys(inherited)) {
    if (isReservedEnv(name) && !own.has(name.toUpperCase())) env[name] = null;
  }
  return env;
}

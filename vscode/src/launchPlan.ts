import { createHash } from 'node:crypto';
import * as fs from 'node:fs';
import * as path from 'node:path';
import type { AgentSettings } from './agentSettings';
import { isFile } from './files';
import {
  CLAUDE_TAB_SETTINGS_FILE,
  claudeTabSettingsOf,
  CodexHttp,
  codexHttpOf,
  codexPythonOf,
  CopilotHttp,
  recordedPython,
} from './mcpHome';
import { isCmdShim } from './pathLookup';
import { AgentLaunch, AgentProfile, CODEX_TAB_ARGS, ORI_PROFILES } from './profiles';
import { writeAtomically } from './registry';
import { BadRequest, isPlainObject, PLUGIN_ENV_PREFIX } from './request';
import { DetectedOri, LaunchVia } from './sharedSettings';

const ORI_COMMAND = 'ori';
const ORI_CMD_UNSAFE = /[|"%^&<>]/;

type Warn = (message: string) => void;

const ignore: Warn = () => {};

export interface LaunchContext {
  prompt?: string;
  args?: string[];
  env?: Record<string, string>;
  model?: string;
  via?: LaunchVia;
  setting: LaunchVia;
  ori: DetectedOri | null;
  windows: boolean;
  searchPath: string;
  python?: readonly string[];
  http?: CodexHttp;
  copilot?: CopilotHttp;
  claudeSettings?: string;
  cwd?: string;
  warn?: Warn;
}

export function launchOf(
  p: AgentProfile,
  prompt?: string,
  callerArgs: string[] = [],
  callerEnv: Record<string, string> = {},
  model?: string,
): AgentLaunch {
  const flag = prompt !== undefined && p.promptFlag !== undefined ? [p.promptFlag] : [];
  const modelArgs = model !== undefined && p.modelFlag !== undefined ? [p.modelFlag, model] : [];
  return {
    agent: p.name,
    command: p.command,
    args: [...p.args, ...modelArgs, ...callerArgs, ...flag],
    prompt,
    env: { ...p.env, ...callerEnv },
    via: 'direct',
  };
}

type OriContext = Pick<LaunchContext, 'ori' | 'windows' | 'searchPath'>;

function oriRefusal(p: AgentProfile, ctx: OriContext, oriArgs: string[]): string | undefined {
  if (ctx.ori === null) return 'Ori is not installed';
  if (!ORI_PROFILES.includes(p.name)) return `Ori does not support ${p.name}`;
  if (!ctx.ori.agents.includes(p.name)) return `Ori does not list ${p.name} as launchable`;
  if (ctx.windows && isCmdShim(p.command, ctx.searchPath) && oriArgs.some(a => ORI_CMD_UNSAFE.test(a))) {
    return 'Ori refuses an argument with | " % ^ & < or > when the agent is a .cmd shim on Windows';
  }
  return undefined;
}

const GOOSE_RUN_ARGS: readonly string[] = ['run', '-s'];
const GOOSE_EMPTY_ARGS: readonly string[] = ['session'];

function withoutPrompt(p: AgentProfile, prompt: string | undefined): AgentProfile {
  const builtinGoose = p.command === 'goose' && p.args.length === GOOSE_RUN_ARGS.length && p.args.every((a, i) => a === GOOSE_RUN_ARGS[i]);
  return prompt === undefined && builtinGoose ? { ...p, args: [...GOOSE_EMPTY_ARGS] } : p;
}

export function wouldUseOri(profile: AgentProfile, ctx: OriContext & Pick<LaunchContext, 'setting'>): boolean {
  return ctx.setting === 'ori' && oriRefusal(profile, ctx, [profile.name, ...profile.args]) === undefined;
}

export function launchContext(
  settings: AgentSettings,
  profile: AgentProfile,
  environment: Pick<LaunchContext, 'windows' | 'searchPath'>,
  extra: Partial<LaunchContext> & { tab?: string } = {},
): LaunchContext {
  const { tab, ...rest } = extra;
  const { windows } = environment;
  const python = recordedPython(settings.home, windows);
  return {
    setting: settings.shared().launchVia,
    ori: settings.detected().ori,
    python: codexPythonOf(python, windows),
    http: tab === undefined ? undefined : codexHttpOf(python, settings.home, windows, tab, profile.name),
    claudeSettings: claudeTabSettingsOf(python, settings.home),
    ...environment,
    ...rest,
  };
}

export function planLaunch(profile: AgentProfile, ctx: LaunchContext): AgentLaunch {
  const unplanned = withoutPrompt(profile, ctx.prompt);
  const codexArgs = withCodexHttp(unplanned.args, ctx.http) ?? withCodexPython(unplanned.args, ctx.python);
  const codex = { ...unplanned, ...withCopilotHttp(unplanned.command, codexArgs, unplanned.env, ctx.copilot, ctx.cwd) };
  const settled = withClaudeSettings(codex.command, codex.args, ctx.args ?? [], ctx.claudeSettings, ctx.cwd, ctx.warn ?? ignore);
  const callerArgs = settled.callerArgs;
  const p = { ...codex, args: settled.args };
  const callerEnv = ctx.env ?? {};
  if ((ctx.via ?? ctx.setting) === 'ori') {
    const flag = ctx.prompt !== undefined && p.promptFlag !== undefined ? [p.promptFlag] : [];
    const args = [p.name, ...(ctx.model !== undefined ? ['--model', ctx.model] : []), ...p.args, ...callerArgs, ...flag];
    const refusal = oriRefusal(p, ctx, ctx.prompt !== undefined ? [...args, ctx.prompt] : args);
    if (refusal === undefined) {
      return { agent: p.name, command: ORI_COMMAND, args, prompt: ctx.prompt, env: { ...p.env, ...callerEnv }, via: 'ori' };
    }
    if (ctx.via === 'ori') throw new BadRequest(`${p.name} can't launch through Ori: ${refusal}`);
  }
  if (ctx.model !== undefined && p.modelFlag === undefined) {
    throw new BadRequest(`${p.name} has no model option; open it without model, or set modelFlag for it in agents.json`);
  }
  return launchOf(p, ctx.prompt, callerArgs, callerEnv, ctx.model);
}

const CODEX_SERVER_ARG = CODEX_TAB_ARGS[2];
const CODEX_SERVER_HEAD = "mcp_servers.ide-agent-tabs={ command = 'python3', args = [";

export function withCodexPython(args: readonly string[], python: readonly string[] | undefined): string[] {
  if (python === undefined || python.length === 0) return [...args];
  const head = python.slice(1).map(a => `'${a}', `).join('');
  const arg = `mcp_servers.ide-agent-tabs={ command = '${python[0]}', args = [${head}${CODEX_SERVER_ARG.slice(CODEX_SERVER_HEAD.length)}`;
  return args.map(a => (a === CODEX_SERVER_ARG ? arg : a));
}

export function codexHttpArg(http: CodexHttp): string {
  const helper = `${http.python} -I -S ${http.headers} --tab ${http.tab} --agent ${http.agent} --port ${http.port} --home ${http.home}`;
  return (
    `mcp_servers.ide-agent-tabs={ url = 'http://127.0.0.1:${http.port}/mcp', http_headers_helper = '${helper}', ` +
    'startup_timeout_sec = 30, tool_timeout_sec = 660 }'
  );
}

export function withCodexHttp(args: readonly string[], http: CodexHttp | undefined): string[] | undefined {
  if (http === undefined || !args.includes(CODEX_SERVER_ARG)) return undefined;
  const arg = codexHttpArg(http);
  return args.map(a => (a === CODEX_SERVER_ARG ? arg : a));
}

export const CWD_ENV = `${PLUGIN_ENV_PREFIX}CWD`;
const MAX_CWD_CHARS = 1024;
const COPILOT_COMMAND = /^(?:.*[\\/])?copilot(?:\.(?:exe|cmd|bat|ps1))?$/i;
const CWD_SAFE = /^[A-Za-z0-9_.~/: \\-]$/;

export function isCopilotCommand(command: string): boolean {
  return COPILOT_COMMAND.test(command);
}

export function cwdHeader(cwd: string): string {
  let out = '';
  for (const c of cwd) {
    out += CWD_SAFE.test(c) ? c : [...Buffer.from(c, 'utf8')].map(b => `%${b.toString(16).toUpperCase().padStart(2, '0')}`).join('');
  }
  return out;
}

// Same rules as with_copilot_http in claude-plugin/mcp/src/ide_agent_tabs/profiles.py. Copilot CLI adds the servers in
// --additional-mcp-config to its own for one run, and an entry with the registered server's name replaces it, so the
// tab starts no stdio server.
export function withCopilotHttp(
  command: string,
  args: readonly string[],
  env: Record<string, string>,
  copilot: CopilotHttp | undefined,
  cwd: string | undefined,
): { args: string[]; env: Record<string, string> } {
  if (copilot === undefined || !isCopilotCommand(command)) return { args: [...args], env };
  const folder: Record<string, string> = cwd !== undefined && cwd.length > 0 && cwd.length <= MAX_CWD_CHARS ? { [CWD_ENV]: cwdHeader(cwd) } : {};
  return { args: [...args, '--additional-mcp-config', `@${copilot.config}`], env: { ...env, ...folder } };
}

const CLAUDE_COMMAND = /^(?:.*[\\/])?claude(?:\.(?:exe|cmd|bat|ps1))?$/i;

function withoutSettings(args: readonly string[]): { kept: string[]; value: string | undefined } {
  const kept: string[] = [];
  let value: string | undefined;
  for (let i = 0; i < args.length; i++) {
    const a = args[i]!;
    if (a === '--settings' && i + 1 < args.length) {
      value = args[++i];
    } else if (a.startsWith('--settings=')) {
      value = a.slice('--settings='.length);
    } else {
      kept.push(a);
    }
  }
  return { kept, value };
}

function readSettings(value: string, cwd: string | undefined, warn: Warn): Record<string, unknown> | undefined {
  let data: unknown;
  const inline = value.trim().startsWith('{');
  try {
    if (inline) {
      data = JSON.parse(value);
    } else {
      const absolute = path.posix.isAbsolute(value) || path.win32.isAbsolute(value);
      if (!absolute && cwd === undefined) return undefined;
      data = JSON.parse(fs.readFileSync(absolute ? value : path.join(cwd!, value), 'utf8'));
    }
  } catch (e) {
    if ((e as NodeJS.ErrnoException).code !== 'ENOENT') {
      warn(`Could not read ${inline ? 'the inline --settings value' : value}, so the tab hooks are not merged into it: ${(e as Error).message}`);
    }
    return undefined;
  }
  return isPlainObject(data) ? data : undefined;
}

function mergedClaudeSettings(settings: string, value: string, cwd: string | undefined, warn: Warn): string | undefined {
  const user = readSettings(value, cwd, warn);
  const ours = readSettings(settings, undefined, warn);
  if (user === undefined || ours === undefined || !isPlainObject(ours.hooks)) return undefined;
  const hooks = user.hooks ?? {};
  if (!isPlainObject(hooks) || Object.keys(ours.hooks).some(e => !Array.isArray(hooks[e] ?? []))) return undefined;
  const merged: Record<string, unknown> = { ...hooks };
  for (const [event, groups] of Object.entries(ours.hooks)) merged[event] = [...((hooks[event] as unknown[]) ?? []), ...(groups as unknown[])];
  const text = `${JSON.stringify({ ...user, hooks: merged }, null, 2)}\n`;
  const digest = createHash('sha256').update(text, 'utf8').digest('hex').slice(0, 16);
  const file = path.join(path.dirname(settings), `${CLAUDE_TAB_SETTINGS_FILE.replace(/\.json$/, '')}-${digest}.json`);
  try {
    if (!isFile(file) || fs.readFileSync(file, 'utf8') !== text) writeAtomically(file, text);
  } catch (e) {
    warn(`Could not write ${file}, so the tab hooks are not merged into the --settings value: ${(e as Error).message}`);
    return undefined;
  }
  return file;
}

// Same rules as with_claude_settings in claude-plugin/mcp/src/ide_agent_tabs/profiles.py. claude reads only the last
// --settings, so a tab whose profile or request passes its own gets one file that merges those settings with the tab's
// hooks, and keeps the user's flag only when that merge is impossible.
function withClaudeSettings(
  command: string,
  args: readonly string[],
  callerArgs: readonly string[],
  settings: string | undefined,
  cwd: string | undefined,
  warn: Warn,
): { args: string[]; callerArgs: string[] } {
  const unchanged = { args: [...args], callerArgs: [...callerArgs] };
  if (settings === undefined || !CLAUDE_COMMAND.test(command)) return unchanged;
  const own = withoutSettings(args);
  const caller = withoutSettings(callerArgs);
  const value = caller.value ?? own.value;
  if (value === undefined) return { args: [...args, '--settings', settings], callerArgs: [...callerArgs] };
  const merged = mergedClaudeSettings(settings, value, cwd, warn);
  if (merged === undefined) return unchanged;
  return { args: [...own.kept, '--settings', merged], callerArgs: caller.kept };
}

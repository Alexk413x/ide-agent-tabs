import { ConfigError, GOOSE_EMPTY_ARGS, GOOSE_RUN_ARGS, launchOf, type AgentLaunch, type AgentProfile, type Env } from './profiles.js';

export type LaunchVia = 'direct' | 'ori';
export const ORI_AGENTS: readonly string[] = ['claude', 'codex', 'grok', 'hermes', 'opencode', 'pi', 'prime-agent'];
export const MODEL_PATTERN = /^[A-Za-z0-9._:/@+-]{1,200}$/;
// Ori refuses to pass these to a .cmd shim such as the npm codex.cmd.
const ORI_CMD_SPECIALS = /["%^&|<>]/;

export interface OriDetection {
  path: string;
  version: string;
  agents: string[];
}

export interface LaunchRequest {
  prompt?: string;
  args: string[];
  env: Env;
  model?: string;
  via?: LaunchVia;
  launchVia: LaunchVia;
  ori: OriDetection | null | undefined;
  platform: NodeJS.Platform;
  cmdShim?: boolean;
}

export interface LaunchPlan {
  launch: AgentLaunch;
  via: LaunchVia;
}

function oriRefusal(p: AgentProfile, r: LaunchRequest): string | undefined {
  if (!r.ori) return "Ori isn't installed";
  if (!ORI_AGENTS.includes(p.name)) return `Ori has no ${p.name} launcher`;
  if (!r.ori.agents.includes(p.name)) return `Ori lists ${p.name} as not installed`;
  const passed = [...p.args, ...r.args, ...(r.model !== undefined ? [r.model] : []), ...(r.prompt !== undefined ? [r.prompt] : [])];
  if (r.platform === 'win32' && r.cmdShim !== false && passed.some((a) => ORI_CMD_SPECIALS.test(a))) {
    return `an argument holds one of " % ^ & | < >, which Ori won't pass to a cmd.exe shim on Windows`;
  }
  return undefined;
}

const sameArgs = (a: readonly string[], b: readonly string[]) => a.length === b.length && a.every((x, i) => x === b[i]);

function withoutPrompt(p: AgentProfile, prompt: string | undefined): AgentProfile {
  if (prompt !== undefined || p.command !== 'goose' || !sameArgs(p.args, GOOSE_RUN_ARGS)) return p;
  return { ...p, args: [...GOOSE_EMPTY_ARGS] };
}

export function planLaunch(profile: AgentProfile, r: LaunchRequest): LaunchPlan {
  const p = withoutPrompt(profile, r.prompt);
  if (r.model !== undefined && !MODEL_PATTERN.test(r.model)) throw new ConfigError(`model must match ${MODEL_PATTERN.source}`);
  let via: LaunchVia = r.via ?? r.launchVia;
  if (via === 'ori') {
    const refusal = oriRefusal(p, r);
    if (refusal !== undefined && r.via === 'ori') throw new ConfigError(`${p.name} can't launch through Ori: ${refusal}`);
    if (refusal !== undefined) via = 'direct';
  }
  if (via === 'ori') {
    const inner = launchOf(p, r.prompt, r.args, r.env);
    const model = r.model !== undefined ? ['--model', r.model] : [];
    return { via, launch: { ...inner, command: 'ori', args: [p.name, ...model, ...inner.args] } };
  }
  if (r.model !== undefined && p.modelFlag === undefined) {
    throw new ConfigError(`${p.name} has no model option; open it without model, or set modelFlag for it in agents.json`);
  }
  const model = r.model !== undefined ? [p.modelFlag!, r.model] : [];
  return { via, launch: launchOf({ ...p, args: [...p.args, ...model] }, r.prompt, r.args, r.env) };
}

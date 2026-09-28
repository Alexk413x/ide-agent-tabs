import type { AgentLaunch, Env } from './profiles.js';

export const SPEC_VERSION = 1;
export const POSIX_SPEC_MAGIC = 'ide-agent-tabs-spec-1';

export interface LaunchSpec {
  id: string;
  agent: string;
  cwd: string;
  command: string;
  args: string[];
  prompt?: string;
  env: Env;
  pidFile?: string;
}

export function launchSpec(id: string, cwd: string, launch: AgentLaunch, pidFile?: string): LaunchSpec {
  return {
    id,
    agent: launch.agent,
    cwd,
    command: launch.command,
    args: launch.args,
    ...(launch.prompt !== undefined ? { prompt: launch.prompt } : {}),
    env: launch.env,
    ...(pidFile !== undefined ? { pidFile } : {}),
  };
}

// env is a list of pairs, not an object: Windows PowerShell's ConvertFrom-Json fails on keys that differ
// only in case, which a caller's env may hold.
export function powerShellSpec(spec: LaunchSpec): string {
  return JSON.stringify({
    version: SPEC_VERSION,
    id: spec.id,
    agent: spec.agent,
    cwd: spec.cwd,
    command: spec.command,
    args: spec.args,
    prompt: spec.prompt ?? null,
    env: Object.entries(spec.env).map(([name, value]) => ({ name, value })),
    pidFile: spec.pidFile ?? null,
  });
}

// The pid file is an optional last field; the launchers treat any field after the prompt as the pid file.
export function posixSpec(spec: LaunchSpec): Buffer {
  const env = Object.entries(spec.env);
  const fields = [
    POSIX_SPEC_MAGIC,
    spec.id,
    spec.agent,
    spec.cwd,
    spec.command,
    String(env.length),
    ...env.flat(),
    String(spec.args.length),
    ...spec.args,
    ...(spec.prompt !== undefined ? ['1', spec.prompt] : ['0']),
    ...(spec.pidFile !== undefined ? [spec.pidFile] : []),
  ];
  if (fields.some((f) => f.includes('\0'))) throw new Error('launch spec fields must not hold a NUL');
  return Buffer.from(fields.map((f) => `${f}\0`).join(''), 'utf8');
}

export function parsePosixSpec(bytes: Buffer): LaunchSpec {
  const fields = bytes.toString('utf8').split('\0');
  fields.pop();
  let i = 0;
  const next = () => {
    if (i >= fields.length) throw new Error('truncated launch spec');
    return fields[i++]!;
  };
  if (next() !== POSIX_SPEC_MAGIC) throw new Error('not a launch spec');
  const id = next();
  const agent = next();
  const cwd = next();
  const command = next();
  const env: [string, string][] = [];
  for (let n = Number(next()); n > 0; n--) env.push([next(), next()]);
  const args: string[] = [];
  for (let n = Number(next()); n > 0; n--) args.push(next());
  const prompt = next() === '1' ? next() : undefined;
  const pidFile = i < fields.length ? next() : undefined;
  return {
    id,
    agent,
    cwd,
    command,
    args,
    ...(prompt !== undefined ? { prompt } : {}),
    env: Object.fromEntries(env),
    ...(pidFile !== undefined ? { pidFile } : {}),
  };
}

const POSIX_ENV_NAME = /^[A-Za-z_][A-Za-z0-9_]*$/;

export function checkPosixEnvNames(env: Env): void {
  const bad = Object.keys(env).filter((n) => !POSIX_ENV_NAME.test(n));
  if (bad.length > 0) {
    throw new Error(`env names must be shell identifiers for a terminal tab: ${bad.join(', ')}`);
  }
}

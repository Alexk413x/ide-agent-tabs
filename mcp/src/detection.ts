import { existsSync } from 'node:fs';
import path from 'node:path';
import { readTextIfExists, writeAtomically } from './files.js';
import { findOnPath } from './installed.js';
import type { OriDetection } from './launchPlan.js';
import { run, type RunResult } from './process.js';
import { detectPowerShells, systemProbe, type DetectedShell, type ShellProbe } from './terminals/powershell.js';
import type { TerminalContext, TerminalDriver } from './terminals/types.js';

export const DETECTED_FILE = 'detected.json';

export interface DetectedTerminal {
  id: string;
  name: string;
}

export interface Detection {
  version: 1;
  detectedAt: string;
  platform: NodeJS.Platform;
  terminals: DetectedTerminal[];
  shells: DetectedShell[];
  ori?: OriDetection | null;
}

export type OriRunner = (exe: string, args: string[]) => Promise<RunResult>;

export interface DetectOptions {
  home: string;
  platform: NodeJS.Platform;
  env: NodeJS.ProcessEnv;
  terminals: DetectedTerminal[];
  probe?: ShellProbe;
  oriRunner?: OriRunner;
  now?: Date;
}

function isShell(value: unknown): value is DetectedShell {
  const s = value as DetectedShell;
  return (
    typeof s === 'object' &&
    s !== null &&
    typeof s.path === 'string' &&
    typeof s.label === 'string' &&
    typeof s.version === 'string' &&
    ['path', 'msi', 'store', 'preview', 'windows'].includes(s.source)
  );
}

export function parseDetection(text: string | undefined): Detection | undefined {
  if (text === undefined) return undefined;
  try {
    const json = JSON.parse(text) as Partial<Detection>;
    if (json.version !== 1 || typeof json.detectedAt !== 'string' || !Array.isArray(json.shells) || !Array.isArray(json.terminals)) {
      return undefined;
    }
    const ori = json.ori;
    const validOri =
      typeof ori?.path === 'string' && typeof ori.version === 'string' && Array.isArray(ori.agents) && ori.agents.every((a) => typeof a === 'string');
    return { ...json, shells: json.shells.filter(isShell), ori: validOri ? ori : null } as Detection;
  } catch {
    return undefined;
  }
}

export async function readDetection(home: string): Promise<Detection | undefined> {
  return parseDetection(await readTextIfExists(path.join(home, DETECTED_FILE)).catch(() => undefined));
}

export async function availableTerminals(drivers: TerminalDriver[], ctx: TerminalContext): Promise<DetectedTerminal[]> {
  const flags = await Promise.all(drivers.map((d) => d.available(ctx).catch(() => false)));
  return drivers.filter((_, i) => flags[i]).map((d) => ({ id: d.name, name: d.label }));
}

export function findOri(env: NodeJS.ProcessEnv, platform: NodeJS.Platform): string | undefined {
  const exe = platform === 'win32' ? 'ori.exe' : 'ori';
  const onPath = findOnPath(env.PATH ?? env.Path ?? '', exe);
  if (onPath) return onPath;
  const home = platform === 'win32' ? env.USERPROFILE : env.HOME;
  const installed = home ? path.join(home, '.local', 'bin', exe) : undefined;
  return installed && existsSync(installed) ? installed : undefined;
}

const defaultOriRunner: OriRunner = (exe, args) => run(exe, args, { timeoutMs: 15_000 });

function dataOf(stdout: string): Record<string, unknown> | undefined {
  const json = JSON.parse(stdout.slice(Math.max(0, stdout.indexOf('{')))) as { ok?: unknown; data?: unknown };
  return json.ok === true && typeof json.data === 'object' && json.data !== null ? (json.data as Record<string, unknown>) : undefined;
}

export async function detectOri(exe: string | undefined, runner: OriRunner = defaultOriRunner): Promise<OriDetection | null> {
  if (exe === undefined) return null;
  try {
    const [version, harnesses] = await Promise.all([runner(exe, ['--version', '--json']), runner(exe, ['harness', 'list', '--json'])]);
    const launchable = dataOf(harnesses.stdout)?.launchable;
    const agents = Array.isArray(launchable)
      ? launchable.filter((h) => h?.installed === true && typeof h.kind === 'string').map((h) => h.kind as string)
      : [];
    const v = dataOf(version.stdout)?.version;
    return { path: exe, version: typeof v === 'string' ? v.split('+')[0]! : '', agents };
  } catch {
    return { path: exe, version: '', agents: [] };
  }
}

export async function detect(o: DetectOptions): Promise<Detection> {
  const previous = o.platform === 'win32' ? await readDetection(o.home) : undefined;
  const [shells, ori] = await Promise.all([
    o.platform === 'win32' ? detectPowerShells(o.probe ?? systemProbe(o.env, true), previous) : Promise.resolve([]),
    detectOri(findOri(o.env, o.platform), o.oriRunner),
  ]);
  return { version: 1, detectedAt: (o.now ?? new Date()).toISOString(), platform: o.platform, terminals: o.terminals, shells, ori };
}

export async function writeDetection(home: string, detection: Detection): Promise<void> {
  await writeAtomically(path.join(home, DETECTED_FILE), `${JSON.stringify(detection, null, 2)}\n`);
}

export async function refreshDetectionFile(o: { home: string; platform: NodeJS.Platform; env: NodeJS.ProcessEnv; drivers: TerminalDriver[] }): Promise<Detection> {
  const ctx: TerminalContext = { home: o.home, scriptsDir: '', pathVar: o.env.PATH ?? o.env.Path ?? '', env: o.env };
  const detection = await detect({ home: o.home, platform: o.platform, env: o.env, terminals: await availableTerminals(o.drivers, ctx) });
  await writeDetection(o.home, detection);
  return detection;
}

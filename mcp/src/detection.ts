import path from 'node:path';
import { readTextIfExists, writeAtomically } from './files.js';
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
}

export interface DetectOptions {
  home: string;
  platform: NodeJS.Platform;
  env: NodeJS.ProcessEnv;
  terminals: DetectedTerminal[];
  probe?: ShellProbe;
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
    return { ...json, shells: json.shells.filter(isShell) } as Detection;
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

export async function detect(o: DetectOptions): Promise<Detection> {
  const previous = o.platform === 'win32' ? await readDetection(o.home) : undefined;
  const shells = o.platform === 'win32' ? await detectPowerShells(o.probe ?? systemProbe(o.env, true), previous) : [];
  return { version: 1, detectedAt: (o.now ?? new Date()).toISOString(), platform: o.platform, terminals: o.terminals, shells };
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

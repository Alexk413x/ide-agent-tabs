import { timingSafeEqual } from 'node:crypto';
import * as path from 'node:path';
import { isDirectory } from './files';

export const MAX_PROMPT_CHARS = 30_000;
export const MAX_ENTRIES = 64;
export const MAX_INPUT_CHARS = 500;
export const PLUGIN_ENV_PREFIX = 'IDE_AGENT_TABS_';
const STARTUP_ENV = 'JEDITERM_SOURCE';

export class BadRequest extends Error {}

interface Refusal {
  status: number;
  message: string;
}

type HeaderLookup = (name: string) => string | undefined;

export function isLoopback(address: string | undefined): boolean {
  if (!address) return false;
  const v4 = address.startsWith('::ffff:') ? address.slice('::ffff:'.length) : address;
  if (/^127\.\d{1,3}\.\d{1,3}\.\d{1,3}$/.test(v4)) return true;
  return address === '::1' || address === '0:0:0:0:0:0:0:1';
}

export function checkAdmission(remote: string | undefined, method: string | undefined, token: string, header: HeaderLookup): Refusal | undefined {
  if (!isLoopback(remote)) return { status: 403, message: 'loopback requests only' };
  if (method !== 'POST') return { status: 405, message: 'use POST' };
  if (header('origin') !== undefined || header('referer') !== undefined) {
    return { status: 403, message: 'browser requests are refused' };
  }
  if (!bearerMatches(header('authorization'), token)) {
    return { status: 401, message: 'missing or wrong token; send Authorization: Bearer <token>' };
  }
  if (header('content-type')?.split(';')[0].trim().toLowerCase() !== 'application/json') {
    return { status: 415, message: 'Content-Type must be application/json' };
  }
  return undefined;
}

export function bearerMatches(authorization: string | undefined, token: string): boolean {
  if (!token || authorization === undefined) return false;
  const trimmed = authorization.trim();
  const space = trimmed.indexOf(' ');
  if (space < 0 || trimmed.slice(0, space).toLowerCase() !== 'bearer') return false;
  const given = Buffer.from(trimmed.slice(space + 1).trim(), 'utf8');
  const expected = Buffer.from(token, 'utf8');
  return given.length === expected.length && timingSafeEqual(given, expected);
}

export function isReservedEnv(name: string): boolean {
  const upper = name.toUpperCase();
  return upper.startsWith(PLUGIN_ENV_PREFIX) || upper.startsWith(STARTUP_ENV);
}

const isBlank = (s: string) => s.trim() === '';

export function checkEnv(env: Record<string, string>, field: string): void {
  const entries = Object.entries(env);
  if (entries.length > MAX_ENTRIES) throw new BadRequest(`${field} exceeds ${MAX_ENTRIES} entries`);
  for (const [name, value] of entries) {
    if (isBlank(name) || /[=\s\0]/.test(name)) throw new BadRequest(`${field} name is not a valid variable name: '${name}'`);
    if (isReservedEnv(name)) throw new BadRequest(`${field} name ${name} is reserved by the plugin`);
    if (value.length > MAX_PROMPT_CHARS || value.includes('\0')) {
      throw new BadRequest(`${field} ${name} is longer than ${MAX_PROMPT_CHARS} characters or holds a NUL`);
    }
  }
}

type JsonObject = Record<string, unknown>;

export const isPlainObject = (v: unknown): v is JsonObject => typeof v === 'object' && v !== null && !Array.isArray(v);

export function parseObject(text: string, what = 'body'): JsonObject {
  let json: unknown;
  try {
    json = JSON.parse(text);
  } catch {
    throw new BadRequest(`${what} is not JSON`);
  }
  if (!isPlainObject(json)) throw new BadRequest(`${what} must be a JSON object`);
  return json;
}

export function optString(obj: JsonObject, key: string, field = key): string | undefined {
  const value = obj[key];
  if (value === undefined || value === null) return undefined;
  if (typeof value !== 'string') throw new BadRequest(`${field} must be a string`);
  return value;
}

export function optStringList(obj: JsonObject, key: string, field = key): string[] {
  const value = obj[key];
  if (value === undefined || value === null) return [];
  if (!Array.isArray(value) || value.some(v => typeof v !== 'string')) throw new BadRequest(`${field} must be an array of strings`);
  return value as string[];
}

export function optStringMap(obj: JsonObject, key: string, field = key): Record<string, string> {
  const value = obj[key];
  if (value === undefined || value === null) return {};
  if (!isPlainObject(value)) throw new BadRequest(`${field} must be an object of strings`);
  const result: Record<string, string> = {};
  for (const [k, v] of Object.entries(value)) {
    if (typeof v !== 'string') throw new BadRequest(`${field}.${k} must be a string`);
    result[k] = v;
  }
  return result;
}

function optBoolean(obj: JsonObject, key: string, field = key): boolean | undefined {
  const value = obj[key];
  if (value === undefined || value === null) return undefined;
  if (typeof value !== 'boolean') throw new BadRequest(`${field} must be true or false`);
  return value;
}

function requireId(obj: JsonObject): string {
  const id = optString(obj, 'id');
  if (id === undefined || isBlank(id)) throw new BadRequest('id is required');
  return id;
}

export function parseCloseId(body: string): string {
  return requireId(parseObject(body));
}

const BUNDLE_SEGMENT = /\.(app|bundle|framework|pkg|plugin|prefPane)$/i;

export interface RevealDeps {
  realpath(target: string): string | undefined;
  isDirectory(target: string): boolean;
  platform: NodeJS.Platform;
}

export function parseRevealPath(body: string): string {
  const target = optString(parseObject(body), 'path');
  if (target === undefined || isBlank(target)) throw new BadRequest('path is required');
  if (/\p{Cc}/u.test(target)) throw new BadRequest('path must have no control characters');
  if (!path.isAbsolute(target)) throw new BadRequest(`path must be absolute: ${target}`);
  return target;
}

// Only a folder this window already shows (a workspace folder or an agent tab's folder) is revealed, and
// never a macOS bundle: the OS opens a bundle by launching it.
export function checkRevealTarget(target: string, known: readonly string[], deps: RevealDeps): string {
  const real = deps.realpath(target);
  if (real === undefined || !deps.isDirectory(real)) throw new BadRequest(`not a folder on this machine: ${target}`);
  if (deps.platform === 'darwin' && real.split(/[\\/]/).some(segment => BUNDLE_SEGMENT.test(segment))) {
    throw new BadRequest(`refused: ${target} is inside a macOS bundle`);
  }
  const key = (p: string) => {
    const trimmed = p.replace(/[\\/]+$/, '') || p;
    return deps.platform === 'win32' || deps.platform === 'darwin' ? trimmed.toLowerCase() : trimmed;
  };
  const allowed = new Set(known.flatMap(k => (deps.realpath(k) === undefined ? [] : [key(deps.realpath(k)!)])));
  if (!allowed.has(key(real))) throw new BadRequest(`refused: ${target} is not a folder of this window or its agent tabs`);
  return real;
}

interface InputRequest {
  id: string;
  text: string;
}

export function parseInput(body: string): InputRequest {
  const obj = parseObject(body);
  const id = requireId(obj);
  const text = optString(obj, 'text');
  if (text === undefined || isBlank(text)) throw new BadRequest('text is required');
  if (text.length > MAX_INPUT_CHARS) throw new BadRequest(`text exceeds ${MAX_INPUT_CHARS} characters`);
  if (/\p{Cc}/u.test(text)) throw new BadRequest('text must be one line with no control characters');
  return { id, text };
}

export function parseEmpty(body: string): void {
  if (!isBlank(body)) parseObject(body);
}

export function isAbsolutePath(p: string, windows = process.platform === 'win32'): boolean {
  if (windows) return /^[A-Za-z]:[\\/]/.test(p) || /^[\\/]{2}[^\\/]/.test(p);
  return p.startsWith('/');
}

const MODEL_PATTERN = /^[A-Za-z0-9._:/@+-]{1,200}$/;
export const ORI_PREFIX = 'ori:';

export interface OpenRequest {
  path: string;
  prompt?: string;
  args: string[];
  env: Record<string, string>;
  agent?: string;
  model?: string;
  via?: 'ori' | 'direct';
  focus: boolean;
}

interface OpenFields {
  path?: string;
  prompt?: string;
  args?: string[];
  env?: Record<string, string>;
  agent?: string;
  model?: string;
  via?: string;
  focus?: boolean;
}

export function parseOpenRequest(body: string): OpenRequest {
  const obj = parseObject(body);
  return openRequestOf({
    path: optString(obj, 'path'),
    prompt: optString(obj, 'prompt'),
    args: optStringList(obj, 'args'),
    env: optStringMap(obj, 'env'),
    agent: optString(obj, 'agent'),
    model: optString(obj, 'model'),
    via: optString(obj, 'via'),
    focus: optBoolean(obj, 'focus'),
  });
}

export function openRequestOf(fields: OpenFields): OpenRequest {
  const { path: dir, prompt, args = [], env = {}, agent, model, via, focus = false } = fields;
  if (dir === undefined || isBlank(dir)) throw new BadRequest('path is required');
  if (dir.includes('\0') || !isAbsolutePath(dir)) throw new BadRequest('path must be absolute');
  if (!isDirectory(dir)) throw new BadRequest(`path is not a directory: ${dir}`);
  if (prompt !== undefined && prompt.length > MAX_PROMPT_CHARS) throw new BadRequest(`prompt exceeds ${MAX_PROMPT_CHARS} characters`);
  if (args.length > MAX_ENTRIES) throw new BadRequest(`args exceeds ${MAX_ENTRIES} entries`);
  if (args.some(a => a.length > MAX_PROMPT_CHARS)) throw new BadRequest(`an arg exceeds ${MAX_PROMPT_CHARS} characters`);
  if (args.some(a => a.includes('\0'))) throw new BadRequest('an arg holds a NUL');
  if (prompt !== undefined && prompt.includes('\0')) throw new BadRequest('prompt holds a NUL');
  checkEnv(env, 'env');
  if (agent !== undefined && isBlank(agent)) throw new BadRequest('agent must not be blank');
  if (model !== undefined && !MODEL_PATTERN.test(model)) {
    throw new BadRequest('model must be 1 to 200 characters from letters, digits and . _ : / @ + -');
  }
  if (via !== undefined && via !== 'ori' && via !== 'direct') throw new BadRequest("via must be 'ori' or 'direct'");
  const ori = agent !== undefined && agent.startsWith(ORI_PREFIX);
  if (ori && via === 'direct') throw new BadRequest(`agent ${agent} opens through Ori, so via must be 'ori' or left out`);
  if (ori && isBlank(agent.slice(ORI_PREFIX.length))) throw new BadRequest(`agent ${agent} names no agent after ${ORI_PREFIX}`);
  return {
    path: stripTrailingSeparator(path.normalize(dir)),
    prompt: prompt !== undefined && !isBlank(prompt) ? prompt : undefined,
    args,
    env,
    agent: ori ? agent.slice(ORI_PREFIX.length) : agent,
    model,
    via: ori ? 'ori' : via,
    focus,
  };
}

function stripTrailingSeparator(p: string, paths: path.PlatformPath = path): string {
  const root = paths.parse(p).root;
  return p.length > root.length ? p.replace(/[\\/]+$/, '') : p;
}

export function closestBase(target: string, bases: readonly string[], windows = process.platform === 'win32'): number | undefined {
  const paths = windows ? path.win32 : path.posix;
  const key = (p: string) => {
    const normal = stripTrailingSeparator(paths.normalize(p), paths);
    return windows ? normal.toLowerCase() : normal;
  };
  const sep = windows ? '\\' : '/';
  const t = key(target);
  let best: number | undefined;
  let bestLength = -1;
  bases.forEach((base, i) => {
    const b = key(base);
    const inside = t === b || t.startsWith(b.endsWith(sep) ? b : b + sep);
    if (inside && b.length > bestLength) {
      best = i;
      bestLength = b.length;
    }
  });
  return best;
}

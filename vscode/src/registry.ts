import { randomBytes } from 'node:crypto';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';

export const PROTOCOL_VERSION = 1;
export const HOME_ENV = 'IDE_AGENT_TABS_HOME';
export const ENDPOINT_BASE = '/ide-agent-tabs';

export function ideAgentTabsHome(env: NodeJS.ProcessEnv = process.env): string {
  const override = env[HOME_ENV];
  return override !== undefined && override.trim() !== '' ? override : path.join(os.homedir(), '.ide-agent-tabs');
}

export function newToken(): string {
  return randomBytes(32).toString('hex');
}

export function newWindowId(): string {
  return randomBytes(4).toString('hex');
}

export function endpointFileName(pid: number, windowId: string): string {
  return `vscode-${pid}-${windowId}.json`;
}

export interface Endpoint {
  product: string;
  version: string;
  pid: number;
  url: string;
  token: string;
}

export function endpointJson(e: Endpoint): string {
  return JSON.stringify({
    protocol: PROTOCOL_VERSION,
    ide: 'vscode',
    product: e.product,
    version: e.version,
    pid: e.pid,
    url: e.url,
    token: e.token,
  });
}

const isPosix = process.platform !== 'win32';

// Windows gets no ACL change: the user profile's inherited permissions already exclude other users.
export function writeAtomically(target: string, content: string, secret = false): string {
  const dir = path.dirname(path.resolve(target));
  const secure = secret && isPosix;
  fs.mkdirSync(dir, { recursive: true, mode: secure ? 0o700 : undefined });
  if (secure) fs.chmodSync(dir, 0o700);
  const temp = path.join(dir, `${path.basename(target)}.${randomBytes(6).toString('hex')}.tmp`);
  try {
    fs.writeFileSync(temp, content, { encoding: 'utf8', mode: secure ? 0o600 : 0o666, flag: 'wx' });
    fs.renameSync(temp, target);
  } catch (e) {
    fs.rmSync(temp, { force: true });
    throw e;
  }
  return target;
}

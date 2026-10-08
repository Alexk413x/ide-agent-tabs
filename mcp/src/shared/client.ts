import { spawn } from 'node:child_process';
import http from 'node:http';
import os from 'node:os';
import { terminalEnvironment } from '../terminals/processes.js';
import { compareVersions } from '../version.js';
import { readState, readToken, SERVICE, type Health, type ServerState } from './state.js';

export const START_WAIT_MS = 3_000;
const POLL_MS = 50;
const HEALTH_TIMEOUT_MS = 1_000;
// A small young generation keeps the long-lived server about 6 MB lighter; its tool calls wait on I/O, not on the GC.
export const SERVER_NODE_FLAGS = ['--max-semi-space-size=1'];

export type Probe = { kind: 'free' } | { kind: 'other' } | { kind: 'ours'; health: Health };

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

function request(port: number, method: string, route: string, token?: string, timeoutMs = HEALTH_TIMEOUT_MS, payload = ''): Promise<{ status: number; body: string } | undefined> {
  return new Promise((resolve) => {
    const req = http.request(
      {
        host: '127.0.0.1',
        port,
        method,
        path: route,
        headers: {
          host: `127.0.0.1:${port}`,
          ...(token ? { authorization: `Bearer ${token}` } : {}),
          ...(payload !== '' ? { 'content-type': 'application/json' } : {}),
          'content-length': Buffer.byteLength(payload),
        },
        timeout: timeoutMs,
      },
      (res) => {
        let body = '';
        res.setEncoding('utf8');
        res.on('data', (c: string) => (body += c));
        res.on('end', () => resolve({ status: res.statusCode ?? 0, body }));
        res.on('error', () => resolve(undefined));
      },
    );
    req.on('timeout', () => req.destroy());
    req.on('error', (e: NodeJS.ErrnoException) => resolve(e.code === 'ECONNREFUSED' ? undefined : { status: 0, body: '' }));
    req.end(payload);
  });
}

export async function probe(port: number): Promise<Probe> {
  const reply = await request(port, 'GET', '/health');
  if (reply === undefined) return { kind: 'free' };
  try {
    const health = JSON.parse(reply.body) as Health;
    if (reply.status === 200 && health.service === SERVICE && typeof health.pid === 'number' && typeof health.version === 'string') return { kind: 'ours', health };
  } catch {}
  return { kind: 'other' };
}

export async function verifiedState(home: string, port: number, health: Health): Promise<ServerState | undefined> {
  const state = await readState(home, port);
  return state !== undefined && state.pid === health.pid && health.port === port ? state : undefined;
}

export async function verifiedToken(home: string, port: number, health: Health): Promise<string | undefined> {
  return (await verifiedState(home, port, health)) === undefined ? undefined : readToken(home);
}

export function startServer(script: string, port: number, home: string, env: NodeJS.ProcessEnv = process.env): number | undefined {
  const child = spawn(process.execPath, [...SERVER_NODE_FLAGS, script, '--port', String(port)], {
    detached: true,
    stdio: 'ignore',
    windowsHide: true,
    cwd: os.homedir(),
    env: { ...terminalEnvironment(env), IDE_AGENT_TABS_HOME: home },
  });
  child.on('error', () => undefined);
  child.unref();
  return child.pid;
}

export interface Ensured {
  health?: Health;
  token?: string;
  started: boolean;
  problem?: string;
}

// Claude Code caches a 403 as "needs auth" and stops connecting, so no caller sends a request without the
// token: each one waits for a server it can verify, starting one when nothing listens.
export async function ensureServer(options: { script: string; port: number; home: string; version: string; waitMs?: number; env?: NodeJS.ProcessEnv }): Promise<Ensured> {
  const { script, port, home, version } = options;
  const deadline = Date.now() + (options.waitMs ?? START_WAIT_MS);
  let started = false;
  for (;;) {
    const found = await probe(port);
    if (found.kind === 'other') {
      return { started, problem: `port ${port} belongs to another program; set the Agent Tabs server_port option to a free port` };
    }
    const stale = found.kind === 'ours' && compareVersions(found.health.version, version) < 0;
    if (found.kind === 'ours' && !stale) {
      const token = await verifiedToken(home, port, found.health);
      if (token !== undefined) return { health: found.health, token, started };
    }
    if (!started && (found.kind === 'free' || stale)) {
      startServer(script, port, home, options.env);
      started = true;
    }
    if (Date.now() >= deadline) {
      if (found.kind === 'ours') {
        const token = await verifiedToken(home, port, found.health);
        if (token !== undefined) return { health: found.health, token, started };
        return { started, health: found.health, problem: `the Agent Tabs server on port ${port} (pid ${found.health.pid}) matches no state file in ${home}` };
      }
      return { started, problem: `the Agent Tabs server did not start on port ${port} within ${(options.waitMs ?? START_WAIT_MS) / 1000} s` };
    }
    await sleep(POLL_MS);
  }
}

export async function stopServer(home: string, port: number, waitMs = 5_000): Promise<{ stopped: boolean; pid?: number; problem?: string }> {
  const found = await probe(port);
  if (found.kind === 'free') return { stopped: false, problem: `nothing listens on port ${port}` };
  if (found.kind === 'other') return { stopped: false, problem: `port ${port} belongs to another program` };
  const state = await verifiedState(home, port, found.health);
  if (state === undefined) return { stopped: false, pid: found.health.pid, problem: `the server on port ${port} matches no state file in ${home}` };
  const reply = await request(port, 'POST', '/shutdown', state.shutdownToken);
  if (reply?.status !== 200) return { stopped: false, pid: state.pid, problem: `the server refused to stop (status ${reply?.status ?? 'none'})` };
  const deadline = Date.now() + waitMs;
  while (Date.now() < deadline) {
    if ((await probe(port)).kind === 'free') return { stopped: true, pid: state.pid };
    await sleep(POLL_MS);
  }
  return { stopped: false, pid: state.pid, problem: `the server still listens on port ${port} after ${waitMs / 1000} s` };
}

export async function askToStop(port: number, shutdownToken: string): Promise<boolean> {
  return (await request(port, 'POST', '/shutdown', shutdownToken))?.status === 200;
}

export async function notifyEnd(home: string, port: number, pid: number): Promise<boolean> {
  const found = await probe(port);
  if (found.kind !== 'ours') return false;
  const token = await verifiedToken(home, port, found.health);
  if (token === undefined) return false;
  return (await request(port, 'POST', '/end', token, HEALTH_TIMEOUT_MS, JSON.stringify({ pid })))?.status === 200;
}

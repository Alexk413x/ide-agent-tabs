import * as http from 'node:http';
import type { AddressInfo } from 'node:net';
import { AgentLaunch, AgentProfile, AgentSettings, codexPython, planLaunch } from './profiles';
import { ENDPOINT_BASE } from './registry';
import * as fs from 'node:fs';
import { BadRequest, checkAdmission, checkRevealTarget, OpenRequest, parseCloseId, parseEmpty, parseInput, parseOpenRequest, parseRevealPath, RevealDeps } from './request';

export const MAX_BODY_BYTES = 16 * 1024 * 1024;

export interface ProjectInfo {
  name: string;
  path: string;
  focused: boolean;
}

export interface TabInfo {
  id: string;
  agent: string;
  project: string;
  path: string;
}

export interface Host {
  info(): { ide: string; product: string; version: string; pid: number; projects: ProjectInfo[] };
  isInstalled(profile: AgentProfile): boolean;
  open(request: OpenRequest, profile: AgentProfile, launch: AgentLaunch): TabInfo | undefined;
  close(id: string): boolean;
  input(id: string, text: string): boolean;
  list(): TabInfo[];
  reveal(path: string): Promise<boolean>;
}

const ROUTES = new Set(['info', 'agents', 'open', 'close', 'list', 'input', 'reveal']);

type Reply = { status: number; body: Record<string, unknown> };

const ok = (fields: Record<string, unknown>): Reply => ({ status: 200, body: { ok: true, ...fields } });
const fail = (status: number, error: string): Reply => ({ status, body: { ok: false, error } });

export function route(url: string | undefined): string | undefined {
  const pathname = (url ?? '').split('?')[0];
  if (!pathname.startsWith(`${ENDPOINT_BASE}/`)) return undefined;
  const name = pathname.slice(ENDPOINT_BASE.length + 1);
  return ROUTES.has(name) ? name : undefined;
}

export function handle(name: string, body: string, host: Host, settings: AgentSettings): Reply {
  try {
    switch (name) {
      case 'open': {
        const request = parseOpenRequest(body);
        let profile = settings.defaultProfile();
        if (request.agent !== undefined) {
          const named = settings.profile(request.agent);
          if (!named) throw new BadRequest(`unknown agent: ${request.agent}`);
          profile = named;
        }
        const launch = planLaunch(profile, {
          prompt: request.prompt,
          args: request.args,
          env: request.env,
          model: request.model,
          via: request.via,
          setting: settings.shared().launchVia,
          ori: settings.detected().ori,
          windows: process.platform === 'win32',
          searchPath: process.env.PATH ?? '',
          python: codexPython(settings.home, process.platform === 'win32'),
        });
        const tab = host.open(request, profile, launch);
        return tab ? ok({ ...tab, via: launch.via }) : fail(409, 'no open folder to host the tab');
      }
      case 'close': {
        const id = parseCloseId(body);
        return host.close(id) ? ok({ id }) : fail(404, `no open agent tab with id ${id}; only tabs this extension opened can be closed`);
      }
      case 'input': {
        const { id, text } = parseInput(body);
        return host.input(id, text) ? ok({ id }) : fail(404, `no open agent tab with id ${id}; only tabs this extension opened take input`);
      }
      case 'agents':
        parseEmpty(body);
        return ok({
          default: settings.defaultProfile().name,
          agents: settings.profiles().map(p => ({ name: p.name, label: p.label, command: p.command, installed: host.isInstalled(p) })),
        });
      case 'info':
        parseEmpty(body);
        return ok({ ...host.info() });
      default:
        parseEmpty(body);
        return ok({ tabs: host.list() });
    }
  } catch (e) {
    if (e instanceof BadRequest) return fail(400, e.message);
    return fail(500, String(e));
  }
}

export const systemReveal: RevealDeps = {
  realpath: target => {
    try {
      return fs.realpathSync.native(target);
    } catch {
      return undefined;
    }
  },
  isDirectory: target => {
    try {
      return fs.statSync(target).isDirectory();
    } catch {
      return false;
    }
  },
  platform: process.platform,
};

export async function handleReveal(body: string, host: Host, deps: RevealDeps = systemReveal): Promise<Reply> {
  try {
    const known = [...host.info().projects.map(p => p.path), ...host.list().map(t => t.path)];
    const target = checkRevealTarget(parseRevealPath(body), known, deps);
    return (await host.reveal(target)) ? ok({ path: target }) : fail(500, `could not open ${target}`);
  } catch (e) {
    if (e instanceof BadRequest) return fail(400, e.message);
    return fail(500, String(e));
  }
}

function respond(res: http.ServerResponse, reply: Reply): void {
  const bytes = Buffer.from(JSON.stringify(reply.body), 'utf8');
  res.writeHead(reply.status, {
    'Content-Type': 'application/json; charset=utf-8',
    'Content-Length': bytes.length,
    Connection: 'close',
  });
  res.end(bytes);
}

export function createApiServer(token: string, host: Host, settings: AgentSettings): http.Server {
  return http.createServer((req, res) => {
    const header = (name: string) => {
      const value = req.headers[name];
      return Array.isArray(value) ? value.join(', ') : value;
    };
    const refusal = checkAdmission(req.socket.remoteAddress, req.method, token, header);
    if (refusal) {
      respond(res, fail(refusal.status, refusal.message));
      req.resume();
      return;
    }
    const name = route(req.url);
    if (!name) {
      respond(res, fail(404, 'no such route'));
      req.resume();
      return;
    }
    const chunks: Buffer[] = [];
    let size = 0;
    let tooLarge = false;
    req.on('data', (chunk: Buffer) => {
      if (tooLarge) return;
      size += chunk.length;
      if (size > MAX_BODY_BYTES) {
        tooLarge = true;
        respond(res, fail(413, `body exceeds ${MAX_BODY_BYTES} bytes`));
        return;
      }
      chunks.push(chunk);
    });
    req.on('end', () => {
      if (tooLarge) return;
      const body = Buffer.concat(chunks).toString('utf8');
      if (name === 'reveal') void handleReveal(body, host).then(reply => respond(res, reply));
      else respond(res, handle(name, body, host, settings));
    });
  });
}

export function listen(server: http.Server): Promise<number> {
  return new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => {
      server.off('error', reject);
      resolve((server.address() as AddressInfo).port);
    });
  });
}

export function apiUrl(port: number): string {
  return `http://127.0.0.1:${port}${ENDPOINT_BASE}`;
}

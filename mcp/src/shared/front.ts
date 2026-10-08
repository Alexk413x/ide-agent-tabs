import { timingSafeEqual } from 'node:crypto';
import http from 'node:http';
import { HEADERS, type Health } from './state.js';

export const MODERN_PROTOCOL = '2026-07-28';
const VERSION_KEY = 'io.modelcontextprotocol/protocolVersion';
const CAPABILITIES_KEY = 'io.modelcontextprotocol/clientCapabilities';
const SERVER_INFO_KEY = 'io.modelcontextprotocol/serverInfo';
const PARSE_ERROR = -32700;
const INVALID_REQUEST = -32600;
const METHOD_NOT_FOUND = -32601;
const INVALID_PARAMS = -32602;
const INTERNAL_ERROR = -32603;
const HEADER_MISMATCH = -32020;
const UNSUPPORTED_VERSION = -32022;
const STATUS: Record<number, number> = { [PARSE_ERROR]: 400, [INVALID_REQUEST]: 400, [INVALID_PARAMS]: 400, [HEADER_MISMATCH]: 400, [UNSUPPORTED_VERSION]: 400, [METHOD_NOT_FOUND]: 404 };
export const MAX_BODY_BYTES = 8 * 1024 * 1024;
const LISTED = { ttlMs: 0, cacheScope: 'private' };
const EMPTY_LISTS: Record<string, string> = { 'prompts/list': 'prompts', 'resources/list': 'resources', 'resources/templates/list': 'resourceTemplates' };
const CLIENT_ID = /^[A-Za-z0-9_-]{1,64}$/;
const TAB_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
const AGENT_NAME = /^[A-Za-z0-9._-]{1,64}$/;

export interface Identity {
  client?: string;
  tab?: string;
  agent?: string;
  pid?: number;
  pidStart?: number;
}

export interface Catalog {
  tools: Record<string, unknown>[];
  instructions: string;
}

export interface FrontDeps {
  port: number;
  token: string;
  shutdownToken: string;
  serverInfo: { name: string; version: string };
  health: () => Health;
  catalog: () => Promise<Catalog>;
  call: (identity: Identity, params: Record<string, unknown>, signal: AbortSignal) => Promise<Record<string, unknown>>;
  end: (pid: number) => Promise<number>;
  onRequest: () => void;
  onShutdown: () => void;
}

class RpcError extends Error {
  constructor(
    readonly code: number,
    message: string,
    readonly data?: unknown,
  ) {
    super(message);
  }
}

function header(req: http.IncomingMessage, name: string): string | undefined {
  const value = req.headers[name];
  return Array.isArray(value) ? undefined : value?.trim() || undefined;
}

function positiveInt(value: string | undefined, min: number): number | undefined {
  if (value === undefined || !/^\d{1,16}$/.test(value)) return undefined;
  const n = Number(value);
  return Number.isSafeInteger(n) && n >= min ? n : undefined;
}

export function identityOf(req: http.IncomingMessage): Identity {
  const client = header(req, HEADERS.client);
  const tab = header(req, HEADERS.tab);
  const agent = header(req, HEADERS.agent);
  const pid = positiveInt(header(req, HEADERS.pid), 1);
  const pidStart = positiveInt(header(req, HEADERS.pidStart), 0);
  return {
    ...(client !== undefined && CLIENT_ID.test(client) ? { client } : {}),
    ...(tab !== undefined && TAB_ID.test(tab) ? { tab } : {}),
    ...(agent !== undefined && AGENT_NAME.test(agent) ? { agent } : {}),
    ...(pid !== undefined ? { pid, ...(pidStart !== undefined ? { pidStart } : {}) } : {}),
  };
}

function bearer(value: string | undefined, expected: string): boolean {
  const [scheme, token = ''] = (value ?? '').split(' ', 2);
  if (scheme?.toLowerCase() !== 'bearer' || expected === '') return false;
  const a = Buffer.from(token.trim());
  const b = Buffer.from(expected);
  return a.length === b.length && timingSafeEqual(a, b);
}

function readBody(req: http.IncomingMessage): Promise<Buffer> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = [];
    let size = 0;
    req.on('data', (chunk: Buffer) => {
      size += chunk.length;
      if (size > MAX_BODY_BYTES) {
        reject(Object.assign(new Error('body too large'), { status: 413 }));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on('end', () => resolve(Buffer.concat(chunks)));
    req.on('error', reject);
  });
}

export class Front {
  readonly server: http.Server;
  private readonly hosts: Set<string>;
  private readonly origins: Set<string>;

  constructor(private readonly deps: FrontDeps) {
    this.hosts = new Set([`127.0.0.1:${deps.port}`, `localhost:${deps.port}`]);
    this.origins = new Set([...this.hosts].map((h) => `http://${h}`));
    this.server = http.createServer((req, res) => void this.handle(req, res).catch(() => this.send(res, 500, { error: 'internal error' })));
    // A wait_for_message request stays open for minutes after its body arrived, which requestTimeout doesn't
    // cover; a client that never finishes sending its request is still cut off.
    this.server.requestTimeout = 60_000;
    this.server.keepAliveTimeout = 60_000;
  }

  private send(res: http.ServerResponse, status: number, body?: unknown, close = false): void {
    if (res.headersSent) return;
    const text = body === undefined ? '' : JSON.stringify(body);
    res.writeHead(status, {
      ...(text !== '' ? { 'content-type': 'application/json' } : {}),
      'content-length': Buffer.byteLength(text),
      ...(close ? { connection: 'close' } : {}),
    });
    res.end(text);
  }

  private refuse(res: http.ServerResponse, status: number, error: string): void {
    this.send(res, status, { error }, true);
  }

  private async handle(req: http.IncomingMessage, res: http.ServerResponse): Promise<void> {
    if (!this.hosts.has((header(req, 'host') ?? '').toLowerCase())) return this.refuse(res, 403, 'Host is not this server');
    const origin = header(req, 'origin');
    if (origin !== undefined && !this.origins.has(origin.toLowerCase())) return this.refuse(res, 403, 'Origin not allowed');
    const route = (req.url ?? '/').split('?', 1)[0];
    if (route === '/health' && req.method === 'GET') return this.send(res, 200, this.deps.health());
    if (route === '/shutdown') {
      if (!bearer(header(req, 'authorization'), this.deps.shutdownToken)) return this.refuse(res, 403, 'missing or wrong bearer token');
      if (req.method !== 'POST') return this.refuse(res, 405, 'use POST');
      this.send(res, 200, { ok: true }, true);
      this.deps.onShutdown();
      return;
    }
    // A 401 starts Claude Code's OAuth flow, which this server doesn't offer, so a bad token gets 403.
    if (!bearer(header(req, 'authorization'), this.deps.token)) return this.refuse(res, 403, 'missing or wrong bearer token');
    if (route === '/end' && req.method === 'POST') return this.ended(req, res);
    if (route !== '/mcp') return this.refuse(res, 404, 'not found');
    if (req.method !== 'POST') {
      res.writeHead(405, { allow: 'POST', 'content-length': 0, connection: 'close' });
      res.end();
      return;
    }
    this.deps.onRequest();
    const accept = header(req, 'accept') ?? '';
    if (!accept.includes('application/json') && !accept.includes('*/*')) return this.refuse(res, 406, 'accept application/json');
    let body: Buffer;
    try {
      body = await readBody(req);
    } catch (e) {
      return this.refuse(res, (e as { status?: number }).status ?? 400, 'unreadable body');
    }
    let message: unknown;
    try {
      message = JSON.parse(body.toString('utf8'));
    } catch {
      return this.rpcError(res, null, new RpcError(PARSE_ERROR, 'Parse error'));
    }
    await this.message(req, res, message);
  }

  private async ended(req: http.IncomingMessage, res: http.ServerResponse): Promise<void> {
    let pid: number | undefined;
    try {
      pid = positiveInt(String((JSON.parse((await readBody(req)).toString('utf8')) as { pid?: unknown }).pid ?? ''), 1);
    } catch {}
    if (pid === undefined) return this.refuse(res, 400, 'pid must be a process id');
    this.send(res, 200, { ended: await this.deps.end(pid) });
  }

  private rpcError(res: http.ServerResponse, id: unknown, error: RpcError): void {
    this.send(res, STATUS[error.code] ?? 200, {
      jsonrpc: '2.0',
      id: id ?? null,
      error: { code: error.code, message: error.message, ...(error.data !== undefined ? { data: error.data } : {}) },
    });
  }

  private async message(req: http.IncomingMessage, res: http.ServerResponse, message: unknown): Promise<void> {
    const version = header(req, 'mcp-protocol-version');
    const msg = message as Record<string, unknown> | null;
    if (typeof msg !== 'object' || msg === null || Array.isArray(msg) || msg.jsonrpc !== '2.0' || typeof msg.method !== 'string') {
      return this.rpcError(res, null, new RpcError(INVALID_REQUEST, 'Body must be a single JSON-RPC request or notification object'));
    }
    const unsupported = (id: unknown) =>
      this.rpcError(res, id, new RpcError(UNSUPPORTED_VERSION, 'Unsupported protocol version', { supported: [MODERN_PROTOCOL], requested: version ?? '' }));
    if (!('id' in msg)) {
      if (version !== MODERN_PROTOCOL) return unsupported(null);
      res.writeHead(202, { 'content-length': 0 });
      res.end();
      return;
    }
    const id = msg.id;
    if (typeof id !== 'string' && !(typeof id === 'number' && Number.isFinite(id))) {
      return this.rpcError(res, null, new RpcError(INVALID_REQUEST, 'Body must be a single JSON-RPC request or notification object'));
    }
    if (version !== MODERN_PROTOCOL) return unsupported(id);
    const params = msg.params as Record<string, unknown> | undefined;
    const meta = params?._meta as Record<string, unknown> | undefined;
    if (typeof params !== 'object' || params === null || typeof meta !== 'object' || meta === null || !(VERSION_KEY in meta) || !(CAPABILITIES_KEY in meta)) {
      return this.rpcError(res, id, new RpcError(INVALID_PARAMS, `params._meta must carry the ${VERSION_KEY} and ${CAPABILITIES_KEY} keys`));
    }
    if (meta[VERSION_KEY] !== version) return this.rpcError(res, id, new RpcError(HEADER_MISMATCH, "mcp-protocol-version header does not match the request envelope's protocol version"));
    if (header(req, 'mcp-method') !== msg.method) return this.rpcError(res, id, new RpcError(HEADER_MISMATCH, "mcp-method header does not match the request body's method"));
    if (msg.method === 'tools/call' && typeof params.name === 'string' && decodeURIComponent(header(req, 'mcp-name') ?? '') !== params.name) {
      return this.rpcError(res, id, new RpcError(HEADER_MISMATCH, "mcp-name header does not match the request body's name"));
    }
    const aborted = new AbortController();
    res.on('close', () => {
      if (!res.writableFinished) aborted.abort();
    });
    let result: Record<string, unknown> | undefined;
    try {
      result = await this.dispatch(msg.method, params, identityOf(req), aborted.signal);
    } catch (e) {
      return this.rpcError(res, id, e instanceof RpcError ? e : new RpcError(INTERNAL_ERROR, e instanceof Error ? e.message : String(e)));
    }
    if (result === undefined) return this.rpcError(res, id, new RpcError(METHOD_NOT_FOUND, 'Method not found', msg.method));
    this.send(res, 200, { jsonrpc: '2.0', id, result: { resultType: 'complete', _meta: { [SERVER_INFO_KEY]: this.deps.serverInfo }, ...result } });
  }

  private async dispatch(method: string, params: Record<string, unknown>, identity: Identity, signal: AbortSignal): Promise<Record<string, unknown> | undefined> {
    if (method === 'server/discover') {
      const { instructions } = await this.deps.catalog();
      return { ...LISTED, supportedVersions: [MODERN_PROTOCOL], capabilities: { tools: { listChanged: false } }, instructions };
    }
    if (method === 'tools/list') return { tools: (await this.deps.catalog()).tools, ...LISTED };
    if (method === 'ping') return {};
    const list = EMPTY_LISTS[method];
    if (list !== undefined) return { [list]: [], ...LISTED };
    if (method === 'tools/call') {
      if (typeof params.name !== 'string') throw new RpcError(INVALID_PARAMS, 'tools/call needs a tool name');
      return this.deps.call(identity, params, signal);
    }
    return undefined;
  }

  listen(): Promise<void> {
    return new Promise((resolve, reject) => {
      this.server.once('error', reject);
      this.server.listen({ port: this.deps.port, host: '127.0.0.1', exclusive: true }, () => {
        this.server.off('error', reject);
        resolve();
      });
    });
  }

  close(): Promise<void> {
    return new Promise((resolve) => {
      this.server.close(() => resolve());
      this.server.closeAllConnections();
    });
  }
}

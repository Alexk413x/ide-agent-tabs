import http from 'node:http';
import net from 'node:net';
import os from 'node:os';
import { pathToFileURL } from 'node:url';

export const PROTOCOL = '2026-07-28';

export function freePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const { port } = server.address() as net.AddressInfo;
      server.close(() => resolve(port));
    });
  });
}

export interface Reply {
  status: number;
  json: Record<string, any>;
}

export function request(port: number, route: string, options: { method?: string; headers?: Record<string, string>; body?: string } = {}): Promise<Reply> {
  return new Promise((resolve, reject) => {
    const body = options.body ?? '';
    const req = http.request(
      { host: '127.0.0.1', port, path: route, method: options.method ?? 'POST', headers: { host: `127.0.0.1:${port}`, 'content-length': Buffer.byteLength(body), ...options.headers } },
      (res) => {
        let text = '';
        res.setEncoding('utf8');
        res.on('data', (c: string) => (text += c));
        res.on('end', () => resolve({ status: res.statusCode ?? 0, json: text === '' ? {} : JSON.parse(text) }));
      },
    );
    req.on('error', reject);
    req.end(body);
  });
}

export class McpHttp {
  private next = 1;

  constructor(
    readonly port: number,
    readonly token: string,
    readonly identity: Record<string, string> = {},
    readonly cwd = os.tmpdir(),
  ) {}

  rpc(method: string, params: Record<string, unknown> = {}, extra: Record<string, string> = {}): Promise<Reply> {
    const body = JSON.stringify({
      jsonrpc: '2.0',
      id: this.next++,
      method,
      params: { ...params, _meta: { 'io.modelcontextprotocol/protocolVersion': PROTOCOL, 'io.modelcontextprotocol/clientCapabilities': { roots: {} } } },
    });
    return request(this.port, '/mcp', {
      body,
      headers: {
        authorization: `Bearer ${this.token}`,
        'content-type': 'application/json',
        accept: 'application/json, text/event-stream',
        'mcp-protocol-version': PROTOCOL,
        'mcp-method': method,
        ...(typeof params.name === 'string' ? { 'mcp-name': params.name } : {}),
        ...this.identity,
        ...extra,
      },
    });
  }

  async call(name: string, args: Record<string, unknown> = {}): Promise<{ isError: boolean; text: string; json?: any }> {
    let reply = await this.rpc('tools/call', { name, arguments: args });
    const asked = reply.json.result?.inputRequests as Record<string, { method: string }> | undefined;
    if (reply.json.result?.resultType === 'input_required' && asked !== undefined) {
      const inputResponses = Object.fromEntries(Object.keys(asked).map((key) => [key, { roots: [{ uri: pathToFileURL(this.cwd).href }] }]));
      reply = await this.rpc('tools/call', { name, arguments: args, inputResponses });
    }
    if (reply.json.error) throw new Error(`${name}: ${reply.json.error.message}`);
    const result = reply.json.result as { isError?: boolean; content: { text: string }[] };
    const text = result.content?.[0]?.text ?? '';
    return { isError: result.isError === true, text, json: result.isError || text === '' ? undefined : JSON.parse(text) };
  }
}

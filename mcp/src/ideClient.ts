import type { Endpoint } from './registry.js';

export const USER_AGENT = 'ide-agent-tabs-mcp/0.1.0';
export const IDE_TIMEOUT_MS = 15_000;

export type Route = 'info' | 'agents' | 'open' | 'close' | 'list';

export class IdeError extends Error {
  constructor(
    message: string,
    readonly status?: number,
    readonly body?: unknown,
  ) {
    super(message);
  }
}

export type IdeCall = (endpoint: Endpoint, route: Route, body?: object) => Promise<Record<string, unknown>>;

export function ideCaller(timeoutMs = IDE_TIMEOUT_MS): IdeCall {
  return async (endpoint, route, body = {}) => {
    let response: Response;
    try {
      response = await fetch(`${endpoint.url}/${route}`, {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${endpoint.token}`,
          'Content-Type': 'application/json',
          'User-Agent': USER_AGENT,
        },
        body: JSON.stringify(body),
        signal: AbortSignal.timeout(timeoutMs),
      });
    } catch (e) {
      const reason = (e as Error).name === 'TimeoutError' ? `no answer within ${timeoutMs / 1000} s` : (e as Error).message;
      throw new IdeError(`${endpoint.id} ${route} failed: ${reason}`);
    }
    const text = await response.text();
    let json: unknown;
    try {
      json = JSON.parse(text);
    } catch {
      throw new IdeError(`${endpoint.id} ${route} answered HTTP ${response.status} with non-JSON: ${text.slice(0, 500)}`, response.status, text);
    }
    const obj = typeof json === 'object' && json !== null && !Array.isArray(json) ? (json as Record<string, unknown>) : undefined;
    if (!response.ok || !obj || obj.ok !== true) {
      throw new IdeError(`${endpoint.id} ${route} answered HTTP ${response.status}: ${text}`, response.status, json);
    }
    return obj;
  };
}

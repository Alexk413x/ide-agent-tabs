import type { Endpoint } from './registry.js';
import { PACKAGE_VERSION } from './version.js';

export const USER_AGENT = `ide-agent-tabs-mcp/${PACKAGE_VERSION}`;
export const IDE_TIMEOUT_MS = 15_000;

export type Route = 'info' | 'agents' | 'open' | 'close' | 'list' | 'input' | 'reveal';

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
      const reason =
        (e as Error).name === 'TimeoutError'
          ? `no answer within ${timeoutMs / 1000} s. ${endpoint.product} may be busy or showing a modal dialog; ask the user to check it, then retry once`
          : (e as Error).message;
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
      const error = typeof obj?.error === 'string' ? obj.error : text;
      const hint = nextStep(endpoint, route, response.status, error);
      throw new IdeError(`${endpoint.id} ${route} answered HTTP ${response.status}: ${error}${hint ? `. ${hint}` : ''}`, response.status, json);
    }
    return obj;
  };
}

function nextStep(endpoint: Endpoint, route: Route, status: number, error: string): string | undefined {
  if (status === 401) return `${endpoint.product} refused the token in ${endpoint.id}, so that endpoint is stale. Call list_ides for the current ids`;
  if (status === 409 && route === 'open') return 'To open the tab in a terminal instead, pass ide set to a terminal id from list_ides';
  if (status === 503) return `A modal dialog is likely open in ${endpoint.product}. Ask the user to close it, then retry once`;
  if (error.startsWith('unknown agent')) return 'Call list_agents for the profile names';
  return undefined;
}

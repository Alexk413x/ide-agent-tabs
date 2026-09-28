import {
  APIConnectionError,
  APIError,
  APITimeoutError,
  APIUserAbortError,
  TypeSafeClient,
  type EntryType,
  type Questions,
  type SystemOneResult,
} from '@typesafe-ai/sdk';
import { JEV_MODEL } from './settings.js';

export const MAX_CHOICE_OPTIONS = 255;
export const MIN_SCORE_LEVELS = 2;
export const MAX_SCORE_LEVELS = 10;
export const MAX_REQUEST_CHARS = 200_000;
export const CALL_LIMIT_MS = 30_000;
const ATTEMPT_TIMEOUT_MS = 10_000;

export type JevResult = SystemOneResult<Questions>;

export class JevError extends Error {
  constructor(
    message: string,
    readonly status?: number | string,
  ) {
    super(message);
  }
}

export function checkRequest(state: EntryType, questions: Questions): void {
  const names = Object.keys(questions);
  if (names.length === 0) throw new JevError('A Jev request needs at least one question.');
  for (const name of names) {
    const q = questions[name]!;
    if (q.type === 'choice') {
      const count = Object.keys(q.criteria ?? {}).length;
      if (count < 2 || count > MAX_CHOICE_OPTIONS) {
        throw new JevError(`Choice question "${name}" has ${count} options; Jev takes 2 to ${MAX_CHOICE_OPTIONS}.`);
      }
    } else if (q.type === 'score') {
      const count = Array.isArray(q.criteria) ? q.criteria.length : 0;
      if (count < MIN_SCORE_LEVELS || count > MAX_SCORE_LEVELS) {
        throw new JevError(`Score question "${name}" has ${count} levels; Jev takes ${MIN_SCORE_LEVELS} to ${MAX_SCORE_LEVELS}.`);
      }
    }
  }
  const chars = JSON.stringify({ state, questions }).length;
  if (chars > MAX_REQUEST_CHARS) {
    throw new JevError(`The request is ${chars} characters; Jev takes at most ${MAX_REQUEST_CHARS}. Send less state.`);
  }
}

function scrub(text: string, key: string): string {
  return key === '' ? text : text.split(key).join('***');
}

function detail(e: APIError): string {
  const text = e.message.replace(new RegExp(`^${e.status}\\s*`), '');
  return text === '' ? '' : `: ${text}`;
}

function httpMessage(e: APIError): string {
  switch (e.status) {
    case 401:
      return 'TypeSafe rejected the API key (HTTP 401). Check the key.';
    case 403:
      return 'TypeSafe refused access for this API key (HTTP 403).';
    case 400:
    case 422:
      return `TypeSafe refused the request as invalid (HTTP ${e.status})${detail(e)}`;
    case 429:
      return 'TypeSafe rate-limited the request (HTTP 429). Try again later.';
    case 529:
      return 'TypeSafe is overloaded (HTTP 529). Try again later.';
    default:
      return `TypeSafe answered HTTP ${e.status}${detail(e)}`;
  }
}

export function toJevError(e: unknown, key: string): JevError {
  if (e instanceof JevError) return new JevError(scrub(e.message, key), e.status);
  if (e instanceof APIError) return new JevError(scrub(httpMessage(e), key), e.status);
  if (e instanceof APIUserAbortError || e instanceof APITimeoutError) {
    return new JevError(`Jev did not answer within ${CALL_LIMIT_MS / 1000} s.`, 'timeout');
  }
  if (e instanceof APIConnectionError) return new JevError(scrub(`Could not reach TypeSafe: ${e.message}`, key), 'connection');
  const text = e instanceof Error ? e.message : String(e);
  return new JevError(scrub(`Jev request failed: ${text}`, key), 'error');
}

export interface CallOptions {
  apiKey: string;
  baseURL?: string;
}

export async function callJev(options: CallOptions, state: EntryType, questions: Questions): Promise<JevResult> {
  const client = new TypeSafeClient({
    apiKey: options.apiKey,
    ...(options.baseURL ? { baseURL: options.baseURL } : {}),
    defaultModel: JEV_MODEL,
    // Any other level lets TYPESAFE_LOG_LEVEL print request bodies to stdout, which carries the MCP protocol.
    logLevel: 'off',
    timeout: ATTEMPT_TIMEOUT_MS,
  });
  try {
    return await client.systemOne({ state, questions, model: JEV_MODEL }, { signal: AbortSignal.timeout(CALL_LIMIT_MS) });
  } catch (e) {
    throw toJevError(e, options.apiKey);
  }
}

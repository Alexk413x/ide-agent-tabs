import * as http from 'node:http';
import * as path from 'node:path';
import { isFile, readJson } from './files';

// A Codex tab runs its server on the interpreter that python.json records, so no py.exe stays behind as its parent.
// The path goes into a TOML literal string and through cmd.exe, so it may not hold a quote, % or !, or end in \.
const CODEX_PYTHON_SAFE = /^[^'"%!\x00-\x1f\x7f]*[^'"%!\x00-\x1f\x7f\\]$/;

// The helper command goes into a TOML literal string and through PowerShell 5.1 and cmd.exe, which mangle a quote, a
// space or a % in an argument, so a path with one leaves the tab on the stdio server.
const HTTP_SAFE = /^[A-Za-z0-9_./:\\+-]+$/;
const HTTP_WORD = /^[A-Za-z0-9._-]{1,128}$/;

export const CLAUDE_TAB_SETTINGS_FILE = 'claude-tab-settings.json';
// The settings path reaches cmd.exe when claude is a .cmd shim, so it may not hold a double quote or a cmd.exe metacharacter.
const CMD_SAFE_PATH = /^[^"%!^&|<>\x00-\x1f\x7f]+$/;

const COPILOT_HEALTH_TIMEOUT_MS = 500;

export interface CodexHttp {
  python: string;
  headers: string;
  port: number;
  tab: string;
  agent: string;
  home: string;
}

// Same rules as copilot_http in claude-plugin/mcp/src/ide_agent_tabs/profiles.py.
export interface CopilotHttp {
  config: string;
  port: number;
}

function readPort(home: string): number | undefined {
  const port = (readJson(path.join(home, 'mcp', 'port.json')) as { port?: unknown } | null | undefined)?.port;
  return typeof port === 'number' && Number.isInteger(port) && port > 0 && port <= 65535 ? port : undefined;
}

export function recordedPython(home: string, windows: boolean): string | undefined {
  const python = (readJson(path.join(home, 'mcp', 'python.json')) as { python?: unknown } | null | undefined)?.python;
  const absolute = windows ? path.win32.isAbsolute : path.posix.isAbsolute;
  return typeof python === 'string' && absolute(python) && CODEX_PYTHON_SAFE.test(python) && isFile(python) ? python : undefined;
}

export function codexPythonOf(python: string | undefined, windows: boolean): string[] {
  if (python !== undefined) return [python];
  return windows ? ['py', '-3'] : ['python3'];
}

export function codexPython(home: string, windows: boolean): string[] {
  return codexPythonOf(recordedPython(home, windows), windows);
}

export function codexHttpOf(python: string | undefined, home: string, windows: boolean, tab: string, agent: string): CodexHttp | undefined {
  const port = readPort(home);
  const headersFile = path.join(home, 'mcp', 'py', 'launch', 'headers.py');
  if (python === undefined || port === undefined || !isFile(headersFile)) return undefined;
  if (!HTTP_WORD.test(tab) || !HTTP_WORD.test(agent)) return undefined;
  const headers = windows ? headersFile.replace(/\\/g, '/') : headersFile;
  if ([python, headers, home].some(p => !HTTP_SAFE.test(p))) return undefined;
  return { python, headers, port, tab, agent, home };
}

export function codexHttp(home: string, windows: boolean, tab: string, agent: string): CodexHttp | undefined {
  return codexHttpOf(recordedPython(home, windows), home, windows, tab, agent);
}

// Same rules as claude_tab_settings in claude-plugin/mcp/src/ide_agent_tabs/profiles.py.
export function claudeTabSettingsOf(python: string | undefined, home: string): string | undefined {
  const file = path.join(home, 'mcp', CLAUDE_TAB_SETTINGS_FILE);
  return python !== undefined && CMD_SAFE_PATH.test(file) && isFile(file) ? file : undefined;
}

export function claudeTabSettings(home: string, windows: boolean): string | undefined {
  return claudeTabSettingsOf(recordedPython(home, windows), home);
}

function copilotConfigPort(home: string): number | undefined {
  const config = readJson(path.join(home, 'mcp', 'copilot-http.json')) as
    | { mcpServers?: Record<string, { url?: unknown } | undefined> }
    | null
    | undefined;
  const url = config?.mcpServers?.['ide-agent-tabs']?.url;
  const match = typeof url === 'string' ? /^http:\/\/127\.0\.0\.1:([0-9]{1,5})\/mcp$/.exec(url) : null;
  const port = match ? Number(match[1]) : 0;
  return port > 0 && port <= 65535 ? port : undefined;
}

export function answersHealth(port: number, timeoutMs = COPILOT_HEALTH_TIMEOUT_MS): Promise<boolean> {
  return new Promise(resolve => {
    const req = http.get({ host: '127.0.0.1', port, path: '/health', timeout: timeoutMs }, res => {
      let body = '';
      res.setEncoding('utf8');
      res.on('data', chunk => (body += chunk));
      res.on('end', () => {
        try {
          resolve(res.statusCode === 200 && (JSON.parse(body) as { service?: unknown }).service === 'ide-agent-tabs');
        } catch {
          resolve(false);
        }
      });
    });
    req.on('timeout', () => req.destroy());
    req.on('error', () => resolve(false));
  });
}

export function copilotHooksFile(env: NodeJS.ProcessEnv, userHome: string): string {
  return path.join(env.COPILOT_HOME || path.join(userHome, '.copilot'), 'hooks', 'ide-agent-tabs.json');
}

export async function copilotHttp(
  home: string,
  windows: boolean,
  hooksFile: string,
  answers: (port: number) => Promise<boolean> = answersHealth,
): Promise<CopilotHttp | undefined> {
  const port = readPort(home);
  const file = path.join(home, 'mcp', 'copilot-http.json');
  const config = windows ? file.replace(/\\/g, '/') : file;
  if (port === undefined || copilotConfigPort(home) !== port || !HTTP_SAFE.test(config) || !isFile(hooksFile)) return undefined;
  return (await answers(port)) ? { config, port } : undefined;
}

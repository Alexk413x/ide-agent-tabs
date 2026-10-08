import { randomBytes } from 'node:crypto';
import path from 'node:path';
import { agentTabsHome } from '../home.js';
import { PACKAGE_VERSION } from '../version.js';
import { findAgentProcess } from './ancestry.js';
import { ensureServer } from './client.js';
import { DEFAULT_PORT, parsePort, PORT_OPTION_ENV, portFromUrl, SERVER_SCRIPT } from './state.js';

const TAB_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
const AGENT_NAME = /^[A-Za-z0-9._-]{1,64}$/;

// A project's .claude/settings.json can set environment variables, so the tab id is only a claim: the server
// binds it when no other live process holds that tab.
export async function helperHeaders(env: NodeJS.ProcessEnv, pluginRoot: string, lookupMs?: number): Promise<Record<string, string>> {
  const port = portFromUrl(env.CLAUDE_CODE_MCP_SERVER_URL) ?? parsePort(env[PORT_OPTION_ENV]) ?? DEFAULT_PORT;
  const home = agentTabsHome(env);
  const headers: Record<string, string> = { 'X-Agent-Tabs-Client': randomBytes(12).toString('hex') };
  const tab = env.IDE_AGENT_TABS_ID;
  if (tab !== undefined && TAB_ID.test(tab) && !tab.startsWith('s-') && !tab.startsWith('codex-')) headers['X-Agent-Tabs-Tab'] = tab;
  const agent = env.IDE_AGENT_TABS_AGENT;
  if (agent !== undefined && AGENT_NAME.test(agent)) headers['X-Agent-Tabs-Agent'] = agent;
  const [found, ensured] = await Promise.all([
    findAgentProcess(process.pid, lookupMs).catch(() => undefined),
    ensureServer({ script: path.join(pluginRoot, 'dist', SERVER_SCRIPT), port, home, version: PACKAGE_VERSION, env }).catch(() => undefined),
  ]);
  if (found !== undefined) {
    headers['X-Agent-Tabs-Pid'] = String(found.pid);
    headers['X-Agent-Tabs-Pid-Start'] = String(Math.max(0, Math.round(found.startMs)));
  }
  if (ensured?.token !== undefined) headers.Authorization = `Bearer ${ensured.token}`;
  return headers;
}

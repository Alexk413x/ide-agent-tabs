import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js';
import { Handoffs } from '../handoff.js';
import type { Jev } from '../jev/service.js';
import { Messaging } from '../messaging/messaging.js';
import { agentFromClient } from '../messaging/sessions.js';
import { Resumes } from '../resume.js';
import { createServer, serverInstructions } from '../server.js';
import { Service } from '../service.js';

export interface CatalogData {
  tools: Record<string, unknown>[];
  instructions: { jev: string; plain: string };
}

export interface ToolCatalog extends CatalogData {
  only: Record<string, string[]>;
}

export const CATALOG_CLIENT = 'claude-code';
const CATALOG_CLIENTS = [CATALOG_CLIENT, 'codex-mcp-client', 'other-client'];

async function listToolsAs(clientName: string): Promise<Record<string, unknown>[]> {
  const home = '/nonexistent/agent-tabs-catalog';
  const service = new Service({ home, scriptsDir: home, platform: 'linux', env: {}, callIde: async () => ({}), drivers: [] });
  const messaging = new Messaging({ home, env: {}, pid: 1, cwd: home, hosts: service });
  const handoffs = new Handoffs({ home, env: {}, sessionId: () => messaging.id, openTab: async () => ({}), findHost: async () => undefined });
  const resumes = new Resumes({ home, settings: () => service.settings(), openTab: async () => ({}), liveHost: async () => undefined, live: async () => [] });
  const server = createServer(service, {} as Jev, messaging, handoffs, resumes);
  const [clientSide, serverSide] = InMemoryTransport.createLinkedPair();
  await server.connect(serverSide);
  const client = new Client({ name: clientName, version: '0' });
  await client.connect(clientSide);
  const { tools } = await client.listTools();
  await client.close();
  return tools as unknown as Record<string, unknown>[];
}

const instructions = () => ({ jev: serverInstructions(true, true), plain: serverInstructions(false, true) });

export async function buildCatalog(): Promise<CatalogData> {
  return { tools: await listToolsAs(CATALOG_CLIENT), instructions: instructions() };
}

export async function buildToolCatalog(): Promise<ToolCatalog> {
  const lists = await Promise.all(CATALOG_CLIENTS.map(async (name) => ({ agent: agentFromClient(name), tools: await listToolsAs(name) })));
  const merged: Record<string, unknown>[] = [];
  const indexOf = (name: unknown) => merged.findIndex((t) => t.name === name);
  for (const { tools } of lists) {
    tools.forEach((tool, i) => {
      if (indexOf(tool.name) !== -1) return;
      const after = i === 0 ? -1 : indexOf(tools[i - 1]!.name);
      merged.splice(after + 1, 0, tool);
    });
  }
  const only: Record<string, string[]> = {};
  for (const tool of merged) {
    const agents = lists.filter((l) => l.tools.some((t) => t.name === tool.name)).map((l) => l.agent);
    if (agents.length !== lists.length) only[String(tool.name)] = agents;
  }
  return { tools: merged, only, instructions: instructions() };
}

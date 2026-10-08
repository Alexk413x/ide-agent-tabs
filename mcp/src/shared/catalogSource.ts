import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js';
import { Handoffs } from '../handoff.js';
import type { Jev } from '../jev/service.js';
import { Messaging } from '../messaging/messaging.js';
import { Resumes } from '../resume.js';
import { createServer, serverInstructions } from '../server.js';
import { Service } from '../service.js';

export interface CatalogData {
  tools: Record<string, unknown>[];
  instructions: { jev: string; plain: string };
}

export const CATALOG_CLIENT = 'claude-code';

export async function buildCatalog(): Promise<CatalogData> {
  const home = '/nonexistent/agent-tabs-catalog';
  const service = new Service({ home, scriptsDir: home, platform: 'linux', env: {}, callIde: async () => ({}), drivers: [] });
  const messaging = new Messaging({ home, env: {}, pid: 1, cwd: home, hosts: service });
  const handoffs = new Handoffs({ home, env: {}, sessionId: () => messaging.id, openTab: async () => ({}), findHost: async () => undefined });
  const resumes = new Resumes({ home, settings: () => service.settings(), openTab: async () => ({}), liveHost: async () => undefined, live: async () => [] });
  const server = createServer(service, {} as Jev, messaging, handoffs, resumes);
  const [clientSide, serverSide] = InMemoryTransport.createLinkedPair();
  await server.connect(serverSide);
  const client = new Client({ name: CATALOG_CLIENT, version: '0' });
  await client.connect(clientSide);
  const { tools } = await client.listTools();
  await client.close();
  return {
    tools: tools as unknown as Record<string, unknown>[],
    instructions: { jev: serverInstructions(true, true), plain: serverInstructions(false, true) },
  };
}

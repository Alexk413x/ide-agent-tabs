import { Handoffs } from './handoff.js';
import { agentTabsHome } from './home.js';
import { startJev } from './jev/service.js';
import { Messaging } from './messaging/messaging.js';
import { Resumes } from './resume.js';
import { createServer } from './server.js';
import { systemService } from './cli.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';

const END_RECORD_MS = 2_000;
const home = agentTabsHome();
const log = (message: string) => console.error(`ide-agent-tabs: ${message}`);
const service = systemService(home, log);
const { jev } = await startJev(service, { home, env: process.env, platform: process.platform });
const messaging = new Messaging({ home, env: process.env, pid: process.pid, cwd: process.cwd(), hosts: service });
await messaging.startRegistered({ log });
let stopping = false;
const stop = () => {
  if (stopping) return;
  stopping = true;
  const exit = () => {
    messaging.stopSync();
    process.exit(0);
  };
  setTimeout(exit, END_RECORD_MS).unref();
  void messaging.recordEnd().catch(() => undefined).finally(exit);
};
process.on('exit', () => messaging.stopSync());
for (const signal of ['SIGINT', 'SIGTERM', 'SIGHUP'] as const) process.on(signal, () => stop());
// Windows sends no SIGTERM to a child; a client ends the server by closing its stdin.
process.stdin.on('end', () => stop());
process.stdin.on('close', () => stop());
const handoffs = new Handoffs({
  home,
  env: process.env,
  sessionId: () => messaging.id,
  openTab: (input) => service.openTab(input),
  findHost: (id) => service.findHost(id),
});
const resumes = new Resumes({
  home,
  settings: () => service.settings(),
  openTab: (input) => service.openTab(input),
  liveHost: (host, product) => service.liveHost(host, product),
  live: () => messaging.live(),
});
await createServer(service, jev, messaging, handoffs, resumes).connect(new StdioServerTransport());
void service.refreshDetection().catch(() => undefined);

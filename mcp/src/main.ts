import { existsSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import { Handoffs } from './handoff.js';
import { agentTabsHome } from './home.js';
import { ideCaller } from './ideClient.js';
import { runJevCli } from './jev/cli.js';
import { startJev } from './jev/service.js';
import { Messaging } from './messaging/messaging.js';
import { createServer } from './server.js';
import { Service } from './service.js';
import { TERMINAL_DRIVERS } from './terminals/index.js';
import { LAUNCHER_PS1 } from './terminals/windowsTerminal.js';

function scriptsDir(): string {
  const here = path.dirname(fileURLToPath(import.meta.url));
  const candidates = [path.join(here, 'launch'), path.join(here, '..', 'launch')];
  return candidates.find((dir) => existsSync(path.join(dir, LAUNCHER_PS1))) ?? candidates[0]!;
}

const home = agentTabsHome();
const service = new Service({
  home,
  scriptsDir: scriptsDir(),
  platform: process.platform,
  env: process.env,
  callIde: ideCaller(),
  drivers: TERMINAL_DRIVERS,
});
const { jev, off } = await startJev(service, { home, env: process.env, platform: process.platform });
const [command, ...args] = process.argv.slice(2);

if (command === 'jev') {
  process.exitCode = await runJevCli(args, jev, off);
} else {
  const messaging = new Messaging({ home, env: process.env, pid: process.pid, cwd: process.cwd(), hosts: service });
  await messaging.start().catch(() => undefined);
  let stopping = false;
  const stop = () => {
    if (stopping) return;
    stopping = true;
    messaging.stopSync();
    process.exit(0);
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
  await createServer(service, jev, messaging, handoffs).connect(new StdioServerTransport());
  void service.refreshDetection().catch(() => undefined);
}

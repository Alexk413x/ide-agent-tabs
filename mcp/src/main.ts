import { existsSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import { ideCaller } from './ideClient.js';
import { createServer } from './server.js';
import { Service } from './service.js';
import { TERMINAL_DRIVERS } from './terminals/index.js';
import { LAUNCHER_PS1 } from './terminals/windowsTerminal.js';

export function agentTabsHome(env: NodeJS.ProcessEnv = process.env): string {
  return env.IDE_AGENT_TABS_HOME || path.join(os.homedir(), '.ide-agent-tabs');
}

function scriptsDir(): string {
  const here = path.dirname(fileURLToPath(import.meta.url));
  const candidates = [path.join(here, 'launch'), path.join(here, '..', 'launch')];
  return candidates.find((dir) => existsSync(path.join(dir, LAUNCHER_PS1))) ?? candidates[0]!;
}

const service = new Service({
  home: agentTabsHome(),
  scriptsDir: scriptsDir(),
  platform: process.platform,
  env: process.env,
  callIde: ideCaller(),
  drivers: TERMINAL_DRIVERS,
});

await createServer(service).connect(new StdioServerTransport());

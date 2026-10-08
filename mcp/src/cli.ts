import { existsSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { agentTabsHome } from './home.js';
import { ideCaller } from './ideClient.js';
import { processIo, runJevCli, type CliIo } from './jev/cli.js';
import { startJev } from './jev/service.js';
import { Service } from './service.js';
import { TERMINAL_DRIVERS } from './terminals/index.js';
import { LAUNCHER_PS1 } from './terminals/windowsTerminal.js';

function scriptsDir(): string {
  const here = path.dirname(fileURLToPath(import.meta.url));
  const candidates = [path.join(here, 'launch'), path.join(here, '..', 'launch')];
  return candidates.find((dir) => existsSync(path.join(dir, LAUNCHER_PS1))) ?? candidates[0]!;
}

export function systemService(home: string, log: (message: string) => void): Service {
  return new Service({ home, scriptsDir: scriptsDir(), platform: process.platform, env: process.env, callIde: ideCaller(), drivers: TERMINAL_DRIVERS, log });
}

export const DETECTION_MAX_AGE_MS = 60 * 60 * 1000;
export const CLI_USAGE = 'Usage: node mcp-server.mjs list-ides | jev <subcommand> | server status|stop [--port <port>]';

export async function runCli(command: string, args: string[], io: CliIo = processIo): Promise<number> {
  const home = agentTabsHome();
  const service = systemService(home, () => undefined);
  if (command === 'jev') {
    const { jev, off } = await startJev(service, { home, env: process.env, platform: process.platform });
    return runJevCli(args, jev, off, io);
  }
  try {
    if (args.length) throw new Error(`list-ides takes no arguments. ${CLI_USAGE}`);
    io.stdout(`${JSON.stringify(await service.listIdes({ detectionMaxAgeMs: DETECTION_MAX_AGE_MS }), null, 2)}\n`);
    return 0;
  } catch (e) {
    io.stderr(`${JSON.stringify({ error: e instanceof Error ? e.message : String(e) })}\n`);
    return 1;
  }
}

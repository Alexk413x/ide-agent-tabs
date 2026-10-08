import { agentTabsHome } from './home.js';
import { probe, stopServer } from './shared/client.js';
import { DEFAULT_PORT, parsePort, PORT_OPTION_ENV } from './shared/state.js';

const USAGE = 'Usage: node mcp-server.mjs server status|stop [--port <port>]';

export async function runServerCli(args: string[], out: (text: string) => void = (text) => void process.stdout.write(text)): Promise<number> {
  const [action, ...rest] = args;
  const at = rest.indexOf('--port');
  const given = at === -1 ? undefined : parsePort(rest[at + 1]);
  if ((action !== 'status' && action !== 'stop') || (at !== -1 && given === undefined) || rest.length !== (at === -1 ? 0 : 2)) {
    out(`${JSON.stringify({ error: USAGE })}\n`);
    return 2;
  }
  const port = given ?? parsePort(process.env[PORT_OPTION_ENV]) ?? DEFAULT_PORT;
  const home = agentTabsHome();
  if (action === 'stop') {
    const result = await stopServer(home, port);
    out(`${JSON.stringify({ port, ...result }, null, 2)}\n`);
    return result.stopped ? 0 : 1;
  }
  const found = await probe(port);
  const status =
    found.kind === 'ours'
      ? { running: true, ...found.health }
      : { port, running: false, ...(found.kind === 'other' ? { problem: `port ${port} belongs to another program` } : {}) };
  out(`${JSON.stringify(status, null, 2)}\n`);
  return found.kind === 'ours' ? 0 : 1;
}

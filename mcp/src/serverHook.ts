import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { agentTabsHome } from './home.js';
import { ensureServer, notifyEnd } from './shared/client.js';
import { DEFAULT_PORT, parsePort, PORT_OPTION_ENV, SERVER_SCRIPT } from './shared/state.js';
import { PACKAGE_VERSION } from './version.js';

const STDIN_WAIT_MS = 1_000;

function readStdin(): Promise<string> {
  return new Promise((resolve) => {
    let text = '';
    const timer = setTimeout(() => resolve(text), STDIN_WAIT_MS);
    process.stdin.setEncoding('utf8');
    process.stdin.on('data', (chunk: string) => (text += chunk));
    process.stdin.on('end', () => {
      clearTimeout(timer);
      resolve(text);
    });
    process.stdin.on('error', () => resolve(text));
  });
}

const print = (text: string) => new Promise<void>((resolve) => process.stdout.write(`${text}\n`, () => resolve()));

async function main(event: string | undefined): Promise<void> {
  const env = process.env;
  const port = parsePort(env[PORT_OPTION_ENV]) ?? DEFAULT_PORT;
  const home = agentTabsHome(env);
  if (event === 'SessionEnd') {
    let reason: unknown;
    try {
      reason = (JSON.parse(await readStdin()) as { reason?: unknown }).reason;
    } catch {}
    const pid = Number(env.CLAUDE_PID);
    if (reason !== 'clear' && Number.isSafeInteger(pid) && pid > 0) await notifyEnd(home, port, pid).catch(() => undefined);
    return;
  }
  const script = path.join(path.dirname(fileURLToPath(import.meta.url)), SERVER_SCRIPT);
  const ensured = await ensureServer({ script, port, home, version: PACKAGE_VERSION, env });
  if (ensured.problem !== undefined) await print(`Agent Tabs: ${ensured.problem}.`);
}

main(process.argv[2]).then(
  () => process.exit(0),
  () => process.exit(0),
);

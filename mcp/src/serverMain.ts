import { appendFileSync, existsSync, readFileSync, statSync, truncateSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { agentTabsHome } from './home.js';
import { CATALOG } from './shared/catalog.generated.js';
import type { Engine } from './shared/engine.js';
import { Front, type Catalog } from './shared/front.js';
import { claimPort, HANDOVER_MS } from './shared/handover.js';
import { Hub } from './shared/hub.js';
import { DEFAULT_PORT, ensureToken, newToken, parsePort, PORT_OPTION_ENV, readState, removeState, serverDir, SERVICE, writeState } from './shared/state.js';
import { PACKAGE_VERSION } from './version.js';

export const IDLE_EXIT_MS = 8 * 60 * 60 * 1000;
const IDLE_CHECK_MS = 60_000;
const STATE_CHECK_MS = 2_000;
const LOG_LIMIT_BYTES = 1024 * 1024;

const home = agentTabsHome();
const portArg = process.argv.indexOf('--port');
const port = parsePort(portArg === -1 ? undefined : process.argv[portArg + 1]) ?? parsePort(process.env[PORT_OPTION_ENV]) ?? DEFAULT_PORT;
const logFile = path.join(serverDir(home), `server-${port}.log`);
const startedAt = new Date().toISOString();

function log(message: string): void {
  try {
    if (existsSync(logFile) && statSync(logFile).size > LOG_LIMIT_BYTES) truncateSync(logFile, 0);
    appendFileSync(logFile, `${new Date().toISOString()} ${process.pid} ${message}\n`, { mode: 0o600 });
  } catch {}
}

process.on('uncaughtException', (e) => log(`uncaught: ${e.stack ?? e.message}`));
process.on('unhandledRejection', (e) => log(`unhandled rejection: ${e instanceof Error ? (e.stack ?? e.message) : String(e)}`));

function scriptsDir(): string {
  const here = path.dirname(fileURLToPath(import.meta.url));
  const candidates = [path.join(here, 'launch'), path.join(here, '..', 'launch')];
  return candidates.find((dir) => existsSync(dir)) ?? candidates[0]!;
}

function jevEnabled(): boolean {
  try {
    const config = JSON.parse(readFileSync(path.join(home, 'config.json'), 'utf8')) as { jev?: { enabled?: unknown } };
    return config.jev?.enabled === true;
  } catch {
    return false;
  }
}

async function catalog(): Promise<Catalog> {
  const jev = jevEnabled();
  return {
    tools: jev ? CATALOG.tools : CATALOG.tools.filter((t) => !String(t.name).startsWith('jev_')),
    instructions: jev ? CATALOG.instructions.jev : CATALOG.instructions.plain,
  };
}

let engine: Promise<Engine> | undefined;
const loadEngine = () =>
  (engine ??= import('./shared/engine.js').then(({ Engine }) => new Engine({ home, scriptsDir: scriptsDir(), env: process.env, log })));

const hub = new Hub({ bind: async (binding) => (await loadEngine()).bind(binding), log });
const token = await ensureToken(home);
const shutdownToken = newToken();
let lastRequest = Date.now();
let stopping = false;

const front = new Front({
  port,
  token,
  shutdownToken,
  serverInfo: { name: SERVICE, version: PACKAGE_VERSION },
  health: () => ({ service: SERVICE, version: PACKAGE_VERSION, pid: process.pid, port, sessions: hub.size, startedAt }),
  catalog,
  call: (identity, params, signal) => hub.call(identity, params, signal),
  end: (pid) => hub.endPid(pid),
  onRequest: () => {
    lastRequest = Date.now();
  },
  onShutdown: () => void stop('asked to stop'),
});

async function stop(why: string): Promise<void> {
  if (stopping) return;
  stopping = true;
  log(`stopping: ${why}`);
  const exit = setTimeout(() => process.exit(0), HANDOVER_MS);
  exit.unref();
  await front.close().catch(() => undefined);
  await hub.close().catch(() => undefined);
  await removeState(home, port, process.pid).catch(() => undefined);
  process.exit(0);
}

if (!(await claimPort({ front, port, home, version: PACKAGE_VERSION, log }))) process.exit(0);
await writeState(home, { pid: process.pid, port, version: PACKAGE_VERSION, startedAt, shutdownToken });
log(`${SERVICE} ${PACKAGE_VERSION} serving http://127.0.0.1:${port}/mcp for ${home}`);
hub.startLiveness();

setInterval(() => {
  if (Date.now() - lastRequest >= IDLE_EXIT_MS && hub.liveSessions() === 0) void stop('idle for 8 hours');
}, IDLE_CHECK_MS).unref();

setInterval(() => {
  void readState(home, port).then((state) => {
    if (state?.pid !== process.pid) void stop(state === undefined ? 'its state file is gone' : `its state file names pid ${state.pid}`);
  });
}, STATE_CHECK_MS).unref();

for (const signal of ['SIGINT', 'SIGTERM', 'SIGHUP'] as const) process.on(signal, () => void stop(signal));

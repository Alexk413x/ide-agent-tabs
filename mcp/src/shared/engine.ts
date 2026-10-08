import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js';
import { Handoffs } from '../handoff.js';
import { ideCaller } from '../ideClient.js';
import { startJev } from '../jev/service.js';
import { setBusyTimeout, SHARED_BUSY_TIMEOUT_MS } from '../messaging/db.js';
import { Messaging } from '../messaging/messaging.js';
import { pollInsteadOfWatch, SHARED_POLL_MS } from '../messaging/wake.js';
import { AGENT_ENV, TAB_ID_ENV } from '../profiles.js';
import { Resumes } from '../resume.js';
import { createServer } from '../server.js';
import { Service } from '../service.js';
import { TERMINAL_DRIVERS } from '../terminals/index.js';
import { PACKAGE_VERSION } from '../version.js';

export const HTTP_MAX_WAIT_S = 240;
const CALL_TIMEOUT_MS = 60 * 60 * 1000;

export interface EngineDeps {
  home: string;
  scriptsDir: string;
  env: NodeJS.ProcessEnv;
  log: (message: string) => void;
}

export interface Binding {
  id: string;
  tab?: string;
  agent: string;
  pid: number;
  pidStart?: number;
  cwd: string;
}

export interface BoundSession {
  readonly id: string;
  call(params: Record<string, unknown>, signal: AbortSignal): Promise<Record<string, unknown>>;
  end(): Promise<void>;
  release(): Promise<void>;
}

const SESSION_ENV = [TAB_ID_ENV, AGENT_ENV, 'IDE_AGENT_TABS_MOD'];

export class Engine {
  private readonly env: NodeJS.ProcessEnv;
  private readonly detection: Service;

  constructor(private readonly deps: EngineDeps) {
    this.env = Object.fromEntries(Object.entries(deps.env).filter(([name]) => !SESSION_ENV.includes(name.toUpperCase())));
    setBusyTimeout(SHARED_BUSY_TIMEOUT_MS);
    pollInsteadOfWatch(SHARED_POLL_MS);
    this.detection = this.service(this.env);
    void this.detection.refreshDetection().catch(() => undefined);
  }

  private service(env: NodeJS.ProcessEnv): Service {
    return new Service({ home: this.deps.home, scriptsDir: this.deps.scriptsDir, platform: process.platform, env, callIde: ideCaller(), drivers: TERMINAL_DRIVERS, log: this.deps.log });
  }

  async bind(binding: Binding): Promise<BoundSession> {
    const { home } = this.deps;
    const env: NodeJS.ProcessEnv = { ...this.env, [AGENT_ENV]: binding.agent, ...(binding.tab !== undefined ? { [TAB_ID_ENV]: binding.tab } : {}) };
    const service = this.service(env);
    const { jev } = await startJev(service, { home, env, platform: process.platform });
    const messaging = new Messaging({
      home,
      env,
      pid: binding.pid,
      ...(binding.pidStart !== undefined ? { pidStart: binding.pidStart } : {}),
      cwd: binding.cwd,
      hosts: service,
      randomId: () => binding.id,
      maxWaitS: HTTP_MAX_WAIT_S,
    });
    await messaging.startRegistered({ log: this.deps.log });
    const handoffs = new Handoffs({ home, env, sessionId: () => messaging.id, openTab: (input) => service.openTab(input), findHost: (id) => service.findHost(id) });
    const resumes = new Resumes({
      home,
      settings: () => service.settings(),
      openTab: (input) => service.openTab(input),
      liveHost: (host, product) => service.liveHost(host, product),
      live: () => messaging.live(),
    });
    const server = createServer(service, jev, messaging, handoffs, resumes);
    const [clientSide, serverSide] = InMemoryTransport.createLinkedPair();
    await server.connect(serverSide);
    const client = new Client({ name: 'claude-code', version: PACKAGE_VERSION });
    await client.connect(clientSide);
    let ended = false;
    const close = async () => {
      await client.close().catch(() => undefined);
      await server.close().catch(() => undefined);
    };
    return {
      get id() {
        return messaging.id;
      },
      async call(params, signal) {
        const result = await client.callTool({ name: String(params.name), arguments: (params.arguments ?? {}) as Record<string, unknown> }, undefined, {
          signal,
          timeout: CALL_TIMEOUT_MS,
        });
        return result as Record<string, unknown>;
      },
      async end() {
        if (ended) return;
        ended = true;
        await messaging.recordEnd().catch(() => undefined);
        messaging.stopSync();
        await close();
      },
      async release() {
        if (ended) return;
        ended = true;
        messaging.stopHeartbeat();
        messaging.stopFollowUps();
        await close();
      },
    };
  }
}

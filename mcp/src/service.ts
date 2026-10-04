import { randomUUID } from 'node:crypto';
import { promises as fs } from 'node:fs';
import path from 'node:path';
import { detect, readDetection, writeDetection, type Detection } from './detection.js';
import { readTextIfExists, removeStaleFiles } from './files.js';
import { IdeError, type IdeCall, type Route } from './ideClient.js';
import { isCmdShim, isInstalled } from './installed.js';
import {
  AGENTS_FILE,
  CONFIG_FILE,
  resolveSettings,
  TAB_ID_ENV,
  type AgentSettings,
} from './profiles.js';
import { isSessionId, updatePresence, withState, type PresenceFile, type Via } from './messaging/sessions.js';
import { isProcessAlive, readRegistry, type Endpoint } from './registry.js';
import { validateOpen, type OpenInput, type OpenRequest } from './request.js';
import { chooseIde, chooseTerminal, type IdeCandidate, type Project } from './routing.js';
import { ORI_AGENTS, planLaunch, type LaunchPlan } from './launchPlan.js';
import { launchSpec } from './spec.js';
import { TabStore } from './tabStore.js';
import { defaultTerminalName } from './terminals/index.js';
import { listPowerShells, pickPowerShell, systemProbe, type ShellProbe } from './terminals/powershell.js';
import type { OpenedTab, TerminalContext, TerminalDriver, TerminalTab } from './terminals/types.js';

export interface ServiceDeps {
  home: string;
  scriptsDir: string;
  platform: NodeJS.Platform;
  env: NodeJS.ProcessEnv;
  callIde: IdeCall;
  drivers: TerminalDriver[];
  isAlive?: (pid: number) => boolean;
  newId?: () => string;
  selfCloseDelayMs?: number;
  shellProbe?: (runShells: boolean) => ShellProbe;
}

export class ToolError extends Error {}

interface IdeInfo {
  endpoint: Endpoint;
  product: string;
  version: string;
  projects: Project[];
}

const SPEC_MAX_AGE_MS = 60 * 60 * 1000;
export const FRESH_TAB_START_MS = 10_000;

function errorText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

function projectsOf(reply: Record<string, unknown>): Project[] {
  const list = Array.isArray(reply.projects) ? reply.projects : [];
  return list
    .filter((p): p is Record<string, unknown> => typeof p === 'object' && p !== null)
    .map((p) => ({ name: String(p.name ?? ''), path: String(p.path ?? ''), focused: p.focused === true }));
}

export class Service {
  private readonly store: TabStore;
  private readonly ctx: TerminalContext;

  constructor(private readonly deps: ServiceDeps) {
    this.store = new TabStore(deps.home);
    this.ctx = { home: deps.home, scriptsDir: deps.scriptsDir, pathVar: deps.env.PATH ?? deps.env.Path ?? '', env: deps.env };
  }

  private get isWindows() {
    return this.deps.platform === 'win32';
  }

  async settings(): Promise<AgentSettings> {
    const agentsPath = path.join(this.deps.home, AGENTS_FILE);
    const configPath = path.join(this.deps.home, CONFIG_FILE);
    const read = (file: string) => readTextIfExists(file).catch(() => undefined);
    const [agents, config] = await Promise.all([read(agentsPath), read(configPath)]);
    return resolveSettings(agents, config, agentsPath, configPath);
  }

  private async registry() {
    return readRegistry(this.deps.home, this.deps.isAlive ?? isProcessAlive);
  }

  private async callIde(endpoint: Endpoint, route: Route, body?: object) {
    try {
      return await this.deps.callIde(endpoint, route, body);
    } catch (e) {
      if (!(e instanceof IdeError) || e.status !== 401) throw e;
      const fresh = (await this.registry()).endpoints.find((f) => f.id === endpoint.id && f.token !== endpoint.token);
      if (!fresh) throw e;
      return this.deps.callIde(fresh, route, body);
    }
  }

  private async infos(endpoints: Endpoint[]) {
    const results = await Promise.all(
      endpoints.map(async (endpoint) => {
        try {
          const reply = await this.callIde(endpoint, 'info');
          return {
            info: {
              endpoint,
              product: typeof reply.product === 'string' ? reply.product : endpoint.product,
              version: typeof reply.version === 'string' ? reply.version : endpoint.version,
              projects: projectsOf(reply),
            } satisfies IdeInfo,
          };
        } catch (e) {
          return { error: { id: endpoint.id, error: errorText(e) } };
        }
      }),
    );
    return {
      infos: results.flatMap((r) => (r.info ? [r.info] : [])),
      errors: results.flatMap((r) => (r.error ? [r.error] : [])),
    };
  }

  private async availableDrivers(): Promise<TerminalDriver[]> {
    const flags = await Promise.all(this.deps.drivers.map((d) => d.available(this.ctx).catch(() => false)));
    return this.deps.drivers.filter((_, i) => flags[i]);
  }

  async refreshDetection(available?: TerminalDriver[]): Promise<Detection> {
    const drivers = available ?? (await this.availableDrivers());
    const detection = await detect({
      home: this.deps.home,
      platform: this.deps.platform,
      env: this.deps.env,
      terminals: drivers.map((d) => ({ id: d.name, name: d.label })),
      ...(this.deps.shellProbe ? { probe: this.deps.shellProbe(true) } : {}),
    });
    await writeDetection(this.deps.home, detection).catch(() => undefined);
    return detection;
  }

  async listIdes() {
    const [{ endpoints, warnings }, settings, drivers] = await Promise.all([this.registry(), this.settings(), this.availableDrivers()]);
    const [{ infos, errors }, capabilities, detection] = await Promise.all([
      this.infos(endpoints),
      Promise.all(drivers.map((d) => d.currentCapabilities?.(this.ctx).catch(() => d.capabilities) ?? d.capabilities)),
      this.refreshDetection(drivers).catch(() => undefined),
    ]);
    return {
      ides: infos.map((i) => ({
        id: i.endpoint.id,
        ide: i.endpoint.ide,
        product: i.product,
        version: i.version,
        projects: i.projects,
      })),
      terminals: drivers.map((d, i) => ({
        id: d.name,
        name: d.label,
        capabilities: capabilities[i],
        preferred: settings.preferredTerminal === d.name,
      })),
      shells: detection?.shells ?? [],
      ...(errors.length ? { errors } : {}),
      ...(warnings.length ? { warnings } : {}),
    };
  }

  async listAgents() {
    const settings = await this.settings();
    const ori = (await readDetection(this.deps.home))?.ori;
    return {
      default: settings.defaultAgent.name,
      agents: settings.profiles.map((p) => ({
        name: p.name,
        label: p.label,
        command: p.command,
        installed: isInstalled(p.command, this.ctx.pathVar, this.isWindows),
        model: p.modelFlag !== undefined,
        ...(ori?.agents.includes(p.name) && ORI_AGENTS.includes(p.name) ? { ori: true } : {}),
      })),
      launchVia: settings.launchVia,
      ...(settings.warnings.length ? { warnings: settings.warnings } : {}),
    };
  }

  async openTab(input: OpenInput) {
    let request: OpenRequest;
    try {
      request = validateOpen(input);
    } catch (e) {
      throw new ToolError(errorText(e));
    }
    const { endpoints } = await this.registry();
    if (request.ide !== undefined) {
      const driver = this.deps.drivers.find((d) => d.name === request.ide);
      if (driver) {
        if (!(await driver.available(this.ctx).catch(() => false))) {
          throw new ToolError(`terminal ${driver.name} is not available on this machine`);
        }
        return this.openInTerminal(driver, request, 'named by ide');
      }
      const endpoint = endpoints.find((e) => e.id === request.ide);
      if (!endpoint) {
        throw new ToolError(`no running IDE or terminal with id ${request.ide}; call list_ides for the ids`);
      }
      return this.openInIde(endpoint, request, 'named by ide');
    }
    const settings = await this.settings();
    const own = this.deps.env[TAB_ID_ENV];
    const callerHost = own ? await this.findHost(own).catch(() => undefined) : undefined;
    const callerTerminal = this.deps.drivers.find((d) => d.name === callerHost);
    if (settings.tabRouting === 'caller' && callerTerminal && (await callerTerminal.available(this.ctx).catch(() => false))) {
      const near = (await this.store.read()).find((t) => t.id === own);
      return this.openInTerminal(callerTerminal, request, "tabRouting is caller; the caller's terminal window", near);
    }
    const { infos, errors } = await this.infos(endpoints);
    const candidates: IdeCandidate[] = infos.map((i) => ({ id: i.endpoint.id, startedAt: i.endpoint.startedAt, projects: i.projects }));
    const choice = chooseIde(candidates, request.path, this.isWindows, callerHost, settings.tabRouting);
    if (choice) return this.openInIde(infos.find((i) => i.endpoint.id === choice.id)!.endpoint, request, choice.reason);

    const available = (await this.availableDrivers()).map((d) => d.name);
    const terminal = chooseTerminal(settings.preferredTerminal, defaultTerminalName(this.deps.platform, available), available);
    if ('error' in terminal) {
      const detail = errors.length ? ` IDE errors: ${errors.map((e) => e.error).join('; ')}` : '';
      throw new ToolError(`${terminal.error}.${detail}`);
    }
    return this.openInTerminal(this.deps.drivers.find((d) => d.name === terminal.name)!, request, terminal.reason);
  }

  private async openInIde(endpoint: Endpoint, request: OpenRequest, reason: string) {
    const body = {
      path: request.path,
      ...(request.agent !== undefined ? { agent: request.agent } : {}),
      ...(request.prompt !== undefined ? { prompt: request.prompt } : {}),
      ...(request.args.length ? { args: request.args } : {}),
      ...(Object.keys(request.env).length ? { env: request.env } : {}),
      ...(request.model !== undefined ? { model: request.model } : {}),
      ...(request.via !== undefined ? { via: request.via } : {}),
    };
    let reply: Record<string, unknown>;
    try {
      reply = await this.callIde(endpoint, 'open', body);
    } catch (e) {
      throw new ToolError(errorText(e));
    }
    await this.markOpened(reply.id, endpoint.id, request.prompt === undefined, {
      via: reply.via === 'ori' ? 'ori' : 'direct',
      ...(typeof reply.project === 'string' && reply.project !== '' ? { project: reply.project } : {}),
    });
    const via = reply.via === 'ori' ? { via: 'ori' } : {};
    return { id: reply.id, ide: endpoint.id, product: endpoint.product, agent: reply.agent, project: reply.project, path: reply.path, reason, ...via };
  }

  private async powerShell(configured: string | undefined): Promise<string | undefined> {
    if (!this.isWindows) return undefined;
    const probe = this.deps.shellProbe?.(false) ?? systemProbe(this.deps.env, false);
    const detected = (await readDetection(this.deps.home))?.shells ?? [];
    const shells = detected.some((s) => probe.exists(s.path)) ? detected : listPowerShells(probe);
    return pickPowerShell(shells, configured, probe.exists);
  }

  private async openInTerminal(driver: TerminalDriver, request: OpenRequest, reason: string, near?: TerminalTab) {
    const settings = await this.settings();
    const profile =
      request.agent === undefined ? settings.defaultAgent : settings.profiles.find((p) => p.name === request.agent);
    if (!profile) throw new ToolError(`unknown agent: ${request.agent}`);
    let plan: LaunchPlan;
    try {
      plan = planLaunch(profile, {
        ...request,
        launchVia: settings.launchVia,
        ori: (await readDetection(this.deps.home))?.ori,
        platform: this.deps.platform,
        cmdShim: this.isWindows && isCmdShim(profile.command, this.ctx.pathVar),
      });
    } catch (e) {
      throw new ToolError(errorText(e));
    }
    const spec = launchSpec((this.deps.newId ?? randomUUID)(), request.path, plan.launch);
    await removeStaleFiles(path.join(this.deps.home, 'launch'), ['.json', '.spec'], SPEC_MAX_AGE_MS);
    const powerShell = await this.powerShell(settings.shell);
    const ctx = powerShell === undefined ? this.ctx : { ...this.ctx, powerShell };
    let opened: OpenedTab;
    try {
      opened = await driver.open(ctx, spec, profile.label, { window: settings.terminalWindow, ...(near ? { near } : {}) });
    } catch (e) {
      throw new ToolError(`${driver.label}: ${errorText(e)}`);
    }
    const { note, ...tab } = opened;
    await this.store.add(tab);
    await this.markOpened(tab.id, driver.name, request.prompt === undefined, { via: plan.via });
    return {
      id: tab.id,
      ide: driver.name,
      product: driver.label,
      agent: profile.name,
      path: request.path,
      reason,
      ...(plan.via === 'ori' ? { via: 'ori' } : {}),
      ...(note !== undefined ? { note } : {}),
    };
  }

  // Some CLIs, such as Codex, run no start hook until their first turn, so a tab opened without a prompt would
  // stay unknown and never be woken. It waits at its prompt once the CLI has had FRESH_TAB_START_MS to start.
  private async markOpened(id: unknown, host: string, fresh: boolean, launch: { via: Via; project?: string }): Promise<void> {
    if (typeof id !== 'string' || !isSessionId(id)) return;
    const at = Date.now() + FRESH_TAB_START_MS;
    await updatePresence(this.deps.home, id, (current) => {
      const base: PresenceFile = { ...(current ?? { id }), ...launch };
      return !fresh || (current?.state !== undefined && current.state !== 'unknown') ? base : withState({ ...base, host }, 'idle', at);
    }).catch(() => undefined);
  }

  async describeHost(host: string): Promise<string | undefined> {
    const driver = this.deps.drivers.find((d) => d.name === host);
    if (driver) return driver.label;
    return (await this.registry()).endpoints.find((e) => e.id === host)?.product;
  }

  private async terminalTabs(only?: TerminalDriver) {
    const tabs = await this.store.read();
    const alive: TerminalTab[] = [];
    const dead = new Set<string>();
    const errors: { id: string; error: string }[] = [];
    for (const driver of only ? [only] : await this.availableDrivers()) {
      const mine = tabs.filter((t) => t.terminal === driver.name);
      if (mine.length === 0) continue;
      try {
        const ids = await driver.alive(this.ctx, mine);
        for (const t of mine) {
          if (ids.has(t.id)) alive.push(t);
          else dead.add(t.id);
        }
      } catch (e) {
        errors.push({ id: driver.name, error: errorText(e) });
      }
    }
    if (dead.size) {
      await this.store.remove(dead);
      await Promise.all(
        tabs.filter((t) => dead.has(t.id) && t.pidFile).map((t) => fs.rm(t.pidFile!, { force: true }).catch(() => undefined)),
      );
    }
    return { tabs: alive, errors };
  }

  async listTabs(ide?: string) {
    const { endpoints } = await this.registry();
    const driver = ide === undefined ? undefined : this.deps.drivers.find((d) => d.name === ide);
    const chosen = ide === undefined ? endpoints : endpoints.filter((e) => e.id === ide);
    if (ide !== undefined && !driver && chosen.length === 0) {
      throw new ToolError(`no running IDE or terminal with id ${ide}; call list_ides for the ids`);
    }
    const tabs: Record<string, unknown>[] = [];
    const errors: { id: string; error: string }[] = [];
    await Promise.all(
      (driver ? [] : chosen).map(async (endpoint) => {
        try {
          const reply = await this.callIde(endpoint, 'list');
          for (const t of Array.isArray(reply.tabs) ? reply.tabs : []) tabs.push({ ...(t as object), ide: endpoint.id });
        } catch (e) {
          errors.push({ id: endpoint.id, error: errorText(e) });
        }
      }),
    );
    if (ide === undefined || driver) {
      const terminal = await this.terminalTabs(driver);
      for (const t of terminal.tabs) tabs.push({ id: t.id, agent: t.agent, path: t.path, ide: t.terminal });
      errors.push(...terminal.errors);
    }
    return { tabs, ...(errors.length ? { errors } : {}) };
  }

  async closeTab(id?: string) {
    const target = id ?? this.deps.env[TAB_ID_ENV];
    if (!target) {
      throw new ToolError(`no id given, and ${TAB_ID_ENV} is not set, so this session was not opened as an agent tab`);
    }
    const self = target === this.deps.env[TAB_ID_ENV];
    const record = (await this.store.read()).find((t) => t.id === target);
    if (record) {
      const driver = this.deps.drivers.find((d) => d.name === record.terminal);
      if (!driver) throw new ToolError(`tab ${target} belongs to terminal ${record.terminal}, which this server can't drive`);
      const close = async () => {
        await driver.close(this.ctx, record);
        await this.store.remove(new Set([target]));
      };
      if (self) {
        setTimeout(() => void close().catch(() => undefined), this.deps.selfCloseDelayMs ?? 500);
        return { id: target, ide: driver.name, closing: true };
      }
      try {
        await close();
      } catch (e) {
        throw new ToolError(`${driver.label}: ${errorText(e)}`);
      }
      return { id: target, ide: driver.name, closed: true };
    }
    const owner = await this.ideOwner(target);
    if (!owner) throw new ToolError(`no open agent tab with id ${target}; list_tabs shows the open ones`);
    try {
      await this.callIde(owner, 'close', { id: target });
    } catch (e) {
      throw new ToolError(errorText(e));
    }
    return { id: target, ide: owner.id, closed: true };
  }

  private async ideOwner(id: string): Promise<Endpoint | undefined> {
    const { endpoints } = await this.registry();
    const owners = await Promise.all(
      endpoints.map(async (endpoint) => {
        try {
          const reply = await this.callIde(endpoint, 'list');
          const tabs = Array.isArray(reply.tabs) ? (reply.tabs as { id?: unknown }[]) : [];
          return tabs.some((t) => t.id === id) ? endpoint : undefined;
        } catch {
          return undefined;
        }
      }),
    );
    return owners.find((o) => o !== undefined);
  }

  async findHost(id: string): Promise<string | undefined> {
    const record = (await this.store.read()).find((t) => t.id === id);
    if (record) return record.terminal;
    return (await this.ideOwner(id))?.id;
  }

  async typeInto(id: string, host: string, text: string): Promise<{ ok: true } | { ok: false; reason: string }> {
    const driver = this.deps.drivers.find((d) => d.name === host);
    if (driver) {
      if (!driver.input) return { ok: false, reason: `${driver.label} can't take input from outside` };
      const record = (await this.store.read()).find((t) => t.id === id && t.terminal === host);
      if (!record) return { ok: false, reason: `no ${driver.label} tab with id ${id}` };
      try {
        await driver.input(this.ctx, record, text);
        return { ok: true };
      } catch (e) {
        return { ok: false, reason: `${driver.label}: ${errorText(e)}` };
      }
    }
    const endpoint = (await this.registry()).endpoints.find((e) => e.id === host);
    if (!endpoint) return { ok: false, reason: `no running IDE or terminal with id ${host}` };
    try {
      await this.callIde(endpoint, 'input', { id, text });
      return { ok: true };
    } catch (e) {
      return { ok: false, reason: errorText(e) };
    }
  }
}

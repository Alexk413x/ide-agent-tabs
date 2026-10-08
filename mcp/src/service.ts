import { randomUUID } from 'node:crypto';
import { promises as fs } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { detect, readDetection, writeDetection, type Detection } from './detection.js';
import { readTextIfExists, removeStaleFiles } from './files.js';
import { findIdeEntry, productMatchesName, type IdeEntry } from './ideCatalog.js';
import { IdeError, type IdeCall, type Route } from './ideClient.js';
import { discoverIdes, ideLaunchCommand, launchEnvironment, spawnIde, systemDiscoveryFs, type IdeInstall } from './ideInstalls.js';
import { isCmdShim, isInstalled } from './installed.js';
import {
  AGENTS_FILE,
  CONFIG_FILE,
  resolveFocus,
  resolveSettings,
  TAB_ID_ENV,
  type AgentSettings,
} from './profiles.js';
import { recordEnded, transcriptDirs, type TranscriptDirs } from './messaging/closed.js';
import { isSessionId, readPresence, updatePresence, withState, type PresenceFile, type Via } from './messaging/sessions.js';
import { isProcessAlive, readRegistry, type Endpoint } from './registry.js';
import { validateOpen, type OpenInput, type OpenRequest } from './request.js';
import { checkRevealTarget, fileManagerCommand, systemReveal, type RevealDeps } from './reveal.js';
import { chooseIde, chooseTerminal, projectDepth, type IdeCandidate, type Project } from './routing.js';
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
  transcripts?: TranscriptDirs;
  reveal?: RevealDeps;
  ides?: IdeLauncher;
  ideWait?: { pollMs?: number; syncMs?: number; progressMs?: number };
  log?: (message: string) => void;
}

export interface IdeLauncher {
  discover(): IdeInstall[];
  launch(install: IdeInstall, folder: string): Promise<void>;
}

export interface OpenTabOptions {
  wait?: 'full' | 'background';
  onProgress?: (elapsedMs: number, totalMs: number, message: string) => void;
}

interface Started {
  endpoint?: Endpoint;
  why?: string;
}

export const systemIdes = (platform: NodeJS.Platform, env: NodeJS.ProcessEnv): IdeLauncher => ({
  discover: () => discoverIdes({ platform, env, userHome: os.homedir(), arch: process.arch, fs: systemDiscoveryFs }),
  launch: (install, folder) => spawnIde(ideLaunchCommand(install, folder, platform, env.ComSpec ?? env.COMSPEC), launchEnvironment(env)),
});

export class ToolError extends Error {}

interface IdeInfo {
  endpoint: Endpoint;
  product: string;
  version: string;
  projects: Project[];
}

const SPEC_MAX_AGE_MS = 60 * 60 * 1000;
export const FRESH_TAB_START_MS = 10_000;
export const IDE_POLL_MS = 500;
export const IDE_SYNC_WAIT_MS = 40_000;
const IDE_PROGRESS_MS = 5_000;

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
  private readonly starting = new Map<string, Promise<Started>>();
  private readonly background = new Set<Promise<unknown>>();

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

  // Detection probes every shell and takes seconds on Windows, so a caller that can live with a recent result
  // passes the age it accepts; the session start hook and the shared server refresh the file.
  private async detection(drivers: TerminalDriver[], maxAgeMs?: number): Promise<Detection | undefined> {
    if (maxAgeMs !== undefined) {
      const known = await readDetection(this.deps.home).catch(() => undefined);
      const age = Date.now() - Date.parse(known?.detectedAt ?? '');
      if (known !== undefined && age >= 0 && age < maxAgeMs) return known;
    }
    return this.refreshDetection(drivers).catch(() => undefined);
  }

  async listIdes(options: { detectionMaxAgeMs?: number } = {}) {
    const [{ endpoints, warnings }, settings, drivers] = await Promise.all([this.registry(), this.settings(), this.availableDrivers()]);
    const [{ infos, errors }, capabilities, detection] = await Promise.all([
      this.infos(endpoints),
      Promise.all(drivers.map((d) => d.currentCapabilities?.(this.ctx).catch(() => d.capabilities) ?? d.capabilities)),
      this.detection(drivers, options.detectionMaxAgeMs),
    ]);
    const running = infos.map((i) => i.product);
    const listed = new Set<string>();
    const installed = this.discover()
      .filter((i) => !running.some((product) => productMatchesName(product, i.key)) && !listed.has(i.key) && listed.add(i.key))
      .map((i) => ({ name: i.key, product: i.product, kind: i.kind, ...(i.version !== undefined ? { version: i.version } : {}) }));
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
      installed,
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

  private ides(): IdeLauncher {
    return this.deps.ides ?? systemIdes(this.deps.platform, this.deps.env);
  }

  private discover(): IdeInstall[] {
    try {
      return this.ides().discover();
    } catch {
      return [];
    }
  }

  async settled(): Promise<void> {
    while (this.background.size) await Promise.allSettled([...this.background]);
  }

  async openTab(input: OpenInput, options: OpenTabOptions = {}) {
    let request: OpenRequest;
    try {
      request = validateOpen(input);
    } catch (e) {
      throw new ToolError(errorText(e));
    }
    const [{ endpoints }, settings] = await Promise.all([this.registry(), this.settings()]);
    request = { ...request, focus: resolveFocus(settings.focusNewTabs, request.focus) };
    if (request.ide !== undefined) {
      const driver = this.deps.drivers.find((d) => d.name === request.ide);
      if (driver) {
        if (!(await driver.available(this.ctx).catch(() => false))) {
          throw new ToolError(`terminal ${driver.name} is not available on this machine`);
        }
        return this.openInTerminal(driver, request, 'named by ide');
      }
      const endpoint = endpoints.find((e) => e.id === request.ide);
      if (endpoint) return this.openInIde(endpoint, request, 'named by ide');
      return this.openNamed(request.ide, request, endpoints, settings, options);
    }
    return this.route(request, endpoints, settings);
  }

  private async route(request: OpenRequest, endpoints: Endpoint[], settings: AgentSettings) {
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

  private async openNamed(name: string, request: OpenRequest, endpoints: Endpoint[], settings: AgentSettings, options: OpenTabOptions) {
    const entry = findIdeEntry(name);
    const matching = endpoints.filter((e) => productMatchesName(e.product, name));
    if (matching.length) {
      const { infos } = await this.infos(matching);
      const ranked = infos
        .map((i) => ({ i, depth: Math.max(-1, ...i.projects.map((p) => projectDepth(p.path, request.path, this.isWindows) ?? -1)) }))
        .sort((a, b) => b.depth - a.depth || b.i.endpoint.startedAt - a.i.endpoint.startedAt);
      const best = ranked[0];
      const endpoint = best?.i.endpoint ?? [...matching].sort((a, b) => b.startedAt - a.startedAt)[0]!;
      const label = entry?.name ?? endpoint.product;
      const reason = best && best.depth >= 0 ? `${label} is running; an open project contains the path` : `${label} is running; most recently started`;
      return this.openInIde(endpoint, request, reason);
    }
    if (!entry) {
      throw new ToolError(
        `no running IDE or terminal with id ${name}, and no IDE by that name; pass an IDE name such as vscode, idea or android-studio, or an id that the Agent Tabs command line's list-ides prints`,
      );
    }
    const install = this.discover().find((i) => i.key === entry.key);
    if (!install) return this.fallback(request, `${entry.name} isn't installed`);
    const budgetMs = settings.ideStartTimeoutSec * 1000;
    const started = this.startIde(entry, install, request.path, new Set(endpoints.map((e) => e.id)), budgetMs);
    const finish = async (s: Started) => {
      if (!s.endpoint) return this.fallback(request, s.why!);
      try {
        return await this.openInIde(s.endpoint, request, `started ${entry.name}`);
      } catch (e) {
        return this.fallback(request, `${entry.name} started but couldn't open the tab: ${errorText(e)}`);
      }
    };
    const message = `Waiting for ${entry.name} to load`;
    if (options.wait !== 'background') return finish(await this.waitFor(started, budgetMs, budgetMs, message, options.onProgress) ?? (await started));
    const syncMs = Math.min(this.deps.ideWait?.syncMs ?? IDE_SYNC_WAIT_MS, budgetMs);
    const early = await this.waitFor(started, syncMs, budgetMs, message, options.onProgress);
    if (early) return finish(early);
    const later = started.then(finish).then(
      (r) => this.log(`opened tab ${String(r.id)} in ${String(r.product)} after starting ${entry.name}`),
      (e) => this.log(`couldn't open the tab after starting ${entry.name}: ${errorText(e)}`),
    );
    this.background.add(later);
    void later.finally(() => this.background.delete(later));
    return {
      pending: true,
      ide: entry.key,
      product: entry.name,
      agent: request.agent ?? settings.defaultAgent.name,
      path: request.path,
      reason: `started ${entry.name}; it is still loading`,
      note:
        `${entry.name} is starting. The agent tab opens there once it loads, up to ${settings.ideStartTimeoutSec} s after the launch. ` +
        "If it doesn't load by then, the tab opens in the caller's IDE or terminal instead.",
    };
  }

  private log(message: string): void {
    this.deps.log?.(message);
  }

  private startIde(entry: IdeEntry, install: IdeInstall, folder: string, before: Set<string>, budgetMs: number): Promise<Started> {
    const running = this.starting.get(entry.key);
    if (running) return running;
    const pollMs = this.deps.ideWait?.pollMs ?? IDE_POLL_MS;
    const seconds = Math.round(budgetMs / 100) / 10;
    const piece = entry.kind === 'jetbrains' ? 'plugin' : 'extension';
    const start = (async (): Promise<Started> => {
      const deadline = Date.now() + budgetMs;
      try {
        await this.ides().launch(install, folder);
      } catch (e) {
        return { why: `${entry.name} couldn't start: ${errorText(e)}` };
      }
      let registered = false;
      while (Date.now() < deadline) {
        await new Promise((r) => setTimeout(r, Math.min(pollMs, Math.max(0, deadline - Date.now()))));
        const fresh = (await this.registry()).endpoints.filter((e) => !before.has(e.id) && entry.product.test(e.product));
        for (const endpoint of fresh) {
          try {
            const reply = await this.callIde(endpoint, 'info');
            registered = true;
            if (projectsOf(reply).length) return { endpoint };
          } catch {
            continue;
          }
        }
      }
      return {
        why: registered
          ? `${entry.name} started but opened no project within ${seconds} s`
          : `${entry.name} started but didn't register within ${seconds} s; if the Agent Tabs ${piece} isn't installed in it, run /ide-agent-tabs:setup`,
      };
    })().finally(() => this.starting.delete(entry.key));
    this.starting.set(entry.key, start);
    return start;
  }

  private async waitFor(
    started: Promise<Started>,
    limitMs: number,
    totalMs: number,
    message: string,
    onProgress: OpenTabOptions['onProgress'],
  ): Promise<Started | undefined> {
    const begin = Date.now();
    let timer: NodeJS.Timeout | undefined;
    const ticker = onProgress ? setInterval(() => onProgress(Date.now() - begin, totalMs, message), this.deps.ideWait?.progressMs ?? IDE_PROGRESS_MS) : undefined;
    try {
      onProgress?.(0, totalMs, message);
      return await Promise.race([started, new Promise<undefined>((r) => (timer = setTimeout(() => r(undefined), limitMs)))]);
    } finally {
      clearTimeout(timer);
      clearInterval(ticker);
    }
  }

  private async fallback(request: OpenRequest, why: string): Promise<Record<string, unknown> & { reason: string; product: string }> {
    const [{ endpoints }, settings] = await Promise.all([this.registry(), this.settings()]);
    const own = this.deps.env[TAB_ID_ENV];
    const host = own ? await this.findHost(own).catch(() => undefined) : undefined;
    const endpoint = endpoints.find((e) => e.id === host);
    const driver = this.deps.drivers.find((d) => d.name === host);
    let opened: Record<string, unknown> & { reason: string; product: string; note?: string };
    if (endpoint) {
      opened = await this.openInIde(endpoint, request, `${why}; the caller's IDE`);
    } else if (driver && (await driver.available(this.ctx).catch(() => false))) {
      const near = (await this.store.read()).find((t) => t.id === own);
      opened = await this.openInTerminal(driver, request, `${why}; the caller's terminal`, near);
    } else {
      const routed = await this.route(request, endpoints, settings);
      opened = { ...routed, reason: `${why}; ${routed.reason}` };
    }
    const note = `${why}, so the tab opened in ${opened.product} instead.`;
    return { ...opened, note: opened.note === undefined ? note : `${note} ${opened.note}` };
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
      focus: request.focus === true,
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
      ...(request.model !== undefined ? { model: request.model } : {}),
      product: endpoint.product,
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
      opened = await driver.open(ctx, spec, profile.label, { window: settings.terminalWindow, focus: request.focus === true, ...(near ? { near } : {}) });
    } catch (e) {
      throw new ToolError(`${driver.label}: ${errorText(e)}`);
    }
    const { note, ...tab } = opened;
    await this.store.add(tab);
    await this.markOpened(tab.id, driver.name, request.prompt === undefined, {
      via: plan.via,
      ...(request.model !== undefined ? { model: request.model } : {}),
      product: driver.label,
    });
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
  private async markOpened(id: unknown, host: string, fresh: boolean, launch: { via: Via; project?: string; model?: string; product?: string }): Promise<void> {
    if (typeof id !== 'string' || !isSessionId(id)) return;
    const at = Date.now() + FRESH_TAB_START_MS;
    await updatePresence(this.deps.home, id, (current) => {
      const base: PresenceFile = { ...(current ?? { id }), ...launch };
      return !fresh || (current?.state !== undefined && current.state !== 'unknown') ? base : withState({ ...base, host }, 'idle', at);
    }).catch(() => undefined);
  }

  async liveHost(host: string | null, product: string | null): Promise<string | undefined> {
    const driver = host === null ? undefined : this.deps.drivers.find((d) => d.name === host);
    if (driver) return (await driver.available(this.ctx).catch(() => false)) ? driver.name : undefined;
    const { endpoints } = await this.registry();
    return (endpoints.find((e) => e.id === host) ?? endpoints.find((e) => product !== null && e.product === product))?.id;
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
      throw new ToolError(`no running IDE or terminal with id ${ide}; the Agent Tabs command line's list-ides prints the ids`);
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

  async reveal(
    target: string,
    preferred: string | undefined,
    sessionFolders: readonly string[],
  ): Promise<{ ok: true; ide: string; product: string; path: string } | { ok: false; reason: string }> {
    const { endpoints } = await this.registry();
    const { infos } = await this.infos(endpoints);
    const revealDeps = this.deps.reveal ?? systemReveal(this.deps.platform);
    let real: string;
    try {
      real = await checkRevealTarget(target, [...sessionFolders, ...infos.flatMap((i) => i.projects.map((p) => p.path))], revealDeps);
    } catch (e) {
      return { ok: false, reason: errorText(e) };
    }
    const order = [...endpoints.filter((e) => e.id === preferred), ...endpoints.filter((e) => e.id !== preferred)];
    const errors: string[] = [];
    for (const endpoint of order) {
      try {
        await this.callIde(endpoint, 'reveal', { path: real });
        return { ok: true, ide: endpoint.id, product: endpoint.product, path: real };
      } catch (e) {
        errors.push(errorText(e));
      }
    }
    if (!order.length) errors.push('no IDE is running');
    try {
      await revealDeps.open(real);
      return { ok: true, ide: 'system', product: fileManagerCommand(this.deps.platform), path: real };
    } catch (e) {
      errors.push(errorText(e));
    }
    return { ok: false, reason: errors.join('; ') };
  }

  async closeTab(id?: string) {
    const target = id ?? this.deps.env[TAB_ID_ENV];
    if (!target) {
      throw new ToolError(`no id given, and ${TAB_ID_ENV} is not set, so this session was not opened as an agent tab`);
    }
    const self = target === this.deps.env[TAB_ID_ENV];
    const ending = await readPresence(this.deps.home, target).catch(() => undefined);
    const ended = () => (ending ? recordEnded(this.deps.home, ending, Date.now(), this.deps.transcripts ?? transcriptDirs(this.deps.env)).catch(() => undefined) : undefined);
    const record = (await this.store.read()).find((t) => t.id === target);
    if (record) {
      const driver = this.deps.drivers.find((d) => d.name === record.terminal);
      if (!driver) throw new ToolError(`tab ${target} belongs to terminal ${record.terminal}, which this server can't drive`);
      const close = async () => {
        await driver.close(this.ctx, record);
        await this.store.remove(new Set([target]));
      };
      if (self) {
        await ended();
        setTimeout(() => void close().catch(() => undefined), this.deps.selfCloseDelayMs ?? 500);
        return { id: target, ide: driver.name, closing: true };
      }
      try {
        await close();
      } catch (e) {
        throw new ToolError(`${driver.label}: ${errorText(e)}`);
      }
      await ended();
      return { id: target, ide: driver.name, closed: true };
    }
    const owner = await this.ideOwner(target);
    if (!owner) throw new ToolError(`no open agent tab with id ${target}; list_tabs shows the open ones`);
    try {
      await this.callIde(owner, 'close', { id: target });
    } catch (e) {
      throw new ToolError(errorText(e));
    }
    await ended();
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

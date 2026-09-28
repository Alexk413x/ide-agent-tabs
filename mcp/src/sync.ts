import { existsSync, promises as fs } from 'node:fs';
import path from 'node:path';
import { ensurePrivateDir, readTextIfExists, writeAtomically } from './files.js';
import { findOnPath } from './installed.js';
import { run, type RunResult } from './process.js';
import { refreshServerCopy, serverCopyDir, serverHash } from './serverCopy.js';

export const EXTENSION_ID = 'alexk413x.ide-agent-tabs';
export const JETBRAINS_PLUGIN_ID = 'dev.alexk.ide-agent-tabs';
export const JETBRAINS_SINCE_BUILD = '262.10315';
export const VSIX_NAME = 'ide-agent-tabs.vsix';
export const JETBRAINS_ZIP_NAME = 'ide-agent-tabs-jetbrains.zip';
export const EDITOR_CLIS = ['code', 'code-insiders', 'cursor', 'windsurf', 'codium', 'antigravity-ide'];
export const MAX_ATTEMPTS = 3;
export const LOCK_STALE_MS = 5 * 60_000;
const LIST_TIMEOUT_MS = 15_000;
const INSTALL_TIMEOUT_MS = 40_000;
const VERSION = /^[0-9A-Za-z.+-]{1,64}$/;

export interface Versions {
  vscode: string;
  jetbrains: string;
}

export interface SyncState extends Versions {
  syncedAt: string;
  failures: number;
  server?: string;
}

export interface Bundle {
  versions: Versions;
  vsix: string;
  zip: string;
}

export interface SyncContext {
  bundleDir: string;
  serverDir: string;
  home: string;
  platform: NodeJS.Platform;
  env: NodeJS.ProcessEnv;
  userHome: string;
}

export interface EditorCli {
  cli: string;
  path: string;
}

export function compareVersions(a: string, b: string): number {
  const parts = (v: string) => v.split('.').map((p) => Number.parseInt(p, 10) || 0);
  const pa = parts(a);
  const pb = parts(b);
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const diff = (pa[i] ?? 0) - (pb[i] ?? 0);
    if (diff !== 0) return Math.sign(diff);
  }
  return 0;
}

export function parseExtensionList(stdout: string): Map<string, string> {
  const extensions = new Map<string, string>();
  for (const line of stdout.split(/\r?\n/)) {
    const match = line.trim().match(/^([\w-]+\.[\w.-]+)@(\S+)$/);
    if (match) extensions.set(match[1]!.toLowerCase(), match[2]!);
  }
  return extensions;
}

export function fileUrl(file: string, platform: NodeJS.Platform): string {
  const forward = platform === 'win32' ? file.replace(/\\/g, '/') : file;
  const absolute = forward.startsWith('/') ? forward : `/${forward}`;
  return `file://${encodeURI(absolute).replace(/[?#]/g, (c) => encodeURIComponent(c))}`;
}

const escapeXml = (s: string) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

export function updatePluginsXml(version: string, zipUrl: string): string {
  return [
    '<plugins>',
    `  <plugin id="${JETBRAINS_PLUGIN_ID}" url="${escapeXml(zipUrl)}" version="${escapeXml(version)}">`,
    `    <idea-version since-build="${JETBRAINS_SINCE_BUILD}"/>`,
    '    <name>Agent Tabs</name>',
    '    <vendor>Alexk413x</vendor>',
    '    <description>Opens AI coding-agent sessions in editor tabs.</description>',
    '  </plugin>',
    '</plugins>',
    '',
  ].join('\n');
}

export function repositoryVersion(xml: string): string | undefined {
  for (const tag of xml.match(/<plugin\s[^>]*>/g) ?? []) {
    if (!tag.includes(`id="${JETBRAINS_PLUGIN_ID}"`)) continue;
    return tag.match(/\sversion="([^"]*)"/)?.[1];
  }
  return undefined;
}

export function editorCliLocations(platform: NodeJS.Platform, env: NodeJS.ProcessEnv, userHome: string): Record<string, string[]> {
  const locations: Record<string, string[]> = Object.fromEntries(EDITOR_CLIS.map((cli) => [cli, []]));
  if (platform === 'win32') {
    const programs = env.LOCALAPPDATA ? path.win32.join(env.LOCALAPPDATA, 'Programs') : undefined;
    const programFiles = env.ProgramFiles ?? env.PROGRAMFILES;
    const add = (cli: string, root: string | undefined, ...rest: string[]) => {
      if (root) locations[cli]!.push(path.win32.join(root, ...rest, `${cli}.cmd`));
    };
    add('code', programs, 'Microsoft VS Code', 'bin');
    add('code', programFiles, 'Microsoft VS Code', 'bin');
    add('code-insiders', programs, 'Microsoft VS Code Insiders', 'bin');
    add('code-insiders', programFiles, 'Microsoft VS Code Insiders', 'bin');
    add('cursor', programs, 'cursor', 'resources', 'app', 'bin');
    add('windsurf', programs, 'Windsurf', 'bin');
    add('codium', programs, 'VSCodium', 'bin');
    add('codium', programFiles, 'VSCodium', 'bin');
    add('antigravity-ide', programs, 'Antigravity IDE', 'bin');
  } else if (platform === 'darwin') {
    const apps: [string, string][] = [
      ['code', 'Visual Studio Code'],
      ['code-insiders', 'Visual Studio Code - Insiders'],
      ['cursor', 'Cursor'],
      ['windsurf', 'Windsurf'],
      ['codium', 'VSCodium'],
      ['antigravity-ide', 'Antigravity IDE'],
      ['antigravity-ide', 'Antigravity'],
    ];
    for (const root of ['/Applications', path.posix.join(userHome, 'Applications')]) {
      for (const [cli, app] of apps) {
        locations[cli]!.push(path.posix.join(root, `${app}.app`, 'Contents', 'Resources', 'app', 'bin', cli));
      }
    }
  }
  return locations;
}

function cliFileNames(cli: string, platform: NodeJS.Platform): string[] {
  return platform === 'win32' ? [`${cli}.cmd`, `${cli}.exe`] : [cli];
}

export function findCliOnPath(cli: string, ctx: Pick<SyncContext, 'platform' | 'env'>): string | undefined {
  const pathVar = ctx.env.PATH ?? ctx.env.Path ?? '';
  return cliFileNames(cli, ctx.platform)
    .map((name) => findOnPath(pathVar, name))
    .find((p) => p !== undefined);
}

export function findEditorClis(ctx: Pick<SyncContext, 'platform' | 'env' | 'userHome'>, exists: (file: string) => boolean = existsSync): EditorCli[] {
  const locations = editorCliLocations(ctx.platform, ctx.env, ctx.userHome);
  const found: EditorCli[] = [];
  for (const cli of EDITOR_CLIS) {
    const file = findCliOnPath(cli, ctx) ?? locations[cli]!.find((p) => exists(p));
    if (file) found.push({ cli, path: file });
  }
  return found;
}

export function resolveEditorCli(nameOrPath: string, ctx: Pick<SyncContext, 'platform' | 'env' | 'userHome'>, exists: (file: string) => boolean = existsSync): EditorCli | undefined {
  const pathApi = ctx.platform === 'win32' ? path.win32 : path.posix;
  if (pathApi.isAbsolute(nameOrPath)) {
    const candidates = ctx.platform === 'win32' && pathApi.extname(nameOrPath) === '' ? cliFileNames(nameOrPath, ctx.platform) : [nameOrPath];
    const file = candidates.find((p) => exists(p));
    return file ? { cli: pathApi.basename(file, pathApi.extname(file)), path: file } : undefined;
  }
  if (/[\\/]/.test(nameOrPath)) return undefined;
  const file = findCliOnPath(nameOrPath, ctx) ?? (editorCliLocations(ctx.platform, ctx.env, ctx.userHome)[nameOrPath] ?? []).find((p) => exists(p));
  return file ? { cli: nameOrPath, path: file } : undefined;
}

// Node refuses to spawn a .cmd or .bat file without a shell, and cmd.exe gives its own meaning to these
// characters even inside quotes, so text that holds one is refused instead of escaped.
const CMD_SPECIAL = /["%^&|<>!\r\n]/;

export function cliInvocation(cli: string, args: string[], platform: NodeJS.Platform, comspec: string | undefined) {
  if (platform !== 'win32' || !/\.(cmd|bat)$/i.test(cli)) return { command: cli, args, windowsVerbatimArguments: false };
  const all = [cli, ...args];
  const unsafe = all.find((a) => CMD_SPECIAL.test(a));
  if (unsafe !== undefined) throw new Error(`cmd.exe can't safely run with the argument ${unsafe}`);
  return {
    command: comspec || 'cmd.exe',
    args: ['/d', '/s', '/c', `"${all.map((a) => `"${a}"`).join(' ')}"`],
    windowsVerbatimArguments: true,
  };
}

type CliContext = Pick<SyncContext, 'platform' | 'env'>;

export async function runCliResult(ctx: CliContext, cli: string, args: string[], timeoutMs: number, cwd?: string): Promise<RunResult> {
  const inv = cliInvocation(cli, args, ctx.platform, ctx.env.ComSpec ?? ctx.env.COMSPEC);
  return run(inv.command, inv.args, { timeoutMs, env: ctx.env, cwd, windowsVerbatimArguments: inv.windowsVerbatimArguments });
}

export function cliFailure(cli: string, args: string[], result: RunResult): Error {
  const detail = (result.stderr.trim() || result.stdout.trim()).split(/\r?\n/).slice(-3).join(' ');
  return new Error(`${path.basename(cli)} ${args[0]} exited with ${result.code}${detail ? `: ${detail}` : ''}`);
}

async function runCli(ctx: CliContext, cli: string, args: string[], timeoutMs: number, cwd?: string): Promise<string> {
  const result = await runCliResult(ctx, cli, args, timeoutMs, cwd);
  if (result.code !== 0) throw cliFailure(cli, args, result);
  return result.stdout;
}

export async function installedExtensionVersion(ctx: SyncContext, cli: string): Promise<string | undefined> {
  const stdout = await runCli(ctx, cli, ['--list-extensions', '--show-versions'], LIST_TIMEOUT_MS);
  return parseExtensionList(stdout).get(EXTENSION_ID);
}

export async function installVsix(ctx: SyncContext, cli: string, vsix: string): Promise<void> {
  await runCli(ctx, cli, ['--install-extension', vsix, '--force'], INSTALL_TIMEOUT_MS);
}

export async function readBundle(bundleDir: string): Promise<Bundle> {
  const raw = JSON.parse(await fs.readFile(path.join(bundleDir, 'versions.json'), 'utf8')) as Partial<Versions>;
  for (const part of ['vscode', 'jetbrains'] as const) {
    if (typeof raw[part] !== 'string' || !VERSION.test(raw[part])) throw new Error(`versions.json has no valid ${part} version`);
  }
  return {
    versions: { vscode: raw.vscode!, jetbrains: raw.jetbrains! },
    vsix: path.join(bundleDir, VSIX_NAME),
    zip: path.join(bundleDir, JETBRAINS_ZIP_NAME),
  };
}

export function parseSyncState(text: string | undefined): SyncState | undefined {
  if (!text) return undefined;
  try {
    const raw = JSON.parse(text) as Partial<SyncState>;
    if (typeof raw.vscode !== 'string' || typeof raw.jetbrains !== 'string') return undefined;
    return {
      vscode: raw.vscode,
      jetbrains: raw.jetbrains,
      syncedAt: typeof raw.syncedAt === 'string' ? raw.syncedAt : '',
      failures: typeof raw.failures === 'number' ? raw.failures : 0,
      ...(typeof raw.server === 'string' ? { server: raw.server } : {}),
    };
  } catch {
    return undefined;
  }
}

export function needsSync(state: SyncState | undefined, bundled: Versions): boolean {
  if (!state || state.vscode !== bundled.vscode || state.jetbrains !== bundled.jetbrains) return true;
  return state.failures > 0 && state.failures < MAX_ATTEMPTS;
}

export function nextSyncState(previous: SyncState | undefined, bundled: Versions, failed: boolean, now: Date): SyncState {
  const sameVersions = previous?.vscode === bundled.vscode && previous?.jetbrains === bundled.jetbrains;
  const failures = failed ? (sameVersions ? previous!.failures : 0) + 1 : 0;
  return { ...bundled, syncedAt: now.toISOString(), failures, ...(previous?.server ? { server: previous.server } : {}) };
}

export async function serverToRefresh(state: SyncState | undefined, copyExists: boolean, bundledHash: () => Promise<string>): Promise<string | undefined> {
  if (!copyExists) return undefined;
  const hash = await bundledHash();
  return state?.server === hash ? undefined : hash;
}

export function hookMessage(bundled: Versions, updatedEditors: string[], jetbrainsUpdated: boolean): string | undefined {
  const parts: string[] = [];
  if (updatedEditors.length > 0) {
    parts.push(`updated the VS Code extension to ${bundled.vscode} in ${updatedEditors.join(', ')} (reload their windows)`);
  }
  if (jetbrainsUpdated) parts.push(`the JetBrains plugin ${bundled.jetbrains} is ready in each JetBrains IDE's plugin updates`);
  return parts.length > 0 ? `Agent Tabs: ${parts.join('; ')}.` : undefined;
}

const syncFile = (home: string) => path.join(home, 'synced.json');
export const repositoryDir = (home: string) => path.join(home, 'repository');

export async function readSyncState(home: string): Promise<SyncState | undefined> {
  return parseSyncState(await readTextIfExists(syncFile(home)).catch(() => undefined));
}

export async function writeSyncState(home: string, state: SyncState): Promise<void> {
  await writeAtomically(syncFile(home), `${JSON.stringify(state, null, 2)}\n`);
}

export async function appendLog(home: string, message: string, now = new Date()): Promise<void> {
  await ensurePrivateDir(home);
  await fs.appendFile(path.join(home, 'sync.log'), `${now.toISOString()} ${message}\n`);
}

export async function tryLock(file: string, staleMs = LOCK_STALE_MS, now = Date.now()): Promise<boolean> {
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      await (await fs.open(file, 'wx', 0o600)).close();
      return true;
    } catch (e) {
      if ((e as NodeJS.ErrnoException).code !== 'EEXIST') throw e;
      const stat = await fs.stat(file).catch(() => undefined);
      if (stat && now - stat.mtimeMs <= staleMs) return false;
      await fs.rm(file, { force: true });
    }
  }
  return false;
}

export interface JetbrainsResult {
  repository: string;
  zip: string;
  updatePluginsXml: string;
  url: string;
  version: string;
  changed: boolean;
}

export async function publishJetbrains(bundle: Bundle, repoDir: string, platform: NodeJS.Platform): Promise<JetbrainsResult> {
  const xmlFile = path.join(repoDir, 'updatePlugins.xml');
  const existing = repositoryVersion((await readTextIfExists(xmlFile)) ?? '');
  const version = bundle.versions.jetbrains;
  const zipName = `ide-agent-tabs-${version}.zip`;
  const result = { repository: repoDir, updatePluginsXml: xmlFile, url: fileUrl(xmlFile, platform) };
  if (existing !== undefined && compareVersions(existing, version) > 0) {
    return { ...result, zip: path.join(repoDir, `ide-agent-tabs-${existing}.zip`), version: existing, changed: false };
  }
  await ensurePrivateDir(repoDir);
  const zip = path.join(repoDir, zipName);
  await fs.copyFile(bundle.zip, zip);
  const xml = updatePluginsXml(version, fileUrl(zip, platform));
  if ((await readTextIfExists(xmlFile)) !== xml) await writeAtomically(xmlFile, xml);
  for (const name of await fs.readdir(repoDir)) {
    if (/^ide-agent-tabs-.+\.zip$/.test(name) && name !== zipName) await fs.rm(path.join(repoDir, name), { force: true });
  }
  return { ...result, zip, version, changed: existing !== version };
}

async function syncEditors(ctx: SyncContext, bundle: Bundle, errors: string[]) {
  const updated = await Promise.all(
    findEditorClis(ctx).map(async ({ cli, path: file }) => {
      try {
        const installed = await installedExtensionVersion(ctx, file);
        if (installed === undefined || compareVersions(installed, bundle.versions.vscode) >= 0) return undefined;
        await installVsix(ctx, file, bundle.vsix);
        return cli;
      } catch (e) {
        errors.push(`${cli}: ${(e as Error).message}`);
        return undefined;
      }
    }),
  );
  let jetbrainsUpdated = false;
  if (existsSync(repositoryDir(ctx.home))) {
    try {
      jetbrainsUpdated = (await publishJetbrains(bundle, repositoryDir(ctx.home), ctx.platform)).changed;
    } catch (e) {
      errors.push(`jetbrains: ${(e as Error).message}`);
    }
  }
  return { updated: updated.filter((c): c is string => c !== undefined), jetbrainsUpdated };
}

export async function syncHook(ctx: SyncContext, now = new Date()): Promise<string | undefined> {
  const bundle = await readBundle(ctx.bundleDir);
  const previous = await readSyncState(ctx.home);
  const server = await serverToRefresh(previous, existsSync(serverCopyDir(ctx.home)), () => serverHash(ctx.serverDir));
  const ides = needsSync(previous, bundle.versions);
  if (!ides && server === undefined) return undefined;
  await ensurePrivateDir(ctx.home);
  const lock = path.join(ctx.home, 'sync.lock');
  if (!(await tryLock(lock, LOCK_STALE_MS, now.getTime()))) return undefined;
  try {
    const errors: string[] = [];
    let syncedServer = previous?.server;
    if (server !== undefined) {
      try {
        await refreshServerCopy(ctx.serverDir, ctx.home);
        syncedServer = server;
      } catch (e) {
        errors.push(`server copy: ${(e as Error).message}`);
      }
    }
    const ideErrors: string[] = [];
    const result = ides ? await syncEditors(ctx, bundle, ideErrors) : { updated: [], jetbrainsUpdated: false };
    const state = ides ? nextSyncState(previous, bundle.versions, ideErrors.length > 0, now) : { ...previous!, syncedAt: now.toISOString() };
    await writeSyncState(ctx.home, { ...state, ...(syncedServer ? { server: syncedServer } : {}) });
    errors.push(...ideErrors);
    if (errors.length > 0) await appendLog(ctx.home, errors.join('\n'), now);
    return hookMessage(bundle.versions, result.updated, result.jetbrainsUpdated);
  } finally {
    await fs.rm(lock, { force: true });
  }
}

export interface EditorInstall {
  cli: string;
  path?: string;
  ok: boolean;
  error?: string;
}

export async function syncInstall(ctx: SyncContext, clis: string[], jetbrains: boolean, now = new Date()) {
  const bundle = await readBundle(ctx.bundleDir);
  const errors: string[] = [];
  const editors: EditorInstall[] = await Promise.all(
    clis.map(async (name): Promise<EditorInstall> => {
      const found = resolveEditorCli(name, ctx);
      if (!found) {
        errors.push(`${name}: not found`);
        return { cli: name, ok: false, error: 'not found' };
      }
      try {
        await installVsix(ctx, found.path, bundle.vsix);
        return { cli: found.cli, path: found.path, ok: true };
      } catch (e) {
        errors.push(`${name}: ${(e as Error).message}`);
        return { cli: found.cli, path: found.path, ok: false, error: (e as Error).message };
      }
    }),
  );
  let jetbrainsResult: JetbrainsResult | undefined;
  if (jetbrains) {
    try {
      jetbrainsResult = await publishJetbrains(bundle, repositoryDir(ctx.home), ctx.platform);
    } catch (e) {
      errors.push(`jetbrains: ${(e as Error).message}`);
    }
  }
  await writeSyncState(ctx.home, nextSyncState(await readSyncState(ctx.home), bundle.versions, errors.length > 0, now));
  if (errors.length > 0) await appendLog(ctx.home, errors.join('\n'), now);
  return {
    bundled: bundle.versions,
    vscode: { version: bundle.versions.vscode, vsix: bundle.vsix, editors },
    jetbrains: jetbrainsResult ?? null,
    errors,
  };
}

export async function syncStatus(ctx: SyncContext) {
  const bundle = await readBundle(ctx.bundleDir);
  const editors = await Promise.all(
    findEditorClis(ctx).map(async ({ cli, path: file }) => {
      try {
        return { cli, path: file, installed: (await installedExtensionVersion(ctx, file)) ?? null };
      } catch (e) {
        return { cli, path: file, installed: null, error: (e as Error).message };
      }
    }),
  );
  const repoDir = repositoryDir(ctx.home);
  const xmlFile = path.join(repoDir, 'updatePlugins.xml');
  const xml = await readTextIfExists(xmlFile).catch(() => undefined);
  return {
    bundled: bundle.versions,
    home: ctx.home,
    synced: (await readSyncState(ctx.home)) ?? null,
    editors,
    jetbrains: {
      repository: repoDir,
      exists: existsSync(repoDir),
      version: (xml && repositoryVersion(xml)) ?? null,
      updatePluginsXml: xmlFile,
      url: fileUrl(xmlFile, ctx.platform),
    },
  };
}

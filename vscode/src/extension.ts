import { randomUUID } from 'node:crypto';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import * as vscode from 'vscode';
import { typeLine } from './input';
import { editorLocation, launchScripts, revivedTabs, terminalEnv, unixShell, windowsShell } from './launch';
import { AgentLaunch, AgentProfile, AgentSettings, CONFIG_FILE, isInstalled, planLaunch } from './profiles';
import { ENDPOINT_BEAT_MS, beatEndpoint, endpointFileName, endpointJson, ideAgentTabsHome, newToken, newWindowId, writeAtomically } from './registry';
import { closestBase } from './request';
import { apiUrl, createApiServer, Host, listen, TabInfo } from './server';
import { AUTO, SHARED_DEFAULTS, SharedSettings, userSettingValue } from './sharedSettings';

const BUILTIN_ICONS = new Set(['claude', 'codex', 'agy', 'copilot', 'gemini', 'grok', 'pi', 'hermes', 'opencode', 'qwen', 'goose', 'codex-local']);
const ICON_FILES: Record<string, string> = { 'codex-local': 'codex' };

const commandKey = (name: string) => name.replace(/-(\w)/g, (_, c: string) => c.toUpperCase());

const SHARED_SETTING_NAMES: Record<keyof SharedSettings, string> = {
  tabRouting: 'openNewTabsIn',
  terminal: 'preferredTerminal',
  shell: 'windowsShell',
  terminalWindow: 'terminalWindow',
  launchVia: 'launchVia',
  closeAfterHandoff: 'closeAfterHandoff',
  allowResume: 'allowResume',
  focusNewTabs: 'focusNewTabs',
  claudeMod: 'claudeMod',
};

const SHARED_KEYS = Object.keys(SHARED_SETTING_NAMES) as (keyof SharedSettings)[];

interface Choice extends vscode.QuickPickItem {
  value: string | undefined;
}

interface Tab extends TabInfo {
  terminal: vscode.Terminal;
}

interface OpenOptions {
  launch: AgentLaunch;
  focus: boolean;
}

const VIA_LABEL = 'via OpenRouter';

export async function activate(context: vscode.ExtensionContext): Promise<void> {
  const log = vscode.window.createOutputChannel('Agent Tabs', { log: true });
  const home = ideAgentTabsHome();
  const config = () => vscode.workspace.getConfiguration('ideAgentTabs');
  const settings = new AgentSettings(home, message => log.warn(message), () => config().get<string>('defaultAgent') || undefined);
  const scripts = launchScripts(context.asAbsolutePath(path.join('resources', 'launch')));
  const tabs = new Map<string, Tab>();
  const isWindows = process.platform === 'win32';
  const searchPath = () => process.env.PATH ?? '';

  const iconUri = (...parts: string[]) => vscode.Uri.file(context.asAbsolutePath(path.join('resources', 'icons', ...parts)));
  const neutralIcon = { light: iconUri('agentTab.svg'), dark: iconUri('agentTab_dark.svg') };

  const icon = (profile: AgentProfile): vscode.IconPath => {
    if (profile.icon !== undefined) {
      const file = path.resolve(home, profile.icon);
      if (fs.existsSync(file)) return vscode.Uri.file(file);
      log.warn(`Could not load agent icon ${file}`);
      return neutralIcon;
    }
    if (BUILTIN_ICONS.has(profile.name)) {
      const file = ICON_FILES[profile.name] ?? profile.name;
      return { light: iconUri('agents', `${file}.svg`), dark: iconUri('agents', `${file}_dark.svg`) };
    }
    return neutralIcon;
  };

  const launchFor = (profile: AgentProfile) =>
    planLaunch(profile, {
      setting: settings.shared().launchVia,
      ori: settings.detected().ori,
      windows: isWindows,
      searchPath: searchPath(),
    });

  const fileFolders = () => (vscode.workspace.workspaceFolders ?? []).filter(f => f.uri.scheme === 'file');
  const drivePath = (fsPath: string) => (isWindows ? fsPath.replace(/^[a-z]:/, drive => drive.toUpperCase()) : fsPath);
  const folderPath = (folder: vscode.WorkspaceFolder) => drivePath(folder.uri.fsPath);
  const projectOf = (dir: string) => {
    const folders = fileFolders();
    const index = closestBase(dir, folders.map(folderPath));
    return index !== undefined ? folders[index].name : (vscode.workspace.name ?? folders[0]?.name ?? path.basename(dir));
  };

  const openTab = (dir: string, project: string, profile: AgentProfile, options: OpenOptions): TabInfo => {
    const id = randomUUID();
    const shell = isWindows ? windowsShell(searchPath(), scripts) : unixShell(process.env.SHELL, process.platform === 'darwin', scripts);
    const { launch } = options;
    const terminal = vscode.window.createTerminal({
      name: launch.via === 'ori' ? `${profile.label} (${VIA_LABEL})` : profile.label,
      cwd: dir,
      env: terminalEnv(shell.kind, launch, id, process.env),
      shellPath: shell.path,
      shellArgs: shell.args,
      iconPath: icon(profile),
      isTransient: true,
      location: editorLocation(vscode.ViewColumn.Active, options.focus),
    });
    const tab: Tab = { id, agent: profile.name, project, path: dir, terminal };
    tabs.set(id, tab);
    return { id, agent: tab.agent, project, path: dir };
  };

  const openInFolder = (folder: vscode.WorkspaceFolder | undefined, profile: AgentProfile) => {
    const dir = folder ? folderPath(folder) : os.homedir();
    openTab(dir, folder?.name ?? path.basename(dir), profile, { launch: launchFor(profile), focus: true });
  };

  const openFromButton = (profile: AgentProfile) => {
    const active = vscode.window.activeTextEditor?.document.uri;
    const activeFolder = active ? vscode.workspace.getWorkspaceFolder(active) : undefined;
    openInFolder(activeFolder?.uri.scheme === 'file' ? activeFolder : fileFolders()[0], profile);
  };

  const opensOnStartup = (folder: vscode.WorkspaceFolder) => {
    const mode = config().get<string>('openOnStartup', 'claudeFolder');
    if (mode === 'always') return true;
    if (mode !== 'claudeFolder') return false;
    try {
      return fs.statSync(path.join(folderPath(folder), '.claude')).isDirectory();
    } catch {
      return false;
    }
  };

  // Terminals outlive an extension host restart in the pty host, and their creation options keep the tab's env.
  const adopt = (terminals: readonly vscode.Terminal[]) => {
    const known = (id: string, terminal: vscode.Terminal) => tabs.has(id) || [...tabs.values()].some(t => t.terminal === terminal);
    const found = revivedTabs(terminals, known);
    for (const tab of found) {
      const dir = drivePath(tab.path);
      tabs.set(tab.id, { ...tab, project: projectOf(dir), path: dir });
    }
    return found.length;
  };

  const openOnStartup = (folders: readonly vscode.WorkspaceFolder[]) => {
    const folder = folders.find(f => f.uri.scheme === 'file' && opensOnStartup(f));
    if (folder) openInFolder(folder, settings.defaultProfile());
  };

  const host: Host = {
    info: () => {
      const focused = vscode.window.state.focused;
      return {
        ide: 'vscode',
        product: vscode.env.appName,
        version: vscode.version,
        pid: process.pid,
        projects: fileFolders().map(f => ({ name: f.name, path: folderPath(f), focused })),
      };
    },
    isInstalled: profile => isInstalled(profile.command, searchPath(), isWindows),
    open: (request, profile, launch) => {
      const folders = fileFolders();
      if (folders.length === 0) return undefined;
      return openTab(request.path, projectOf(request.path), profile, { launch, focus: request.focus });
    },
    close: id => {
      const tab = tabs.get(id);
      if (!tab) return false;
      tabs.delete(id);
      tab.terminal.dispose();
      return true;
    },
    input: (id, text) => {
      const tab = tabs.get(id);
      if (!tab) return false;
      typeLine(data => tab.terminal.sendText(data, false), text, () => tabs.get(id) === tab);
      return true;
    },
    list: () => [...tabs.values()].map(({ id, agent, project, path: dir }) => ({ id, agent, project, path: dir })),
    reveal: async target => {
      await vscode.commands.executeCommand('revealFileInOS', vscode.Uri.file(target));
      return true;
    },
  };

  const status = vscode.window.createStatusBarItem('ideAgentTabs.newTab', vscode.StatusBarAlignment.Right, 1);
  status.name = 'New Agent Tab';
  status.command = 'ideAgentTabs.newTab';
  const refreshStatus = () => {
    const profile = settings.defaultProfile();
    void vscode.commands.executeCommand('setContext', 'ideAgentTabs.buttonAgent', BUILTIN_ICONS.has(profile.name) ? commandKey(profile.name) : 'other');
    status.text = `$(agent-tabs) ${profile.label}`;
    const escape = (text: string) => text.replace(/[\\`*_{}[\]()#+\-.!|<>]/g, '\\$&');
    const all = settings.profiles();
    const installed = all.filter(p => isInstalled(p.command, searchPath(), isWindows));
    const missing = all.filter(p => !installed.includes(p));
    const dark = [vscode.ColorThemeKind.Dark, vscode.ColorThemeKind.HighContrast].includes(vscode.window.activeColorTheme.kind);
    const iconFor = (p: AgentProfile) => {
      const path = icon(p);
      if (path instanceof vscode.Uri) return path;
      if (path instanceof vscode.ThemeIcon || typeof path === 'string') return undefined;
      const uri = dark ? path.dark : path.light;
      return uri instanceof vscode.Uri ? uri : undefined;
    };
    const viaLogo = iconUri(dark ? 'openrouter_dark.svg' : 'openrouter.svg');
    const links = installed.map(p => {
      const logo = iconFor(p);
      const args = encodeURIComponent(JSON.stringify([p.name]));
      const img = logo ? `<img src="${logo.toString()}" width="16" height="16" align="absmiddle"> ` : '';
      const via = launchFor(p).via === 'ori' ? ` <img src="${viaLogo.toString()}" width="16" height="16" align="absmiddle"> ${VIA_LABEL}` : '';
      return `${img}[${escape(p.label)}](command:ideAgentTabs.openAgent?${args})${via}`;
    });
    for (const name of BUILTIN_ICONS) {
      void vscode.commands.executeCommand('setContext', `ideAgentTabs.installed.${commandKey(name)}`, installed.some(p => p.name === name));
    }
    void vscode.commands.executeCommand('setContext', 'ideAgentTabs.customInstalled', installed.some(p => !BUILTIN_ICONS.has(p.name)));
    const lines = [
      installed.length > 0 ? `Open an agent in an editor tab.  
New tab: **${process.platform === 'darwin' ? '⌘⌥A' : 'Ctrl+Alt+A'}**` : 'No agent CLI found on PATH.',
      links.join('  \n'),
      missing.length > 0 ? `Not installed: ${missing.map(p => escape(p.label)).join(', ')}` : '',
      '[$(gear) Settings](command:ideAgentTabs.openSettings)',
    ];
    const tooltip = new vscode.MarkdownString(lines.filter(Boolean).join('\n\n'));
    tooltip.isTrusted = { enabledCommands: ['ideAgentTabs.openAgent', 'ideAgentTabs.openSettings'] };
    tooltip.supportThemeIcons = true;
    tooltip.supportHtml = true;
    status.tooltip = tooltip;
  };
  refreshStatus();
  status.show();

  const shareDefault = () => {
    const name = settings.defaultProfile().name;
    if (settings.sharedDefault() !== name) settings.setDefaultAgent(name);
  };

  // config.json holds the default for every IDE and the MCP server; the setting shows it and writes it back.
  const followSharedDefault = async () => {
    const shared = settings.sharedDefault();
    if (shared === undefined || !settings.profile(shared)) {
      shareDefault();
      return;
    }
    if (config().get<string>('defaultAgent') === shared) return;
    await config()
      .update('defaultAgent', shared, vscode.ConfigurationTarget.Global)
      .then(undefined, e => log.warn(`Could not update ideAgentTabs.defaultAgent: ${(e as Error).message}`));
  };

  const sharedValue = <K extends keyof SharedSettings>(key: K): SharedSettings[K] =>
    userSettingValue(key, config().inspect<SharedSettings[K]>(SHARED_SETTING_NAMES[key]));

  const shareSettings = () => {
    const found = settings.sharedFound();
    if (found === undefined) return;
    for (const key of SHARED_KEYS) {
      const value = sharedValue(key);
      if ((found[key] ?? SHARED_DEFAULTS[key]) !== value) settings.setShared(key, value);
    }
  };

  const followSharedSettings = async () => {
    const found = settings.sharedFound();
    if (found === undefined) return;
    for (const key of SHARED_KEYS) {
      const shared = found[key];
      if (shared === undefined) {
        if (sharedValue(key) !== SHARED_DEFAULTS[key]) settings.setShared(key, sharedValue(key));
      } else if (sharedValue(key) !== shared) {
        await config()
          .update(SHARED_SETTING_NAMES[key], shared, vscode.ConfigurationTarget.Global)
          .then(undefined, e => log.warn(`Could not update ideAgentTabs.${SHARED_SETTING_NAMES[key]}: ${(e as Error).message}`));
      }
    }
  };

  const choose = async (key: 'terminal' | 'shell', title: string, options: Choice[], custom?: () => Promise<string | undefined>) => {
    const current = sharedValue(key);
    const items: Choice[] = [{ label: 'Automatic', description: current === AUTO ? 'current' : undefined, value: AUTO }];
    for (const option of options) items.push({ ...option, description: option.value === current ? 'current' : option.description });
    if (current !== AUTO && !options.some(o => o.value === current)) items.push({ label: current, description: 'current, not detected', value: current });
    if (custom) items.push({ label: 'Custom path…', value: undefined });
    const picked = await vscode.window.showQuickPick(items, { title, placeHolder: current });
    if (!picked) return;
    const value = picked.value ?? (await custom?.());
    if (value === undefined) return;
    await config().update(SHARED_SETTING_NAMES[key], value, vscode.ConfigurationTarget.Global);
  };

  const chooseTerminal = () =>
    choose(
      'terminal',
      'Preferred Terminal',
      settings.detected().terminals.map(t => ({ label: t.name, description: t.id, value: t.id })),
    );

  const chooseShell = async () => {
    if (!isWindows) {
      void vscode.window.showInformationMessage('The shell setting applies on Windows only.');
      return;
    }
    await choose(
      'shell',
      'Windows Shell',
      settings.detected().shells.map(s => ({ label: s.label, description: s.path, value: s.path })),
      async () => {
        const [file] =
          (await vscode.window.showOpenDialog({ canSelectMany: false, openLabel: 'Use Shell', filters: { Executables: ['exe'] } })) ?? [];
        return file?.fsPath;
      },
    );
  };

  const chooseAgent = async () => {
    const items = settings
      .profiles()
      .filter(p => isInstalled(p.command, searchPath(), isWindows))
      .map(p => ({ label: p.label, description: launchFor(p).via === 'ori' ? VIA_LABEL : undefined, iconPath: icon(p), profile: p }));
    if (items.length === 0) {
      void vscode.window.showInformationMessage('No agent CLI found on PATH.');
      return;
    }
    const picked = await vscode.window.showQuickPick(items, { title: 'Open Agent' });
    if (picked) openFromButton(picked.profile);
  };

  context.subscriptions.push(
    log,
    status,
    vscode.commands.registerCommand('ideAgentTabs.newTab', () => openFromButton(settings.defaultProfile())),
    ...[...BUILTIN_ICONS].map(commandKey).concat('other').map(name =>
      vscode.commands.registerCommand(`ideAgentTabs.newTab.${name}`, () => openFromButton(settings.defaultProfile())),
    ),
    vscode.commands.registerCommand('ideAgentTabs.newTabWith', chooseAgent),
    vscode.commands.registerCommand('ideAgentTabs.choosePreferredTerminal', chooseTerminal),
    vscode.commands.registerCommand('ideAgentTabs.chooseWindowsShell', chooseShell),
    vscode.commands.registerCommand('ideAgentTabs.openSettings', () =>
      vscode.commands.executeCommand('workbench.action.openSettings', `@ext:${context.extension.id}`),
    ),
    ...[...BUILTIN_ICONS].map(name =>
      vscode.commands.registerCommand(`ideAgentTabs.open.${commandKey(name)}`, () => {
        const profile = settings.profile(name);
        if (profile) openFromButton(profile);
      }),
    ),
    vscode.commands.registerCommand('ideAgentTabs.openAgent', (name: unknown) => {
      const profile = typeof name === 'string' ? settings.profile(name) : undefined;
      if (profile) openFromButton(profile);
    }),
    vscode.window.onDidOpenTerminal(terminal => adopt([terminal])),
    vscode.window.onDidCloseTerminal(terminal => {
      for (const [id, tab] of tabs) if (tab.terminal === terminal) tabs.delete(id);
    }),
    vscode.window.onDidChangeActiveColorTheme(() => refreshStatus()),
    vscode.window.onDidChangeWindowState(state => {
      if (!state.focused) return;
      void followSharedDefault();
      void followSharedSettings();
      refreshStatus();
    }),
    vscode.workspace.onDidChangeConfiguration(e => {
      if (SHARED_KEYS.some(key => e.affectsConfiguration(`ideAgentTabs.${SHARED_SETTING_NAMES[key]}`))) shareSettings();
      if (!e.affectsConfiguration('ideAgentTabs.defaultAgent')) return;
      shareDefault();
      refreshStatus();
    }),
    vscode.workspace.onDidChangeWorkspaceFolders(e => openOnStartup(e.added)),
  );

  await followSharedDefault();
  await followSharedSettings();
  const sharedConfig = vscode.workspace.createFileSystemWatcher(new vscode.RelativePattern(vscode.Uri.file(home), CONFIG_FILE));
  context.subscriptions.push(
    sharedConfig,
    sharedConfig.onDidChange(() => {
      void followSharedDefault();
      void followSharedSettings();
    }),
    sharedConfig.onDidCreate(() => {
      void followSharedDefault();
      void followSharedSettings();
    }),
  );
  refreshStatus();
  const revived = adopt(vscode.window.terminals);
  if (revived > 0) log.info(`Found ${revived} agent tab${revived === 1 ? '' : 's'} from before the extension host restarted`);
  else openOnStartup(fileFolders());

  const token = newToken();
  const server = createApiServer(token, host, settings);
  context.subscriptions.push({ dispose: () => server.close() });
  let port: number;
  try {
    port = await listen(server);
  } catch (e) {
    log.error(`Could not start the agent tab server: ${(e as Error).message}`);
    return;
  }
  const file = path.join(home, 'endpoints', endpointFileName(process.pid, newWindowId()));
  try {
    const content = endpointJson({
      product: vscode.env.appName,
      version: vscode.version,
      pid: process.pid,
      url: apiUrl(port),
      token,
      startedAt: Date.now(),
      beatMs: ENDPOINT_BEAT_MS,
    });
    writeAtomically(file, content, true);
    const beat = setInterval(() => {
      try {
        beatEndpoint(file, content);
      } catch (e) {
        log.error(`Could not refresh ${file}: ${(e as Error).message}`);
      }
    }, ENDPOINT_BEAT_MS);
    context.subscriptions.push({
      dispose: () => {
        clearInterval(beat);
        fs.rmSync(file, { force: true });
      },
    });
    log.info(`Agent tab server at ${apiUrl(port)}, registered in ${file}`);
  } catch (e) {
    log.error(`Could not write ${file}: ${(e as Error).message}`);
  }
}

export function deactivate(): void {}

import { randomUUID } from 'node:crypto';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import * as vscode from 'vscode';
import { launchScripts, terminalEnv, unixShell, windowsShell } from './launch';
import { AgentProfile, AgentSettings, isInstalled, launchOf } from './profiles';
import { endpointFileName, endpointJson, ideAgentTabsHome, newToken, newWindowId, writeAtomically } from './registry';
import { closestBase } from './request';
import { apiUrl, createApiServer, Host, listen, TabInfo } from './server';

const BUILTIN_ICONS = new Set(['claude', 'codex', 'gemini', 'copilot']);

interface Tab extends TabInfo {
  terminal: vscode.Terminal;
}

interface OpenOptions {
  prompt?: string;
  args?: string[];
  env?: Record<string, string>;
  focus: boolean;
}

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
      return { light: iconUri('agents', `${profile.name}.svg`), dark: iconUri('agents', `${profile.name}_dark.svg`) };
    }
    return neutralIcon;
  };

  const fileFolders = () => (vscode.workspace.workspaceFolders ?? []).filter(f => f.uri.scheme === 'file');
  const folderPath = (folder: vscode.WorkspaceFolder) => {
    const fsPath = folder.uri.fsPath;
    return isWindows ? fsPath.replace(/^[a-z]:/, drive => drive.toUpperCase()) : fsPath;
  };

  const openTab = (dir: string, project: string, profile: AgentProfile, options: OpenOptions): TabInfo => {
    const id = randomUUID();
    const shell = isWindows ? windowsShell(searchPath(), scripts) : unixShell(process.env.SHELL, process.platform === 'darwin', scripts);
    const launch = launchOf(profile, options.prompt, options.args, options.env);
    const terminal = vscode.window.createTerminal({
      name: profile.label,
      cwd: dir,
      env: terminalEnv(shell.kind, launch, id, process.env),
      shellPath: shell.path,
      shellArgs: shell.args,
      iconPath: icon(profile),
      isTransient: true,
      location: { viewColumn: vscode.ViewColumn.Active, preserveFocus: !options.focus },
    });
    const tab: Tab = { id, agent: profile.name, project, path: dir, terminal };
    tabs.set(id, tab);
    return { id, agent: tab.agent, project, path: dir };
  };

  const openInFolder = (folder: vscode.WorkspaceFolder | undefined, profile: AgentProfile) => {
    const dir = folder ? folderPath(folder) : os.homedir();
    openTab(dir, folder?.name ?? path.basename(dir), profile, { focus: true });
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
    open: (request, profile) => {
      const folders = fileFolders();
      if (folders.length === 0) return undefined;
      const index = closestBase(request.path, folders.map(folderPath));
      const project = index !== undefined ? folders[index].name : (vscode.workspace.name ?? folders[0].name);
      return openTab(request.path, project, profile, { prompt: request.prompt, args: request.args, env: request.env, focus: false });
    },
    close: id => {
      const tab = tabs.get(id);
      if (!tab) return false;
      tabs.delete(id);
      tab.terminal.dispose();
      return true;
    },
    list: () => [...tabs.values()].map(({ id, agent, project, path: dir }) => ({ id, agent, project, path: dir })),
  };

  const status = vscode.window.createStatusBarItem('ideAgentTabs.newTab', vscode.StatusBarAlignment.Right, -1000);
  status.name = 'New Agent Tab';
  status.command = 'ideAgentTabs.newTab';
  const refreshStatus = () => {
    const profile = settings.defaultProfile();
    const label = profile.label;
    void vscode.commands.executeCommand('setContext', 'ideAgentTabs.buttonAgent', BUILTIN_ICONS.has(profile.name) ? profile.name : 'other');
    status.text = `$(terminal) New ${label}`;
    const tooltip = new vscode.MarkdownString(
      `New Agent Tab: open **${label}**, the default agent, in an editor tab.\n\n[Choose another agent…](command:ideAgentTabs.newTabWith)`,
    );
    tooltip.isTrusted = { enabledCommands: ['ideAgentTabs.newTabWith'] };
    status.tooltip = tooltip;
  };
  refreshStatus();
  status.show();

  const syncDefaultSetting = async (name: string) => {
    const setting = config().inspect<string>('defaultAgent');
    const target =
      setting?.workspaceFolderValue !== undefined || setting?.workspaceValue !== undefined
        ? vscode.ConfigurationTarget.Workspace
        : setting?.globalValue !== undefined
          ? vscode.ConfigurationTarget.Global
          : undefined;
    if (target === undefined) return;
    const value = BUILTIN_ICONS.has(name) ? name : '';
    await config().update('defaultAgent', value, target).then(undefined, e => log.warn(`Could not save ideAgentTabs.defaultAgent: ${(e as Error).message}`));
  };

  const chooseAgent = async () => {
    const current = settings.defaultProfile().name;
    const items = settings
      .profiles()
      .filter(p => isInstalled(p.command, searchPath(), isWindows))
      .map(p => ({ label: p.label, description: p.name === current ? 'default' : undefined, iconPath: icon(p), profile: p }));
    if (items.length === 0) {
      void vscode.window.showInformationMessage('No agent CLI found on PATH.');
      return;
    }
    const picked = await vscode.window.showQuickPick(items, { title: 'Open Agent', placeHolder: 'The agent you choose becomes the default' });
    if (!picked) return;
    settings.setDefaultAgent(picked.profile.name);
    await syncDefaultSetting(picked.profile.name);
    refreshStatus();
    openFromButton(picked.profile);
  };

  context.subscriptions.push(
    log,
    status,
    vscode.commands.registerCommand('ideAgentTabs.newTab', () => openFromButton(settings.defaultProfile())),
    ...[...BUILTIN_ICONS, 'other'].map(name =>
      vscode.commands.registerCommand(`ideAgentTabs.newTab.${name}`, () => openFromButton(settings.defaultProfile())),
    ),
    vscode.commands.registerCommand('ideAgentTabs.newTabWith', chooseAgent),
    vscode.window.onDidCloseTerminal(terminal => {
      for (const [id, tab] of tabs) if (tab.terminal === terminal) tabs.delete(id);
    }),
    vscode.window.onDidChangeWindowState(state => {
      if (state.focused) refreshStatus();
    }),
    vscode.workspace.onDidChangeConfiguration(e => {
      if (e.affectsConfiguration('ideAgentTabs.defaultAgent')) refreshStatus();
    }),
    vscode.workspace.onDidChangeWorkspaceFolders(e => openOnStartup(e.added)),
  );

  openOnStartup(fileFolders());

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
    writeAtomically(file, endpointJson({ product: vscode.env.appName, version: vscode.version, pid: process.pid, url: apiUrl(port), token }), true);
    context.subscriptions.push({ dispose: () => fs.rmSync(file, { force: true }) });
    log.info(`Agent tab server at ${apiUrl(port)}, registered in ${file}`);
  } catch (e) {
    log.error(`Could not write ${file}: ${(e as Error).message}`);
  }
}

export function deactivate(): void {}

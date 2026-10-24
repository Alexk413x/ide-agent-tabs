import { randomUUID } from 'node:crypto';
import * as os from 'node:os';
import * as path from 'node:path';
import * as vscode from 'vscode';
import { config, drivePath, Ext, fileFolders, folderPath, icon, installed, projectOf, searchPath, Tab } from './ext';
import { isDirectory } from './files';
import { typeLine } from './input';
import { editorLocation, revivedTabs, terminalEnv, unixShell, windowsShell } from './launch';
import { isCopilotCommand, launchContext, planLaunch } from './launchPlan';
import { VIA_LABEL } from './labels';
import { copilotHooksFile, copilotHttp } from './mcpHome';
import { AgentLaunch, AgentProfile } from './profiles';
import { BadRequest } from './request';
import { Host, TabInfo } from './server';
import { LaunchVia } from './sharedSettings';

interface OpenOptions {
  id: string;
  launch: AgentLaunch;
  focus: boolean;
}

function openTab(ext: Ext, dir: string, project: string, profile: AgentProfile, options: OpenOptions): TabInfo {
  const { id, launch } = options;
  const shell = ext.windows ? windowsShell(searchPath(), ext.scripts) : unixShell(process.env.SHELL, process.platform === 'darwin', ext.scripts);
  const terminal = vscode.window.createTerminal({
    name: launch.via === 'ori' ? `${profile.label} (${VIA_LABEL})` : profile.label,
    cwd: dir,
    env: terminalEnv(shell.kind, launch, id, process.env),
    shellPath: shell.path,
    shellArgs: shell.args,
    iconPath: icon(ext, profile),
    isTransient: true,
    location: editorLocation(vscode.ViewColumn.Active, options.focus),
  });
  const tab: Tab = { id, agent: profile.name, project, path: dir, terminal };
  ext.tabs.set(id, tab);
  return { id, agent: tab.agent, project, path: dir };
}

async function openInFolder(ext: Ext, folder: vscode.WorkspaceFolder | undefined, profile: AgentProfile, via?: LaunchVia): Promise<void> {
  const dir = folder ? folderPath(ext, folder) : os.homedir();
  const id = randomUUID();
  const copilot = isCopilotCommand(profile.command) ? await copilotHttp(ext.home, ext.windows, copilotHooksFile(process.env, os.homedir())) : undefined;
  let launch: AgentLaunch;
  try {
    launch = planLaunch(profile, launchContext(ext.settings, profile, { windows: ext.windows, searchPath: searchPath() }, { tab: id, copilot, via, cwd: dir, warn: message => ext.log.warn(message) }));
  } catch (e) {
    if (!(e instanceof BadRequest)) throw e;
    void vscode.window.showErrorMessage(e.message);
    return;
  }
  openTab(ext, dir, folder?.name ?? path.basename(dir), profile, { id, launch, focus: true });
}

export function openFromButton(ext: Ext, profile: AgentProfile, via?: LaunchVia): void {
  const active = vscode.window.activeTextEditor?.document.uri;
  const activeFolder = active ? vscode.workspace.getWorkspaceFolder(active) : undefined;
  void openInFolder(ext, activeFolder?.uri.scheme === 'file' ? activeFolder : fileFolders()[0], profile, via);
}

function opensOnStartup(ext: Ext, folder: vscode.WorkspaceFolder): boolean {
  const mode = config().get<string>('openOnStartup', 'claudeFolder');
  if (mode === 'always') return true;
  if (mode !== 'claudeFolder') return false;
  return isDirectory(path.join(folderPath(ext, folder), '.claude'));
}

export function openOnStartup(ext: Ext, folders: readonly vscode.WorkspaceFolder[]): void {
  const folder = folders.find(f => f.uri.scheme === 'file' && opensOnStartup(ext, f));
  if (folder) void openInFolder(ext, folder, ext.settings.defaultProfile());
}

// Terminals outlive an extension host restart in the pty host, and their creation options keep the tab's env.
export function adopt(ext: Ext, terminals: readonly vscode.Terminal[]): number {
  const { tabs } = ext;
  const known = (id: string, terminal: vscode.Terminal) => tabs.has(id) || [...tabs.values()].some(t => t.terminal === terminal);
  const found = revivedTabs(terminals, known);
  for (const tab of found) {
    const dir = drivePath(ext, tab.path);
    tabs.set(tab.id, { ...tab, project: projectOf(ext, dir), path: dir });
  }
  return found.length;
}

export function forget(ext: Ext, terminal: vscode.Terminal): void {
  for (const [id, tab] of ext.tabs) if (tab.terminal === terminal) ext.tabs.delete(id);
}

export function tabHost(ext: Ext): Host {
  const { tabs } = ext;
  return {
    info: () => {
      const focused = vscode.window.state.focused;
      return {
        ide: 'vscode',
        product: vscode.env.appName,
        version: vscode.version,
        pid: process.pid,
        projects: fileFolders().map(f => ({ name: f.name, path: folderPath(ext, f), focused })),
      };
    },
    isInstalled: profile => installed(ext, profile),
    open: (request, profile, launch, id) => {
      const folders = fileFolders();
      if (folders.length === 0) return undefined;
      return openTab(ext, request.path, projectOf(ext, request.path), profile, { id, launch, focus: request.focus });
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
}

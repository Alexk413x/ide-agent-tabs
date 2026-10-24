import * as fs from 'node:fs';
import * as path from 'node:path';
import * as vscode from 'vscode';
import { AgentSettings } from './agentSettings';
import { LaunchScripts, launchScripts } from './launch';
import { wouldUseOri } from './launchPlan';
import { isInstalled } from './pathLookup';
import { agentEntries, AgentProfile, BUILTIN_PROFILES, ORI_PROFILES } from './profiles';
import { ideAgentTabsHome } from './registry';
import { closestBase } from './request';
import type { TabInfo } from './server';
import { SharedSettings, SyncedSetting, userSettingValue } from './sharedSettings';
import { IdeSettings, SHARED_SETTING_NAMES, SyncDeps } from './sharedSync';

export const BUILTIN_AGENTS = new Set(BUILTIN_PROFILES.map(p => p.name));
export const ORI_MENU_AGENTS = ORI_PROFILES.filter(name => BUILTIN_AGENTS.has(name));
const ICON_FILES: Record<string, string> = { 'codex-lmstudio': 'codex', 'codex-ollama': 'codex' };

export interface Tab extends TabInfo {
  terminal: vscode.Terminal;
}

export interface Ext {
  context: vscode.ExtensionContext;
  log: vscode.LogOutputChannel;
  home: string;
  settings: AgentSettings;
  sync: SyncDeps;
  scripts: LaunchScripts;
  tabs: Map<string, Tab>;
  windows: boolean;
}

export const config = () => vscode.workspace.getConfiguration('ideAgentTabs');
export const searchPath = () => process.env.PATH ?? '';

export function createExt(context: vscode.ExtensionContext): Ext {
  const log = vscode.window.createOutputChannel('Agent Tabs', { log: true });
  const home = ideAgentTabsHome();
  const warn = (message: string) => log.warn(message);
  const settings = new AgentSettings(home, warn, () => config().get<string>('defaultAgent') || undefined);
  const ide: IdeSettings = {
    defaultAgent: () => config().get<string>('defaultAgent'),
    value: <K extends SyncedSetting>(key: K) => userSettingValue(key, config().inspect<SharedSettings[K]>(SHARED_SETTING_NAMES[key])),
    update: (name, value) => config().update(name, value, vscode.ConfigurationTarget.Global),
  };
  return {
    context,
    log,
    home,
    settings,
    sync: { settings, ide, warn },
    scripts: launchScripts(context.asAbsolutePath(path.join('resources', 'launch'))),
    tabs: new Map(),
    windows: process.platform === 'win32',
  };
}

export const iconUri = (ext: Ext, ...parts: string[]) => vscode.Uri.file(ext.context.asAbsolutePath(path.join('resources', 'icons', ...parts)));

export function icon(ext: Ext, profile: AgentProfile): vscode.IconPath {
  const neutral = { light: iconUri(ext, 'agentTab.svg'), dark: iconUri(ext, 'agentTab_dark.svg') };
  if (profile.icon !== undefined) {
    const file = path.resolve(ext.home, profile.icon);
    if (fs.existsSync(file)) return vscode.Uri.file(file);
    ext.log.warn(`Could not load agent icon ${file}`);
    return neutral;
  }
  if (BUILTIN_AGENTS.has(profile.name)) {
    const file = ICON_FILES[profile.name] ?? profile.name;
    return { light: iconUri(ext, 'agents', `${file}.svg`), dark: iconUri(ext, 'agents', `${file}_dark.svg`) };
  }
  return neutral;
}

export const installed = (ext: Ext, profile: AgentProfile) => isInstalled(profile.command, searchPath(), ext.windows);

export const usesOri = (ext: Ext, profile: AgentProfile) =>
  wouldUseOri(profile, { setting: ext.settings.shared().launchVia, ori: ext.settings.detected().ori, windows: ext.windows, searchPath: searchPath() });

export const entries = (ext: Ext) =>
  agentEntries(ext.settings.profiles(), ext.settings.detected().ori, ext.settings.shared(), p => installed(ext, p));

export const fileFolders = () => (vscode.workspace.workspaceFolders ?? []).filter(f => f.uri.scheme === 'file');

export const drivePath = (ext: Ext, fsPath: string) => (ext.windows ? fsPath.replace(/^[a-z]:/, drive => drive.toUpperCase()) : fsPath);

export const folderPath = (ext: Ext, folder: vscode.WorkspaceFolder) => drivePath(ext, folder.uri.fsPath);

export function projectOf(ext: Ext, dir: string): string {
  const folders = fileFolders();
  const index = closestBase(dir, folders.map(f => folderPath(ext, f)));
  return index !== undefined ? folders[index].name : (vscode.workspace.name ?? folders[0]?.name ?? path.basename(dir));
}

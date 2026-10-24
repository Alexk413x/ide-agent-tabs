import * as vscode from 'vscode';
import { BUILTIN_AGENTS, entries, Ext, icon, iconUri, installed, ORI_MENU_AGENTS, usesOri } from './ext';
import { agentLink, commandKey, statusTooltip } from './labels';
import { AgentProfile, variantShown } from './profiles';

const setContext = (key: string, value: unknown) => void vscode.commands.executeCommand('setContext', key, value);

export function createStatus(): vscode.StatusBarItem {
  const status = vscode.window.createStatusBarItem('ideAgentTabs.newTab', vscode.StatusBarAlignment.Right, 1);
  status.name = 'New Agent Tab';
  status.command = 'ideAgentTabs.newTab';
  return status;
}

function themedIcon(ext: Ext, profile: AgentProfile, dark: boolean): vscode.Uri | undefined {
  const iconPath = icon(ext, profile);
  if (iconPath instanceof vscode.Uri) return iconPath;
  if (iconPath instanceof vscode.ThemeIcon || typeof iconPath === 'string') return undefined;
  const uri = dark ? iconPath.dark : iconPath.light;
  return uri instanceof vscode.Uri ? uri : undefined;
}

export function refreshStatus(ext: Ext, status: vscode.StatusBarItem): void {
  const { settings } = ext;
  const profile = settings.defaultProfile();
  setContext('ideAgentTabs.buttonAgent', BUILTIN_AGENTS.has(profile.name) ? commandKey(profile.name) : 'other');
  status.text = `$(agent-tabs) ${profile.label}`;
  const all = settings.profiles();
  const installedProfiles = all.filter(p => installed(ext, p));
  const shared = settings.shared();
  const missing = all.filter(p => !installedProfiles.includes(p) && variantShown(p.name, shared));
  const listed = entries(ext);
  const dark = [vscode.ColorThemeKind.Dark, vscode.ColorThemeKind.HighContrast].includes(vscode.window.activeColorTheme.kind);
  const viaLogo = iconUri(ext, dark ? 'openrouter_dark.svg' : 'openrouter.svg').toString();
  const links = listed.map(entry => {
    const logo = themedIcon(ext, entry.profile, dark)?.toString();
    const ori = entry.via === 'ori' || usesOri(ext, entry.profile);
    return agentLink(entry.name, entry.label, logo, ori ? viaLogo : undefined);
  });
  for (const name of BUILTIN_AGENTS) {
    setContext(`ideAgentTabs.installed.${commandKey(name)}`, listed.some(e => e.via === undefined && e.profile.name === name));
  }
  for (const name of ORI_MENU_AGENTS) {
    setContext(`ideAgentTabs.ori.${commandKey(name)}`, listed.some(e => e.via === 'ori' && e.profile.name === name));
  }
  setContext('ideAgentTabs.customInstalled', listed.some(e => !BUILTIN_AGENTS.has(e.profile.name)));
  const tooltip = new vscode.MarkdownString(statusTooltip(links, missing.map(p => p.label), process.platform === 'darwin'));
  tooltip.isTrusted = { enabledCommands: ['ideAgentTabs.openAgent', 'ideAgentTabs.openSettings'] };
  tooltip.supportThemeIcons = true;
  tooltip.supportHtml = true;
  status.tooltip = tooltip;
}

import * as vscode from 'vscode';
import { BUILTIN_AGENTS, config, entries, Ext, icon, ORI_MENU_AGENTS, usesOri } from './ext';
import { commandKey, VIA_LABEL } from './labels';
import { ORI_PREFIX } from './request';
import { AUTO } from './sharedSettings';
import { SHARED_SETTING_NAMES } from './sharedSync';
import { openFromButton } from './tabs';

interface Choice extends vscode.QuickPickItem {
  value: string | undefined;
}

async function choose(ext: Ext, key: 'terminal' | 'shell', title: string, options: Choice[], custom?: () => Promise<string | undefined>) {
  const current = ext.sync.ide.value(key);
  const items: Choice[] = [{ label: 'Automatic', description: current === AUTO ? 'current' : undefined, value: AUTO }];
  for (const option of options) items.push({ ...option, description: option.value === current ? 'current' : option.description });
  if (current !== AUTO && !options.some(o => o.value === current)) items.push({ label: current, description: 'current, not detected', value: current });
  if (custom) items.push({ label: 'Custom path…', value: undefined });
  const picked = await vscode.window.showQuickPick(items, { title, placeHolder: current });
  if (!picked) return;
  const value = picked.value ?? (await custom?.());
  if (value === undefined) return;
  await config().update(SHARED_SETTING_NAMES[key], value, vscode.ConfigurationTarget.Global);
}

const chooseTerminal = (ext: Ext) =>
  choose(
    ext,
    'terminal',
    'Preferred Terminal',
    ext.settings.detected().terminals.map(t => ({ label: t.name, description: t.id, value: t.id })),
  );

async function chooseShell(ext: Ext) {
  if (!ext.windows) {
    void vscode.window.showInformationMessage('The shell setting applies on Windows only.');
    return;
  }
  await choose(
    ext,
    'shell',
    'Windows Shell',
    ext.settings.detected().shells.map(s => ({ label: s.label, description: s.path, value: s.path })),
    async () => {
      const [file] = (await vscode.window.showOpenDialog({ canSelectMany: false, openLabel: 'Use Shell', filters: { Executables: ['exe'] } })) ?? [];
      return file?.fsPath;
    },
  );
}

async function chooseAgent(ext: Ext) {
  const items = entries(ext).map(entry => ({
    label: entry.label,
    description: entry.via === undefined && usesOri(ext, entry.profile) ? VIA_LABEL : undefined,
    iconPath: icon(ext, entry.profile),
    entry,
  }));
  if (items.length === 0) {
    void vscode.window.showInformationMessage('No agent CLI found on PATH.');
    return;
  }
  const picked = await vscode.window.showQuickPick(items, { title: 'Open Agent' });
  if (picked) openFromButton(ext, picked.entry.profile, picked.entry.via);
}

export function registerCommands(ext: Ext): vscode.Disposable[] {
  const { settings } = ext;
  const openDefault = () => openFromButton(ext, settings.defaultProfile());
  return [
    vscode.commands.registerCommand('ideAgentTabs.newTab', openDefault),
    ...[...BUILTIN_AGENTS].map(commandKey).concat('other').map(name => vscode.commands.registerCommand(`ideAgentTabs.newTab.${name}`, openDefault)),
    vscode.commands.registerCommand('ideAgentTabs.newTabWith', () => chooseAgent(ext)),
    vscode.commands.registerCommand('ideAgentTabs.choosePreferredTerminal', () => chooseTerminal(ext)),
    vscode.commands.registerCommand('ideAgentTabs.chooseWindowsShell', () => chooseShell(ext)),
    vscode.commands.registerCommand('ideAgentTabs.openSettings', () =>
      vscode.commands.executeCommand('workbench.action.openSettings', `@ext:${ext.context.extension.id}`),
    ),
    ...[...BUILTIN_AGENTS].map(name =>
      vscode.commands.registerCommand(`ideAgentTabs.open.${commandKey(name)}`, () => {
        const profile = settings.profile(name);
        if (profile) openFromButton(ext, profile);
      }),
    ),
    ...ORI_MENU_AGENTS.map(name =>
      vscode.commands.registerCommand(`ideAgentTabs.openOri.${commandKey(name)}`, () => {
        const profile = settings.profile(name);
        if (profile) openFromButton(ext, profile, 'ori');
      }),
    ),
    vscode.commands.registerCommand('ideAgentTabs.openAgent', (name: unknown) => {
      if (typeof name !== 'string') return;
      const ori = name.startsWith(ORI_PREFIX);
      const profile = settings.profile(ori ? name.slice(ORI_PREFIX.length) : name);
      if (profile) openFromButton(ext, profile, ori ? 'ori' : undefined);
    }),
  ];
}

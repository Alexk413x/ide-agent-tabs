import type { AgentSettings } from './agentSettings';
import { SHARED_DEFAULTS, SharedSettings, SyncedSetting } from './sharedSettings';

export const SHARED_SETTING_NAMES: Record<SyncedSetting, string> = {
  tabRouting: 'openNewTabsIn',
  terminal: 'preferredTerminal',
  shell: 'windowsShell',
  terminalWindow: 'terminalWindow',
  closeAfterHandoff: 'closeAfterHandoff',
  allowResume: 'allowResume',
  showOriAgents: 'showOriAgents',
  showLmStudioAgents: 'showLmStudioAgents',
  showOllamaAgents: 'showOllamaAgents',
  focusNewTabs: 'focusNewTabs',
  claudeMod: 'claudeMod',
};

export const SHARED_KEYS = Object.keys(SHARED_SETTING_NAMES) as SyncedSetting[];

export interface IdeSettings {
  defaultAgent(): string | undefined;
  value<K extends SyncedSetting>(key: K): SharedSettings[K];
  update(name: string, value: unknown): PromiseLike<void>;
}

export interface SyncDeps {
  settings: AgentSettings;
  ide: IdeSettings;
  warn(message: string): void;
}

const updateIde = (deps: SyncDeps, name: string, value: unknown) =>
  deps.ide.update(name, value).then(undefined, e => deps.warn(`Could not update ideAgentTabs.${name}: ${(e as Error).message}`));

export function shareDefault({ settings }: SyncDeps): void {
  const name = settings.defaultProfile().name;
  if (settings.sharedDefault() !== name) settings.setDefaultAgent(name);
}

// config.json holds the default for every IDE and the MCP server; the setting shows it and writes it back.
export async function followSharedDefault(deps: SyncDeps): Promise<void> {
  const shared = deps.settings.sharedDefault();
  if (shared === undefined || !deps.settings.profile(shared)) {
    shareDefault(deps);
    return;
  }
  if (deps.ide.defaultAgent() === shared) return;
  await updateIde(deps, 'defaultAgent', shared);
}

export function shareSettings({ settings, ide }: SyncDeps): void {
  const found = settings.sharedFound();
  if (found === undefined) return;
  for (const key of SHARED_KEYS) {
    const value = ide.value(key);
    if ((found[key] ?? SHARED_DEFAULTS[key]) !== value) settings.setShared(key, value);
  }
}

export async function followSharedSettings(deps: SyncDeps): Promise<void> {
  const { settings, ide } = deps;
  const found = settings.sharedFound();
  if (found === undefined) return;
  for (const key of SHARED_KEYS) {
    const shared = found[key];
    if (shared === undefined) {
      if (ide.value(key) !== SHARED_DEFAULTS[key]) settings.setShared(key, ide.value(key));
    } else if (ide.value(key) !== shared) {
      await updateIde(deps, SHARED_SETTING_NAMES[key], shared);
    }
  }
}

export function followShared(deps: SyncDeps): void {
  void followSharedDefault(deps);
  void followSharedSettings(deps);
}

export function settingsChanged(deps: SyncDeps, affects: (section: string) => boolean): boolean {
  const shared = SHARED_KEYS.some(key => affects(`ideAgentTabs.${SHARED_SETTING_NAMES[key]}`));
  if (shared) shareSettings(deps);
  const agent = affects('ideAgentTabs.defaultAgent');
  if (agent) shareDefault(deps);
  return shared || agent;
}

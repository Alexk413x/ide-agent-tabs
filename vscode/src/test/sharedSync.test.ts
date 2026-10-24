import assert from 'node:assert/strict';
import * as fs from 'node:fs';
import * as path from 'node:path';
import { test } from 'node:test';
import { AgentSettings } from '../agentSettings';
import { CONFIG_FILE, SHARED_DEFAULTS, SharedSettings, SyncedSetting } from '../sharedSettings';
import { followSharedDefault, followSharedSettings, settingsChanged, SHARED_KEYS, SHARED_SETTING_NAMES, shareSettings, SyncDeps } from '../sharedSync';
import { tempDir } from './support';

function fixture(values: Partial<SharedSettings> = {}, defaultAgent?: string, fail = false) {
  const home = tempDir('iat-sync-');
  const warnings: string[] = [];
  const updates: [string, unknown][] = [];
  const settings = new AgentSettings(home, w => warnings.push(w));
  const deps: SyncDeps = {
    settings,
    ide: {
      defaultAgent: () => defaultAgent,
      value: <K extends SyncedSetting>(key: K) => (values[key] ?? SHARED_DEFAULTS[key]) as SharedSettings[K],
      update: (name, value) => {
        updates.push([name, value]);
        return fail ? Promise.reject(new Error('read-only')) : Promise.resolve();
      },
    },
    warn: w => warnings.push(w),
  };
  const config = path.join(home, CONFIG_FILE);
  const saved = () => (fs.existsSync(config) ? JSON.parse(fs.readFileSync(config, 'utf8')) : undefined);
  return { deps, warnings, updates, config, saved };
}

test('every synced setting has an IDE setting name', () => {
  assert.deepEqual(new Set(SHARED_KEYS), new Set(Object.keys(SHARED_DEFAULTS).filter(k => k !== 'launchVia')));
  assert.equal(SHARED_SETTING_NAMES.tabRouting, 'openNewTabsIn');
});

test('sharing writes only the IDE values that differ from config.json', () => {
  const { deps, saved } = fixture({ terminal: 'wezterm', allowResume: false });
  shareSettings(deps);
  assert.deepEqual(saved(), { terminal: 'wezterm', allowResume: false });
});

test('following config.json updates the IDE settings, and shares IDE values config.json lacks', async () => {
  const { deps, updates, config, saved } = fixture({ shell: '/opt/pwsh' });
  fs.writeFileSync(config, '{"terminal": "kitty"}');
  await followSharedSettings(deps);
  assert.deepEqual(updates, [['preferredTerminal', 'kitty']]);
  assert.deepEqual(saved(), { terminal: 'kitty', shell: '/opt/pwsh' });
});

test('a broken config.json is neither followed nor overwritten', async () => {
  const { deps, updates, config } = fixture({ terminal: 'wezterm' });
  fs.writeFileSync(config, '{broken');
  shareSettings(deps);
  await followSharedSettings(deps);
  assert.deepEqual(updates, []);
  assert.equal(fs.readFileSync(config, 'utf8'), '{broken');
});

test('the IDE follows the shared default agent and logs a failed update', async () => {
  const { deps, updates, warnings, config } = fixture({}, 'claude', true);
  fs.writeFileSync(config, '{"defaultAgent": "codex"}');
  await followSharedDefault(deps);
  assert.deepEqual(updates, [['defaultAgent', 'codex']]);
  assert.deepEqual(warnings, ['Could not update ideAgentTabs.defaultAgent: read-only']);
});

test('an unknown shared default is replaced by the IDE default', async () => {
  const { deps, updates, config, saved } = fixture();
  fs.writeFileSync(config, '{"defaultAgent": "nope"}');
  await followSharedDefault(deps);
  assert.deepEqual(updates, []);
  assert.equal(saved().defaultAgent, 'claude');
});

test('a configuration change reports whether a shared setting or the default agent changed', () => {
  const { deps, saved } = fixture({ focusNewTabs: 'never' });
  assert.equal(settingsChanged(deps, () => false), false);
  assert.equal(saved(), undefined);
  assert.equal(settingsChanged(deps, s => s === 'ideAgentTabs.focusNewTabs'), true);
  assert.deepEqual(saved(), { focusNewTabs: 'never' });
  assert.equal(settingsChanged(deps, s => s === 'ideAgentTabs.defaultAgent'), true);
  assert.deepEqual(saved(), { focusNewTabs: 'never', defaultAgent: 'claude' });
});

import { BadRequest, checkEnv, isPlainObject, MAX_ENTRIES, optString, optStringList, optStringMap, ORI_PREFIX, parseObject } from './request';
import { DetectedOri, LaunchVia, SharedSettings } from './sharedSettings';

export const AGENTS_FILE = 'agents.json';

const PROFILE_NAME = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;

export interface AgentProfile {
  name: string;
  label: string;
  command: string;
  args: string[];
  promptFlag?: string;
  modelFlag?: string;
  env: Record<string, string>;
  icon?: string;
}

export interface AgentLaunch {
  agent: string;
  command: string;
  args: string[];
  prompt?: string;
  env: Record<string, string>;
  via?: LaunchVia;
}

export function profile(name: string, label: string, command: string, extra: Partial<AgentProfile> = {}): AgentProfile {
  return { name, label, command, args: [], env: {}, ...extra };
}

export const ORI_PROFILES: readonly string[] = ['claude', 'codex', 'grok', 'hermes', 'opencode', 'pi', 'prime-agent'];

function oriLaunchable(name: string, ori: DetectedOri | null): boolean {
  return ori !== null && ori.agents.includes(name) && ORI_PROFILES.includes(name);
}

// Same rows as list_agents in claude-plugin/mcp/src/ide_agent_tabs/service.py, less the agents that aren't installed.
interface AgentEntry {
  name: string;
  label: string;
  profile: AgentProfile;
  via?: 'ori';
}

type EntryToggles = Pick<SharedSettings, 'showOriAgents' | 'showLmStudioAgents' | 'showOllamaAgents'>;

interface LocalVariant {
  name: string;
  base: string;
  toggle: 'showLmStudioAgents' | 'showOllamaAgents';
}

const LOCAL_VARIANTS: readonly LocalVariant[] = [
  { name: 'codex-lmstudio', base: 'codex', toggle: 'showLmStudioAgents' },
  { name: 'codex-ollama', base: 'codex', toggle: 'showOllamaAgents' },
];

const AGENT_ALIASES: ReadonlyMap<string, string> = new Map([['codex-local', 'codex-ollama']]);

export function findProfile(profiles: readonly AgentProfile[], name: string | undefined): AgentProfile | undefined {
  if (name === undefined) return undefined;
  const exact = profiles.find(p => p.name === name);
  const alias = AGENT_ALIASES.get(name);
  return exact ?? (alias === undefined ? undefined : profiles.find(p => p.name === alias));
}

export function variantShown(name: string, toggles: EntryToggles): boolean {
  const variant = LOCAL_VARIANTS.find(v => v.name === name);
  return variant === undefined || toggles[variant.toggle];
}

export function agentEntries(
  profiles: readonly AgentProfile[],
  ori: DetectedOri | null,
  toggles: EntryToggles,
  installed: (p: AgentProfile) => boolean,
): AgentEntry[] {
  const entries: AgentEntry[] = [];
  for (const p of profiles) {
    if (LOCAL_VARIANTS.some(v => v.name === p.name)) continue;
    if (installed(p)) entries.push({ name: p.name, label: p.label, profile: p });
    if (toggles.showOriAgents && oriLaunchable(p.name, ori)) entries.push({ name: `${ORI_PREFIX}${p.name}`, label: `${p.label} - Ori`, profile: p, via: 'ori' });
    for (const v of LOCAL_VARIANTS) {
      const local = v.base === p.name && toggles[v.toggle] ? findProfile(profiles, v.name) : undefined;
      if (local !== undefined && installed(local)) entries.push({ name: local.name, label: local.label, profile: local });
    }
  }
  return entries;
}

// Same strings as CODEX_TAB_ARGS in claude-plugin/mcp/src/ide_agent_tabs/profiles.py, which explains them; mcp/tests/test_codex_tab.py checks both.
export const CODEX_TAB_ARGS: readonly string[] = Object.freeze([
  "--no-daemon",
  "-c",
  "mcp_servers.ide-agent-tabs={ command = 'python3', args = ['-I', '-S', '-c', '''import os,runpy;h=os.environ.get('IDE_AGENT_TABS_HOME') or os.path.join(os.path.expanduser('~'),'.ide-agent-tabs');runpy.run_path(os.path.join(h,'mcp','py','launch','mcp_server.py'),run_name='__main__')'''], env_vars = ['IDE_AGENT_TABS_ID', 'IDE_AGENT_TABS_AGENT', 'IDE_AGENT_TABS_HOME'], tool_timeout_sec = 660 }",
  "-c",
  "hooks.UserPromptSubmit=[{ hooks = [{ type = 'mcp_tool', server = 'ide-agent-tabs', tool = 'agent_tabs_hook', input = { event = 'UserPromptSubmit', session_id = '${session_id}', turn_id = '${turn_id}' }, timeout = 10 }] }]",
  "-c",
  "hooks.PostToolUse=[{ hooks = [{ type = 'mcp_tool', server = 'ide-agent-tabs', tool = 'agent_tabs_hook', input = { event = 'PostToolUse', session_id = '${session_id}', turn_id = '${turn_id}' }, timeout = 10 }] }]",
  "-c",
  "hooks.PermissionRequest=[{ hooks = [{ type = 'mcp_tool', server = 'ide-agent-tabs', tool = 'agent_tabs_hook', input = { event = 'PermissionRequest', session_id = '${session_id}', turn_id = '${turn_id}' }, timeout = 10 }] }]",
  "-c",
  "hooks.Stop=[{ hooks = [{ type = 'mcp_tool', server = 'ide-agent-tabs', tool = 'agent_tabs_hook', input = { event = 'Stop', session_id = '${session_id}', turn_id = '${turn_id}' }, timeout = 10 }] }]",
  "-c",
  "hooks.Interrupt=[{ hooks = [{ type = 'mcp_tool', server = 'ide-agent-tabs', tool = 'agent_tabs_hook', input = { event = 'Interrupt', session_id = '${session_id}', turn_id = '${turn_id}' }, timeout = 3 }] }]",
  "-c",
  "hooks.state={ '/<session-flags>/config.toml:user_prompt_submit:0:0' = { trusted_hash = 'sha256:aac36b4c0cfafe0f4ae641176bcc1ab25ae590dbe3be9268f7570b55ab4afe89' }, 'C:\\<session-flags>\\config.toml:user_prompt_submit:0:0' = { trusted_hash = 'sha256:aac36b4c0cfafe0f4ae641176bcc1ab25ae590dbe3be9268f7570b55ab4afe89' }, '/<session-flags>/config.toml:post_tool_use:0:0' = { trusted_hash = 'sha256:75aa06c6f44c8918fe729537b56d5f498c931e032f5d89499593b4cd67ba335e' }, 'C:\\<session-flags>\\config.toml:post_tool_use:0:0' = { trusted_hash = 'sha256:75aa06c6f44c8918fe729537b56d5f498c931e032f5d89499593b4cd67ba335e' }, '/<session-flags>/config.toml:permission_request:0:0' = { trusted_hash = 'sha256:5e1483151807db1577272adc730b9ffe56c96d22b1a8fe7c7ff6d6efe42f3626' }, 'C:\\<session-flags>\\config.toml:permission_request:0:0' = { trusted_hash = 'sha256:5e1483151807db1577272adc730b9ffe56c96d22b1a8fe7c7ff6d6efe42f3626' }, '/<session-flags>/config.toml:stop:0:0' = { trusted_hash = 'sha256:a97c883d6b41f88f6879ce99d0d343a7069f3fded56573aaa2b15fc5bbd01c6f' }, 'C:\\<session-flags>\\config.toml:stop:0:0' = { trusted_hash = 'sha256:a97c883d6b41f88f6879ce99d0d343a7069f3fded56573aaa2b15fc5bbd01c6f' }, '/<session-flags>/config.toml:interrupt:0:0' = { trusted_hash = 'sha256:c2704217d5db401ed47f178ff9db1a7be09662f73b3e55753f8600e42bd53165' }, 'C:\\<session-flags>\\config.toml:interrupt:0:0' = { trusted_hash = 'sha256:c2704217d5db401ed47f178ff9db1a7be09662f73b3e55753f8600e42bd53165' } }",
]);

export const BUILTIN_PROFILES: readonly AgentProfile[] = Object.freeze([
  profile('claude', 'Claude Code', 'claude', { modelFlag: '--model' }),
  profile('codex', 'Codex', 'codex', { args: [...CODEX_TAB_ARGS], modelFlag: '-m' }),
  profile('agy', 'Antigravity CLI', 'agy', { promptFlag: '-i', modelFlag: '--model' }),
  profile('copilot', 'Copilot CLI', 'copilot', { promptFlag: '-i', modelFlag: '--model' }),
  profile('grok', 'Grok Build', 'grok', { modelFlag: '-m' }),
  profile('pi', 'Pi', 'pi', { modelFlag: '--model' }),
  profile('hermes', 'Hermes', 'hermes', { args: ['chat'], promptFlag: '-q', modelFlag: '-m' }),
  profile('opencode', 'OpenCode', 'opencode', { promptFlag: '--prompt', modelFlag: '-m' }),
  profile('qwen', 'Qwen Code', 'qwen', { promptFlag: '-i', modelFlag: '-m' }),
  profile('goose', 'Goose', 'goose', { args: ['run', '-s'], promptFlag: '-t', modelFlag: '--model' }),
  // Without --local-provider, --oss stops at a picker between LM Studio and Ollama.
  profile('codex-lmstudio', 'Codex - LM Studio', 'codex', { args: [...CODEX_TAB_ARGS, '--oss', '--local-provider', 'lmstudio'], modelFlag: '-m' }),
  profile('codex-ollama', 'Codex - Ollama', 'codex', { args: [...CODEX_TAB_ARGS, '--oss', '--local-provider', 'ollama'], modelFlag: '-m' }),
]);

export function parseProfiles(text: string): AgentProfile[] {
  const root = parseObject(text, AGENTS_FILE);
  return Object.entries(root).map(([name, obj]) => {
    if (!PROFILE_NAME.test(name)) throw new BadRequest(`profile name '${name}' must be letters, digits, '.', '_' or '-'`);
    if (!isPlainObject(obj)) throw new BadRequest(`profile ${name} must be an object`);
    const command = optString(obj, 'command', `${name}.command`);
    if (command === undefined || command.trim() === '' || command.includes('\0')) throw new BadRequest(`profile ${name} needs a command`);
    const args = optStringList(obj, 'args', `${name}.args`);
    if (args.length > MAX_ENTRIES) throw new BadRequest(`${name}.args exceeds ${MAX_ENTRIES} entries`);
    if (args.some(a => a.includes('\0'))) throw new BadRequest(`${name}.args holds a NUL`);
    const promptFlag = optString(obj, 'promptFlag', `${name}.promptFlag`);
    if (promptFlag !== undefined && (promptFlag.trim() === '' || promptFlag.includes('\0'))) {
      throw new BadRequest(`${name}.promptFlag must not be blank`);
    }
    const modelFlag = optString(obj, 'modelFlag', `${name}.modelFlag`);
    if (modelFlag !== undefined && (modelFlag.trim() === '' || modelFlag.includes('\0'))) {
      throw new BadRequest(`${name}.modelFlag must not be blank`);
    }
    const env = optStringMap(obj, 'env', `${name}.env`);
    checkEnv(env, `${name}.env`);
    const label = optString(obj, 'label', `${name}.label`);
    const icon = optString(obj, 'icon', `${name}.icon`);
    return {
      name,
      label: label !== undefined && label.trim() !== '' ? label : name,
      command,
      args,
      promptFlag,
      modelFlag,
      env,
      icon: icon !== undefined && icon.trim() !== '' ? icon : undefined,
    };
  });
}

export function mergeProfiles(builtins: readonly AgentProfile[], custom: AgentProfile[]): AgentProfile[] {
  const byName = new Map(custom.map(c => [c.name, c]));
  const builtinNames = new Set(builtins.map(b => b.name));
  return [...builtins.map(b => byName.get(b.name) ?? b), ...custom.filter(c => !builtinNames.has(c.name))];
}

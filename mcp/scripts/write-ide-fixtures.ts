import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { cliInvocation, editorCliLocations } from '../src/editorClis.js';
import { entryForProduct, entryForProductInfo, findIdeEntry, normalizeIdeName, productMatchesName } from '../src/ideCatalog.js';
import { discoverIdes, ideLaunchCommand, launchEnvironment, launcherPath, parseProductInfo, type IdeInstall } from '../src/ideInstalls.js';
import { planLaunch } from '../src/launchPlan.js';
import { parseDetection } from '../src/detection.js';
import { parseClosed, previewOf } from '../src/messaging/closed.js';
import { BUILTIN_PROFILES, launchOf, resolveSettings, type AgentProfile } from '../src/profiles.js';
import { parseEndpoint } from '../src/registry.js';
import { ago, closedListing, costCheck, sizeText } from '../src/resume.js';
import { chooseIde, chooseTerminal, projectDepth, type IdeCandidate } from '../src/routing.js';
import { checkPosixEnvNames, posixSpec, powerShellSpec, type LaunchSpec } from '../src/spec.js';
import { parseTabs } from '../src/tabStore.js';
import { appleScriptString, closeScript, ghosttyCapabilities, ghosttyLinuxArgs, ghosttyPlace, inputScript, listScript, openScript, parseOpenResult } from '../src/terminals/ghostty.js';
import { defaultTerminalName } from '../src/terminals/index.js';
import {
  CLOSE_SCRIPT,
  INPUT_SCRIPT,
  LIST_SCRIPT,
  OPEN_SCRIPT,
  classifyOsascriptError,
  createIterm2,
  iterm2Command,
  iterm2Placement,
  osascriptErrorMessage,
  parseOpenAnswer,
} from '../src/terminals/iterm2.js';
import { kittyInputCalls, kittyLaunchArgs, kittySpawnArgs, parseKittyOsWindows, parseKittyWindowId, planKittyPlace } from '../src/terminals/kitty.js';
import { candidatePaths, compareVersions, detectPowerShells, listPowerShells, parseVersionOutput, pickPowerShell, shellLabel, type ShellProbe } from '../src/terminals/powershell.js';
import { isShellName, terminalEnvironment } from '../src/terminals/processes.js';
import { argvModeCommand, checkArgvPaths, loginShell, surfaceArgv, surfaceCommand, tabTitle } from '../src/terminals/shell.js';
import {
  parseTmuxSessions,
  parseTmuxWindow,
  parseTmuxWindowList,
  planDedicatedTmuxTarget,
  planTmuxTarget,
  tmuxInputArgs,
  tmuxOpenArgs,
  tmuxTitle,
} from '../src/terminals/tmux.js';
import type { OpenOptions, TerminalTab } from '../src/terminals/types.js';
import {
  parsePaneId,
  parseWeztermPaneWindows,
  planWeztermTargets,
  weztermCliArgs,
  weztermInputArgs,
  weztermSpawnArgs,
  weztermStartArgs,
} from '../src/terminals/wezterm.js';
import { parseTasklist, powerShellArgv, wtArgs, wtTitle, wtWindow } from '../src/terminals/windowsTerminal.js';
import { compareVersions as compareBuilds } from '../src/version.js';

export const IDE_FIXTURES_FILE = fileURLToPath(new URL('../tests/fixtures/ide.json', import.meta.url));

type Case = { args: unknown[]; result?: unknown; error?: string };

function cases(fn: (...args: never[]) => unknown, argsList: unknown[][]): Case[] {
  return argsList.map((args) => {
    try {
      const result = (fn as (...a: unknown[]) => unknown)(...args);
      return { args, result: result === undefined ? null : result };
    } catch (e) {
      return { args, error: (e as Error).message };
    }
  });
}

async function asyncCases(fn: (...args: never[]) => Promise<unknown>, argsList: unknown[][]): Promise<Case[]> {
  const out: Case[] = [];
  for (const args of argsList) {
    try {
      const result = await (fn as (...a: unknown[]) => Promise<unknown>)(...args);
      out.push({ args, result: result === undefined ? null : result });
    } catch (e) {
      out.push({ args, error: (e as Error).message });
    }
  }
  return out;
}

const profileJson = (p: AgentProfile) => ({ ...p });

const AGENTS_TEXTS: (string | null)[] = [
  null,
  '{}',
  '{"mine": {"command": "my-cli", "args": ["--fast"], "promptFlag": "-p", "modelFlag": "--model", "env": {"B": "2", "A": "1", "10": "x"}, "label": "Mine", "icon": "star"}}',
  '{"claude": {"command": "claude-beta", "label": "  "}, "2": {"command": "two"}, "b": {"command": "bee"}}',
  '{"bad name!": {"command": "x"}}',
  '{"x": {"command": ""}}',
  '{"x": {"command": "x", "args": "no"}}',
  '{"x": {"command": "x", "env": {"IDE_AGENT_TABS_ID": "1"}}}',
  '{"x": {"command": "x", "env": {"A B": "1"}}}',
  '{"x": {"command": "x", "promptFlag": " "}}',
  '{"x": {"command": "x", "env": {"A": 1}}}',
  '[]',
  '{"x": 5}',
];

const CONFIG_TEXTS: (string | null)[] = [
  null,
  '{}',
  '{"defaultAgent": "codex", "tabRouting": "caller", "terminal": "wezterm", "terminalWindow": "dedicated", "launchVia": "ori", "focusNewTabs": "always", "claudeMod": "off", "allowResume": false, "ideStartTimeoutSec": 0.4, "shell": "C:\\\\Program Files\\\\PowerShell\\\\7\\\\pwsh.exe"}',
  '{"defaultAgent": "nope", "tabRouting": "sideways", "terminal": 5, "terminalWindow": null, "focusNewTabs": "sometimes", "allowResume": "yes", "ideStartTimeoutSec": 99999, "shell": "pwsh"}',
  '{"terminal": "auto", "shell": "auto", "ideStartTimeoutSec": -1}',
  '{"terminal": "  ", "shell": "/bin/zsh", "ideStartTimeoutSec": 3600}',
  '{"jev": {"enabled": true, "sure": 0.9, "tiers": {"claude": "smart", "codex:gpt-5": "fast"}, "pricePerMillionInput": 0.1}}',
  '{"jev": {"enabled": "yes"}}',
  '{"jev": {"sure": 2}}',
  '{"jev": {"tiers": {"bad name": "x"}}}',
  '{"jev": {"tiers": {"x": " "}}}',
  '{"jev": 5}',
  '[1]',
];

const SETTINGS_INPUTS: [string | null, string | null][] = [
  ...AGENTS_TEXTS.map((a): [string | null, string | null] => [a, null]),
  ...CONFIG_TEXTS.map((c): [string | null, string | null] => [null, c]),
  [AGENTS_TEXTS[2]!, '{"defaultAgent": "mine"}'],
  [AGENTS_TEXTS[4]!, CONFIG_TEXTS[3]!],
  [AGENTS_TEXTS[3]!, CONFIG_TEXTS[12]!],
];

const settingsCases = SETTINGS_INPUTS.map(([agents, config]) => {
  {
    const s = resolveSettings(agents ?? undefined, config ?? undefined, '/h/agents.json', '/h/config.json');
    return {
      args: [agents, config],
      result: {
        profiles: s.profiles.map((p) => (BUILTIN_PROFILES.includes(p) ? p.name : profileJson(p))),
        defaultAgent: s.defaultAgent.name,
        tabRouting: s.tabRouting,
        terminalWindow: s.terminalWindow,
        launchVia: s.launchVia,
        focusNewTabs: s.focusNewTabs,
        claudeMod: s.claudeMod,
        allowResume: s.allowResume,
        ideStartTimeoutSec: s.ideStartTimeoutSec,
        preferredTerminal: s.preferredTerminal ?? null,
        shell: s.shell ?? null,
        jev: s.jev,
        warnings: s.warnings,
      },
    };
  }
});

const profile = (name: string) => BUILTIN_PROFILES.find((p) => p.name === name)!;
const ORI = { path: '/x/ori', version: '1.2.3', agents: ['claude', 'codex', 'opencode'] };
const planArgs: unknown[][] = [
  ['claude', { args: [], env: {}, launchVia: 'direct', ori: null, platform: 'linux' }],
  ['claude', { prompt: 'hi', args: ['--x'], env: { A: '1' }, model: 'opus', launchVia: 'direct', ori: null, platform: 'linux' }],
  ['gemini', { prompt: 'hi there', args: [], env: {}, launchVia: 'direct', ori: null, platform: 'win32' }],
  ['goose', { args: [], env: {}, launchVia: 'direct', ori: null, platform: 'linux' }],
  ['goose', { prompt: 'go', args: [], env: {}, model: 'm', launchVia: 'direct', ori: null, platform: 'linux' }],
  ['hermes', { prompt: 'p', args: [], env: {}, launchVia: 'direct', ori: null, platform: 'linux' }],
  ['claude', { prompt: 'p', args: [], env: {}, model: 'sonnet', launchVia: 'ori', ori: ORI, platform: 'linux' }],
  ['claude', { args: [], env: {}, launchVia: 'ori', ori: null, platform: 'linux' }],
  ['claude', { args: [], env: {}, via: 'ori', launchVia: 'direct', ori: null, platform: 'linux' }],
  ['gemini', { args: [], env: {}, via: 'ori', launchVia: 'direct', ori: ORI, platform: 'linux' }],
  ['grok', { args: [], env: {}, via: 'ori', launchVia: 'direct', ori: ORI, platform: 'linux' }],
  ['codex', { prompt: 'a & b', args: [], env: {}, via: 'ori', launchVia: 'direct', ori: ORI, platform: 'win32', cmdShim: true }],
  ['codex', { prompt: 'a & b', args: [], env: {}, launchVia: 'ori', ori: ORI, platform: 'win32', cmdShim: false }],
  ['claude', { args: [], env: {}, model: 'bad model', launchVia: 'direct', ori: null, platform: 'linux' }],
];

function launchJson(name: string, r: Record<string, unknown>) {
  const plan = planLaunch(profile(name), r as unknown as Parameters<typeof planLaunch>[1]);
  return { via: plan.via, launch: plan.launch };
}

const candidates = (list: [string, number, [string, string, boolean][]][]): IdeCandidate[] =>
  list.map(([id, startedAt, projects]) => ({ id, startedAt, projects: projects.map(([name, p, focused]) => ({ name, path: p, focused })) }));

const ROUTING: unknown[][] = [
  [candidates([['a', 1, [['w', '/work', false]]], ['b', 2, [['w2', '/work/app', false]]]]), '/work/app/src', false, undefined, 'project'],
  [candidates([['a', 1, [['w', '/work', false]]], ['b', 2, [['w', '/work', true]]]]), '/work/x', false, 'a', 'project'],
  [candidates([['a', 1, [['w', '/work', false]]], ['b', 2, [['w', '/work', false]]]]), '/work/x', false, undefined, 'project'],
  [candidates([['a', 1, [['w', '/other', false]]], ['b', 3, [['w', '/else', false]]]]), '/work/x', false, undefined, 'project'],
  [candidates([['a', 1, [['w', '/other', false]]], ['b', 3, [['w', '/else', false]]]]), '/work/x', false, 'a', 'project'],
  [candidates([['a', 1, [['w', '/work', false]]], ['b', 3, []]]), '/work/x', false, 'b', 'caller'],
  [candidates([['a', 1, [['w', '/work', false]]], ['b', 3, [['p', '/p', false]]]]), '/work/x', false, 'b', 'caller'],
  [candidates([['a', 1, [['W', 'C:\\Work', false]]]]), 'c:/work/App', true, undefined, 'project'],
  [candidates([['a', 1, [['W', 'C:\\Work', false]]]]), 'D:\\work', true, undefined, 'project'],
  [candidates([]), '/x', false, undefined, 'project'],
];

const ENDPOINTS: unknown[][] = [
  ['{"protocol": 1, "ide": "jetbrains", "product": "IntelliJ IDEA", "version": "2026.1", "pid": 42, "url": "http://127.0.0.1:5000/ide-agent-tabs/", "token": "t", "startedAt": 5, "beatMs": 1000}', '/h/endpoints/jetbrains-1.json', 99],
  ['{"protocol": 1, "ide": "vscode", "pid": 42, "url": "http://localhost:1/x", "token": "t"}', '/h/endpoints/vscode-2.JSON', 77.5],
  ['{"protocol": 1, "ide": "vscode", "pid": 42, "url": "http://[::1]:1/x", "token": "t", "product": "", "beatMs": -1, "startedAt": 0}', '/h/e/v.json', 3],
  ['{"protocol": 2}', '/h/e/a.json', 1],
  ['{"ide": "x"}', '/h/e/a.json', 1],
  ['{"protocol": "1"}', '/h/e/a.json', 1],
  ['{"protocol": null}', '/h/e/a.json', 1],
  ['{"protocol": 1, "ide": "x", "pid": 0, "url": "http://127.0.0.1", "token": "t"}', '/h/e/a.json', 1],
  ['{"protocol": 1, "ide": "x", "pid": 1.5, "url": "http://127.0.0.1", "token": "t"}', '/h/e/a.json', 1],
  ['{"protocol": 1, "ide": "x", "pid": 1, "url": "http://example.com", "token": "t"}', '/h/e/a.json', 1],
  ['{"protocol": 1, "ide": "x", "pid": 1, "url": "ftp://127.0.0.1", "token": "t"}', '/h/e/a.json', 1],
  ['{"protocol": 1, "ide": "x", "pid": 1, "url": "https://LOCALHOST:8443", "token": "t"}', '/h/e/a.json', 1],
  ['not json', '/h/e/a.json', 1],
  ['[1]', '/h/e/a.json', 1],
];

const IDE_NAMES = ['vscode', 'Code', 'VS Code', 'visual-studio-code', 'insiders', 'Cursor', 'Windsurf Next', 'codium', 'antigravity-ide', 'studio', 'Android Studio', 'intellij', 'IntelliJ IDEA Ultimate', 'idea', 'pycharm', 'PyCharm Community', 'rider', 'notepad', '', '---', 'Kiro'];
const PRODUCTS = ['Visual Studio Code', 'visual studio code - insiders', ' Cursor ', 'Windsurf Next', 'IntelliJ IDEA', 'IntelliJ IDEA Community Edition', 'Android Studio Narwhal', 'PyCharm', 'Positron', 'Fake Studio', 'Antigravity'];

const productInfos: unknown[][] = [
  ['{"name": "IntelliJ IDEA", "version": "2026.1", "buildNumber": "261.1", "productCode": "IU", "launch": [{"os": "Windows", "arch": "amd64", "launcherPath": "bin/idea64.exe"}, {"os": "Linux", "arch": "amd64", "launcherPath": "bin/idea.sh"}, {"os": "Linux", "arch": "aarch64", "launcherPath": "bin/idea-arm.sh"}, {"os": "macOS", "launcherPath": "MacOS/idea"}]}'],
  ['{"name": " ", "productCode": "AI", "launch": [{"os": "Windows"}, 5, null, {"os": "Windows", "launcherPath": "bin/studio64.exe"}]}'],
  ['{"launch": "x"}'],
  ['[1]'],
  ['nope'],
  [null],
];

function fakeFs(files: Record<string, string>, dirs: Record<string, string[]>) {
  return {
    exists: (f: string) => Object.hasOwn(files, f) || Object.hasOwn(dirs, f),
    readdir: (d: string) => dirs[d] ?? [],
    readText: (f: string) => files[f],
  };
}

const IDEA = (version: string, extra = '') =>
  `{"name": "IntelliJ IDEA", "version": "${version}", "productCode": "IU"${extra}, "launch": [{"os": "Windows", "arch": "amd64", "launcherPath": "bin/idea64.exe"}, {"os": "Linux", "launcherPath": "bin/idea.sh"}]}`;
const DISCOVERY: { platform: string; env: Record<string, string>; userHome: string; arch: string; files: Record<string, string>; dirs: Record<string, string[]> }[] = [
  {
    platform: 'win32',
    env: { ProgramFiles: 'C:\\Program Files', LOCALAPPDATA: 'C:\\Users\\u\\AppData\\Local', PATH: '' },
    userHome: 'C:\\Users\\u',
    arch: 'x64',
    files: {
      'C:\\Users\\u\\AppData\\Local\\Programs\\Microsoft VS Code\\bin\\code.cmd': '',
      'C:\\Program Files\\JetBrains\\IntelliJ IDEA 2025.3\\product-info.json': IDEA('2025.3'),
      'C:\\Program Files\\JetBrains\\IntelliJ IDEA 2025.3\\bin\\idea64.exe': '',
      'C:\\Users\\u\\AppData\\Local\\JetBrains\\Toolbox\\apps\\intellij-idea\\ch-0\\261.1\\product-info.json': IDEA('2026.1'),
      'C:\\Users\\u\\AppData\\Local\\JetBrains\\Toolbox\\apps\\intellij-idea\\ch-0\\261.1\\bin\\idea64.exe': '',
      'C:\\Program Files\\Android\\Android Studio\\product-info.json':
        '{"name": "Android Studio", "version": "AI-251.1", "productCode": "AI", "launch": [{"os": "Windows", "launcherPath": "bin/studio64.exe"}]}',
      'C:\\Program Files\\Android\\Android Studio\\bin\\studio64.exe': '',
      'C:\\Program Files\\JetBrains\\Evil\\product-info.json': '{"name": "PyCharm", "launch": [{"os": "Windows", "launcherPath": "..\\\\..\\\\evil.exe"}]}',
      'C:\\Program Files\\JetBrains\\Missing\\product-info.json': '{"name": "GoLand", "launch": [{"os": "Windows", "launcherPath": "bin/goland64.exe"}]}',
    },
    dirs: {
      'C:\\Program Files\\JetBrains': ['IntelliJ IDEA 2025.3', 'Evil', 'Missing', '.hidden'],
      'C:\\Program Files\\Android': ['Android Studio'],
      'C:\\Users\\u\\AppData\\Local\\Programs': ['Microsoft VS Code'],
      'C:\\Users\\u\\AppData\\Local\\JetBrains\\Toolbox\\apps': ['intellij-idea'],
      'C:\\Users\\u\\AppData\\Local\\JetBrains\\Toolbox\\apps\\intellij-idea': ['ch-0'],
      'C:\\Users\\u\\AppData\\Local\\JetBrains\\Toolbox\\apps\\intellij-idea\\ch-0': ['261.1'],
    },
  },
  {
    platform: 'linux',
    env: { PATH: '' },
    userHome: '/home/u',
    arch: 'arm64',
    files: {
      '/snap/bin/code': '',
      '/home/u/.local/share/flatpak/exports/bin/com.vscodium.codium': '',
      '/opt/idea/product-info.json': IDEA('2026.2'),
      '/opt/idea/bin/idea.sh': '',
      '/snap/pycharm-community/current/product-info.json':
        '{"name": "PyCharm", "version": "2026.1", "launch": [{"os": "Linux", "arch": "aarch64", "launcherPath": "bin/pycharm-arm.sh"}, {"os": "Linux", "launcherPath": "bin/pycharm.sh"}]}',
      '/snap/pycharm-community/current/bin/pycharm-arm.sh': '',
    },
    dirs: { '/opt': ['idea'], '/snap': ['pycharm-community'] },
  },
  {
    platform: 'darwin',
    env: { PATH: '' },
    userHome: '/Users/u',
    arch: 'arm64',
    files: {
      '/Applications/Cursor.app/Contents/Resources/app/bin/cursor': '',
      '/Applications/IntelliJ IDEA.app/Contents/Resources/product-info.json': IDEA('2026.1'),
      '/Users/u/Library/Application Support/JetBrains/Toolbox/apps/IntelliJ IDEA Ultimate.app/Contents/Resources/product-info.json': IDEA('2026.3'),
    },
    dirs: {
      '/Applications': ['IntelliJ IDEA.app', 'Notes.app'],
      '/Users/u/Library/Application Support/JetBrains/Toolbox/apps': ['IntelliJ IDEA Ultimate.app'],
    },
  },
];

const INSTALLS: IdeInstall[] = [
  { key: 'vscode', product: 'VS Code', kind: 'vscode', launcher: 'C:\\Program Files\\Microsoft VS Code\\bin\\code.cmd' },
  { key: 'vscode', product: 'VS Code', kind: 'vscode', launcher: '/usr/bin/code' },
  { key: 'idea', product: 'IntelliJ IDEA', kind: 'jetbrains', version: '2026.1', launcher: '/Applications/IntelliJ IDEA.app' },
  { key: 'idea', product: 'IntelliJ IDEA', kind: 'jetbrains', version: '2026.1', launcher: 'C:\\JB\\bin\\idea64.exe' },
];
const LAUNCH_COMMANDS: unknown[][] = [
  [INSTALLS[0], 'C:\\work\\app', 'win32', 'C:\\Windows\\system32\\cmd.exe'],
  [INSTALLS[0], 'C:\\work\\a&b', 'win32', undefined],
  [INSTALLS[0], 'C:\\work\\app', 'win32', undefined],
  [INSTALLS[1], '/work/app', 'linux', undefined],
  [INSTALLS[2], '/work/app', 'darwin', undefined],
  [INSTALLS[3], 'C:\\work', 'win32', undefined],
  [INSTALLS[1], '/work/\u0007', 'linux', undefined],
];

const SPEC: LaunchSpec = { id: 'tab-1', agent: 'claude', cwd: '/work', command: 'claude', args: ['--model', 'opus'], prompt: 'hi\nthere', env: { B: '2', A: '1' }, pidFile: '/h/launch/tab-1.pid' };
const SPECS: LaunchSpec[] = [SPEC, { id: 'tab-2', agent: 'codex', cwd: 'C:\\w', command: 'codex', args: [], env: {} }, { ...SPEC, prompt: 'lone \ud800 é' }];

const SHELL_POSIX = loginShell('/bin/bash', 'linux');
const SHELL_FISH = loginShell('/usr/local/bin/fish', 'darwin');
const NEAR = (o: Partial<TerminalTab>): TerminalTab => ({ id: 'near', terminal: 'kitty', agent: 'claude', path: '/w', createdAt: 1, ...o });
const opts = (o: Partial<OpenOptions> & { window?: 'last' | 'dedicated' }): OpenOptions => ({ window: 'last', ...o });

function probe(files: string[], dirs: Record<string, string[]>, links: Record<string, string>, env: Record<string, string>, versions: Record<string, string> = {}, mtimes: Record<string, number> = {}): ShellProbe {
  return {
    env,
    exists: (f) => files.includes(f),
    readdir: (d) => dirs[d],
    readlink: (f) => links[f],
    mtimeMs: (f) => mtimes[f],
    version: async (exe) => versions[exe],
  };
}

const WIN_ENV = { ProgramFiles: 'C:\\Program Files', LOCALAPPDATA: 'C:\\Users\\u\\AppData\\Local', SystemRoot: 'C:\\WINDOWS', PATH: 'C:\\Program Files\\PowerShell\\7;"C:\\Users\\u\\AppData\\Local\\Microsoft\\WindowsApps";C:\\WINDOWS\\System32\\WindowsPowerShell\\v1.0' };
const PROBES: { files: string[]; dirs: Record<string, string[]>; links: Record<string, string>; env: Record<string, string>; versions?: Record<string, string>; mtimes?: Record<string, number> }[] = [
  {
    files: [
      'C:\\Program Files\\PowerShell\\7\\pwsh.exe',
      'C:\\Program Files\\PowerShell\\7-preview\\pwsh.exe',
      'C:\\Users\\u\\AppData\\Local\\Microsoft\\WindowsApps\\pwsh.exe',
      'C:\\WINDOWS\\System32\\WindowsPowerShell\\v1.0\\powershell.exe',
    ],
    dirs: { 'C:\\Program Files\\PowerShell': ['7-preview', '7'], 'C:\\Program Files\\WindowsApps': ['Microsoft.PowerShell_7.5.4.0_x64__8wekyb3d8bbwe', 'Microsoft.PowerShell_7.4.1.0_x64__8wekyb3d8bbwe'] },
    links: { 'C:\\Users\\u\\AppData\\Local\\Microsoft\\WindowsApps\\pwsh.exe': 'C:\\Program Files\\WindowsApps\\Microsoft.PowerShell_7.6.6.0_x64__8wekyb3d8bbwe\\pwsh.exe' },
    env: WIN_ENV,
    versions: { 'C:\\Program Files\\PowerShell\\7\\pwsh.exe': '7.5.1', 'C:\\Program Files\\PowerShell\\7-preview\\pwsh.exe': '7.6.0-preview.4' },
  },
  {
    files: ['C:\\Users\\u\\AppData\\Local\\Microsoft\\WindowsApps\\pwsh.exe', 'C:\\WINDOWS\\System32\\WindowsPowerShell\\v1.0\\powershell.exe', 'D:\\tools\\pwsh.exe'],
    dirs: { 'C:\\Program Files\\WindowsApps': ['Microsoft.PowerShell_7.5.4.0_x64__8wekyb3d8bbwe', 'Microsoft.PowerShellPreview_7.6.0.0_x64__8wekyb3d8bbwe'] },
    links: {},
    env: { ...WIN_ENV, PATH: 'D:\\tools' },
    versions: { 'D:\\tools\\pwsh.exe': 'garbage' },
  },
  { files: [], dirs: {}, links: {}, env: {} },
];

function probeOf(p: (typeof PROBES)[number]) {
  return probe(p.files, p.dirs, p.links, p.env, p.versions ?? {}, p.mtimes ?? {});
}

const CLOSED_TEXTS = [
  '{"id": "s-1", "agent": "claude", "folder": "/w", "endedAt": "2026-10-08T12:00:00.000Z", "tokens": 1200, "cache": "1h", "via": "ori", "name": null, "label": "Claude Code", "harness": "Claude Code via OpenRouter", "model": "opus", "product": "IntelliJ IDEA", "tab": "t-1"}',
  '{"id": "s-2", "agent": "pi", "folder": "/w", "endedAt": "2026-10-08T11:00:00Z", "tokens": 1.5, "cache": "2h", "via": "x"}',
  '{"id": "-bad", "agent": "pi", "folder": "/w", "endedAt": "2026-10-08T11:00:00Z"}',
  '{"id": "s-3", "agent": "pi", "folder": "/w", "endedAt": "never"}',
  'nope',
];
const NOW = Date.parse('2026-10-08T12:30:00.000Z');
const RECORDS = CLOSED_TEXTS.map((t) => parseClosed(t)).filter((r) => r !== undefined);

async function iterm2Opens(): Promise<unknown[]> {
  const out: unknown[] = [];
  const optionsList: (OpenOptions | undefined)[] = [
    undefined,
    opts({ focus: false }),
    opts({ window: 'dedicated' }),
    opts({ window: 'dedicated', focus: false }),
    opts({ near: NEAR({ terminal: 'iterm2', terminalId: 'w0t0p0:ABC' }) }),
  ];
  for (const options of optionsList) {
    const calls: { script: string; args: string[] }[] = [];
    const remembered: unknown[] = [];
    const driver = createIterm2({
      platform: 'darwin',
      findApp: () => '/Applications/iTerm.app',
      osascript: async (script, args) => {
        calls.push({ script: script === OPEN_SCRIPT ? 'OPEN' : script, args });
        return { code: 0, stdout: 'w0t1p0:XYZ\n77\n', stderr: '' };
      },
      writeSpec: async () => undefined,
      removeSpec: async () => undefined,
      readWindow: async () => ({ id: '5' }),
      rememberWindow: async (_home, w) => void remembered.push(w),
    });
    const tab = await driver.open({ home: '/h', scriptsDir: '/p/launch', pathVar: '', env: { SHELL: '/bin/zsh' } }, SPEC, 'Claude Code', options);
    out.push({ options: options ?? null, calls, remembered, tab: { ...tab, createdAt: 0 } });
  }
  return out;
}

export async function ideFixturesText(): Promise<string> {
  const data = {
    probes: PROBES,
    builtinProfiles: [{ args: [], result: BUILTIN_PROFILES.map(profileJson) }],
    settings: settingsCases,
    launchOf: cases(
      (name: string, prompt: string | null, args: string[], env: Record<string, string>) => launchOf(profile(name), prompt ?? undefined, args, env),
      [
        ['claude', null, [], {}],
        ['gemini', 'hi', ['--yolo'], { A: '1' }],
        ['hermes', 'q', [], {}],
      ],
    ),
    planLaunch: cases(launchJson, planArgs),
    projectDepth: cases(projectDepth, [
      ['/work', '/work/app', false],
      ['/work/', '/work', false],
      ['/work/app', '/work', false],
      ['/wor', '/work', false],
      ['  ', '/work', false],
      ['C:\\Work', 'c:/work/app/', true],
      ['C:\\Work\\..\\Other', 'C:\\other\\x', true],
      ['/a//b/./c', '/a/b/c/d', false],
    ]),
    chooseIde: cases(chooseIde, ROUTING),
    chooseTerminal: cases(chooseTerminal, [
      ['wezterm', 'windows-terminal', ['windows-terminal', 'wezterm']],
      ['kitty', 'windows-terminal', ['windows-terminal']],
      ['kitty', undefined, []],
      [undefined, 'ghostty', ['ghostty']],
      [undefined, undefined, []],
    ]),
    defaultTerminal: cases(defaultTerminalName, [
      ['win32', ['wezterm', 'windows-terminal']],
      ['darwin', ['tmux', 'kitty']],
      ['linux', ['iterm2']],
      ['freebsd', ['tmux']],
    ]),
    parseEndpoint: cases((text: string, file: string, at: number) => {
      const p = parseEndpoint(text, file, at);
      return p.kind === 'skip' ? p : { kind: 'endpoint', endpoint: { ...p.endpoint } };
    }, ENDPOINTS),
    findIdeEntry: cases((n: string) => findIdeEntry(n)?.key ?? null, IDE_NAMES.map((n) => [n])),
    normalizeIdeName: cases(normalizeIdeName, IDE_NAMES.map((n) => [n])),
    entryForProduct: cases((p: string) => entryForProduct(p)?.key ?? null, PRODUCTS.map((p) => [p])),
    productMatchesName: cases(productMatchesName, PRODUCTS.flatMap((p) => ['vscode', 'idea', 'studio', 'fake studio', 'cursor'].map((n) => [p, n]))),
    entryForProductInfo: cases((n: string | null, c: string | null) => entryForProductInfo(n ?? undefined, c ?? undefined)?.key ?? null, [
      ['IntelliJ IDEA', null],
      [null, 'ai'],
      ['Unknown', 'PY'],
      [null, null],
      ['Unknown', 'ZZ'],
    ]),
    parseProductInfo: cases((t: string | null) => parseProductInfo(t ?? undefined) ?? null, productInfos),
    launcherPath: cases((t: string, platform: string, arch: string) => launcherPath(parseProductInfo(t)!, platform as NodeJS.Platform, arch) ?? null, [
      [productInfos[0]![0], 'win32', 'x64'],
      [productInfos[0]![0], 'linux', 'arm64'],
      [productInfos[0]![0], 'linux', 'ia32'],
      [productInfos[0]![0], 'darwin', 'arm64'],
      [productInfos[0]![0], 'freebsd', 'x64'],
      [productInfos[1]![0], 'win32', 'x64'],
    ]),
    discoverIdes: cases(
      (d: (typeof DISCOVERY)[number]) =>
        discoverIdes({ platform: d.platform as NodeJS.Platform, env: d.env, userHome: d.userHome, arch: d.arch, fs: fakeFs(d.files, d.dirs) }),
      DISCOVERY.map((d) => [d]),
    ),
    editorCliLocations: cases(editorCliLocations, [
      ['win32', { LOCALAPPDATA: 'C:\\L', PROGRAMFILES: 'C:\\P' }, 'C:\\U'],
      ['win32', {}, 'C:\\U'],
      ['darwin', {}, '/Users/u'],
      ['linux', {}, '/home/u'],
      ['aix', {}, '/'],
    ]),
    cliInvocation: cases(cliInvocation, [
      ['C:\\x\\code.cmd', ['C:\\w'], 'win32', 'C:\\cmd.exe'],
      ['C:\\x\\code.BAT', ['C:\\w x'], 'win32', ''],
      ['C:\\x\\code.exe', ['C:\\w'], 'win32', undefined],
      ['C:\\x\\code.cmd', ['C:\\100%'], 'win32', undefined],
      ['/usr/bin/code.cmd', ['/w'], 'linux', undefined],
    ]),
    ideLaunchCommand: cases(ideLaunchCommand, LAUNCH_COMMANDS),
    launchEnvironment: cases(launchEnvironment, [
      [
        {
          PATH: '/bin',
          Path: 'x',
          NO_COLOR: '1',
          claude_code_entrypoint: 'cli',
          ANTHROPIC_API_KEY: 'k',
          IDE_AGENT_TABS_HOME: '/h',
          IDE_AGENT_TABS_ID: 't',
          ide_agent_tabs_home: '/h2',
          TERM_PROGRAM: 'vscode',
          TERM_PROGRAM_VERSION: '1',
          VSCODE_PID: '1',
          MCP_X: '1',
          GEMINI_CLI_X: '1',
          OPENCODE_SESSION_ID: 's',
          HOME: '/home/u',
        },
      ],
    ]),
    terminalEnvironment: cases(terminalEnvironment, [[{ CLAUDECODE: '1', claude_pid: '2', HOME: '/h', IDE_AGENT_TABS_HOME: '/x', IDE_AGENT_TABS_ID: 't' }]]),
    compareBuilds: cases(compareBuilds, [
      ['2026.1', '2025.3'],
      ['1.10', '1.9'],
      ['1.0', '1'],
      ['261.1a', '261.1'],
      ['x', '0'],
      ['1.2.3', '1.2.4'],
    ]),
    powerShellSpec: cases(powerShellSpec, SPECS.map((s) => [s])),
    posixSpec: cases((s: LaunchSpec) => posixSpec(s).toString('hex'), [...SPECS.map((s) => [s]), [{ ...SPEC, args: ['a\0b'] }]]),
    checkPosixEnvNames: cases(checkPosixEnvNames, [[{ A_1: 'x', _B: 'y' }], [{ '1A': 'x', 'A-B': 'y', OK: 'z' }]]),
    loginShell: cases(loginShell, [
      ['/bin/bash', 'linux'],
      ['/usr/bin/zsh', 'darwin'],
      ['/opt/homebrew/bin/fish', 'darwin'],
      ['/bin/tcsh', 'linux'],
      ['/bin/ba sh', 'darwin'],
      [undefined, 'linux'],
      [undefined, 'darwin'],
    ]),
    surfaceArgv: cases(surfaceArgv, [[SHELL_POSIX], [SHELL_FISH]]),
    surfaceCommand: cases(surfaceCommand, [[SHELL_POSIX], [SHELL_FISH]]),
    argvModeCommand: cases(argvModeCommand, [
      [SHELL_POSIX, '/p/launch/agent-launch.sh', '/h/launch/t.spec'],
      [SHELL_FISH, '/p/launch/agent-launch.fish', '/h/launch/t.spec'],
      [SHELL_POSIX, '/p/launch/agent-launch.sh', '/h/la\nunch/t.spec'],
    ]),
    checkArgvPaths: cases(checkArgvPaths, [
      ['wt', ['C:\\a;b'], ';'],
      ['wt', ['C:\\a\u0001b'], ''],
      ['wt', ['C:\\ok'], ';'],
      ['x', ['caf\u00e9 "q"', 'tab\tx'], ''],
    ]),
    tabTitle: cases(tabTitle, [
      ['Claude Code'],
      ['  a\u0000b\u200bc\td  '],
      ['\u0001\u0002'],
      ['x'.repeat(50)],
      ['\u{1F600}'.repeat(45)],
      ['a  \u00a0 b'],
      ['abc' + ' '.repeat(38) + 'z'],
    ]),
    isShellName: cases(isShellName, [['bash\n'], ['-zsh'], ['/bin/fish'], ['python'], ['  -bash  ']]),
    wtTitle: cases(wtTitle, [['a;b;c'], ['Claude Code']]),
    wtWindow: cases(wtWindow, [[undefined], [opts({ window: 'dedicated' })], [opts({ near: NEAR({ window: 'agent-tabs' }) })], [opts({ near: NEAR({}) })]]),
    powerShellArgv: cases(powerShellArgv, [['pwsh.exe', 'C:\\p\\agent-launch.ps1', 'C:\\h\\launch\\t.json']]),
    wtArgs: cases(
      (title: string, shell: string, launcher: string, spec: string, window?: string) => wtArgs({ title, shell, launcher, spec, ...(window !== undefined ? { window } : {}) }),
      [
        ['Claude; Code', 'C:\\Program Files\\PowerShell\\7\\pwsh.exe', 'C:\\p\\agent-launch.ps1', 'C:\\h\\launch\\t.json'],
        ['Codex', 'pwsh.exe', 'C:\\p\\agent-launch.ps1', 'C:\\h\\launch\\t.json', 'agent-tabs'],
        ['Codex', 'pwsh.exe', 'C:\\p;x\\agent-launch.ps1', 'C:\\h\\launch\\t.json'],
      ],
    ),
    parseTasklist: cases((csv: string) => [...parseTasklist(csv)], [['"pwsh.exe","1234","Console","1","90,000 K"\r\n"System Idle Process","0","Services","0","8 K"\r\n"bad","x"\r\n"PowerShell.EXE","77"']]),
    ghosttyCapabilities: cases(ghosttyCapabilities, [['linux'], ['darwin'], ['win32']]),
    ghosttyLinuxArgs: cases(ghosttyLinuxArgs, [
      ['/work/app', SHELL_POSIX],
      ['/work/a\nb', SHELL_POSIX],
    ]),
    appleScriptString: cases(appleScriptString, [['plain'], ['quote " back \\ slash'], ['bad\u0007']]),
    openScript: cases(
      (command: string, env: Record<string, string>, place: unknown, keepFocus?: boolean) => openScript(command, env, (place ?? undefined) as never, keepFocus),
      [
        ["/bin/zsh -l -i -c '. \"$IDE_AGENT_TABS_LAUNCHER\"; exec /bin/zsh -l -i'", { IDE_AGENT_TABS_LAUNCHER: '/p/l.sh', IDE_AGENT_TABS_SPEC: '/h/t.spec' }, null],
        ['cmd', { A: '1' }, null, true],
        ['cmd', {}, { nearTab: 'tab-9' }],
        ['cmd', {}, { nearTab: 'tab-9' }, true],
        ['cmd', {}, { dedicated: '42' }],
        ['cmd', {}, { dedicated: undefined }],
      ],
    ),
    ghosttyPlace: cases(
      (options: OpenOptions | null, remembered: { id: string } | null) => ghosttyPlace(options ?? undefined, remembered ?? undefined) ?? null,
      [
        [null, null],
        [opts({ near: NEAR({ terminal: 'ghostty', terminalTabId: 't1' }) }), null],
        [opts({ near: NEAR({ terminal: 'kitty', terminalTabId: 't1' }), window: 'dedicated' }), { id: '7' }],
        [opts({ window: 'dedicated' }), null],
      ],
    ),
    listScript: cases(listScript, [[]]),
    closeScript: cases(closeScript, [['term-1']]),
    inputScript: cases(inputScript, [['term-1', 'wake up'], ['term-1', 'x\ny']]),
    parseOpenResult: cases(parseOpenResult, [['tab-1\nterm-2\n'], ['tab-1\r\nterm-2\r\nwin-3\r\n'], ['only\n']]),
    kittyLaunchArgs: cases(
      (address: string, cwd: string, title: string, launcher: string, spec: string, argv: string[], place: unknown, focus: boolean | null) =>
        kittyLaunchArgs({ address, cwd, title, launcher, spec, argv, ...(place ? { place: place as never } : {}), ...(focus !== null ? { focus } : {}) }),
      [
        ['unix:/tmp/kitty-agent-tabs-1', '/w', 'Claude Code', '/p/agent-launch.sh', '/h/t.spec', surfaceArgv(SHELL_POSIX), null, null],
        ['unix:/tmp/k', '/w', 'C', '/p/l', '/h/s', ['x'], { windowId: '12' }, false],
        ['unix:/tmp/k', '/w', 'C', '/p/l', '/h/s', ['x'], { osWindow: true }, true],
        ['unix:/tmp/k', '/w', 'C', '/p/l', '/h/s', ['x'], { windowId: 'a1' }, null],
      ],
    ),
    kittySpawnArgs: cases(kittySpawnArgs, [['/w', ['/bin/bash', '-l']]]),
    kittyInputCalls: cases(kittyInputCalls, [['unix:/tmp/k', '3', 'wake'], ['unix:/tmp/k', '3', 'a\rb']]),
    parseKittyWindowId: cases(parseKittyWindowId, [[' 17\n'], ['x']]),
    parseKittyOsWindows: cases((s: string) => [...parseKittyOsWindows(s)], [
      ['[{"id": 1, "tabs": [{"windows": [{"id": 3}, {"id": 4}]}, {"windows": [{"id": "x"}]}]}, {"tabs": [{"windows": [{"id": 9}]}]}, {"id": 2.5, "tabs": []}]'],
      ['{}'],
    ]),
    planKittyPlace: cases(
      (options: OpenOptions | null, address: string, near: string[] | null, remembered: { id: string; socket?: string } | null, osw: [string, string[]][] | null) =>
        planKittyPlace(options ?? undefined, address, near ? new Set(near) : undefined, remembered ?? undefined, osw ? new Map(osw) : undefined),
      [
        [null, 'unix:/a', null, null, null],
        [opts({ near: NEAR({ socket: 'unix:/b', terminalId: '5' }) }), 'unix:/a', ['5'], null, null],
        [opts({ near: NEAR({ socket: 'unix:/b', terminalId: '5' }) }), 'unix:/a', ['6'], null, null],
        [opts({ window: 'dedicated' }), 'unix:/a', null, { id: '2', socket: 'unix:/c' }, [['2', ['8', '9']]]],
        [opts({ window: 'dedicated' }), 'unix:/a', null, { id: '2', socket: 'unix:/c' }, [['3', ['8']]]],
        [opts({ window: 'dedicated' }), 'unix:/a', null, { id: '2' }, null],
      ],
    ),
    weztermCliArgs: cases(weztermCliArgs, [[['list', '--format', 'json']]]),
    weztermSpawnArgs: cases(
      (cwd: string, argv: string[], place: unknown) => weztermSpawnArgs(cwd, argv, (place ?? undefined) as never),
      [
        ['C:\\w', ['pwsh.exe', '-NoLogo'], null],
        ['/w', ['x'], { paneId: '3' }],
        ['/w', ['x'], { windowId: '4' }],
        ['/w', ['x'], { newWindow: true }],
        ['/w', ['x'], { paneId: 'z' }],
      ],
    ),
    weztermStartArgs: cases(weztermStartArgs, [['/w', ['x', 'y']]]),
    weztermInputArgs: cases(weztermInputArgs, [['7', 'wake']]),
    parsePaneId: cases(parsePaneId, [['12\n'], ['nope']]),
    parseWeztermPaneWindows: cases((s: string) => [...parseWeztermPaneWindows(s)], [['[{"pane_id": 1, "window_id": 0}, {"pane_id": 2}, {"pane_id": "3"}, null]'], ['{}']]),
    planWeztermTargets: cases(
      (options: OpenOptions | null, near: [string, string][] | null, remembered: { id: string; socket?: string } | null, panes: [string, string][] | null) =>
        planWeztermTargets(options ?? undefined, near ? new Map(near) : undefined, remembered ?? undefined, panes ? new Map(panes) : undefined),
      [
        [null, null, null, null],
        [opts({ near: NEAR({ socket: '/s', terminalId: '5' }) }), [['5', '1']], null, null],
        [opts({ near: NEAR({ socket: '/s', terminalId: '5' }), window: 'dedicated' }), [['5', '1']], { id: '1', socket: '/r' }, [['9', '1']]],
        [opts({ window: 'dedicated' }), null, { id: '1', socket: '/r' }, [['9', '2']]],
      ],
    ),
    tmuxTitle: cases(tmuxTitle, [['a#b;c'], ['Claude Code']]),
    tmuxOpenArgs: cases(
      (target: unknown, title: string, launcher: string, spec: string, argv: string[], focus: boolean | null) =>
        tmuxOpenArgs(target as never, { title, launcher, spec, argv, ...(focus !== null ? { focus } : {}) }),
      [
        [{ session: '$1', detached: false }, 'Claude', '/p/l.sh', '/h/t.spec', surfaceArgv(SHELL_POSIX), null],
        [{ newSession: 'agents' }, 'Claude', '/p/l.sh', '/h/t.spec', ['x'], false],
        [{ after: '@3', socket: '/tmp/tmux-1/default' }, 'C#;', '/p/l.sh', '/h/t.spec', ['x'], false],
        [{ session: '$1', detached: true }, 'C', '/p/l;x', '/h/t.spec', ['x'], null],
      ],
    ),
    tmuxInputArgs: cases(tmuxInputArgs, [['/s', '@1', 'wake'], ['/s', '@1', 'bad;']]),
    parseTmuxSessions: cases(parseTmuxSessions, [['1 1700 $1 main\n0 0 $2 agents\n0 x $3 two words\nbad line\n0 0 1 nodollar']]),
    planTmuxTarget: cases((s: string) => planTmuxTarget(parseTmuxSessions(s)), [['1 1700 $1 main\n1 1800 $4 other\n0 0 $2 agents'], ['0 0 $2 agents'], ['']]),
    planDedicatedTmuxTarget: cases((s: string) => planDedicatedTmuxTarget(parseTmuxSessions(s)), [['1 1 $5 agent-tabs'], ['0 1 $5 agent-tabs'], ['']]),
    parseTmuxWindow: cases(parseTmuxWindow, [['@4 $1 999 /tmp/tmux 1/default\n'], ['@4 $1 x /s'], ['bad']]),
    parseTmuxWindowList: cases((s: string) => {
      const r = parseTmuxWindowList(s);
      return { serverPid: r.serverPid ?? null, windows: [...r.windows] };
    }, [['42 @1\n42 @2\nnoise\n'], ['']]),
    iterm2Command: cases(iterm2Command, [[argvModeCommand(SHELL_POSIX, '/p/agent-launch.sh', '/h/t.spec')], [["it's"]], [['back\\slash']]]),
    iterm2Placement: cases(
      (options: OpenOptions | null, remembered: { id: string } | null) => iterm2Placement(options ?? undefined, remembered ?? undefined),
      [
        [null, null],
        [opts({ focus: false }), null],
        [opts({ window: 'dedicated' }), { id: '9' }],
        [opts({ window: 'dedicated', focus: false }), null],
        [opts({ near: NEAR({ terminal: 'iterm2', terminalId: 's1' }) }), null],
      ],
    ),
    parseOpenAnswer: cases(parseOpenAnswer, [['w0t0p0:ABC\n12\n'], ['w0t0p0:ABC\nnope'], ['bad id!']]),
    classifyOsascriptError: cases(classifyOsascriptError, [['execution error: Not authorized to send Apple events to iTerm. (-1743)'], ["Can’t find application"], ['(-1712)'], ['boom']]),
    osascriptErrorMessage: cases(osascriptErrorMessage, [['denied', 'x'], ['not-installed', 'y'], ['timeout', 'z'], ['other', 'w']]),
    iterm2Scripts: [{ args: [], result: { OPEN_SCRIPT, LIST_SCRIPT, CLOSE_SCRIPT, INPUT_SCRIPT } }],
    iterm2Opens: [{ args: [], result: await iterm2Opens() }],
    candidatePaths: cases((i: number) => candidatePaths(probeOf(PROBES[i]!)), PROBES.map((_, i) => [i])),
    listPowerShells: cases((i: number) => listPowerShells(probeOf(PROBES[i]!)), PROBES.map((_, i) => [i])),
    detectPowerShells: await asyncCases(
      (i: number, previous: unknown) => detectPowerShells(probeOf(PROBES[i]!), (previous ?? undefined) as never),
      [
        [0, null],
        [1, null],
        [1, { detectedAt: '2026-01-01T00:00:00.000Z', shells: [{ path: 'D:\\tools\\pwsh.exe', label: 'x', version: '7.1.0', source: 'path' }] }],
      ],
    ),
    pickPowerShell: cases(
      (shells: unknown[], configured: string | null, present: string[]) => pickPowerShell(shells as never, configured ?? undefined, (f) => present.includes(f)),
      [
        [[{ path: 'a\\pwsh.exe', label: '', version: '7.4.0', source: 'msi' }, { path: 'b\\pwsh.exe', label: '', version: '7.5.0-preview.1', source: 'preview' }], null, ['a\\pwsh.exe', 'b\\pwsh.exe']],
        [[{ path: 'b\\pwsh.exe', label: '', version: '7.5.0-preview.1', source: 'preview' }], null, ['b\\pwsh.exe']],
        [[{ path: 'w\\powershell.exe', label: '', version: '5.1', source: 'windows' }], null, ['w\\powershell.exe']],
        [[], 'C:\\mine.exe', ['C:\\mine.exe']],
        [[], 'C:\\mine.exe', []],
        [[{ path: 'c\\pwsh.exe', label: '', version: '', source: 'store' }, { path: 'd\\pwsh.exe', label: '', version: '6.2.0', source: 'path' }], null, ['c\\pwsh.exe', 'd\\pwsh.exe']],
      ],
    ),
    comparePowerShellVersions: cases(compareVersions, [
      ['7.5.0', '7.4.9'],
      ['7.5.0', '7.5.0-preview.1'],
      ['7.5.0-preview.10', '7.5.0-preview.9'],
      ['7.5.0-rc.1', '7.5.0-preview.9'],
      ['7', '7.0.0'],
      ['x', '1'],
      ['', '0'],
    ]),
    shellLabel: cases(shellLabel, [
      ['C:\\p\\pwsh.exe', '7.5.0', 'msi'],
      ['C:\\p\\pwsh.exe', '', 'store'],
      ['C:\\p\\powershell.exe', '5.1', 'windows'],
      ['C:\\p\\powershell.exe', '', 'path'],
      ['C:\\p\\pwsh-preview.exe', '7.6.0-preview.1', 'preview'],
    ]),
    parseVersionOutput: cases((s: string) => parseVersionOutput(s) ?? null, [['7.5.1\r\n'], ['  7.6.0-preview.4  \nmore'], ['nope'], ['']]),
    ago: cases(ago, [[0], [499], [500], [59_499], [59_500], [89_999], [90_000], [3_570_000], [5_400_000], [172_799_999], [172_800_000], [-5]]),
    sizeText: cases(sizeText, [[null], [0], [999], [1000], [1499], [1500], [999_499], [999_500], [1_000_000], [2_250_000], [2_350_000], [12_345_678], [12.5]]),
    costCheck: cases(costCheck, [
      [{ cache: null, tokens: 1000, model: 'opus' }, 60_000, undefined],
      [{ cache: '1h', tokens: 60_000, model: 'opus' }, 30 * 60_000, 'sonnet'],
      [{ cache: '5m', tokens: null, model: null }, 6 * 60_000, undefined],
      [{ cache: null, tokens: null, model: null }, 4 * 60_000, 'x'],
    ]),
    previewOf: cases((t: string) => previewOf(t), [['\n\n  hello  \nworld'], ['   '], ['x'.repeat(130)], ['\u{1F600}'.repeat(70)]]),
    parseClosed: cases((t: string) => parseClosed(t) ?? null, CLOSED_TEXTS.map((t) => [t])),
    closedListing: cases((now: number) => closedListing(RECORDS, now), [[NOW]]),
    closedListingEmpty: cases((now: number) => closedListing([], now), [[NOW]]),
    parseDetection: cases((t: string | null) => parseDetection(t ?? undefined) ?? null, [
      ['{"version": 1, "detectedAt": "2026-10-08T00:00:00.000Z", "platform": "win32", "terminals": [], "shells": [{"path": "p", "label": "l", "version": "v", "source": "msi"}, {"path": "p", "source": "nope"}], "ori": {"path": "o", "version": "1", "agents": ["claude"]}, "extra": 1}'],
      ['{"version": 1, "detectedAt": "x", "terminals": [], "shells": [], "ori": {"path": "o"}}'],
      ['{"version": 2, "detectedAt": "x", "terminals": [], "shells": []}'],
      ['{"detectedAt": "x", "terminals": [], "shells": [], "version": 1}'],
      ['nope'],
      [null],
    ]),
    parseTabs: cases((t: string | null) => parseTabs(t ?? undefined), [
      ['{"tabs": [{"id": "a", "terminal": "tmux", "agent": "claude", "path": "/w", "createdAt": 1, "socket": "/s"}, {"id": "b"}, 5]}'],
      ['  '],
      ['{"tabs": {}}'],
      ['bad'],
      [null],
    ]),
  };
  return `${JSON.stringify(data, null, 2)}\n`;
}

if (process.argv[1] !== undefined && fileURLToPath(import.meta.url) === process.argv[1]) {
  const text = await ideFixturesText();
  if (process.argv.includes('--check')) {
    const current = existsSync(IDE_FIXTURES_FILE) ? readFileSync(IDE_FIXTURES_FILE, 'utf8') : '';
    if (current !== text) {
      console.error(`${IDE_FIXTURES_FILE} is stale; run node --import tsx scripts/write-ide-fixtures.ts`);
      process.exit(1);
    }
  } else {
    mkdirSync(path.dirname(IDE_FIXTURES_FILE), { recursive: true });
    writeFileSync(IDE_FIXTURES_FILE, text);
    console.log(`Wrote ${IDE_FIXTURES_FILE}`);
  }
}

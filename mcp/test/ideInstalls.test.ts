import assert from 'node:assert/strict';
import path from 'node:path';
import { test } from 'node:test';
import {
  discoverIdes,
  ideLaunchCommand,
  launchEnvironment,
  launcherPath,
  parseProductInfo,
  type DiscoveryContext,
  type DiscoveryFs,
  type IdeInstall,
} from '../src/ideInstalls.js';

function fakeFs(platform: NodeJS.Platform, files: Record<string, string>): DiscoveryFs {
  const api = platform === 'win32' ? path.win32 : path.posix;
  const dirs = new Set<string>();
  for (const file of Object.keys(files)) {
    for (let dir = api.dirname(file); dir !== api.dirname(dir); dir = api.dirname(dir)) dirs.add(dir);
  }
  return {
    exists: (p) => p in files || dirs.has(p),
    readdir: (dir) => {
      const names = new Set<string>();
      for (const p of [...Object.keys(files), ...dirs]) if (api.dirname(p) === dir) names.add(api.basename(p));
      return [...names];
    },
    readText: (p) => files[p],
  };
}

const info = (name: string, version: string, launch: object[]) => JSON.stringify({ name, version, buildNumber: `262.${version.length}`, productCode: 'XX', launch });
const ctx = (platform: NodeJS.Platform, files: Record<string, string>, env: NodeJS.ProcessEnv = {}, userHome = platform === 'win32' ? 'C:\\Users\\a' : '/home/a'): DiscoveryContext => ({
  platform,
  env: { PATH: '', ...env },
  userHome,
  arch: 'x64',
  fs: fakeFs(platform, files),
});
const summary = (installs: IdeInstall[]) => installs.map((i) => `${i.key}|${i.kind}|${i.version ?? ''}|${i.launcher}`);

test('Windows: JetBrains folders, Android Studio, Toolbox 2.x Programs and Toolbox 1.x builds three levels down, plus editor CLIs', () => {
  const winLaunch = [{ os: 'Windows', arch: 'amd64', launcherPath: 'bin/idea64.exe' }];
  const files = {
    'C:\\Program Files\\JetBrains\\IntelliJ IDEA 2026.2\\product-info.json': info('IntelliJ IDEA', '2026.2', winLaunch),
    'C:\\Program Files\\JetBrains\\IntelliJ IDEA 2026.2\\bin\\idea64.exe': '',
    'C:\\Program Files\\Android\\Android Studio\\product-info.json': info('Android Studio', '2026.1.1', [{ os: 'Windows', launcherPath: 'bin/studio64.exe' }]),
    'C:\\Program Files\\Android\\Android Studio\\bin\\studio64.exe': '',
    'C:\\Users\\a\\AppData\\Local\\Programs\\PyCharm\\product-info.json': info('PyCharm', '2026.2.1', [{ os: 'Windows', launcherPath: 'bin/pycharm64.exe' }]),
    'C:\\Users\\a\\AppData\\Local\\Programs\\PyCharm\\bin\\pycharm64.exe': '',
    'C:\\Users\\a\\AppData\\Local\\Programs\\Microsoft VS Code\\bin\\code.cmd': '',
    'C:\\Users\\a\\AppData\\Local\\JetBrains\\Toolbox\\apps\\Rider\\ch-0\\262.1\\product-info.json': info('Rider', '2026.2', [{ os: 'Windows', launcherPath: 'bin/rider64.exe' }]),
    'C:\\Users\\a\\AppData\\Local\\JetBrains\\Toolbox\\apps\\Rider\\ch-0\\262.1\\bin\\rider64.exe': '',
    'C:\\Users\\a\\AppData\\Local\\JetBrains\\Toolbox\\apps\\Gateway\\ch-0\\262.1\\product-info.json': info('JetBrains Gateway', '2026.2', [{ os: 'Windows', launcherPath: 'bin/gateway64.exe' }]),
    'C:\\Program Files\\JetBrains\\Broken\\product-info.json': info('GoLand', '2026.2', [{ os: 'Windows', launcherPath: '../../../evil.exe' }]),
    'C:\\Program Files\\JetBrains\\NoLauncher\\product-info.json': info('WebStorm', '2026.2', [{ os: 'Windows', launcherPath: 'bin/webstorm64.exe' }]),
  };
  const found = discoverIdes(ctx('win32', files, { ProgramFiles: 'C:\\Program Files', LOCALAPPDATA: 'C:\\Users\\a\\AppData\\Local' }));
  assert.deepEqual(summary(found).sort(), [
    'android-studio|jetbrains|2026.1.1|C:\\Program Files\\Android\\Android Studio\\bin\\studio64.exe',
    'idea|jetbrains|2026.2|C:\\Program Files\\JetBrains\\IntelliJ IDEA 2026.2\\bin\\idea64.exe',
    'pycharm|jetbrains|2026.2.1|C:\\Users\\a\\AppData\\Local\\Programs\\PyCharm\\bin\\pycharm64.exe',
    'rider|jetbrains|2026.2|C:\\Users\\a\\AppData\\Local\\JetBrains\\Toolbox\\apps\\Rider\\ch-0\\262.1\\bin\\rider64.exe',
    'vscode|vscode||C:\\Users\\a\\AppData\\Local\\Programs\\Microsoft VS Code\\bin\\code.cmd',
  ]);
  assert.equal(found.find((i) => i.key === 'android-studio')?.product, 'Android Studio');
});

test('macOS: .app bundles in /Applications and ~/Applications, and Toolbox 1.x bundles four levels down', () => {
  const files = {
    '/Applications/Android Studio.app/Contents/Resources/product-info.json': info('Android Studio', '2026.1', []),
    '/Users/a/Applications/GoLand.app/Contents/Resources/product-info.json': info('GoLand', '2026.2', []),
    '/Users/a/Library/Application Support/JetBrains/Toolbox/apps/IDEA-U/ch-0/262.1/IntelliJ IDEA.app/Contents/Resources/product-info.json': info('IntelliJ IDEA', '2026.2', []),
    '/Applications/Cursor.app/Contents/Resources/app/bin/cursor': '',
    '/Applications/Notes.app/Contents/Info.plist': '',
  };
  const found = discoverIdes(ctx('darwin', files, {}, '/Users/a'));
  assert.deepEqual(summary(found).sort(), [
    'android-studio|jetbrains|2026.1|/Applications/Android Studio.app',
    'cursor|vscode||/Applications/Cursor.app/Contents/Resources/app/bin/cursor',
    'goland|jetbrains|2026.2|/Users/a/Applications/GoLand.app',
    'idea|jetbrains|2026.2|/Users/a/Library/Application Support/JetBrains/Toolbox/apps/IDEA-U/ch-0/262.1/IntelliJ IDEA.app',
  ]);
});

test('Linux: Toolbox apps, /opt and snaps, newest build first, and the launcher for this OS', () => {
  const linux = (bin: string) => [{ os: 'Windows', launcherPath: 'bin/x64.exe' }, { os: 'Linux', arch: 'aarch64', launcherPath: `bin/${bin}-arm` }, { os: 'Linux', arch: 'amd64', launcherPath: `bin/${bin}` }];
  const files = {
    '/home/a/.local/share/JetBrains/Toolbox/apps/webstorm/product-info.json': info('WebStorm', '2026.2', linux('webstorm')),
    '/home/a/.local/share/JetBrains/Toolbox/apps/webstorm/bin/webstorm': '',
    '/home/a/.local/share/JetBrains/Toolbox/apps/WebStorm/ch-0/261.1/product-info.json': info('WebStorm', '2026.1', linux('webstorm')),
    '/home/a/.local/share/JetBrains/Toolbox/apps/WebStorm/ch-0/261.1/bin/webstorm': '',
    '/opt/android-studio/product-info.json': info('Android Studio', '2026.1', [{ os: 'Linux', launcherPath: 'bin/studio.sh' }]),
    '/opt/android-studio/bin/studio.sh': '',
    '/snap/clion/current/product-info.json': info('CLion', '2026.2', [{ os: 'Linux', launcherPath: 'bin/clion.sh' }]),
    '/snap/clion/current/bin/clion.sh': '',
    '/usr/share/code/bin/code': '',
  };
  const found = discoverIdes(ctx('linux', files));
  assert.deepEqual(summary(found), [
    'vscode|vscode||/usr/share/code/bin/code',
    'webstorm|jetbrains|2026.2|/home/a/.local/share/JetBrains/Toolbox/apps/webstorm/bin/webstorm',
    'clion|jetbrains|2026.2|/snap/clion/current/bin/clion.sh',
    'webstorm|jetbrains|2026.1|/home/a/.local/share/JetBrains/Toolbox/apps/WebStorm/ch-0/261.1/bin/webstorm',
    'android-studio|jetbrains|2026.1|/opt/android-studio/bin/studio.sh',
  ]);
  const studio = (v: string) => JSON.stringify({ name: 'Android Studio', buildNumber: v, launch: [{ os: 'Linux', launcherPath: 'bin/studio.sh' }] });
  const builds = discoverIdes(ctx('linux', { '/opt/as-old/product-info.json': studio('AI-251.26094.121'), '/opt/as-old/bin/studio.sh': '', '/opt/as-new/product-info.json': studio('AI-262.10968.63'), '/opt/as-new/bin/studio.sh': '' }));
  assert.deepEqual(builds.map((i) => i.version), ['AI-262.10968.63', 'AI-251.26094.121'], 'a build number sorts without its product prefix');
});

test('product-info.json parsing skips junk and picks the launcher for the OS and architecture', () => {
  assert.equal(parseProductInfo('not json'), undefined);
  assert.equal(parseProductInfo('[]'), undefined);
  const parsed = parseProductInfo(JSON.stringify({ name: 'PyCharm', launch: [{ os: 'Linux', launcherPath: 'bin/pycharm.sh' }, { os: 'macOS' }, 'x', { os: 'Windows', arch: 'aarch64', launcherPath: 'bin/arm.exe' }, { os: 'Windows', arch: 'amd64', launcherPath: 'bin/pycharm64.exe' }] }))!;
  assert.equal(parsed.launch.length, 3);
  assert.equal(launcherPath(parsed, 'win32', 'x64'), 'bin/pycharm64.exe');
  assert.equal(launcherPath(parsed, 'win32', 'arm64'), 'bin/arm.exe');
  assert.equal(launcherPath(parsed, 'linux', 'arm64'), 'bin/pycharm.sh');
  assert.equal(launcherPath(parsed, 'darwin', 'arm64'), undefined);
});

test('the launch environment drops the calling agent session and color overrides but keeps the user environment and the Agent Tabs home', () => {
  const env = {
    PATH: '/usr/bin',
    HOME: '/home/a',
    JAVA_HOME: '/jdk',
    NO_COLOR: '1',
    FORCE_COLOR: '1',
    CLAUDECODE: '1',
    CLAUDE_CODE_ENTRYPOINT: 'cli',
    CLAUDE_PLUGIN_ROOT: '/p',
    ANTHROPIC_API_KEY: 'k',
    CODEX_HOME: '/c',
    GEMINI_CLI: '1',
    GEMINI_CLI_IDE_SERVER_PORT: '1',
    COPILOT_AGENT: '1',
    IDE_AGENT_TABS_ID: 'tab-1',
    IDE_AGENT_TABS_AGENT: 'claude',
    IDE_AGENT_TABS_HOME: '/tabs',
    JEDITERM_SOURCE: 'x',
    JEDITERM_SOURCE_ARGS: 'y',
    TERMINAL_EMULATOR: 'JetBrains-JediTerm',
    TERM_PROGRAM: 'vscode',
    TERM_PROGRAM_VERSION: '1',
    VSCODE_IPC_HOOK_CLI: '/tmp/x.sock',
    VSCODE_GIT_ASKPASS_MAIN: 'x',
    ELECTRON_RUN_AS_NODE: '1',
    MCP_TIMEOUT: '1',
    OPENCODE_SESSION_ID: 's',
    Path: 'C:\\Windows',
    vscode_pid: '7',
  };
  assert.deepEqual(launchEnvironment(env), { PATH: '/usr/bin', HOME: '/home/a', JAVA_HOME: '/jdk', IDE_AGENT_TABS_HOME: '/tabs', Path: 'C:\\Windows' });
});

test('launch commands pass the folder as one argument, never through a shell, and only to a discovered launcher', () => {
  const folder = 'C:\\work\\my app';
  const code: IdeInstall = { key: 'vscode', product: 'VS Code', kind: 'vscode', launcher: 'C:\\VS Code\\bin\\code.cmd' };
  assert.deepEqual(ideLaunchCommand(code, folder, 'win32', 'C:\\Windows\\system32\\cmd.exe'), {
    command: 'C:\\Windows\\system32\\cmd.exe',
    args: ['/d', '/s', '/c', `""C:\\VS Code\\bin\\code.cmd" "${folder}""`],
    windowsVerbatimArguments: true,
    windowsHide: true,
  });
  assert.throws(() => ideLaunchCommand(code, 'C:\\work\\a&b', 'win32', undefined), /cmd.exe can't safely run/);
  const linuxCode: IdeInstall = { ...code, launcher: '/usr/bin/code' };
  assert.deepEqual(ideLaunchCommand(linuxCode, '/w/app', 'linux', undefined), { command: '/usr/bin/code', args: ['/w/app'], windowsVerbatimArguments: false, windowsHide: false });

  const idea: IdeInstall = { key: 'idea', product: 'IntelliJ IDEA', kind: 'jetbrains', launcher: 'C:\\JetBrains\\IDEA\\bin\\idea64.exe' };
  assert.deepEqual(ideLaunchCommand(idea, 'C:\\work\\a&b', 'win32', undefined), {
    command: 'C:\\JetBrains\\IDEA\\bin\\idea64.exe',
    args: ['C:\\work\\a&b'],
    windowsVerbatimArguments: false,
    windowsHide: false,
  });
  const mac: IdeInstall = { ...idea, launcher: '/Applications/IntelliJ IDEA.app' };
  assert.deepEqual(ideLaunchCommand(mac, '/Users/a/app', 'darwin', undefined).args, ['-na', '/Applications/IntelliJ IDEA.app', '--args', '/Users/a/app']);
  assert.throws(() => ideLaunchCommand(idea, '/w/app\nrm', 'linux', undefined), /control character/);
});

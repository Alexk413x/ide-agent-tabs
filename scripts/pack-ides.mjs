import { spawnSync } from 'node:child_process';
import { copyFileSync, existsSync, mkdirSync, readdirSync, readFileSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.join(path.dirname(fileURLToPath(import.meta.url)), '..');
const out = path.join(root, 'claude-plugin', 'dist', 'ide');
const windows = process.platform === 'win32';
const javaExe = windows ? 'java.exe' : 'java';

const jetbrainsVersion = readFileSync(path.join(root, 'jetbrains', 'gradle.properties'), 'utf8').match(/^pluginVersion=(.+)$/m)?.[1]?.trim();
const vscodeVersion = JSON.parse(readFileSync(path.join(root, 'vscode', 'package.json'), 'utf8')).version;
if (!jetbrainsVersion || !vscodeVersion) throw new Error('Could not read the plugin versions.');

function javaMajor(home) {
  const java = path.join(home, 'bin', javaExe);
  if (!existsSync(java)) return 0;
  const result = spawnSync(java, ['-version'], { encoding: 'utf8' });
  return Number(`${result.stderr}`.match(/version "(\d+)/)?.[1] ?? 0);
}

function jdkCandidates() {
  const candidates = [process.env.JAVA_HOME];
  const children = (dir, suffix = '') =>
    existsSync(dir) ? readdirSync(dir).map((name) => path.join(dir, name, suffix)) : [];
  if (windows) {
    const programFiles = process.env.ProgramFiles ?? 'C:\\Program Files';
    candidates.push(...children(path.join(programFiles, 'Android'), 'jbr'), ...children(path.join(programFiles, 'JetBrains'), 'jbr'));
    candidates.push(...children(path.join(programFiles, 'Java')), ...children(path.join(programFiles, 'Eclipse Adoptium')));
  } else if (process.platform === 'darwin') {
    for (const apps of ['/Applications', path.join(os.homedir(), 'Applications')]) {
      candidates.push(...children(apps, 'Contents/jbr/Contents/Home'));
    }
    candidates.push(...children('/Library/Java/JavaVirtualMachines', 'Contents/Home'));
  } else {
    candidates.push(...children('/opt', 'jbr'), ...children('/usr/lib/jvm'));
  }
  candidates.push(...children(path.join(os.homedir(), '.jdks')));
  return candidates.filter(Boolean);
}

function findJdk() {
  const jdk = jdkCandidates().find((home) => javaMajor(home) >= 25);
  if (!jdk) throw new Error('JDK 25 or later not found. Set JAVA_HOME to one, such as the JBR bundled with Android Studio 2026.2.');
  return jdk;
}

function runStep(command, args, options) {
  console.log(`> ${[command, ...args].join(' ')}`);
  // Node refuses to spawn .bat and .cmd files without cmd.exe. Every argument here is a fixed string or a path.
  const result = windows
    ? spawnSync(process.env.ComSpec ?? 'cmd.exe', ['/d', '/s', '/c', `"${[command, ...args].map((a) => (/\s/.test(a) ? `"${a}"` : a)).join(' ')}"`], {
        stdio: 'inherit',
        windowsVerbatimArguments: true,
        ...options,
      })
    : spawnSync(command, args, { stdio: 'inherit', ...options });
  if (result.status !== 0) throw new Error(`${command} failed with exit code ${result.status}`);
}

const jdk = findJdk();
console.log(`Using JDK ${jdk}`);
const gradlew = path.join(root, 'jetbrains', windows ? 'gradlew.bat' : 'gradlew');
const gradle = (task, ...more) =>
  runStep(gradlew, [task, ...more, '--no-build-cache', '--console=plain'], {
    cwd: path.join(root, 'jetbrains'),
    env: { ...process.env, JAVA_HOME: jdk },
  });
const npm = windows ? 'npm.cmd' : 'npm';
if (process.argv.includes('--test')) {
  gradle('test', '--rerun-tasks');
  runStep(npm, ['test'], { cwd: path.join(root, 'vscode') });
  console.log('IDE tests passed.');
  process.exit(0);
}
gradle('buildPlugin');
runStep(npm, ['run', 'package'], { cwd: path.join(root, 'vscode') });

const zip = path.join(root, 'jetbrains', 'build', 'distributions', `ide-agent-tabs-${jetbrainsVersion}.zip`);
const vsix = path.join(root, 'vscode', `ide-agent-tabs-${vscodeVersion}.vsix`);
mkdirSync(out, { recursive: true });
copyFileSync(zip, path.join(out, 'ide-agent-tabs-jetbrains.zip'));
copyFileSync(vsix, path.join(out, 'ide-agent-tabs.vsix'));
writeFileSync(path.join(out, 'versions.json'), `${JSON.stringify({ vscode: vscodeVersion, jetbrains: jetbrainsVersion }, null, 2)}\n`);
console.log(`Packed VS Code ${vscodeVersion} and JetBrains ${jetbrainsVersion} into ${path.relative(root, out)}`);

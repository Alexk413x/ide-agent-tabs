import assert from 'node:assert/strict';
import { chmodSync, existsSync, mkdirSync, readdirSync, readFileSync, utimesSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import { tempDir } from './tempDir.js';
import {
  cliInvocation,
  compareVersions,
  EDITOR_CLIS,
  editorCliLocations,
  fileUrl,
  findEditorClis,
  hookMessage,
  JETBRAINS_ZIP_NAME,
  MAX_ATTEMPTS,
  needsSync,
  nextSyncState,
  parseExtensionList,
  parseSyncState,
  publishJetbrains,
  readBundle,
  repositoryVersion,
  resolveEditorCli,
  syncHook,
  tryLock,
  updatePluginsXml,
  VSIX_NAME,
} from '../src/sync.js';

const bundled = { vscode: '0.1.17', jetbrains: '0.4.1' };

function makeBundle(versions = bundled): string {
  const dir = tempDir('iat-bundle-');
  writeFileSync(path.join(dir, 'versions.json'), JSON.stringify(versions));
  writeFileSync(path.join(dir, VSIX_NAME), 'vsix');
  writeFileSync(path.join(dir, JETBRAINS_ZIP_NAME), `zip ${versions.jetbrains}`);
  return dir;
}

test('compares dotted versions numerically', () => {
  assert.equal(compareVersions('0.1.9', '0.1.17'), -1);
  assert.equal(compareVersions('0.1.17', '0.1.17'), 0);
  assert.equal(compareVersions('0.2', '0.1.17'), 1);
  assert.equal(compareVersions('1.0', '1.0.0'), 0);
});

test('parses --list-extensions --show-versions output', () => {
  const out = 'Extensions installed on WSL:\r\nAlexk413x.ide-agent-tabs@0.1.16\r\nms-python.python@2026.1.0\n\nnot an extension line\n';
  const list = parseExtensionList(out);
  assert.equal(list.get('alexk413x.ide-agent-tabs'), '0.1.16');
  assert.equal(list.get('ms-python.python'), '2026.1.0');
  assert.equal(list.size, 2);
});

test('builds file URLs with forward slashes and escapes', () => {
  assert.equal(fileUrl('C:\\Users\\A B\\.ide-agent-tabs\\repository\\updatePlugins.xml', 'win32'), 'file:///C:/Users/A%20B/.ide-agent-tabs/repository/updatePlugins.xml');
  assert.equal(fileUrl('/home/a/#x?/r.zip', 'linux'), 'file:///home/a/%23x%3F/r.zip');
});

test('writes updatePlugins.xml in the Gradle build format and reads its version back', () => {
  const xml = updatePluginsXml('0.4.1', 'file:///C:/r/ide-agent-tabs-0.4.1.zip');
  assert.equal(
    xml,
    `<plugins>
  <plugin id="dev.alexk.ide-agent-tabs" url="file:///C:/r/ide-agent-tabs-0.4.1.zip" version="0.4.1">
    <idea-version since-build="262.10315"/>
    <name>Agent Tabs</name>
    <vendor>Alexk413x</vendor>
    <description>Opens AI coding-agent sessions in editor tabs.</description>
  </plugin>
</plugins>
`,
  );
  assert.equal(repositoryVersion(xml), '0.4.1');
  assert.match(updatePluginsXml('1', 'file:///a&b'), /url="file:\/\/\/a&amp;b"/);
  assert.equal(repositoryVersion('<plugins><plugin id="other" version="9"/></plugins>'), undefined);
});

test('lists editor CLI install locations per platform', () => {
  const win = editorCliLocations('win32', { LOCALAPPDATA: 'C:\\Users\\a\\AppData\\Local', ProgramFiles: 'C:\\Program Files' }, 'C:\\Users\\a');
  assert.deepEqual(win.code, [
    'C:\\Users\\a\\AppData\\Local\\Programs\\Microsoft VS Code\\bin\\code.cmd',
    'C:\\Program Files\\Microsoft VS Code\\bin\\code.cmd',
  ]);
  assert.deepEqual(win.cursor, ['C:\\Users\\a\\AppData\\Local\\Programs\\cursor\\resources\\app\\bin\\cursor.cmd']);
  assert.deepEqual(win['antigravity-ide'], ['C:\\Users\\a\\AppData\\Local\\Programs\\Antigravity IDE\\bin\\antigravity-ide.cmd']);
  const mac = editorCliLocations('darwin', {}, '/Users/a');
  assert.ok(mac.code!.includes('/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code'));
  assert.ok(mac.codium!.includes('/Users/a/Applications/VSCodium.app/Contents/Resources/app/bin/codium'));
  const linux = editorCliLocations('linux', {}, '/home/a');
  assert.deepEqual(Object.keys(linux), EDITOR_CLIS);
  assert.ok(Object.values(linux).every((l) => l.length === 0));
});

test('finds editor CLIs on PATH first, then in install locations', () => {
  const bin = tempDir('iat-clis-');
  const windows = process.platform === 'win32';
  writeFileSync(path.join(bin, windows ? 'code.cmd' : 'code'), '');
  if (windows) writeFileSync(path.join(bin, 'cursor'), '');
  const fallback = 'C:\\Users\\a\\AppData\\Local\\Programs\\Antigravity IDE\\bin\\antigravity-ide.cmd';
  const found = findEditorClis(
    { platform: process.platform, env: { PATH: bin, LOCALAPPDATA: 'C:\\Users\\a\\AppData\\Local' }, userHome: bin },
    (p) => p === fallback,
  );
  const expected = [{ cli: 'code', path: path.join(bin, windows ? 'code.cmd' : 'code') }];
  if (windows) expected.push({ cli: 'antigravity-ide', path: fallback });
  assert.deepEqual(found, expected);
});

test('resolves an editor CLI named by the user', () => {
  const ctx = { platform: 'win32' as const, env: { PATH: '' }, userHome: 'C:\\Users\\a' };
  const exists = (p: string) => p === 'D:\\VS Code\\bin\\code.cmd';
  assert.deepEqual(resolveEditorCli('D:\\VS Code\\bin\\code', ctx, exists), { cli: 'code', path: 'D:\\VS Code\\bin\\code.cmd' });
  assert.equal(resolveEditorCli('..\\code', ctx, exists), undefined);
  assert.equal(resolveEditorCli('code', ctx, exists), undefined);
});

test('runs .cmd CLIs through cmd.exe and refuses cmd.exe metacharacters', () => {
  const inv = cliInvocation('C:\\VS Code\\bin\\code.cmd', ['--install-extension', 'C:\\p\\x.vsix', '--force'], 'win32', 'C:\\Windows\\system32\\cmd.exe');
  assert.deepEqual(inv, {
    command: 'C:\\Windows\\system32\\cmd.exe',
    args: ['/d', '/s', '/c', '""C:\\VS Code\\bin\\code.cmd" "--install-extension" "C:\\p\\x.vsix" "--force""'],
    windowsVerbatimArguments: true,
  });
  for (const bad of ['C:\\a&b\\x.vsix', 'C:\\100%\\x.vsix', 'C:\\a"b', 'C:\\a^b', 'C:\\a|b']) {
    assert.throws(() => cliInvocation('C:\\code.cmd', ['--install-extension', bad], 'win32', undefined));
  }
  assert.deepEqual(cliInvocation('/usr/bin/code', ['--list-extensions'], 'linux', undefined), {
    command: '/usr/bin/code',
    args: ['--list-extensions'],
    windowsVerbatimArguments: false,
  });
  assert.equal(cliInvocation('C:\\code.exe', ['a&b'], 'win32', undefined).command, 'C:\\code.exe');
});

test('skips the sync when synced.json records the bundled versions', () => {
  const at = new Date('2026-09-28T00:00:00Z');
  assert.ok(needsSync(undefined, bundled));
  assert.ok(needsSync({ ...bundled, vscode: '0.1.16', syncedAt: '', failures: 0 }, bundled));
  assert.ok(!needsSync({ ...bundled, syncedAt: '', failures: 0 }, bundled));
  assert.ok(needsSync({ ...bundled, syncedAt: '', failures: 1 }, bundled));
  assert.ok(!needsSync({ ...bundled, syncedAt: '', failures: MAX_ATTEMPTS }, bundled));
  assert.deepEqual(nextSyncState(undefined, bundled, false, at), { ...bundled, syncedAt: at.toISOString(), failures: 0 });
  assert.equal(nextSyncState({ ...bundled, syncedAt: '', failures: 2 }, bundled, true, at).failures, 3);
  assert.equal(nextSyncState({ ...bundled, vscode: '0.1.1', syncedAt: '', failures: 2 }, bundled, true, at).failures, 1);
  assert.deepEqual(parseSyncState(JSON.stringify({ ...bundled })), { ...bundled, syncedAt: '', failures: 0 });
  assert.equal(parseSyncState('{'), undefined);
  assert.equal(parseSyncState(undefined), undefined);
});

test('describes what the hook updated in one line', () => {
  assert.equal(hookMessage(bundled, [], false), undefined);
  assert.equal(
    hookMessage(bundled, ['code', 'cursor'], true),
    "Agent Tabs: updated the VS Code extension to 0.1.17 in code, cursor (reload their windows); the JetBrains plugin 0.4.1 is ready in each JetBrains IDE's plugin updates.",
  );
});

test('a fresh lock blocks a second sync and a stale one is taken over', async () => {
  const lock = path.join(tempDir('iat-lock-'), 'sync.lock');
  assert.ok(await tryLock(lock));
  assert.ok(!(await tryLock(lock)));
  const old = new Date(Date.now() - 10 * 60_000);
  utimesSync(lock, old, old);
  assert.ok(await tryLock(lock));
});

test('rejects a bundle without valid versions', async () => {
  const dir = tempDir('iat-badbundle-');
  writeFileSync(path.join(dir, 'versions.json'), JSON.stringify({ vscode: '0.1.17', jetbrains: '1" x="' }));
  await assert.rejects(readBundle(dir));
});

test('publishes the JetBrains zip to the local repository without downgrading it', async () => {
  const bundle = await readBundle(makeBundle());
  const repo = path.join(tempDir('iat-repo-'), 'repository');
  mkdirSync(repo);
  writeFileSync(path.join(repo, 'ide-agent-tabs-0.4.0.zip'), 'old');
  writeFileSync(path.join(repo, 'updatePlugins.xml'), updatePluginsXml('0.4.0', 'file:///old.zip'));
  const first = await publishJetbrains(bundle, repo, process.platform);
  assert.equal(first.changed, true);
  assert.deepEqual(readdirSync(repo).sort(), ['ide-agent-tabs-0.4.1.zip', 'updatePlugins.xml']);
  assert.equal(readFileSync(path.join(repo, 'updatePlugins.xml'), 'utf8'), updatePluginsXml('0.4.1', fileUrl(path.join(repo, 'ide-agent-tabs-0.4.1.zip'), process.platform)));
  assert.equal((await publishJetbrains(bundle, repo, process.platform)).changed, false);

  writeFileSync(path.join(repo, 'updatePlugins.xml'), updatePluginsXml('0.5.0', 'file:///dev.zip'));
  const newer = await publishJetbrains(bundle, repo, process.platform);
  assert.equal(newer.changed, false);
  assert.equal(newer.version, '0.5.0');
  assert.equal(repositoryVersion(readFileSync(path.join(repo, 'updatePlugins.xml'), 'utf8')), '0.5.0');
});

function fakeClis(bin: string, installed: Record<string, string>): void {
  for (const cli of EDITOR_CLIS) {
    const line = installed[cli] ? `alexk413x.ide-agent-tabs@${installed[cli]}` : 'ms-python.python@1.0.0';
    if (process.platform === 'win32') {
      writeFileSync(
        path.join(bin, `${cli}.cmd`),
        `@echo off\r\necho ${cli} %*>> "%~dp0calls.txt"\r\nif "%~1"=="--list-extensions" echo ${line}\r\nexit /b 0\r\n`,
      );
    } else {
      const file = path.join(bin, cli);
      writeFileSync(file, `#!/bin/sh\necho "${cli} $*" >> "$(dirname "$0")/calls.txt"\n[ "$1" = "--list-extensions" ] && echo "${line}"\nexit 0\n`);
      chmodSync(file, 0o755);
    }
  }
}

test('the hook updates only editors with an older extension, then stays quiet', async () => {
  const bundleDir = makeBundle();
  const home = path.join(tempDir('iat-home-'), '.ide-agent-tabs');
  const bin = tempDir('iat-bin-');
  fakeClis(bin, { code: '0.1.16', cursor: '0.1.2', windsurf: '0.1.17' });
  mkdirSync(path.join(home, 'repository'), { recursive: true });
  const env = { PATH: bin, ComSpec: process.env.ComSpec, SystemRoot: process.env.SystemRoot };
  const ctx = { bundleDir, home, platform: process.platform, env, userHome: bin };

  const message = await syncHook(ctx);
  assert.equal(
    message,
    "Agent Tabs: updated the VS Code extension to 0.1.17 in code, cursor (reload their windows); the JetBrains plugin 0.4.1 is ready in each JetBrains IDE's plugin updates.",
  );
  const calls = readFileSync(path.join(bin, 'calls.txt'), 'utf8').split(/\r?\n/).map((l) => l.replace(/"/g, '').trim()).filter(Boolean);
  const installs = calls.filter((c) => c.includes('--install-extension')).sort();
  const vsix = path.join(bundleDir, VSIX_NAME);
  assert.deepEqual(installs, [`code --install-extension ${vsix} --force`, `cursor --install-extension ${vsix} --force`]);
  assert.equal(calls.filter((c) => c.includes('--list-extensions')).length, EDITOR_CLIS.length);
  assert.ok(existsSync(path.join(home, 'repository', 'ide-agent-tabs-0.4.1.zip')));
  assert.deepEqual(parseSyncState(readFileSync(path.join(home, 'synced.json'), 'utf8'))?.failures, 0);
  assert.ok(!existsSync(path.join(home, 'sync.lock')));

  writeFileSync(path.join(bin, 'calls.txt'), '');
  assert.equal(await syncHook(ctx), undefined);
  assert.equal(readFileSync(path.join(bin, 'calls.txt'), 'utf8'), '');
});

import { readFileSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const USAGE = 'Usage: node scripts/bump.mjs <version> [--vscode <version>] [--jetbrains <version>]';
const SEMVER = /^\d+\.\d+\.\d+$/;
const defaultRoot = path.join(path.dirname(fileURLToPath(import.meta.url)), '..');

const fail = (message) => {
  console.error(message);
  process.exit(1);
};

const args = process.argv.slice(2);
const option = (name) => {
  const i = args.indexOf(name);
  if (i === -1) return undefined;
  const [, value] = args.splice(i, 2);
  if (value === undefined) fail(`${name} needs a value.\n${USAGE}`);
  return value;
};
const root = option('--root') ?? defaultRoot;
const vscode = option('--vscode');
const jetbrains = option('--jetbrains');
const [version, ...extra] = args;
if (!version || extra.length) fail(USAGE);
for (const [name, value] of [['version', version], ['--vscode', vscode], ['--jetbrains', jetbrains]]) {
  if (value !== undefined && !SEMVER.test(value)) fail(`${name} ${value} is not a MAJOR.MINOR.PATCH version.`);
}

const file = (name) => path.join(root, name);
const readText = (name) => readFileSync(file(name), 'utf8');
const changed = [];

function setJsonVersion(name, next, lock = false) {
  const json = JSON.parse(readText(name));
  json.version = next;
  if (lock) json.packages[''].version = next;
  writeFileSync(file(name), `${JSON.stringify(json, null, 2)}\n`);
  changed.push(name);
}

setJsonVersion('claude-plugin/.claude-plugin/plugin.json', version);
if (vscode !== undefined) {
  setJsonVersion('vscode/package.json', vscode);
  setJsonVersion('vscode/package-lock.json', vscode, true);
}
if (jetbrains !== undefined) {
  const properties = readText('jetbrains/gradle.properties');
  if (!/^pluginVersion=.+$/m.test(properties)) fail('jetbrains/gradle.properties has no pluginVersion line.');
  writeFileSync(file('jetbrains/gradle.properties'), properties.replace(/^pluginVersion=.+$/m, `pluginVersion=${jetbrains}`));
  changed.push('jetbrains/gradle.properties');
}

const changelog = readText('CHANGELOG.md');
if (changelog.includes(`## ${version}\n`)) {
  console.log(`CHANGELOG.md already has a "## ${version}" entry.`);
} else {
  const first = changelog.search(/^## /m);
  const stub = `## ${version}\n\nPlugin and MCP server ${version}.\n\n`;
  writeFileSync(file('CHANGELOG.md'), first === -1 ? `${changelog.trimEnd()}\n\n${stub.trimEnd()}\n` : `${changelog.slice(0, first)}${stub}${changelog.slice(first)}`);
  changed.push('CHANGELOG.md');
}

console.log(`Set the plugin version to ${version}${vscode ? `, VS Code to ${vscode}` : ''}${jetbrains ? `, JetBrains to ${jetbrains}` : ''}.`);
for (const name of changed) console.log(`  ${name}`);
const steps = [
  `Fill in the CHANGELOG.md entry for ${version}.`,
  ...(vscode || jetbrains ? ['Build the IDE packages: node scripts/pack-ides.mjs'] : []),
  'Check: node scripts/check.mjs',
  'Commit the changes.',
  `Tag: git tag ide-agent-tabs--v${version}`,
];
console.log(`\nNext steps:\n${steps.map((step, i) => `  ${i + 1}. ${step}`).join('\n')}`);

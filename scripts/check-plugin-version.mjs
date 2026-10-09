import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.join(path.dirname(fileURLToPath(import.meta.url)), '..');
const git = (...args) => execFileSync('git', args, { cwd: root, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }).trim();
const read = (file) => JSON.parse(readFileSync(path.join(root, file), 'utf8')).version;

const plugin = read('claude-plugin/.claude-plugin/plugin.json');

let tag;
try {
  tag = git('describe', '--tags', '--abbrev=0', '--match', 'ide-agent-tabs--v*');
} catch {
  console.log('No ide-agent-tabs--v* tag yet; nothing to compare.');
  process.exit(0);
}
const released = tag.replace(/^ide-agent-tabs--v/, '');
const changed = git('diff', '--name-only', tag, '--', 'claude-plugin').split('\n').filter(Boolean);
if (changed.length && plugin === released) {
  console.error(`claude-plugin/ changed since ${tag}, but its version is still ${plugin}. Bump the version with`);
  console.error('node scripts/bump.mjs and add a CHANGELOG entry. Changed:');
  for (const file of changed) console.error(`  ${file}`);
  process.exit(1);
}
if (changed.length && !readFileSync(path.join(root, 'CHANGELOG.md'), 'utf8').includes(`## ${plugin}`)) {
  console.error(`CHANGELOG.md has no "## ${plugin}" entry.`);
  process.exit(1);
}
console.log(changed.length ? `claude-plugin/ changed since ${tag}; version ${plugin} is new.` : `claude-plugin/ is unchanged since ${tag}.`);

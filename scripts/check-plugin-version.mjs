import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { root } from './shared.mjs';

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
if (plugin === released) {
  console.log(changed.length ? `claude-plugin/ changed since ${tag}; version ${plugin} is released, so these changes ship with the next version.` : `claude-plugin/ is unchanged since ${tag}.`);
  process.exit(0);
}
if (!readFileSync(path.join(root, 'CHANGELOG.md'), 'utf8').includes(`## ${plugin}`)) {
  console.error(`CHANGELOG.md has no "## ${plugin}" entry.`);
  process.exit(1);
}
console.log(`Version ${plugin} is new since ${tag}; merging it to main releases it.`);

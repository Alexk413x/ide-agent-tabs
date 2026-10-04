import { spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { readdirSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.join(path.dirname(fileURLToPath(import.meta.url)), '..');
const dist = path.join(root, 'claude-plugin', 'dist');
const skipTests = process.argv.includes('--skip-tests');
const shell = process.platform === 'win32';

const run = (command, args, cwd) => spawnSync(command, args, { cwd, stdio: 'inherit', shell }).status === 0;

const hashDist = (dir = dist, prefix = '') => {
  const hashes = new Map();
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const rel = `${prefix}${entry.name}`;
    if (entry.isDirectory()) {
      for (const [file, hash] of hashDist(path.join(dir, entry.name), `${rel}/`)) hashes.set(file, hash);
    } else {
      hashes.set(rel, createHash('sha256').update(readFileSync(path.join(dir, entry.name))).digest('hex'));
    }
  }
  return hashes;
};

const bundleIsCurrent = () => {
  const before = hashDist();
  if (!run('npm', ['run', 'bundle'], path.join(root, 'mcp'))) return false;
  const after = hashDist();
  const changed = [...new Set([...before.keys(), ...after.keys()])].filter((file) => before.get(file) !== after.get(file)).sort();
  if (!changed.length) return true;
  console.error('claude-plugin/dist was stale; it is now rebuilt. Review and stage it. Changed:');
  for (const file of changed) console.error(`  claude-plugin/dist/${file}`);
  return false;
};

const steps = [
  ['typecheck', () => run('npm', ['run', 'typecheck'], path.join(root, 'mcp'))],
  ...(skipTests ? [] : [['test', () => run('npm', ['test'], path.join(root, 'mcp'))]]),
  ['bundle is current', bundleIsCurrent],
  ['plugin version', () => run('node', ['scripts/check-plugin-version.mjs'], root)],
  ['validate plugin', () => run('claude', ['plugin', 'validate', '--strict', 'claude-plugin'], root)],
  ...(skipTests ? [] : [['mod tests', () => run('claude', ['plugin', 'test', 'claude-plugin'], root)]]),
  ['validate marketplace', () => run('claude', ['plugin', 'validate', '--strict', '.'], root)],
];

for (const [name, step] of steps) {
  console.log(`\n== ${name}`);
  if (!step()) {
    console.error(`FAIL ${name}`);
    process.exit(1);
  }
  console.log(`ok ${name}`);
}
console.log('\nAll checks passed.');

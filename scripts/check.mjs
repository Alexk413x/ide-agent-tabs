import { readFileSync } from 'node:fs';
import path from 'node:path';
import { shipsNoNode } from './check-no-node.mjs';
import { hashAllIdeSources } from './ide-sources.mjs';
import { root, runCommand } from './shared.mjs';

const dist = path.join(root, 'claude-plugin', 'dist');
const skipTests = process.argv.includes('--skip-tests');
const skipIde = process.argv.includes('--skip-ide');

const run = (command, args, cwd, env = process.env) => runCommand(command, args, { cwd, env }) === 0;
const RUFF = 'ruff@0.16.10';
const pythonTests = (version) => () =>
  run('uv', ['run', '--python', version, '--no-project', 'python', '-I', '-S', 'mcp/tests/run.py'], root, { ...process.env, IDE_AGENT_TABS_INTEROP: '1' });

const idePackagesAreCurrent = () => {
  const recorded = JSON.parse(readFileSync(path.join(dist, 'ide', 'versions.json'), 'utf8')).sources;
  const current = hashAllIdeSources();
  if (!recorded) {
    console.error('claude-plugin/dist/ide/versions.json has no source hashes; run node scripts/pack-ides.mjs');
    return false;
  }
  const stale = Object.keys(current).filter((ide) => recorded[ide] !== current[ide]);
  for (const ide of stale) console.error(`${ide}/ changed since the last repack; run node scripts/pack-ides.mjs`);
  return !stale.length;
};

const unlessSkipped = (skipped, name, step) => (skipped ? [] : [[name, step]]);

const steps = [
  ['ships no node', shipsNoNode],
  ['python lint', () => run('uvx', [RUFF, 'check'], root) && run('uvx', [RUFF, 'format', '--check'], root)],
  ['python types', () => run('uv', ['run', '--frozen', 'pyright'], root) && run('uv', ['run', '--frozen', 'pyright', '--pythonplatform', 'Linux'], root)],
  ...unlessSkipped(skipTests, 'python tests (3.13)', pythonTests('3.13')),
  ...unlessSkipped(skipTests, 'python tests (3.9)', pythonTests('3.9')),
  ['plugin version', () => run('node', ['scripts/check-plugin-version.mjs'], root)],
  ['shared files in sync', () => run('node', ['scripts/sync-shared.mjs', '--check'], root)],
  ['ide packages current', idePackagesAreCurrent],
  ['validate plugin', () => run('claude', ['plugin', 'validate', '--strict', 'claude-plugin'], root)],
  ...unlessSkipped(skipTests, 'mod tests', () => run('claude', ['plugin', 'test', 'claude-plugin'], root)),
  ...unlessSkipped(skipTests, 'script tests', () => run('node', ['--test', 'scripts/test/*.test.mjs'], root)),
  ...unlessSkipped(skipTests || skipIde, 'ide tests', () => run('node', ['scripts/pack-ides.mjs', '--test'], root)),
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

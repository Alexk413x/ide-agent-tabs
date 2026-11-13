import { idePackagesAreCurrent } from './check-ide-sources.mjs';
import { shipsNoNode } from './check-no-node.mjs';
import { root, runCommand } from './shared.mjs';

const skipTests = process.argv.includes('--skip-tests');
const skipIde = process.argv.includes('--skip-ide');

const run = (command, args, cwd, env = process.env) => runCommand(command, args, { cwd, env }) === 0;
const RUFF = 'ruff@0.16.10';
const pythonTests = (version) => () =>
  run('uv', ['run', '--python', version, '--no-project', 'python', '-I', '-S', 'mcp/tests/run.py'], root);

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

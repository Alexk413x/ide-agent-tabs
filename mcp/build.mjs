import { build } from 'esbuild';
import { cpSync, existsSync, mkdirSync, readdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.dirname(fileURLToPath(import.meta.url));
const dist = path.join(root, '..', 'claude-plugin', 'dist');
const helperDir = path.join(root, '..', 'claude-plugin', 'mcp', 'launch');
const { version } = JSON.parse(readFileSync(path.join(root, 'package.json'), 'utf8'));

// dist/ide holds the IDE builds from scripts/pack-ides.mjs, which this build doesn't make, so it stays.
for (const name of existsSync(dist) ? readdirSync(dist) : []) {
  if (name !== 'ide') rmSync(path.join(dist, name), { recursive: true, force: true });
}
mkdirSync(path.join(dist, 'launch'), { recursive: true });
rmSync(helperDir, { recursive: true, force: true });
mkdirSync(helperDir, { recursive: true });

const common = {
  outExtension: { '.js': '.mjs' },
  bundle: true,
  platform: 'node',
  format: 'esm',
  target: 'node22',
  minify: true,
  legalComments: 'none',
  metafile: true,
  define: { BUNDLED_VERSION: JSON.stringify(version) },
  // Bundled CommonJS dependencies call require(); an ES module has none unless one is made.
  banner: { js: "import { createRequire as __createRequire } from 'node:module'; const require = __createRequire(import.meta.url);" },
};

const result = await build({
  ...common,
  entryPoints: {
    'mcp-server': path.join(root, 'src', 'main.ts'),
    'sync-ides': path.join(root, 'src', 'syncMain.ts'),
    'agent-hook': path.join(root, 'src', 'agentHook.ts'),
    'shared-server': path.join(root, 'src', 'serverMain.ts'),
    'server-start': path.join(root, 'src', 'serverHook.ts'),
  },
  outdir: dist,
});
await build({ ...common, entryPoints: { headers: path.join(root, 'src', 'headersMain.ts') }, outdir: helperDir, metafile: false });

for (const name of readdirSync(path.join(root, 'launch'))) {
  cpSync(path.join(root, 'launch', name), path.join(dist, 'launch', name));
}

const packages = new Set();
for (const input of Object.keys(result.metafile.inputs)) {
  const match = input.replace(/\\/g, '/').match(/node_modules\/((?:@[^/]+\/)?[^/]+)\//);
  if (match) packages.add(match[1]);
}
const notices = [...packages].sort().map((name) => {
  const dir = path.join(root, 'node_modules', name);
  const pkg = JSON.parse(readFileSync(path.join(dir, 'package.json'), 'utf8'));
  const file = readdirSync(dir).find((f) => /^licen[cs]e/i.test(f));
  const text = file ? readFileSync(path.join(dir, file), 'utf8').trim() : `License: ${pkg.license ?? 'unknown'}`;
  return `${name} ${pkg.version}\n\n${text}\n`;
});
writeFileSync(
  path.join(dist, 'THIRD_PARTY_NOTICES.txt'),
  `mcp-server.mjs bundles these packages.\n\n${notices.join('\n' + '-'.repeat(72) + '\n\n')}`,
);
console.log(`Bundled ${packages.size} packages into ${path.relative(root, path.join(dist, 'mcp-server.mjs'))}`);

import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { hashAllIdeSources } from './ide-sources.mjs';
import { root } from './shared.mjs';

export function idePackagesAreCurrent() {
  const recorded = JSON.parse(readFileSync(path.join(root, 'claude-plugin', 'dist', 'ide', 'versions.json'), 'utf8')).sources;
  const current = hashAllIdeSources();
  if (!recorded) {
    console.error('claude-plugin/dist/ide/versions.json has no source hashes; run node scripts/pack-ides.mjs');
    return false;
  }
  const stale = Object.keys(current).filter((ide) => recorded[ide] !== current[ide]);
  for (const ide of stale) console.error(`${ide}/ changed since the last repack; run node scripts/pack-ides.mjs`);
  return !stale.length;
}

if (process.argv[1] !== undefined && fileURLToPath(import.meta.url) === path.resolve(process.argv[1])) {
  process.exit(idePackagesAreCurrent() ? 0 : 1);
}

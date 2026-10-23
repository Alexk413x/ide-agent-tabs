import { existsSync, mkdirSync, readdirSync, readFileSync, statSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { root } from './shared.mjs';

export const SHARED_DIRS = ['icons', 'launch'];
export const TARGETS = ['vscode/resources', 'jetbrains/src/main/resources'];

const filesUnder = (dir, rel = '') =>
  readdirSync(path.join(dir, rel)).flatMap((name) => {
    const child = path.posix.join(rel, name);
    return statSync(path.join(dir, child)).isDirectory() ? filesUnder(dir, child) : [child];
  });

export function syncShared(base = root, { write = true } = {}) {
  const drifted = [];
  for (const dir of SHARED_DIRS) {
    const source = path.join(base, 'shared', dir);
    for (const rel of filesUnder(source).sort()) {
      const bytes = readFileSync(path.join(source, rel));
      for (const target of TARGETS) {
        const copy = path.join(base, target, dir, rel);
        if (existsSync(copy) && readFileSync(copy).equals(bytes)) continue;
        drifted.push(path.posix.join(target, dir, rel));
        if (write) {
          mkdirSync(path.dirname(copy), { recursive: true });
          writeFileSync(copy, bytes);
        }
      }
    }
  }
  return drifted;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const args = process.argv.slice(2);
  const rootFlag = args.indexOf('--root');
  const base = rootFlag === -1 ? root : path.resolve(args[rootFlag + 1]);
  const check = args.includes('--check');
  const drifted = syncShared(base, { write: !check });
  if (check && drifted.length) {
    console.error(`${drifted.length} copies of shared/ files differ from the source; run node scripts/sync-shared.mjs`);
    for (const file of drifted) console.error(`  ${file}`);
    process.exit(1);
  }
  console.log(check ? 'Shared files are in sync.' : `Updated ${drifted.length} files from shared/.`);
}

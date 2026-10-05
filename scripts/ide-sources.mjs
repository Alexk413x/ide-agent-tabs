import { createHash } from 'node:crypto';
import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.join(path.dirname(fileURLToPath(import.meta.url)), '..');
const BINARY = /\.(png|jpe?g|gif|ico|woff2?|ttf|jar|zip|vsix)$/i;

const SOURCES = {
  vscode: ['src', 'package.json', 'resources'],
  jetbrains: ['src/main', 'build.gradle.kts', 'gradle.properties', 'settings.gradle.kts'],
};

const filesUnder = (base, rel) => {
  const full = path.join(base, rel);
  if (!existsSync(full)) return [];
  if (!statSync(full).isDirectory()) return [rel];
  return readdirSync(full).flatMap((name) => filesUnder(base, `${rel}/${name}`));
};

export function hashIdeSources(ide, base = root) {
  const dir = path.join(base, ide);
  const files = SOURCES[ide].flatMap((entry) => filesUnder(dir, entry)).sort();
  const hash = createHash('sha256');
  for (const file of files) {
    const bytes = readFileSync(path.join(dir, file));
    const content = BINARY.test(file) ? bytes : Buffer.from(bytes.toString('utf8').replace(/\r\n/g, '\n'));
    hash.update(`${file}\n${createHash('sha256').update(content).digest('hex')}\n`);
  }
  return hash.digest('hex');
}

export const hashAllIdeSources = (base = root) => Object.fromEntries(Object.keys(SOURCES).map((ide) => [ide, hashIdeSources(ide, base)]));

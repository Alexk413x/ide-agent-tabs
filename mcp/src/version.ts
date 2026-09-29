import { readFileSync } from 'node:fs';

declare const BUNDLED_VERSION: string | undefined;

// build.mjs defines BUNDLED_VERSION from package.json; the tests run the sources unbundled, so they read the file.
export const PACKAGE_VERSION: string =
  typeof BUNDLED_VERSION === 'string'
    ? BUNDLED_VERSION
    : (JSON.parse(readFileSync(new URL('../package.json', import.meta.url), 'utf8')) as { version: string }).version;

export function compareVersions(a: string, b: string): number {
  const parts = (v: string) => v.split('.').map((p) => Number.parseInt(p, 10) || 0);
  const pa = parts(a);
  const pb = parts(b);
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const diff = (pa[i] ?? 0) - (pb[i] ?? 0);
    if (diff !== 0) return Math.sign(diff);
  }
  return 0;
}

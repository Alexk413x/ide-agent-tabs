import { writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { buildCatalog } from '../src/shared/catalogSource.js';

export const CATALOG_FILE = fileURLToPath(new URL('../src/shared/catalog.generated.ts', import.meta.url));

export async function catalogText(): Promise<string> {
  const data = await buildCatalog();
  return [
    '// Written by scripts/write-catalog.ts from the tool registrations in server.ts; test/catalog.test.ts fails when it is stale.',
    "import type { CatalogData } from './catalogSource.js';",
    '',
    `export const CATALOG: CatalogData = ${JSON.stringify(data, null, 2)};`,
    '',
  ].join('\n');
}

if (process.argv[1] !== undefined && fileURLToPath(import.meta.url) === process.argv[1]) {
  writeFileSync(CATALOG_FILE, await catalogText());
  console.log(`Wrote ${CATALOG_FILE}`);
}

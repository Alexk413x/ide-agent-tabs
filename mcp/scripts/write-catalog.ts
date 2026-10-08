import { readFileSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { buildCatalog, buildToolCatalog } from '../src/shared/catalogSource.js';

export const CATALOG_FILE = fileURLToPath(new URL('../src/shared/catalog.generated.ts', import.meta.url));
export const CATALOG_JSON = fileURLToPath(new URL('../../claude-plugin/mcp/src/ide_agent_tabs/catalog.json', import.meta.url));

export async function catalogText(): Promise<string> {
  const data = await buildCatalog();
  return [
    '// Written by scripts/write-catalog.ts from the tool registrations in server.ts; test/sharedServer.test.ts fails when it is stale.',
    "import type { CatalogData } from './catalogSource.js';",
    '',
    `export const CATALOG: CatalogData = ${JSON.stringify(data, null, 2)};`,
    '',
  ].join('\n');
}

export async function catalogJson(): Promise<string> {
  return `${JSON.stringify(await buildToolCatalog(), null, 2)}\n`;
}

const read = (file: string) => {
  try {
    return readFileSync(file, 'utf8').replace(/\r\n/g, '\n');
  } catch {
    return undefined;
  }
};

if (process.argv[1] !== undefined && fileURLToPath(import.meta.url) === process.argv[1]) {
  const outputs: [string, string][] = [
    [CATALOG_FILE, await catalogText()],
    [CATALOG_JSON, await catalogJson()],
  ];
  if (process.argv.includes('--check')) {
    const stale = outputs.filter(([file, text]) => read(file) !== text).map(([file]) => file);
    for (const file of stale) console.error(`${file} is stale; run node --import tsx scripts/write-catalog.ts in mcp/`);
    process.exit(stale.length ? 1 : 0);
  }
  for (const [file, text] of outputs) {
    writeFileSync(file, text);
    console.log(`Wrote ${file}`);
  }
}

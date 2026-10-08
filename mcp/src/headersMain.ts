import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { helperHeaders } from './shared/headers.js';

const pluginRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const headers = await helperHeaders(process.env, pluginRoot).catch(() => ({}));
process.stdout.write(`${JSON.stringify(headers)}\n`, () => process.exit(0));

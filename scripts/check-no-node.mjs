import { readdirSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const plugin = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', 'claude-plugin');

// The release tree drops evals/ and *.test.tsx; dist/ide/ holds the IDE packages, and the .tsx mods run inside Claude Code.
const NOT_SHIPPED = /^(evals\/|dist\/ide\/)|\.test\.tsx$/;
const NODE_RUNTIME = /\.(mjs|cjs)$|(^|\/)package(-lock)?\.json$/;
const NODE_COMMANDS = [
  /(^|[\s"'`(=;&|])node(\.exe)?["']?\s+("[^"]*|'[^']*|\S*)\.[mc]?js\b/m,
  /"(command|cmd|exec)"\s*:\s*"node(\.exe)?"/,
  /Bash\(\s*node\b/,
];
const PY_NODE = /["']node(\.exe)?["'\s]/;

function shippedFiles(dir = plugin, prefix = '') {
  const files = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const rel = `${prefix}${entry.name}`;
    if (entry.isDirectory()) files.push(...shippedFiles(path.join(dir, entry.name), `${rel}/`));
    else if (!NOT_SHIPPED.test(rel)) files.push(rel);
  }
  return files;
}

function commandText(rel, text) {
  if (!rel.endsWith('.md')) return text;
  const front = /^---\n([\s\S]*?)\n---/.exec(text.replace(/\r\n/g, '\n'))?.[1] ?? '';
  const fences = [...text.replace(/\r\n/g, '\n').matchAll(/^```[^\n]*\n([\s\S]*?)^```/gm)].map((m) => m[1]);
  return [front, ...fences].join('\n');
}

export function shipsNoNode() {
  const found = [];
  for (const rel of shippedFiles()) {
    if (NODE_RUNTIME.test(rel)) {
      found.push(`${rel}: a Node file`);
      continue;
    }
    if (/\.(tsx|ts|zip|vsix|png|ico)$/.test(rel)) continue;
    const text = readFileSync(path.join(plugin, rel), 'utf8');
    if (rel.endsWith('.py')) {
      const code = text.split('\n').filter((line) => !line.trimStart().startsWith('#'));
      for (const line of code) if (PY_NODE.test(line)) found.push(`${rel}: ${line.trim()}`);
      continue;
    }
    const commands = commandText(rel, text);
    for (const pattern of NODE_COMMANDS) {
      const match = pattern.exec(commands);
      if (match) found.push(`${rel}: ${match[0].trim()}`);
    }
  }
  for (const line of found) console.error(`claude-plugin/${line}`);
  if (found.length) console.error('The plugin must run without Node.js: use the Python launchers in claude-plugin/mcp/launch/.');
  return !found.length;
}

if (process.argv[1] !== undefined && fileURLToPath(import.meta.url) === path.resolve(process.argv[1])) {
  process.exit(shipsNoNode() ? 0 : 1);
}

const escapeRe = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
const keyRe = (k: string) => `(?:${escapeRe(k)}|"${escapeRe(k)}"|'${escapeRe(k)}')`;

export function tomlHeader(section: string, name: string): RegExp {
  return new RegExp(`^\\s*\\[\\s*${keyRe(section)}\\s*\\.\\s*${keyRe(name)}\\s*(\\.[^\\]]*)?\\]\\s*(?:#.*)?$`);
}

const isHeader = (line: string) => /^\s*\[/.test(line);
const isTrailing = (line: string) => /^\s*(?:#.*)?$/.test(line);

export function tomlBlocks(lines: string[], section: string, name: string): [number, number][] {
  const header = tomlHeader(section, name);
  const blocks: [number, number][] = [];
  for (let i = 0; i < lines.length; i++) {
    if (!header.test(lines[i]!)) continue;
    let end = i + 1;
    while (end < lines.length && !isHeader(lines[end]!)) end++;
    while (end > i + 1 && isTrailing(lines[end - 1]!)) end--;
    blocks.push([i, end]);
    i = end - 1;
  }
  return blocks;
}

export const tomlString = (s: string) => JSON.stringify(s);

export function tomlTable(section: string, name: string, values: Record<string, string | number | string[] | Record<string, string>>): string[] {
  const value = (v: string | number | string[] | Record<string, string>): string =>
    typeof v === 'number'
      ? String(v)
      : typeof v === 'string'
        ? tomlString(v)
        : Array.isArray(v)
          ? `[${v.map(tomlString).join(', ')}]`
          : `{ ${Object.entries(v).map(([k, s]) => `${k} = ${tomlString(s)}`).join(', ')} }`;
  return [`[${section}.${name}]`, ...Object.entries(values).map(([k, v]) => `${k} = ${value(v)}`)];
}

// Only the [section.name] form can be found and replaced; any other spelling of the same table would be a
// duplicate key once a second one is added.
export function withTomlTable(text: string | undefined, file: string, section: string, name: string, table: string[] | undefined): string | undefined {
  const source = text ?? '';
  const eol = source.includes('\r\n') ? '\r\n' : '\n';
  const lines = source.split(/\r?\n/);
  const blocks = tomlBlocks(lines, section, name);
  const kept = lines.filter((_, i) => !blocks.some(([s, e]) => i >= s && i < e));
  const other = new RegExp(`(?:^|[\\s.\\[])${keyRe(name)}\\s*(?:[.=\\]])`);
  if (kept.some((l) => !/^\s*#/.test(l) && other.test(l))) {
    throw new Error(`${file} names ${name} in a form other than a [${section}.${name}] table; edit it by hand`);
  }
  if (blocks.length > 0) {
    const out: string[] = [];
    for (let i = 0; i < lines.length; i++) {
      const block = blocks.find(([s]) => s === i);
      if (block === undefined) {
        out.push(lines[i]!);
        continue;
      }
      if (table !== undefined && block === blocks[0]) out.push(...table);
      else if (out.length > 0 && out[out.length - 1]!.trim() === '' && (block[1] >= lines.length || lines[block[1]]!.trim() === '')) out.pop();
      i = block[1] - 1;
    }
    const next = out.join(eol);
    return next === source ? undefined : next;
  }
  if (table === undefined) return undefined;
  const body = source.replace(/(?:\r?\n)+$/, '');
  return `${body}${body === '' ? '' : `${eol}${eol}`}${table.join(eol)}${eol}`;
}

function parseString(src: string, i: number): [string, number] | undefined {
  if (src[i] === "'") {
    const close = src.indexOf("'", i + 1);
    return close < 0 ? undefined : [src.slice(i + 1, close), close + 1];
  }
  if (src[i] !== '"') return undefined;
  let j = i + 1;
  while (j < src.length && src[j] !== '"') j += src[j] === '\\' ? 2 : 1;
  try {
    return [JSON.parse(src.slice(i, j + 1)) as string, j + 1];
  } catch {
    return undefined;
  }
}

function skipSpace(src: string, i: number): number {
  while (i < src.length) {
    if (/\s|,/.test(src[i]!)) i++;
    else if (src[i] === '#') while (i < src.length && src[i] !== '\n') i++;
    else break;
  }
  return i;
}

function parseValue(src: string, i: number): string | string[] | undefined {
  i = skipSpace(src, i);
  if (src[i] !== '[') return parseString(src, i)?.[0];
  const items: string[] = [];
  i = skipSpace(src, i + 1);
  while (i < src.length && src[i] !== ']') {
    const item = parseString(src, i);
    if (item === undefined) return undefined;
    items.push(item[0]);
    i = skipSpace(src, item[1]);
  }
  return items;
}

export function readTomlTable(text: string | undefined, section: string, name: string): Record<string, string | string[] | undefined> | undefined {
  if (text === undefined) return undefined;
  const lines = text.split(/\r?\n/);
  const main = tomlBlocks(lines, section, name).find(([s]) => tomlHeader(section, name).exec(lines[s]!)?.[1] === undefined);
  if (main === undefined) return undefined;
  const body = lines.slice(main[0] + 1, main[1]).join('\n');
  const values: Record<string, string | string[] | undefined> = {};
  for (const m of body.matchAll(/^\s*([A-Za-z0-9_-]+)\s*=/gm)) values[m[1]!] = parseValue(body, m.index! + m[0].length);
  return values;
}

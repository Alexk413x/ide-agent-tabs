import { isMap, isNode, isScalar, isSeq, parseDocument, type Document, type YAMLMap } from 'yaml';

export function editYaml(text: string | undefined, file: string, change: (doc: Document) => void): string | undefined {
  const source = text ?? '';
  const doc = parseDocument(source);
  if (doc.errors.length) throw new Error(`${file} isn't valid YAML (${doc.errors[0]!.message.split('\n')[0]}); edit it by hand`);
  if (doc.contents !== null && !isMap(doc.contents)) throw new Error(`${file} doesn't hold a YAML mapping; edit it by hand`);
  const before = JSON.stringify(doc.toJS());
  change(doc);
  if (JSON.stringify(doc.toJS()) === before) return undefined;
  const eol = source.includes('\r\n') ? '\r\n' : '\n';
  return doc.toString({ lineWidth: 0 }).replace(/\r?\n/g, eol);
}

export function readYaml(text: string | undefined, file: string): Record<string, unknown> | undefined {
  if (text === undefined || text.trim() === '') return undefined;
  const doc = parseDocument(text);
  if (doc.errors.length) throw new Error(`${file} isn't valid YAML (${doc.errors[0]!.message.split('\n')[0]})`);
  const root: unknown = doc.toJS();
  return typeof root === 'object' && root !== null && !Array.isArray(root) ? (root as Record<string, unknown>) : undefined;
}

export function yamlMap(doc: Document, key: string, file: string, create: boolean): YAMLMap | undefined {
  const node = doc.get(key, true);
  if (node === undefined || node === null || (isScalar(node) && node.value === null)) {
    if (!create) return undefined;
    const map = doc.createNode({}) as YAMLMap;
    doc.set(key, map);
    return map;
  }
  if (!isMap(node)) throw new Error(`${file}: "${key}" isn't a mapping; edit it by hand`);
  return node;
}

export function setYamlEntry(doc: Document, section: string, name: string, entry: unknown, file: string): void {
  if (entry === undefined) {
    yamlMap(doc, section, file, false)?.delete(name);
    return;
  }
  yamlMap(doc, section, file, true)!.set(name, doc.createNode(entry));
}

export function filterYamlLists(doc: Document, section: string, file: string, drop: (item: unknown) => boolean): YAMLMap | undefined {
  const map = yamlMap(doc, section, file, false);
  if (map === undefined) return undefined;
  let changed = false;
  for (const pair of [...map.items]) {
    if (!isSeq(pair.value)) continue;
    const kept = pair.value.items.filter((item) => !drop(isNode(item) ? item.toJSON() : item));
    if (kept.length === pair.value.items.length) continue;
    changed = true;
    if (kept.length) pair.value.items = kept;
    else map.delete(pair.key);
  }
  if (changed && map.items.length === 0) doc.delete(section);
  return map;
}

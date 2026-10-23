import * as fs from 'node:fs';

export function isFile(target: string): boolean {
  try {
    return fs.statSync(target).isFile();
  } catch {
    return false;
  }
}

export function isDirectory(target: string): boolean {
  try {
    return fs.statSync(target).isDirectory();
  } catch {
    return false;
  }
}

export function readText(target: string): string | undefined {
  try {
    return fs.readFileSync(target, 'utf8');
  } catch {
    return undefined;
  }
}

export function readJson(target: string): unknown {
  const text = readText(target);
  if (text === undefined) return undefined;
  try {
    return JSON.parse(text);
  } catch {
    return undefined;
  }
}

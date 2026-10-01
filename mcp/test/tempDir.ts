import { mkdtempSync, realpathSync, rmSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { after } from 'node:test';

const dirs: string[] = [];

after(() => {
  for (const dir of dirs) rmSync(dir, { recursive: true, force: true });
});

export function tempDir(prefix: string): string {
  // A child process reports the real path: macOS keeps the temp folder behind a symlink, and Windows
  // may name it with a short 8.3 path, which only the native call expands.
  const dir = realpathSync.native(mkdtempSync(path.join(os.tmpdir(), prefix)));
  dirs.push(dir);
  return dir;
}

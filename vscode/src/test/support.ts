import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';

const created: string[] = [];

process.on('exit', () => {
  for (const dir of created) {
    try {
      fs.rmSync(dir, { recursive: true, force: true });
    } catch {
      // A shell started by a test can still hold the folder on Windows; the OS temp cleanup removes it.
    }
  }
});

export function tempDir(prefix: string): string {
  const dir = fs.mkdtempSync(path.join(fs.realpathSync.native(os.tmpdir()), prefix));
  created.push(dir);
  return dir;
}

export const onWindows = process.platform === 'win32';

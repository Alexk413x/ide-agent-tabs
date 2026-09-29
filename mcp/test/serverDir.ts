import { mkdirSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { tempDir } from './tempDir.js';

export function makeServerDir(server = 'server v1'): string {
  const dir = tempDir('iat-dist-');
  mkdirSync(path.join(dir, 'launch'));
  writeFileSync(path.join(dir, 'mcp-server.mjs'), server);
  writeFileSync(path.join(dir, 'agent-hook.mjs'), 'hook');
  writeFileSync(path.join(dir, 'THIRD_PARTY_NOTICES.txt'), 'notices');
  writeFileSync(path.join(dir, 'launch', 'agent-launch.sh'), 'sh');
  writeFileSync(path.join(dir, 'launch', 'agent-launch.ps1'), 'ps1');
  return dir;
}

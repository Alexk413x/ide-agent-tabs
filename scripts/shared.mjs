import { spawnSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

export const root = path.join(path.dirname(fileURLToPath(import.meta.url)), '..');
export const windows = process.platform === 'win32';
export const PLUGIN_VERSION_LINE = /^pluginVersion=(.+)$/m;

export function runCommand(command, args, options = {}) {
  // Node refuses to spawn .bat and .cmd files without cmd.exe. Every argument here is a fixed string or a path.
  const result = windows
    ? spawnSync(process.env.ComSpec ?? 'cmd.exe', ['/d', '/s', '/c', `"${[command, ...args].map((a) => (/\s/.test(a) ? `"${a}"` : a)).join(' ')}"`], {
        stdio: 'inherit',
        windowsVerbatimArguments: true,
        ...options,
      })
    : spawnSync(command, args, { stdio: 'inherit', ...options });
  return result.status;
}

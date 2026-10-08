import { execFile } from 'node:child_process';
import { promises as fs } from 'node:fs';
import path from 'node:path';

export interface ProcessInfo {
  pid: number;
  ppid: number;
  name: string;
  startMs: number;
}

export type ProcessTable = Map<number, ProcessInfo>;

const SHELL = /^(cmd|bash|sh|dash|zsh|fish|ksh|mksh|tcsh|csh|pwsh|powershell|nu|busybox|env|wsl|conhost)(\.exe)?$/i;
const MAX_DEPTH = 16;
const LOOKUP_TIMEOUT_MS = 5_000;
// A child can't start before its parent; process start times on macOS have one-second steps.
const START_SLACK_MS = 1_500;
const WINDOWS_EPOCH_OFFSET_MS = 11_644_473_600_000;

const baseName = (name: string) => path.basename(name.trim().replace(/\\/g, '/')).replace(/^-/, '');

export function isShellName(name: string): boolean {
  return SHELL.test(baseName(name));
}

// Claude Code runs a headersHelper through a shell, and a hook through a shell or directly, so the agent is
// the nearest ancestor that is not a shell. A name check would pick an outer session for a nested one that
// runs as `node cli.js`.
export function findAgent(table: ProcessTable, self: number): ProcessInfo | undefined {
  let child = table.get(self);
  for (let depth = 0; child !== undefined && depth < MAX_DEPTH; depth++) {
    const parent = table.get(child.ppid);
    if (parent === undefined || parent.pid === child.pid || parent.startMs > child.startMs + START_SLACK_MS) return undefined;
    if (!isShellName(parent.name)) return parent;
    child = parent;
  }
  return undefined;
}

function exec(file: string, args: string[], timeoutMs: number): Promise<string> {
  return new Promise((resolve, reject) => {
    execFile(file, args, { encoding: 'utf8', windowsHide: true, timeout: timeoutMs, maxBuffer: 32 << 20 }, (error, stdout) =>
      error ? reject(error) : resolve(stdout),
    );
  });
}

const WINDOWS_QUERY =
  'Get-CimInstance Win32_Process -Property ProcessId,ParentProcessId,Name,CreationDate | ForEach-Object { ' +
  '"{0}`t{1}`t{2}`t{3}" -f $_.ProcessId,$_.ParentProcessId,$(if ($_.CreationDate) { $_.CreationDate.ToFileTimeUtc() } else { 0 }),$_.Name }';

export function parseWindowsTable(text: string): ProcessTable {
  const table: ProcessTable = new Map();
  for (const line of text.split(/\r?\n/)) {
    const [pid, ppid, fileTime, ...name] = line.split('\t');
    const info = { pid: Number(pid), ppid: Number(ppid), startMs: Number(fileTime) / 10_000 - WINDOWS_EPOCH_OFFSET_MS, name: name.join('\t') };
    if (Number.isSafeInteger(info.pid) && info.pid > 0 && Number.isSafeInteger(info.ppid) && Number(fileTime) > 0) table.set(info.pid, info);
  }
  return table;
}

export function parsePsTable(text: string): ProcessTable {
  const table: ProcessTable = new Map();
  for (const line of text.split('\n')) {
    const parts = line.trim().split(/\s+/);
    if (parts.length < 8) continue;
    const [pid, ppid, ...rest] = parts;
    const startMs = Date.parse(rest.slice(0, 5).join(' '));
    const info = { pid: Number(pid), ppid: Number(ppid), startMs, name: rest.slice(5).join(' ') };
    if (Number.isSafeInteger(info.pid) && info.pid > 0 && Number.isFinite(startMs)) table.set(info.pid, info);
  }
  return table;
}

async function linuxProcess(pid: number, bootMs: number, tickMs: number): Promise<ProcessInfo | undefined> {
  const stat = await fs.readFile(`/proc/${pid}/stat`, 'utf8').catch(() => undefined);
  if (stat === undefined) return undefined;
  const open = stat.indexOf('(');
  const close = stat.lastIndexOf(')');
  const fields = stat.slice(close + 2).split(' ');
  return { pid, ppid: Number(fields[1]), name: stat.slice(open + 1, close), startMs: bootMs + Number(fields[19]) * tickMs };
}

async function linuxChain(self: number): Promise<ProcessTable> {
  const boot = /^btime (\d+)$/m.exec(await fs.readFile('/proc/stat', 'utf8'))?.[1];
  const bootMs = Number(boot) * 1000;
  const table: ProcessTable = new Map();
  let pid = self;
  for (let depth = 0; depth <= MAX_DEPTH && pid > 0 && !table.has(pid); depth++) {
    const info = await linuxProcess(pid, bootMs, 10);
    if (info === undefined) break;
    table.set(pid, info);
    pid = info.ppid;
  }
  return table;
}

function powershell(env: NodeJS.ProcessEnv): string {
  const root = env.SystemRoot ?? env.SYSTEMROOT ?? 'C:\\Windows';
  return path.join(root, 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe');
}

export async function readProcessTable(self = process.pid, timeoutMs = LOOKUP_TIMEOUT_MS, platform: NodeJS.Platform = process.platform, env = process.env): Promise<ProcessTable> {
  if (platform === 'win32') return parseWindowsTable(await exec(powershell(env), ['-NoProfile', '-NonInteractive', '-Command', WINDOWS_QUERY], timeoutMs));
  if (platform === 'linux') return linuxChain(self);
  return parsePsTable(await exec('ps', ['-A', '-o', 'pid=,ppid=,lstart=,comm='], timeoutMs));
}

export async function findAgentProcess(self = process.pid, timeoutMs = LOOKUP_TIMEOUT_MS): Promise<ProcessInfo | undefined> {
  return findAgent(await readProcessTable(self, timeoutMs), self);
}

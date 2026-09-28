import { run, type RunResult } from '../process.js';

export const KEY_ENV = 'TYPESAFE_API_KEY';
export const KEY_SERVICE = 'typesafe';
export const KEY_ACCOUNT = 'api_key';
const COMPOUND_TARGET = `${KEY_ACCOUNT}@${KEY_SERVICE}`;
const STORE_TIMEOUT_MS = 15_000;

export type CommandRunner = (command: string, args: string[], options: { timeoutMs: number; env?: NodeJS.ProcessEnv }) => Promise<RunResult>;

export type KeySource = 'env' | 'credential-store';

export interface FoundKey {
  key: string;
  source: KeySource;
}

export interface KeyLookup {
  found?: FoundKey;
  missing?: string;
}

export interface KeyDeps {
  env: NodeJS.ProcessEnv;
  platform: NodeJS.Platform;
  runCommand?: CommandRunner;
}

const WINDOWS_SCRIPT = `
$ErrorActionPreference = 'Stop'
Add-Type -Namespace IdeAgentTabs -Name Cred -MemberDefinition @'
[StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
public struct CREDENTIAL {
  public int Flags;
  public int Type;
  public string TargetName;
  public string Comment;
  public uint LastWrittenLow;
  public uint LastWrittenHigh;
  public int CredentialBlobSize;
  public IntPtr CredentialBlob;
  public int Persist;
  public int AttributeCount;
  public IntPtr Attributes;
  public string TargetAlias;
  public string UserName;
}
[DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
public static extern bool CredReadW(string target, int type, int flags, out IntPtr credential);
[DllImport("advapi32.dll")]
public static extern void CredFree(IntPtr credential);
'@
$found = @()
foreach ($target in @('${KEY_SERVICE}', '${COMPOUND_TARGET}')) {
  $pointer = [IntPtr]::Zero
  if (-not [IdeAgentTabs.Cred]::CredReadW($target, 1, 0, [ref]$pointer)) { continue }
  try {
    $cred = [Runtime.InteropServices.Marshal]::PtrToStructure($pointer, [type][IdeAgentTabs.Cred+CREDENTIAL])
    $bytes = New-Object byte[] $cred.CredentialBlobSize
    if ($cred.CredentialBlobSize -gt 0) {
      [Runtime.InteropServices.Marshal]::Copy($cred.CredentialBlob, $bytes, 0, $cred.CredentialBlobSize)
    }
    $found += [pscustomobject]@{ target = $target; user = $cred.UserName; blob = [Convert]::ToBase64String($bytes) }
  } finally {
    [IdeAgentTabs.Cred]::CredFree($pointer)
  }
}
ConvertTo-Json -InputObject @($found) -Compress
`;

const ENCODED_WINDOWS_SCRIPT = Buffer.from(WINDOWS_SCRIPT, 'utf16le').toString('base64');
const POWERSHELLS = ['pwsh', 'powershell.exe'];

interface StoredCredential {
  target: string;
  user: string | null;
  blob: string;
}

export function storeDescription(platform: NodeJS.Platform): string {
  if (platform === 'win32') {
    return `the Windows Credential Manager (generic credential ${KEY_SERVICE} with user name ${KEY_ACCOUNT}, then ${COMPOUND_TARGET})`;
  }
  if (platform === 'darwin') return `the macOS keychain (service ${KEY_SERVICE}, account ${KEY_ACCOUNT})`;
  return `the Secret Service keyring (service ${KEY_SERVICE}, username ${KEY_ACCOUNT})`;
}

function decode(encoding: 'utf-16le' | 'utf-8', bytes: Buffer): string | undefined {
  try {
    return new TextDecoder(encoding, { fatal: true }).decode(bytes);
  } catch {
    return undefined;
  }
}

// An API key is printable ASCII, so a UTF-8 blob read as UTF-16LE never passes this test.
export function decodeBlob(bytes: Buffer): string | undefined {
  const wide = bytes.length % 2 === 0 ? decode('utf-16le', bytes) : undefined;
  if (wide !== undefined && /^[ -~]+$/.test(wide)) return wide;
  return decode('utf-8', bytes)?.trim() || undefined;
}

export function pickWindowsCredential(entries: StoredCredential[]): string | undefined {
  const chosen =
    entries.find((e) => e.target === KEY_SERVICE && e.user === KEY_ACCOUNT) ?? entries.find((e) => e.target === COMPOUND_TARGET);
  return chosen ? decodeBlob(Buffer.from(chosen.blob, 'base64')) : undefined;
}

function parseStored(stdout: string): StoredCredential[] {
  const text = stdout.trim();
  if (text === '') return [];
  const json: unknown = JSON.parse(text);
  const list = Array.isArray(json) ? json : [json];
  return list.filter(
    (e): e is StoredCredential =>
      typeof e === 'object' && e !== null && typeof e.target === 'string' && typeof e.blob === 'string',
  );
}

function failure(command: string, e: unknown): string {
  if ((e as NodeJS.ErrnoException).code === 'ENOENT') return `${command} is not installed`;
  const message = e instanceof Error ? e.message : String(e);
  return message.startsWith(command) ? message : `${command} failed: ${message}`;
}

async function readWindows(runCommand: CommandRunner, env: NodeJS.ProcessEnv, problems: string[]): Promise<string | undefined> {
  for (const shell of POWERSHELLS) {
    let result: RunResult;
    try {
      result = await runCommand(shell, ['-NoProfile', '-NonInteractive', '-EncodedCommand', ENCODED_WINDOWS_SCRIPT], {
        timeoutMs: STORE_TIMEOUT_MS,
        env,
      });
    } catch (e) {
      problems.push(failure(shell, e));
      continue;
    }
    if (result.code !== 0) {
      problems.push(`${shell} exited with code ${result.code}`);
      continue;
    }
    try {
      return pickWindowsCredential(parseStored(result.stdout));
    } catch {
      problems.push(`${shell} printed an answer that is not JSON`);
    }
  }
  return undefined;
}

async function readCommand(
  runCommand: CommandRunner,
  env: NodeJS.ProcessEnv,
  command: string,
  args: string[],
  problems: string[],
): Promise<string | undefined> {
  try {
    const result = await runCommand(command, args, { timeoutMs: STORE_TIMEOUT_MS, env });
    const key = result.stdout.trim();
    return result.code === 0 && key !== '' ? key : undefined;
  } catch (e) {
    problems.push(failure(command, e));
    return undefined;
  }
}

function readStore(deps: KeyDeps, problems: string[]): Promise<string | undefined> {
  const runCommand = deps.runCommand ?? run;
  if (deps.platform === 'win32') return readWindows(runCommand, deps.env, problems);
  if (deps.platform === 'darwin') {
    return readCommand(runCommand, deps.env, 'security', ['find-generic-password', '-s', KEY_SERVICE, '-a', KEY_ACCOUNT, '-w'], problems);
  }
  return readCommand(runCommand, deps.env, 'secret-tool', ['lookup', 'service', KEY_SERVICE, 'username', KEY_ACCOUNT], problems);
}

export async function lookUpKey(deps: KeyDeps): Promise<KeyLookup> {
  const fromEnv = deps.env[KEY_ENV]?.trim();
  if (fromEnv) return { found: { key: fromEnv, source: 'env' } };
  const problems: string[] = [];
  const stored = await readStore(deps, problems);
  if (stored) return { found: { key: stored, source: 'credential-store' } };
  const detail = problems.length ? ` (${problems.join('; ')})` : '';
  return {
    missing:
      `No TypeSafe API key found. The server looked in the ${KEY_ENV} environment variable and in ${storeDescription(deps.platform)}${detail}. ` +
      `Set ${KEY_ENV}, or store the key in the credential store under service ${KEY_SERVICE}, account ${KEY_ACCOUNT}.`,
  };
}

export class KeyStore {
  private cached?: FoundKey;

  constructor(private readonly deps: KeyDeps) {}

  async lookUp(): Promise<KeyLookup> {
    if (this.cached) return { found: this.cached };
    const result = await lookUpKey(this.deps);
    if (result.found) this.cached = result.found;
    return result;
  }
}

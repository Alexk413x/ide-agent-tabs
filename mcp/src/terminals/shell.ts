import path from 'node:path';

export const LAUNCHER_SH = 'agent-launch.sh';
export const LAUNCHER_FISH = 'agent-launch.fish';
const SAFE_PATH = /^\/[A-Za-z0-9._/+-]+$/;
const TITLE_MAX = 40;

export interface LoginShell {
  path: string;
  kind: 'posix' | 'fish';
}

export function loginShell(shellEnv: string | undefined, platform: NodeJS.Platform): LoginShell {
  if (shellEnv && SAFE_PATH.test(shellEnv)) {
    const name = path.posix.basename(shellEnv);
    if (name === 'bash' || name === 'zsh') return { path: shellEnv, kind: 'posix' };
    if (name === 'fish') return { path: shellEnv, kind: 'fish' };
  }
  return { path: platform === 'linux' ? '/bin/bash' : '/bin/zsh', kind: 'posix' };
}

export function launcherName(shell: LoginShell): string {
  return shell.kind === 'fish' ? LAUNCHER_FISH : LAUNCHER_SH;
}

function checkShell(shell: LoginShell): void {
  if (!SAFE_PATH.test(shell.path)) throw new Error(`unsafe shell path: ${shell.path}`);
}

function sourceLauncher(shell: LoginShell): string {
  checkShell(shell);
  return shell.kind === 'fish'
    ? `source "$IDE_AGENT_TABS_LAUNCHER"; exec ${shell.path} -l -i`
    : `. "$IDE_AGENT_TABS_LAUNCHER"; exec ${shell.path} -l -i`;
}

export function surfaceArgv(shell: LoginShell): string[] {
  return [shell.path, '-l', '-i', '-c', sourceLauncher(shell)];
}

// Ghostty on macOS runs a surface's command through /bin/sh -c. The shell path is checked against
// SAFE_PATH, and everything else the launch needs reaches it through the surface's environment variables.
export function surfaceCommand(shell: LoginShell): string {
  return `${shell.path} -l -i -c '${sourceLauncher(shell)}'`;
}

// Argv mode, for a terminal whose new tab can't take environment variables: the launcher and spec paths are
// positional arguments of the shell, never part of the script it interprets. fish 3.2 or later puts them in
// $argv; bash and zsh take the first one as $0.
export function argvModeCommand(shell: LoginShell, launcher: string, spec: string): string[] {
  checkShell(shell);
  checkArgvPaths('the login shell', [launcher, spec]);
  if (shell.kind === 'fish') {
    const script = `set -gx IDE_AGENT_TABS_SPEC $argv[2]; source "$argv[1]"; exec ${shell.path} -l -i`;
    return [shell.path, '-l', '-i', '-c', script, launcher, spec];
  }
  const script = `IDE_AGENT_TABS_SPEC=$2; export IDE_AGENT_TABS_SPEC; . "$1"; exec ${shell.path} -l -i`;
  return [shell.path, '-l', '-i', '-c', script, 'agent-tabs', launcher, spec];
}

export function checkArgvPaths(terminal: string, paths: string[], refuse = ''): void {
  for (const p of paths) {
    const bad = [...p].find((c) => /\p{Cc}/u.test(c) || refuse.includes(c));
    if (bad !== undefined) {
      const what = /\p{Cc}/u.test(bad) ? 'a control character' : `'${bad}'`;
      throw new Error(`${terminal} can't start a path that holds ${what}: ${JSON.stringify(p)}`);
    }
  }
}

export function tabTitle(label: string): string {
  const clean = [...label.replace(/\p{C}+/gu, ' ').replace(/\s+/g, ' ').trim()].slice(0, TITLE_MAX).join('').trim();
  return clean === '' ? 'Agent' : clean;
}

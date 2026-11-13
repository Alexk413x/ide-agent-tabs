// An untrusted workspace never opens an agent on its own: a cloned repository's settings or .claude folder
// must not start a CLI before the user has trusted it.
export function startupFolder<T>(mode: string, trusted: boolean, folders: readonly T[], hasClaudeFolder: (folder: T) => boolean): T | undefined {
  if (!trusted) return undefined;
  if (mode === 'always') return folders[0];
  if (mode === 'claudeFolder') return folders.find(hasClaudeFolder);
  return undefined;
}

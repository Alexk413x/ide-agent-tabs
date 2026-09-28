import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { agentTabsHome } from './home.js';
import { appendLog, syncHook, syncInstall, syncStatus, type SyncContext } from './sync.js';

const print = (text: string) => new Promise<void>((resolve) => process.stdout.write(text, () => resolve()));

const USAGE = 'Usage: sync-ides.mjs --hook | --status | --install [--jetbrains] [<editor cli>...]';

const ctx: SyncContext = {
  bundleDir: path.join(path.dirname(fileURLToPath(import.meta.url)), 'ide'),
  home: agentTabsHome(),
  platform: process.platform,
  env: process.env,
  userHome: os.homedir(),
};

async function main(args: string[]): Promise<number> {
  const [mode, ...rest] = args;
  if (mode === '--hook') {
    try {
      const message = await syncHook(ctx);
      if (message) {
        await print(
          `${JSON.stringify({ systemMessage: message, hookSpecificOutput: { hookEventName: 'SessionStart', additionalContext: message } })}\n`,
        );
      }
    } catch (e) {
      await appendLog(ctx.home, `hook: ${(e as Error).message}`).catch(() => undefined);
    }
    return 0;
  }
  if (mode === '--status') {
    await print(`${JSON.stringify(await syncStatus(ctx), null, 2)}\n`);
    return 0;
  }
  if (mode === '--install') {
    const report = await syncInstall(
      ctx,
      rest.filter((a) => a !== '--jetbrains'),
      rest.includes('--jetbrains'),
    );
    await print(`${JSON.stringify(report, null, 2)}\n`);
    return report.errors.length > 0 ? 1 : 0;
  }
  process.stderr.write(`${USAGE}\n`);
  return 2;
}

main(process.argv.slice(2)).then(
  (code) => process.exit(code),
  (e: unknown) => {
    process.stderr.write(`${(e as Error).message}\n`);
    process.exit(process.argv[2] === '--hook' ? 0 : 1);
  },
);

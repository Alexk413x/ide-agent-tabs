import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { refreshDetectionFile } from './detection.js';
import { agentTabsHome } from './home.js';
import { agentsReport, registerAgents, unregisterAgents } from './register.js';
import { appendLog, syncHook, syncInstall, syncStatus, type SyncContext } from './sync.js';
import { TERMINAL_DRIVERS } from './terminals/index.js';

const print = (text: string) => new Promise<void>((resolve) => process.stdout.write(text, () => resolve()));

const USAGE =
  'Usage: sync-ides.mjs --hook | --status | --install [--jetbrains] [<editor cli>...] | --agents | --register <agent>... | --unregister <agent>...';

const dist = path.dirname(fileURLToPath(import.meta.url));

const ctx: SyncContext = {
  bundleDir: path.join(dist, 'ide'),
  serverDir: dist,
  home: agentTabsHome(),
  platform: process.platform,
  env: process.env,
  userHome: os.homedir(),
};

async function main(args: string[]): Promise<number> {
  const [mode, ...rest] = args;
  if (mode === '--hook') {
    const detection = refreshDetectionFile({ home: ctx.home, platform: ctx.platform, env: ctx.env, drivers: TERMINAL_DRIVERS }).catch((e: unknown) =>
      appendLog(ctx.home, `detection: ${(e as Error).message}`).catch(() => undefined),
    );
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
    await detection;
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
  if (mode === '--agents') {
    await print(`${JSON.stringify(await agentsReport(ctx), null, 2)}\n`);
    return 0;
  }
  if ((mode === '--register' || mode === '--unregister') && rest.length > 0) {
    const report = mode === '--register' ? await registerAgents(ctx, rest) : await unregisterAgents(ctx, rest);
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

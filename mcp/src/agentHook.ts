import { agentTabsHome } from './home.js';
import { runHook } from './messaging/hook.js';
import { TAB_ID_ENV } from './profiles.js';

const STDIN_WAIT_MS = 2_000;

function readStdin(): Promise<string> {
  return new Promise((resolve) => {
    let text = '';
    const done = () => {
      clearTimeout(timer);
      resolve(text);
    };
    const timer = setTimeout(done, STDIN_WAIT_MS);
    process.stdin.setEncoding('utf8');
    process.stdin.on('data', (chunk: string) => (text += chunk));
    process.stdin.on('end', done);
    process.stdin.on('error', done);
  });
}

function parseInput(text: string): Record<string, unknown> {
  try {
    const json: unknown = JSON.parse(text);
    return typeof json === 'object' && json !== null && !Array.isArray(json) ? (json as Record<string, unknown>) : {};
  } catch {
    return {};
  }
}

async function main(): Promise<void> {
  const [cli = '', event = ''] = process.argv.slice(2);
  const sessionId = process.env[TAB_ID_ENV];
  if (!sessionId) return;
  const input = parseInput(await readStdin());
  const output = await runHook({ cli, event, input, home: agentTabsHome(), sessionId });
  if (output !== undefined) await new Promise<void>((r) => process.stdout.write(`${JSON.stringify(output)}\n`, () => r()));
}

main().then(
  () => process.exit(0),
  () => process.exit(0),
);

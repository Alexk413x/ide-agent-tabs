// The names repeat TAB_ID_ENV and the mod's marker on purpose: a session the Claude Code mod drives exits
// here before any module loads, which saves about a third of each hook's run time.
const tab = process.env.IDE_AGENT_TABS_ID;
if (!tab || tab === process.env.IDE_AGENT_TABS_MOD) process.exit(0);

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
  const [{ agentTabsHome }, { runHook }] = await Promise.all([import('./home.js'), import('./messaging/hook.js')]);
  const input = parseInput(await readStdin());
  const output = await runHook({ cli, event, input, home: agentTabsHome(), sessionId: tab });
  if (output !== undefined) await new Promise<void>((r) => process.stdout.write(`${JSON.stringify(output)}\n`, () => r()));
}

main().then(
  () => process.exit(0),
  () => process.exit(0),
);

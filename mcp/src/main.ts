const [command, ...args] = process.argv.slice(2);

async function exitWith(code: Promise<number>): Promise<never> {
  const status = await code;
  await new Promise<void>((resolve) => process.stdout.write('', () => resolve()));
  process.exit(status);
}

if (command === 'jev' || command === 'list-ides') {
  const { runCli } = await import('./cli.js');
  await exitWith(runCli(command, args));
} else if (command === 'server') {
  const { runServerCli } = await import('./serverCli.js');
  await exitWith(runServerCli(args));
} else {
  await import('./stdioMain.js');
}

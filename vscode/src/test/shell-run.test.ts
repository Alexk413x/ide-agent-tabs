import assert from 'node:assert/strict';
import { spawn, spawnSync } from 'node:child_process';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { describe, test } from 'node:test';
import { launchScripts, powerShellArgs, ShellPlan, terminalEnv, unixShell } from '../launch';
import { AgentProfile, findOnPath, launchOf, profile } from '../profiles';

const isWindows = process.platform === 'win32';
const scriptDir = path.resolve(__dirname, '..', '..', 'resources', 'launch');
const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'iat-run-'));
const searchPath = process.env.PATH ?? '';

const NASTY_PROMPT = `Say "hi" & run $(whoami); \`tick\` 'quote' --flag é ✓ 🙂 a;b|c > d
second line`;

interface Run {
  out: string;
  alive: string;
}

function run(shell: ShellPlan, env: Record<string, string | null>, stdin: string, extraEnv: Record<string, string> = {}): Promise<Run> {
  const out = path.join(dir, `out-${Math.random().toString(16).slice(2)}`);
  const alive = `${out}.alive`;
  const childEnv: Record<string, string> = { ...(process.env as Record<string, string>), IAT_OUT: out, IAT_ALIVE: alive, ...extraEnv };
  for (const [name, value] of Object.entries(env)) {
    if (value === null) delete childEnv[name];
    else childEnv[name] = value;
  }
  return new Promise((resolve, reject) => {
    const child = spawn(shell.path, shell.args, { env: childEnv, cwd: dir, stdio: ['pipe', 'ignore', 'pipe'] });
    let stderr = '';
    child.stderr.on('data', d => (stderr += d));
    const timer = setTimeout(() => child.kill(), 60_000);
    child.on('error', reject);
    child.on('exit', () => {
      clearTimeout(timer);
      const read = (file: string) => (fs.existsSync(file) ? fs.readFileSync(file, 'utf8') : `missing; stderr: ${stderr}`);
      resolve({ out: read(out), alive: read(alive) });
    });
    child.stdin.end(stdin);
  });
}

const pwsh = findOnPath(searchPath, isWindows ? 'pwsh.exe' : 'pwsh');

describe('PowerShell launcher delivers every argument byte for byte', { skip: pwsh ? false : 'pwsh not on PATH' }, () => {
  const probe = path.join(dir, 'probe.ps1');
  fs.writeFileSync(
    probe,
    [
      "$left = $env:IDE_AGENT_TABS_PROMPT ?? $env:IDE_AGENT_TABS_ARGS ?? $env:IDE_AGENT_TABS_COMMAND ?? 'null'",
      '$o = [ordered]@{ args = @($args); left = $left; agent = $env:IDE_AGENT_TABS_AGENT; id = $env:IDE_AGENT_TABS_ID; v = $env:IAT_TEST_VAR }',
      '[IO.File]::WriteAllText($env:IAT_OUT, (ConvertTo-Json -InputObject $o -Compress), [Text.UTF8Encoding]::new($false))',
    ].join('\n'),
  );
  const alive =
    "$c = if ($null -eq $env:IDE_AGENT_TABS_COMMAND) { 'unset' } else { $env:IDE_AGENT_TABS_COMMAND }\n" +
    "[IO.File]::WriteAllText($env:IAT_ALIVE, 'alive|' + $env:IDE_AGENT_TABS_ID + '|' + $c)\nexit\n";
  const scripts = launchScripts(scriptDir);
  const probeProfile = (promptFlag?: string) => profile('test', 'Test', pwsh!, { args: ['-NoProfile', '-File', probe], promptFlag });

  async function runPwsh(p: AgentProfile, prompt?: string, args: string[] = [], env: Record<string, string> = {}, shellPath = pwsh!) {
    const shell: ShellPlan = { path: shellPath, kind: 'powershell', args: powerShellArgs(scripts.powershell, isWindows) };
    const result = await run(shell, terminalEnv('powershell', launchOf(p, prompt, args, env), 'tab-3', process.env), alive);
    assert.equal(result.alive, 'alive|tab-3|unset', 'the shell stays open with the tab id and without the command');
    return JSON.parse(result.out);
  }

  test('plain launch passes no arguments', async () => {
    assert.deepEqual(await runPwsh(probeProfile()), { args: [], left: 'null', agent: 'test', id: 'tab-3', v: null });
  });

  test('a nasty prompt arrives as one intact argument', async () => {
    const out = await runPwsh(probeProfile(), NASTY_PROMPT);
    assert.deepEqual(out.args, [NASTY_PROMPT]);
    assert.equal(out.left, 'null');
  });

  test('args arrive intact and before the prompt, and env reaches the session', async () => {
    const args = ['--plugin-dir', 'C:\\Program Files\\a b', `say "hi" $(whoami) \`t\` 'q'`, 'é ✓', '2024-01-01T00:00:00Z', 'C:\\trailing\\', '@a'];
    const out = await runPwsh(probeProfile(), 'the prompt', args, { IAT_TEST_VAR: 'value with spaces' });
    assert.deepEqual(out, { args: [...args, 'the prompt'], left: 'null', agent: 'test', id: 'tab-3', v: 'value with spaces' });
  });

  test('a native program gets every arg byte for byte, including empty and flag-like ones', async () => {
    const probeJs = path.join(dir, 'probe.js');
    fs.writeFileSync(
      probeJs,
      "const e = process.env; require('fs').writeFileSync(e.IAT_OUT, JSON.stringify({ args: process.argv.slice(2), " +
        "left: e.IDE_AGENT_TABS_PROMPT ?? e.IDE_AGENT_TABS_ARGS ?? e.IDE_AGENT_TABS_COMMAND ?? 'null', agent: e.IDE_AGENT_TABS_AGENT }));",
    );
    const node = profile('node', 'Node', process.execPath, { args: [probeJs], promptFlag: '-p' });
    const args = ['', '-x:y', '--', 'a "quoted" \\"arg\\"', 'C:\\trailing\\', 'tab\there', '%PATH%', '!x!', '^&|<>', '2024-01-01T00:00:00Z'];
    const out = await runPwsh(node, NASTY_PROMPT, args);
    assert.deepEqual(out, { args: [...args, '-p', NASTY_PROMPT], left: 'null', agent: 'node' });
  });

  test('the prompt flag goes right before the prompt', async () => {
    assert.deepEqual((await runPwsh(probeProfile('--prompt'), 'the prompt', ['--yolo'])).args, ['--yolo', '--prompt', 'the prompt']);
  });

  test('a single arg stays one argument', async () => {
    assert.deepEqual((await runPwsh(probeProfile(), undefined, ['--verbose'])).args, ['--verbose']);
  });

  const windowsPowerShell = isWindows ? findOnPath(searchPath, 'powershell.exe') : undefined;
  test('Windows PowerShell 5.1 runs the same launcher', { skip: windowsPowerShell ? false : 'Windows only' }, async () => {
    const out = await runPwsh(probeProfile(), 'hello world', ['--model', 'm b'], {}, windowsPowerShell);
    assert.deepEqual(out.args, ['--model', 'm b', 'hello world']);
  });
});

function toWsl(p: string): string {
  return `/mnt/${p[0].toLowerCase()}${p.slice(2).replace(/\\/g, '/')}`;
}

const wslBash = isWindows && findOnPath(searchPath, 'wsl.exe') && spawnSync('wsl.exe', ['--exec', '/bin/bash', '-c', 'echo ok'], { encoding: 'utf8' }).stdout?.trim() === 'ok';
const nativeBash = !isWindows ? findOnPath(searchPath, 'bash') : undefined;

describe('bash launcher delivers every argument and stays interactive', { skip: wslBash || nativeBash ? false : 'no bash (native or WSL)' }, () => {
  const local = (p: string) => (isWindows ? toWsl(p) : p);
  const fake = [
    '#!/bin/sh',
    'out=""; for x in "$@"; do out="$out$x$(printf \'\\037\')"; done',
    'printf \'%s|%s|%s|%s|%s|%s\' "$#" "$out" "${IDE_AGENT_TABS_PROMPT-${IDE_AGENT_TABS_ARG_0-${IDE_AGENT_TABS_ARGC-${IDE_AGENT_TABS_COMMAND-unset}}}}" "$IDE_AGENT_TABS_AGENT" "$IAT_TEST_VAR" "$IAT_RC" > "$IAT_OUT"',
    '',
  ].join('\n');
  const home = path.join(dir, 'home');
  fs.mkdirSync(path.join(dir, 'my bin'), { recursive: true });
  fs.mkdirSync(home, { recursive: true });
  for (const target of [path.join(dir, 'cst-agent'), path.join(dir, 'my bin', 'cst agent')]) {
    fs.writeFileSync(target, fake);
    fs.chmodSync(target, 0o755);
  }
  fs.writeFileSync(path.join(home, '.bashrc'), 'export IAT_RC=rc\n');
  fs.writeFileSync(path.join(home, '.bash_profile'), '. "$HOME/.bashrc"\n');
  const scripts = { powershell: '', posix: `${local(scriptDir)}/agent.sh`, fish: `${local(scriptDir)}/agent.fish` };
  const agent = profile('test', 'Test', local(path.join(dir, 'cst-agent')));
  const spaced = profile('spaced', 'Spaced', local(path.join(dir, 'my bin', 'cst agent')), { args: ['--model', 'm b'], promptFlag: '-i' });
  const alive = 'printf \'alive|%s|%s|%s\' "$IDE_AGENT_TABS_ID" "${IDE_AGENT_TABS_COMMAND-${IDE_AGENT_TABS_PROMPT-unset}}" "$IAT_RC" > "$IAT_ALIVE"\nexit\n';

  async function runBash(isMac: boolean, p: AgentProfile, prompt?: string, args: string[] = [], env: Record<string, string> = {}) {
    const plan = unixShell('/bin/bash', isMac, scripts);
    const launchEnv = terminalEnv('posix', launchOf(p, prompt, args, env), 'tab-1', process.env);
    let shell = plan;
    let extra: Record<string, string> = { HOME: home };
    if (isWindows) {
      shell = { ...plan, path: 'wsl.exe', args: ['--exec', '/usr/bin/env', `HOME=${toWsl(home)}`, plan.path, ...plan.args] };
      const names = Object.keys(launchEnv).filter(k => launchEnv[k] !== null);
      extra = { WSLENV: [...names, 'IAT_OUT/p', 'IAT_ALIVE/p'].join(':') };
    }
    const result = await run(shell, launchEnv, alive, extra);
    assert.equal(result.alive, 'alive|tab-1|unset|rc', 'the shell stays open with the tab id and the rc file loaded');
    return result.out;
  }

  for (const isMac of [false, true]) {
    const flags = isMac ? '-l -i' : '-i';
    test(`bash ${flags}: plain, nasty prompt, args and env, spaced command`, async () => {
      assert.equal(await runBash(isMac, agent), '0||unset|test||rc');
      assert.equal(await runBash(isMac, agent, NASTY_PROMPT), `1|${NASTY_PROMPT}\u001f|unset|test||rc`);
      const args = ['--plugin-dir', '/a b/c', 'say "hi" $(whoami) `t` *', '', 'é ✓'];
      assert.equal(
        await runBash(isMac, agent, 'the prompt', args, { IAT_TEST_VAR: 'value with spaces' }),
        `6|${[...args, 'the prompt'].map(a => `${a}\u001f`).join('')}|unset|test|value with spaces|rc`,
      );
      assert.equal(await runBash(isMac, spaced, 'hi'), '4|--model\u001fm b\u001f-i\u001fhi\u001f|unset|spaced||rc');
    });
  }
});

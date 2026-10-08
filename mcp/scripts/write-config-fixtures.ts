import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { isScalar, isSeq, type Document } from 'yaml';
import { AGY_ALLOW_RULES, withAgyAllowRule, withCodexSettings } from '../src/hookConfig.js';
import { editJson, entryServerPath, parseCodexGet, samePath, stripJsonComments, withServerEntry } from '../src/register.js';
import { fileUrl, hookMessage, needsSync, parseExtensionList, repositoryVersion, updatePluginsXml } from '../src/sync.js';
import { readTomlTable, tomlTable, withTomlTable } from '../src/tomlTable.js';
import { editYaml, filterYamlLists, setYamlEntry, yamlMap } from '../src/yamlConfig.js';

export const CONFIG_FIXTURES_FILE = fileURLToPath(new URL('../tests/fixtures/config.json', import.meta.url));

type Case = { fn: string; args: unknown[]; result?: unknown; error?: string };

function cases(fn: string, run: (...args: never[]) => unknown, inputs: unknown[][]): Case[] {
  return inputs.map((args) => {
    try {
      const result = (run as (...a: unknown[]) => unknown)(...args);
      return { fn, args, result: result === undefined ? null : result };
    } catch (e) {
      return { fn, args, error: (e as Error).message };
    }
  });
}

const PY = '/usr/bin/python3';
const WIN_PY = 'C:/Users/a/AppData/Local/Programs/Python/Python313/python.exe';
const SERVER = '/home/a/.ide-agent-tabs/mcp/py/launch/mcp_server.py';
const WIN_SERVER = 'C:/Users/a/.ide-agent-tabs/mcp/py/launch/mcp_server.py';
const HOOK = '/home/a/.ide-agent-tabs/mcp/py/launch/agent_hook.py';
const argv = (python: string, server: string) => [python, '-I', '-S', server];
const tabEnv = { IDE_AGENT_TABS_ID: '${IDE_AGENT_TABS_ID}', IDE_AGENT_TABS_AGENT: '${IDE_AGENT_TABS_AGENT}' };

const COPILOT = { type: 'local', command: PY, args: ['-I', '-S', SERVER], env: tabEnv, tools: ['*'] };
const OPENCODE = { type: 'local', command: argv(WIN_PY, WIN_SERVER), enabled: true, timeout: 660_000 };
const HERMES = { command: PY, args: ['-I', '-S', SERVER], env: tabEnv, timeout: 660 };
const HERMES_SPACED = { command: 'C:/Users/a b/python.exe', args: ['-I', '-S', 'C:/Users/a b/.ide-agent-tabs/mcp/py/launch/mcp_server.py'], env: tabEnv, timeout: 660 };
const GOOSE = {
  name: 'ide-agent-tabs',
  type: 'stdio',
  cmd: PY,
  args: ['-I', '-S', SERVER],
  enabled: true,
  timeout: 700,
  envs: {},
  env_keys: [],
  description: 'Agent Tabs',
};

const TOML_TABLE = tomlTable('mcp_servers', 'ide-agent-tabs', { command: WIN_PY, args: ['-I', '-S', WIN_SERVER] });
const TOML_TEXTS = [
  undefined,
  '',
  '# mine\nmodel = "grok-4"\n\n[mcp_servers.other]\ncommand = "x"\n',
  'a = 1\r\n\r\n[mcp_servers."ide-agent-tabs"]\r\ncommand = "old"\r\n\r\n[mcp_servers.ide-agent-tabs.env]\r\nX = "1"\r\n\r\n# next\r\n[other]\r\nb = 2\r\n',
  '[mcp_servers.ide-agent-tabs]\ncommand = "node"\nargs = ["/home/a/.ide-agent-tabs/mcp/mcp-server.mjs"]\n\n[after]\nx = 1\n',
  '[mcp_servers]\nide-agent-tabs = { command = "x" }\n',
  'mcp_servers.ide-agent-tabs.command = "x"\n',
  '# ide-agent-tabs = old note\nargs = ["/x/.ide-agent-tabs/y"]\n',
  'model = "x"\n\n\n',
];

const JSON_TEXTS = [
  undefined,
  '',
  '{}',
  '{\n  "mcpServers": {\n    "github": {\n      "type": "http",\n      "url": "https://example.test/mcp"\n    }\n  }\n}\n',
  '{\r\n    "mcpServers": {\r\n        "other": { "command": "x" }\r\n    },\r\n    "theme": "dark"\r\n}\r\n',
  '{\n\t"mcpServers": {\n\t\t"ide-agent-tabs": {"command": "node", "args": ["/home/a/.ide-agent-tabs/mcp/mcp-server.mjs"]}\n\t},\n\t"n": 1.0,\n\t"big": 1e21\n}',
  '{ // comment\n}',
  '[]',
  '{"mcpServers": []}',
  '{"mcp": {"ide-agent-tabs": {"type": "local", "command": ["node", "x"]}}, "2": "two", "1": "one"}',
];

const YAML_TEXTS = [
  undefined,
  '',
  'mcp_servers:\n',
  '# my config\nmodel: x # pinned\nmcp_servers:\n  other:\n    command: foo\n',
  '# hermes\nmodel:\n  default: x # mine\nmcp_servers:\n  other:\n    command: x\nhooks:\n  pre_llm_call:\n    - command: echo hi\n',
  'extensions:\n  developer:\n    enabled: true\n    type: builtin\n    name: developer\nGOOSE_PROVIDER: ollama\n',
  'extensions:\n  ide-agent-tabs:\n    name: ide-agent-tabs\n    type: stdio\n    cmd: node\n    args:\n      - /home/a/.ide-agent-tabs/mcp/mcp-server.mjs\n    enabled: true\n    timeout: 700\n    envs: {}\n    env_keys: []\n    description: Agent Tabs\n',
  'mcp_servers:\n  ide-agent-tabs:\n    command: node\n    args:\n      - /home/a/.ide-agent-tabs/mcp/mcp-server.mjs\n    env:\n      IDE_AGENT_TABS_ID: ${IDE_AGENT_TABS_ID}\n      IDE_AGENT_TABS_AGENT: ${IDE_AGENT_TABS_AGENT}\n    timeout: 660\n',
  'a: 1\r\nmcp_servers:\r\n  other:\r\n    command: x\r\n',
];

const YAML_SCALARS = [
  'node', 'C:\\a b\\s.mjs', "'C:/Py/python.exe' -I -S 'C:/x/agent_hook.py' hermes pre_llm_call", '${IDE_AGENT_TABS_ID}', '-I', '-S', 'true', '1',
  'a: b', 'a #b', ' x', '@x', '%x', "x'y", 'x"y', 'it\'s "q"', '~', 'null', '0x1', '1.5', '-3', '*', ':x', 'x:', '?x', 'a\tb', 'yes', 'on',
  '1e3', '.5', 'Infinity', '.inf', 'C:/Users/a b/python.exe', '/usr/bin/python3', '#x', '[x', '{x', '|x', '>x', '!x', '&x', 'x,y', 'x]', 'x}',
  '- x', '-', '--', '---', '...', '=', '<<', '-.5', '+1', '0o7', '1_000', 'NaN', '.nan', 'é', 'back\\slash', 'a  b', 'x ', "'", 'False', 'TRUE',
  'Null', '0b1', '-x', '?', ':', 'x?', 'a:b', 'a#b', '1.', '+.5', '0x', '12e', '-.inf', '+.INF', '007', '1:2', '"x', 'ide-agent-tabs',
];

const HERMES_EVENTS = ['pre_llm_call', 'post_tool_call', 'pre_approval_request', 'post_approval_response', 'pre_verify', 'on_session_end'];
const hermesItems = (python: string, hook: string) =>
  HERMES_EVENTS.map((event) => ({ event, command: `${python} -I -S ${hook} hermes ${event}`, timeout: 5 }));
const OLD_HOOK = /[\\/](?:agent-hook\.mjs|mcp[\\/]py[\\/]launch[\\/]agent_hook\.py)(?:["'\s]|$)/;
const isOurs = (item: unknown) => typeof item === 'object' && item !== null && OLD_HOOK.test(String((item as { command?: unknown }).command));

// The same steps as withHermesHooks in hookConfig.ts, with the Python hook commands in place of the node ones.
function hermesHooks(doc: Document, file: string, items: { event: string; command: string; timeout: number }[] | undefined): void {
  filterYamlLists(doc, 'hooks', file, isOurs);
  if (items === undefined) return;
  const hooks = yamlMap(doc, 'hooks', file, true)!;
  for (const { event, command, timeout } of items) {
    const list = hooks.get(event, true);
    if (list === undefined || list === null || (isScalar(list) && list.value === null)) hooks.set(event, doc.createNode([{ command, timeout }]));
    else if (isSeq(list)) list.add(doc.createNode({ command, timeout }));
    else throw new Error(`${file}: hooks.${event} isn't a list; edit it by hand`);
  }
}

const HERMES_HOOK_TEXTS = [
  undefined,
  '# hermes\nmodel:\n  default: x # mine\nmcp_servers:\n  other:\n    command: x\nhooks:\n  pre_llm_call:\n    - command: echo hi\n',
  "hooks:\n  pre_llm_call:\n    - command: node '/home/a/.ide-agent-tabs/mcp/agent-hook.mjs' hermes pre_llm_call\n      timeout: 5\n  post_tool_call:\n    - command: node '/home/a/.ide-agent-tabs/mcp/agent-hook.mjs' hermes post_tool_call\n      timeout: 5\n    - command: echo mine\n",
  'hooks:\n  pre_llm_call:\n',
  'hooks: [1]\n',
  'model: x\nhooks:\n  pre_llm_call: ~\n  other:\n    - command: x\n',
];

function yamlCases(): Case[] {
  const out: Case[] = [];
  const entries: [string, unknown][] = [
    ['mcp_servers', HERMES],
    ['mcp_servers', HERMES_SPACED],
    ['extensions', GOOSE],
    ['mcp_servers', undefined],
    ['extensions', undefined],
  ];
  for (const text of YAML_TEXTS) {
    for (const [section, entry] of entries) {
      out.push(
        ...cases('setYamlEntry', (t: string | null, s: string, e: unknown) => editYaml(t ?? undefined, 'f', (doc) => setYamlEntry(doc, s, 'ide-agent-tabs', e ?? undefined, 'f')), [
          [text ?? null, section, entry ?? null],
        ]),
      );
    }
  }
  for (const text of HERMES_HOOK_TEXTS) {
    for (const items of [hermesItems(PY, HOOK), hermesItems("'C:/Users/a b/python.exe'", "'C:/Users/a b/.ide-agent-tabs/mcp/py/launch/agent_hook.py'"), undefined]) {
      out.push(...cases('hermesHooks', (t: string | null, i: typeof items | null) => editYaml(t ?? undefined, 'f', (doc) => hermesHooks(doc, 'f', i ?? undefined)), [[text ?? null, items ?? null]]));
    }
  }
  return out;
}

function fixturesText(): string {
  const all: Case[] = [
    ...TOML_TEXTS.flatMap((text) =>
      cases('withTomlTable', (t: string | undefined, table: string[] | null) => withTomlTable(t, 'f', 'mcp_servers', 'ide-agent-tabs', table ?? undefined), [
        [text ?? null, TOML_TABLE],
        [text ?? null, null],
      ]),
    ),
    ...cases('readTomlTable', (t: string) => readTomlTable(t, 'mcp_servers', 'ide-agent-tabs'), [
      [`a = 1\n\n${TOML_TABLE.join('\n')}\n`],
      ["[mcp_servers.ide-agent-tabs]\ncommand = 'node'\nargs = [\n  'a', # first\n  \"b\",\n]\n"],
      ['[mcp_servers.ide-agent-tabs]\ncommand = "C:\\\\Py\\\\python.exe"\nargs = ["-I", "-S", "x"]\nenv_vars = ["A"]\n'],
      ['[mcp_servers.other]\ncommand = "x"\n'],
    ]),
    ...cases('tomlTable', (python: string, server: string) => tomlTable('mcp_servers', 'ide-agent-tabs', { command: python, args: ['-I', '-S', server] }), [
      [WIN_PY, WIN_SERVER],
      ['C:\\Py\\python.exe', 'C:\\h "q"\\x.py'],
    ]),
    ...JSON_TEXTS.flatMap((text) =>
      cases(
        'withServerEntry',
        (t: string | null, section: string, entry: unknown, skeleton: Record<string, unknown> | null) => withServerEntry(t ?? undefined, 'f', section, entry ?? undefined, skeleton ?? undefined),
        [
          [text ?? null, 'mcpServers', COPILOT, null],
          [text ?? null, 'mcp', OPENCODE, { $schema: 'https://opencode.ai/config.json' }],
          [text ?? null, 'mcpServers', null, null],
          [text ?? null, 'mcp', null, null],
        ],
      ),
    ),
    ...JSON_TEXTS.flatMap((text) =>
      cases('editJsonAllow', (t: string | null, allow: boolean) => editJson(t ?? undefined, 'f', (root) => withAgyAllowRule(root, 'f', allow)), [
        [text ?? null, true],
        [text ?? null, false],
      ]),
    ),
    ...cases('editJsonAllow', (t: string | null, allow: boolean) => editJson(t ?? undefined, 'f', (root) => withAgyAllowRule(root, 'f', allow)), [
      ['{\n  "permissions": {\n    "allow": ["command(git)", "mcp(ide-agent-tabs/*)"]\n  }\n}\n', true],
      ['{\n  "permissions": {\n    "allow": ["command(git)", "mcp(ide-agent-tabs/*)"]\n  }\n}\n', false],
      [`{"permissions": {"allow": ${JSON.stringify(['x', ...AGY_ALLOW_RULES])}}}`, true],
      ['{"permissions": []}', true],
      ['{"permissions": {"allow": {}}}', true],
    ]),
    ...cases('withCodexSettings', (t: string) => withCodexSettings(t, 'f'), [
      ['model = "o"\r\n\r\n[mcp_servers."ide-agent-tabs"]\r\ncommand = "node"\r\n\r\n[mcp_servers.ide-agent-tabs.env]\r\nA = "1"\r\n'],
      ['[mcp_servers.ide-agent-tabs]\ntool_timeout_sec = 30\n'],
      ['[mcp_servers.ide-agent-tabs]\nenv_vars = ["IDE_AGENT_TABS_ID", "IDE_AGENT_TABS_AGENT", "IDE_AGENT_TABS_HOME"]\ntool_timeout_sec = 660\n'],
      ['[mcp_servers.ide-agent-tabs]\nenv_vars = ["OTHER"]\n'],
      ['[mcp_servers.other]\n'],
      [`model = "x"\n\n[mcp_servers.ide-agent-tabs]\ncommand = "${PY}"\nargs = ["-I", "-S", "${SERVER}"]\n`],
    ]),
    ...cases('stripJsonComments', stripJsonComments, [
      ['{\n  // a comment\n  "url": "http://x/*y*/", /* block */ "n": "a\\"//b"\n}'],
      ['{"a": 1 /* unclosed'],
      ['// only\n'],
    ]),
    ...cases('entryServerPath', entryServerPath, [
      [COPILOT],
      [OPENCODE],
      [{ command: 'node', args: ['--flag', 'D:\\x\\server.js'] }],
      [{ command: 'uvx', args: ['thing'] }],
      ['nope'],
      [{ command: PY, args: ['-I', '-S', 'C:/X/MCP_SERVER.PY'] }],
    ]),
    ...cases('parseCodexGet', parseCodexGet, [
      ['WARNING: something\n{"name":"ide-agent-tabs","transport":{"type":"stdio","command":"node","args":["C:/x/mcp-server.mjs"]}}\n'],
      ['nothing'],
    ]),
    ...cases('samePath', samePath, [
      ['C:\\Users\\A\\.ide-agent-tabs\\mcp\\py\\launch\\mcp_server.py', 'c:/users/a/.ide-agent-tabs/mcp/py/launch/mcp_server.py', 'win32'],
      ['/home/A/x.py', '/home/a/x.py', 'linux'],
      ['/Users/A//x.py', '/users/a/x.py', 'darwin'],
    ]),
    ...cases('parseExtensionList', (s: string) => Object.fromEntries(parseExtensionList(s)), [
      ['ms-python.python@2024.1.0\r\nAlexk413x.ide-agent-tabs@0.8.0\nnot an extension\n  spaced.ext@1.2.3  \n'],
    ]),
    ...cases('fileUrl', fileUrl, [
      ['C:\\Users\\a b\\repo\\updatePlugins.xml', 'win32'],
      ['/home/a/x#y?z%/é.zip', 'linux'],
      ["/home/a/it's (1);[2]&=+$,@!~*.zip", 'darwin'],
    ]),
    ...cases('updatePluginsXml', updatePluginsXml, [['0.8.0', 'file:///C:/a&b/"x".zip']]),
    ...cases('repositoryVersion', repositoryVersion, [
      [updatePluginsXml('1.2.3', 'file:///x.zip')],
      ['<plugins><plugin id="other" version="9"/><plugin version="2" id="dev.alexk.ide-agent-tabs"></plugin></plugins>'],
      ['<plugins/>'],
    ]),
    ...cases('hookMessage', hookMessage, [
      [{ vscode: '0.8.0', jetbrains: '0.8.1' }, ['code', 'cursor'], true],
      [{ vscode: '0.8.0', jetbrains: '0.8.1' }, [], false],
      [{ vscode: '0.8.0', jetbrains: '0.8.1' }, [], true],
    ]),
    ...cases('needsSync', (state: never, bundled: never) => needsSync(state ?? undefined, bundled), [
      [null, { vscode: '1', jetbrains: '1' }],
      [{ vscode: '1', jetbrains: '1', syncedAt: '', failures: 0 }, { vscode: '1', jetbrains: '1' }],
      [{ vscode: '1', jetbrains: '1', syncedAt: '', failures: 2 }, { vscode: '1', jetbrains: '1' }],
      [{ vscode: '1', jetbrains: '1', syncedAt: '', failures: 3 }, { vscode: '1', jetbrains: '1' }],
      [{ vscode: '1', jetbrains: '1', syncedAt: '', failures: 0 }, { vscode: '2', jetbrains: '1' }],
    ]),
    ...yamlCases(),
    ...cases('yamlScalar', (value: string) => editYaml('k: 1\n', 'f', (doc) => setYamlEntry(doc, 'm', value, { [value]: value }, 'f')), YAML_SCALARS.map((s) => [s])),
  ];
  return `${JSON.stringify({ cases: all }, null, 2)}\n`;
}

if (process.argv[1] !== undefined && fileURLToPath(import.meta.url) === process.argv[1]) {
  const text = fixturesText();
  if (process.argv.includes('--check')) {
    const current = existsSync(CONFIG_FIXTURES_FILE) ? readFileSync(CONFIG_FIXTURES_FILE, 'utf8') : '';
    if (current !== text) {
      console.error(`${CONFIG_FIXTURES_FILE} is stale; run node --import tsx scripts/write-config-fixtures.ts`);
      process.exit(1);
    }
  } else {
    mkdirSync(path.dirname(CONFIG_FIXTURES_FILE), { recursive: true });
    writeFileSync(CONFIG_FIXTURES_FILE, text);
    console.log(`Wrote ${CONFIG_FIXTURES_FILE}`);
  }
}

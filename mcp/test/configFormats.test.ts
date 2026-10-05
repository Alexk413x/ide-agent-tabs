import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readTomlTable, tomlTable, withTomlTable } from '../src/tomlTable.js';
import { editYaml, setYamlEntry } from '../src/yamlConfig.js';

const TABLE = tomlTable('mcp_servers', 'ide-agent-tabs', { command: 'node', args: ['C:\\Users\\a\\mcp-server.mjs'] });

test('a TOML table is appended once, replaced in place, and removed back to the original text', () => {
  const original = '# mine\nmodel = "grok-4"\n\n[mcp_servers.other]\ncommand = "x"\n';
  const added = withTomlTable(original, 'f', 'mcp_servers', 'ide-agent-tabs', TABLE)!;
  assert.equal(added, `${original}\n[mcp_servers.ide-agent-tabs]\ncommand = "node"\nargs = ["C:\\\\Users\\\\a\\\\mcp-server.mjs"]\n`);
  assert.equal(withTomlTable(added, 'f', 'mcp_servers', 'ide-agent-tabs', TABLE), undefined);
  assert.deepEqual(readTomlTable(added, 'mcp_servers', 'ide-agent-tabs'), { command: 'node', args: ['C:\\Users\\a\\mcp-server.mjs'] });
  assert.equal(withTomlTable(added, 'f', 'mcp_servers', 'ide-agent-tabs', undefined), original);
  assert.equal(withTomlTable(original, 'f', 'mcp_servers', 'ide-agent-tabs', undefined), undefined);
  assert.equal(withTomlTable(undefined, 'f', 'mcp_servers', 'ide-agent-tabs', TABLE), `${TABLE.join('\n')}\n`);
});

test('a TOML table in the middle keeps its place and the tables around it, with CRLF kept', () => {
  const text = 'a = 1\r\n\r\n[mcp_servers."ide-agent-tabs"]\r\ncommand = "old"\r\n\r\n[mcp_servers.ide-agent-tabs.env]\r\nX = "1"\r\n\r\n# next\r\n[other]\r\nb = 2\r\n';
  const next = withTomlTable(text, 'f', 'mcp_servers', 'ide-agent-tabs', TABLE)!;
  assert.equal(next, `a = 1\r\n\r\n${TABLE.join('\r\n')}\r\n\r\n# next\r\n[other]\r\nb = 2\r\n`);
  assert.equal(withTomlTable(next, 'f', 'mcp_servers', 'ide-agent-tabs', undefined), 'a = 1\r\n\r\n# next\r\n[other]\r\nb = 2\r\n');
  assert.deepEqual(readTomlTable("[mcp_servers.ide-agent-tabs]\ncommand = 'node'\nargs = [\n  'a', # first\n  \"b\",\n]\n", 'mcp_servers', 'ide-agent-tabs'), {
    command: 'node',
    args: ['a', 'b'],
  });
});

test('a TOML file that names the server another way is left for the user', () => {
  for (const text of ['[mcp_servers]\nide-agent-tabs = { command = "x" }\n', 'mcp_servers.ide-agent-tabs.command = "x"\n']) {
    assert.throws(() => withTomlTable(text, 'f', 'mcp_servers', 'ide-agent-tabs', TABLE), /edit it by hand/);
  }
  assert.ok(withTomlTable('# ide-agent-tabs = old note\nargs = ["/x/.ide-agent-tabs/y"]\n', 'f', 'mcp_servers', 'ide-agent-tabs', TABLE));
});

test('a YAML edit keeps comments and other keys, and an unchanged result writes nothing', () => {
  const original = '# my config\nmodel: x # pinned\nmcp_servers:\n  other:\n    command: foo\n';
  const added = editYaml(original, 'f', (doc) => setYamlEntry(doc, 'mcp_servers', 'ide-agent-tabs', { command: 'node', args: ['C:\\a b\\s.mjs'] }, 'f'))!;
  assert.match(added, /^# my config\nmodel: x # pinned\n/);
  assert.match(added, /ide-agent-tabs:\n {4}command: node\n {4}args:\n {6}- C:\\a b\\s\.mjs\n/);
  assert.equal(editYaml(added, 'f', (doc) => setYamlEntry(doc, 'mcp_servers', 'ide-agent-tabs', { command: 'node', args: ['C:\\a b\\s.mjs'] }, 'f')), undefined);
  assert.equal(editYaml(added, 'f', (doc) => setYamlEntry(doc, 'mcp_servers', 'ide-agent-tabs', undefined, 'f')), original);
  assert.equal(editYaml('mcp_servers:\n', 'f', (doc) => setYamlEntry(doc, 'mcp_servers', 's', { a: 1 }, 'f')), 'mcp_servers:\n  s:\n    a: 1\n');
  assert.throws(() => editYaml('mcp_servers: [1]\n', 'f', (doc) => setYamlEntry(doc, 'mcp_servers', 's', {}, 'f')), /isn't a mapping/);
  assert.throws(() => editYaml('a: [\n', 'f', () => undefined), /isn't valid YAML/);
  assert.throws(() => editYaml('- 1\n', 'f', () => undefined), /doesn't hold a YAML mapping/);
});

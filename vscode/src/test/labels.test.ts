import assert from 'node:assert/strict';
import { test } from 'node:test';
import { agentLink, commandKey, escapeMarkdown, statusTooltip } from '../labels';

test('command keys camel-case a profile name', () => {
  assert.equal(commandKey('claude'), 'claude');
  assert.equal(commandKey('codex-lmstudio'), 'codexLmstudio');
  assert.equal(commandKey('prime-agent'), 'primeAgent');
});

test('markdown escaping covers link and emphasis characters', () => {
  assert.equal(escapeMarkdown('Codex - LM Studio'), 'Codex \\- LM Studio');
  assert.equal(escapeMarkdown('[a](b)*_`#|<>!.'), '\\[a\\]\\(b\\)\\*\\_\\`\\#\\|\\<\\>\\!\\.');
});

test('an agent link opens the agent by name and shows its logos', () => {
  const args = encodeURIComponent(JSON.stringify(['ori:claude']));
  assert.equal(agentLink('ori:claude', 'Claude Code', undefined, undefined), `[Claude Code](command:ideAgentTabs.openAgent?${args})`);
  assert.equal(
    agentLink('ori:claude', 'Claude Code', 'file:///a.svg', 'file:///o.svg'),
    `<img src="file:///a.svg" width="16" height="16" align="absmiddle"> [Claude Code](command:ideAgentTabs.openAgent?${args})` +
      ' <img src="file:///o.svg" width="16" height="16" align="absmiddle"> via OpenRouter',
  );
});

test('the status tooltip lists the agents, the missing ones and the settings link', () => {
  assert.equal(
    statusTooltip(['a', 'b'], ['Pi', 'Grok.Build'], false),
    'Open an agent in an editor tab.  \nNew tab: **Ctrl+Alt+A**\n\na  \nb\n\nNot installed: Pi, Grok\\.Build\n\n[$(gear) Settings](command:ideAgentTabs.openSettings)',
  );
  assert.equal(statusTooltip([], [], true), 'No agent CLI found on PATH.\n\n[$(gear) Settings](command:ideAgentTabs.openSettings)');
  assert.ok(statusTooltip(['a'], [], true).includes('**⌘⌥A**'));
});

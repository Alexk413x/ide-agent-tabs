import assert from 'node:assert/strict';
import { test } from 'node:test';
import { serverInstructions } from '../src/server.js';

const CLAUDE_CODE_LIMIT = 2048;

test('the server instructions fit Claude Code\'s limit with every part on, and name the tab tools first', () => {
  for (const [jev, messaging] of [[true, true], [true, false], [false, true], [false, false]] as const) {
    const text = serverInstructions(jev, messaging);
    assert.ok(text.length <= CLAUDE_CODE_LIMIT, `jev ${jev}, messaging ${messaging}: ${text.length} characters`);
    assert.match(text.split('\n')[0]!, /open_tab.*close_tab/);
  }
});

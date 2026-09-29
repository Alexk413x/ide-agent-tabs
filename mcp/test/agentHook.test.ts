import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { deliver, newMessageId, takeMessages } from '../src/messaging/mailbox.js';
import { HOOK_EVENTS, runHook } from '../src/messaging/hook.js';
import { presencePath, readPresence } from '../src/messaging/sessions.js';
import { tempDir } from './tempDir.js';

const ID = 'tab-hook-1';

function hook(home: string, cli: string, event: string, input: Record<string, unknown> = {}) {
  return runHook({ cli, event, input, home, sessionId: ID });
}

async function mail(home: string) {
  await deliver(home, { id: newMessageId(), from: { id: 'abcdef0123456', agent: 'codex', path: '/w' }, to: ID, text: 'secret text', sentAt: new Date().toISOString() });
}

const state = async (home: string) => (await readPresence(home, ID))?.state;
const REMINDER = 'Agent Tabs: 1 unread message from codex abcdef01; call read_messages.';

test('hooks set busy, permission and idle for each CLI', async () => {
  const cases: [string, string, Record<string, unknown>, string][] = [
    ['claude', 'UserPromptSubmit', {}, 'busy'],
    ['claude', 'Notification', { notification_type: 'permission_prompt' }, 'permission'],
    ['claude', 'PostToolUse', {}, 'busy'],
    ['claude', 'Notification', { notification_type: 'idle_prompt' }, 'idle'],
    ['claude', 'Stop', { stop_hook_active: false }, 'idle'],
    ['codex', 'UserPromptSubmit', {}, 'busy'],
    ['codex', 'PermissionRequest', {}, 'permission'],
    ['codex', 'PostToolUse', {}, 'busy'],
    ['codex', 'Stop', {}, 'idle'],
    ['gemini', 'BeforeAgent', {}, 'busy'],
    ['gemini', 'Notification', { notification_type: 'ToolPermission' }, 'permission'],
    ['gemini', 'AfterTool', {}, 'busy'],
    ['gemini', 'AfterAgent', {}, 'idle'],
    ['copilot', 'userPromptSubmitted', {}, 'busy'],
    ['copilot', 'notification', { notificationType: 'permission_prompt' }, 'permission'],
    ['copilot', 'postToolUse', {}, 'busy'],
    ['copilot', 'agentStop', {}, 'idle'],
  ];
  const home = tempDir('iat-hook-');
  for (const [cli, event, input, want] of cases) {
    assert.equal(await hook(home, cli, event, input), undefined, `${cli} ${event} prints nothing without mail`);
    assert.equal(await state(home), want, `${cli} ${event}`);
  }
  await hook(home, 'claude', 'Notification', { notification_type: 'auth_success' });
  assert.equal(await state(home), 'idle', 'other notifications leave the state alone');
});

test('each CLI gets the reminder in its own context format after a prompt and a tool call', async () => {
  const home = tempDir('iat-hook-');
  await mail(home);
  const context = (event: string) => ({ hookSpecificOutput: { hookEventName: event, additionalContext: REMINDER } });
  assert.deepEqual(await hook(home, 'claude', 'UserPromptSubmit'), context('UserPromptSubmit'));
  assert.deepEqual(await hook(home, 'claude', 'PostToolUse'), context('PostToolUse'));
  assert.equal(await hook(home, 'codex', 'PreToolUse'), undefined);
  assert.deepEqual(await hook(home, 'codex', 'UserPromptSubmit'), context('UserPromptSubmit'));
  assert.deepEqual(await hook(home, 'codex', 'PostToolUse'), context('PostToolUse'));
  assert.deepEqual(await hook(home, 'gemini', 'BeforeAgent'), context('BeforeAgent'));
  assert.deepEqual(await hook(home, 'gemini', 'AfterTool'), context('AfterTool'));
  assert.equal(await hook(home, 'copilot', 'userPromptSubmitted'), undefined, "Copilot CLI drops a prompt hook's output");
  assert.deepEqual(await hook(home, 'copilot', 'postToolUse'), { additionalContext: REMINDER });
  assert.doesNotMatch(JSON.stringify(await hook(home, 'claude', 'PostToolUse')), /secret text/);
});

test('turn end blocks at most three times in a row, and a prompt or a read resets the count', async () => {
  const home = tempDir('iat-hook-');
  await mail(home);
  const block = { decision: 'block', reason: `${REMINDER} Read them before you end your turn.` };
  for (let i = 1; i <= 3; i++) {
    assert.deepEqual(await hook(home, 'claude', 'Stop', { stop_hook_active: i > 1 }), block, `block ${i}`);
    assert.equal(await state(home), 'busy');
    assert.equal((await readPresence(home, ID))!.nudges, i);
  }
  assert.equal(await hook(home, 'claude', 'Stop', { stop_hook_active: true }), undefined);
  assert.equal(await state(home), 'idle');

  await hook(home, 'claude', 'UserPromptSubmit');
  assert.equal((await readPresence(home, ID))!.nudges, 0);
  assert.deepEqual(await hook(home, 'codex', 'Stop'), block);
  assert.deepEqual(await hook(home, 'gemini', 'AfterAgent'), { decision: 'deny', reason: block.reason });
  assert.deepEqual(await hook(home, 'copilot', 'agentStop'), block);

  await takeMessages(home, ID);
  assert.equal(await hook(home, 'claude', 'Stop'), undefined);
  assert.equal((await readPresence(home, ID))!.nudges, 0);
});

test('a hook without a session id, or for an unknown CLI or event, does nothing', async () => {
  const home = tempDir('iat-hook-');
  assert.equal(await runHook({ cli: 'claude', event: 'Stop', input: {}, home, sessionId: undefined }), undefined);
  assert.equal(await runHook({ cli: 'claude', event: 'Stop', input: {}, home, sessionId: '../x' }), undefined);
  assert.equal(await hook(home, 'vim', 'Stop'), undefined);
  assert.equal(await hook(home, 'claude', 'SessionEnd'), undefined);
  assert.ok(!existsSync(presencePath(home, ID)));
  assert.deepEqual(Object.keys(HOOK_EVENTS).sort(), ['claude', 'codex', 'copilot', 'gemini']);
});

test('the hook script reads stdin, prints one JSON line, and exits 0 even on bad input', async () => {
  const home = tempDir('iat-hook-');
  await mail(home);
  const script = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', 'src', 'agentHook.ts');
  const run = (args: string[], input: string, env: NodeJS.ProcessEnv) =>
    spawnSync(process.execPath, ['--import', 'tsx', script, ...args], { input, encoding: 'utf8', env: { ...process.env, IDE_AGENT_TABS_HOME: home, ...env } });
  const blocked = run(['claude', 'Stop'], '{"stop_hook_active":false}', { IDE_AGENT_TABS_ID: ID });
  assert.equal(blocked.status, 0);
  assert.equal(JSON.parse(blocked.stdout).decision, 'block');
  const garbage = run(['claude', 'UserPromptSubmit'], 'not json', { IDE_AGENT_TABS_ID: ID });
  assert.equal(garbage.status, 0);
  assert.equal(JSON.parse(garbage.stdout).hookSpecificOutput.additionalContext, REMINDER);
  const noId = run(['claude', 'Stop'], '{}', { IDE_AGENT_TABS_ID: '' });
  assert.deepEqual([noId.status, noId.stdout], [0, '']);
});

import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { deliver, newMessageId, takeMessages } from '../src/messaging/mailbox.js';
import { HOOK_EVENTS, runHook } from '../src/messaging/hook.js';
import { presencePath, readPresence, updatePresence } from '../src/messaging/sessions.js';
import { tempDir } from './tempDir.js';

const ID = 'tab-hook-1';

function hook(home: string, cli: string, event: string, input: Record<string, unknown> = {}) {
  return runHook({ cli, event, input, home, sessionId: ID });
}

async function mail(home: string) {
  await deliver(home, { id: newMessageId(), from: { id: 'abcdef0123456', agent: 'codex', path: '/w' }, to: ID, text: 'secret text', sentAt: new Date().toISOString() });
}

const state = async (home: string) => (await readPresence(home, ID))?.state;
const REMINDER = 'Agent Tabs: 1 unread message from codex abcdef01. read_messages returns it.';

test('hooks set busy, permission and idle for each CLI', async () => {
  const cases: [string, string, Record<string, unknown>, string][] = [
    ['claude', 'SessionStart', { source: 'startup' }, 'idle'],
    ['claude', 'UserPromptSubmit', {}, 'busy'],
    ['claude', 'Notification', { notification_type: 'permission_prompt' }, 'permission'],
    ['claude', 'PostToolUse', {}, 'busy'],
    ['claude', 'Notification', { notification_type: 'idle_prompt' }, 'idle'],
    ['claude', 'Stop', { stop_hook_active: false }, 'idle'],
    ['claude', 'PostToolUseFailure', { is_interrupt: false }, 'busy'],
    ['claude', 'PostToolUseFailure', { is_interrupt: true }, 'idle'],
    ['claude', 'UserPromptSubmit', {}, 'busy'],
    ['claude', 'StopFailure', { error: 'rate_limit' }, 'idle'],
    ['codex', 'UserPromptSubmit', {}, 'busy'],
    ['codex', 'PermissionRequest', {}, 'permission'],
    ['codex', 'PostToolUse', {}, 'busy'],
    ['codex', 'Stop', {}, 'idle'],
    ['codex', 'UserPromptSubmit', {}, 'busy'],
    ['codex', 'Interrupt', {}, 'idle'],
    ['gemini', 'BeforeAgent', {}, 'busy'],
    ['gemini', 'Notification', { notification_type: 'ToolPermission' }, 'permission'],
    ['gemini', 'AfterTool', {}, 'busy'],
    ['gemini', 'AfterAgent', {}, 'idle'],
    ['copilot', 'sessionStart', { source: 'new' }, 'idle'],
    ['copilot', 'userPromptSubmitted', {}, 'busy'],
    ['copilot', 'notification', { notificationType: 'permission_prompt' }, 'permission'],
    ['copilot', 'postToolUse', {}, 'busy'],
    ['copilot', 'agentStop', {}, 'idle'],
    ['agy', 'PreInvocation', { invocationNum: 0 }, 'busy'],
    ['agy', 'Stop', { fullyIdle: true, terminationReason: 'NO_TOOL_CALL' }, 'idle'],
    ['agy', 'PostToolUse', {}, 'busy'],
    ['agy', 'Stop', { fullyIdle: true }, 'idle'],
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
  const context = (event: string) => ({ hookSpecificOutput: { hookEventName: event, additionalContext: REMINDER } });
  const cases: [string, string, object | undefined][] = [
    ['claude', 'SessionStart', context('SessionStart')],
    ['claude', 'UserPromptSubmit', context('UserPromptSubmit')],
    ['claude', 'PostToolUse', context('PostToolUse')],
    ['codex', 'PreToolUse', undefined],
    ['codex', 'UserPromptSubmit', context('UserPromptSubmit')],
    ['codex', 'PostToolUse', context('PostToolUse')],
    ['gemini', 'BeforeAgent', context('BeforeAgent')],
    ['gemini', 'AfterTool', context('AfterTool')],
    ['copilot', 'userPromptSubmitted', undefined],
    ['copilot', 'postToolUse', { additionalContext: REMINDER }],
    ['agy', 'PreInvocation', { injectSteps: [{ ephemeralMessage: REMINDER }] }],
    ['agy', 'PostToolUse', undefined],
  ];
  for (const [cli, event, want] of cases) {
    const home = tempDir('iat-hook-');
    await mail(home);
    const got = await hook(home, cli, event);
    assert.deepEqual(got, want, `${cli} ${event}`);
    assert.doesNotMatch(JSON.stringify(got ?? {}), /secret text/);
  }
});

test('a message is reminded once, and a new message brings a new reminder', async () => {
  const home = tempDir('iat-hook-');
  await mail(home);
  const context = (text: string) => ({ hookSpecificOutput: { hookEventName: 'PostToolUse', additionalContext: text } });
  assert.deepEqual(await hook(home, 'claude', 'PostToolUse'), context(REMINDER));
  assert.equal(await hook(home, 'claude', 'PostToolUse'), undefined);
  assert.equal(await hook(home, 'claude', 'UserPromptSubmit'), undefined);
  await mail(home);
  assert.deepEqual(await hook(home, 'claude', 'PostToolUse'), context('Agent Tabs: 2 unread messages from codex abcdef01. read_messages returns them.'));
  assert.equal(await hook(home, 'claude', 'PostToolUse'), undefined);
  await takeMessages(home, ID);
  await hook(home, 'claude', 'PostToolUse');
  assert.equal((await readPresence(home, ID))!.reminded, undefined);
});

test('turn end blocks at most three times in a row, and a prompt or a read resets the count', async () => {
  const home = tempDir('iat-hook-');
  await mail(home);
  const block = { decision: 'block', reason: `Agent Tabs kept this turn open. ${REMINDER}` };
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
  await hook(home, 'agy', 'PreInvocation', { invocationNum: 0 });
  assert.deepEqual(await hook(home, 'agy', 'Stop'), { decision: 'continue', reason: block.reason });

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
  assert.deepEqual(Object.keys(HOOK_EVENTS).sort(), ['agy', 'claude', 'codex', 'copilot', 'gemini']);
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

test('a headless agent started inside a tab cannot change the tab session', async () => {
  const home = tempDir('iat-hook-');
  await hook(home, 'claude', 'SessionStart', { source: 'startup', session_id: 'tab-agent' });
  await hook(home, 'claude', 'UserPromptSubmit', { session_id: 'tab-agent' });
  await mail(home);
  for (const event of ['SessionStart', 'UserPromptSubmit', 'PostToolUse', 'Stop']) {
    assert.equal(await hook(home, 'claude', event, { source: 'startup', session_id: 'child' }), undefined, event);
  }
  assert.equal(await state(home), 'busy');
  assert.equal((await readPresence(home, ID))?.nudges ?? 0, 0);
  assert.equal((await hook(home, 'claude', 'Stop', { session_id: 'tab-agent' }) as { decision?: string })?.decision, 'block');
});

test('a clear or resume in the tab hands the session to the new agent session id', async () => {
  const home = tempDir('iat-hook-');
  await hook(home, 'claude', 'UserPromptSubmit', { session_id: 'first' });
  await hook(home, 'claude', 'SessionStart', { source: 'clear', session_id: 'second' });
  assert.equal(await state(home), 'idle');
  await hook(home, 'claude', 'UserPromptSubmit', { session_id: 'second' });
  assert.equal(await state(home), 'busy');
  await hook(home, 'claude', 'Stop', { session_id: 'first' });
  assert.equal(await state(home), 'busy');
});

test('an Antigravity CLI turn resets the turn-end count on its first model call only', async () => {
  const home = tempDir('iat-hook-');
  await mail(home);
  await hook(home, 'agy', 'Stop');
  await hook(home, 'agy', 'PreInvocation', { invocationNum: 3 });
  assert.equal((await readPresence(home, ID))!.nudges, 1, 'a later model call in the same turn keeps the count');
  await hook(home, 'agy', 'PreInvocation', { invocationNum: 0 });
  assert.equal((await readPresence(home, ID))!.nudges, 0);
});

test('a child agent of the same CLI is refused while the tab is in a turn, for every CLI', async () => {
  const cases: [string, string, string, Record<string, unknown>, Record<string, unknown>][] = [
    ['codex', 'UserPromptSubmit', 'Stop', { session_id: 'tab' }, { session_id: 'child' }],
    ['agy', 'PreInvocation', 'Stop', { conversationId: 'tab', invocationNum: 0 }, { conversationId: 'child' }],
    ['gemini', 'BeforeAgent', 'AfterAgent', { session_id: 'tab' }, { session_id: 'child' }],
  ];
  for (const [cli, prompt, stop, own, child] of cases) {
    const home = tempDir('iat-hook-');
    await hook(home, cli, prompt, own);
    await mail(home);
    assert.equal(await hook(home, cli, stop, child), undefined, cli);
    assert.equal(await state(home), 'busy', `${cli}: the child's turn end leaves the tab busy`);
    assert.equal((await readPresence(home, ID))?.nudges ?? 0, 0, `${cli}: the child uses none of the tab's nudges`);
  }
});

test('a new session in an idle tab takes it over without a start hook, as after /clear or /new', async () => {
  const home = tempDir('iat-hook-');
  await hook(home, 'agy', 'PreInvocation', { conversationId: 'first', invocationNum: 0 });
  await hook(home, 'agy', 'Stop', { conversationId: 'first' });
  assert.equal(await state(home), 'idle');
  await hook(home, 'agy', 'PreInvocation', { conversationId: 'second', invocationNum: 0 });
  assert.equal(await state(home), 'busy');
  assert.equal((await readPresence(home, ID))?.owner, 'second');
  await hook(home, 'agy', 'Stop', { conversationId: 'first' });
  assert.equal(await state(home), 'busy', 'the old session no longer counts');
});

test('a hook from another CLI than the tab agent is ignored', async () => {
  const home = tempDir('iat-hook-');
  await updatePresence(home, ID, () => ({ id: ID, agent: 'codex', state: 'busy', stateAt: new Date().toISOString() }));
  assert.equal(await hook(home, 'claude', 'Stop', { session_id: 'child' }), undefined);
  assert.equal(await hook(home, 'claude', 'SessionStart', { source: 'startup', session_id: 'child' }), undefined);
  assert.equal(await state(home), 'busy');
});

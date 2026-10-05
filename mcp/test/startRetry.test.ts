import assert from 'node:assert/strict';
import { test } from 'node:test';
import { Messaging } from '../src/messaging/messaging.js';
import { tempDir } from './tempDir.js';

class FailingStart extends Messaging {
  attempts = 0;

  constructor(
    home: string,
    private readonly failures: number,
  ) {
    super({ home, env: { IDE_AGENT_TABS_ID: 'tab-aaaa-1' }, pid: 101, cwd: '/work/a', hosts: { findHost: async () => undefined, typeInto: async () => ({ ok: true }) }, isAlive: () => true, sleep: async () => undefined });
  }

  override async start(): Promise<void> {
    this.attempts++;
    if (this.attempts <= this.failures) throw new Error('timed out waiting for the presence lock');
    await super.start();
  }
}

test('a start that fails every attempt is logged and reported by list_sessions, send and read', async () => {
  const messaging = new FailingStart(tempDir('iat-start-fail-'), Infinity);
  const logged: string[] = [];
  await messaging.startRegistered({ delaysMs: [1, 2], log: (m) => logged.push(m) });
  await messaging.registration;
  assert.equal(messaging.attempts, 3);
  assert.deepEqual(logged, Array(3).fill("this session isn't registered: timed out waiting for the presence lock"));

  const warning = ["this session isn't registered: timed out waiting for the presence lock"];
  const listed = await messaging.listSessions();
  assert.deepEqual(listed.sessions, []);
  assert.deepEqual(listed.warnings, warning);
  assert.deepEqual((await messaging.read()).warnings, warning);
  await assert.rejects(messaging.send({ to: 'tab-bbbb-2', text: 'hi' }), /no live session with id or name tab-bbbb-2; call list_sessions\. Warning: this session isn't registered: timed out/);
});

test('a start that succeeds on a retry registers the session and drops the warning', async () => {
  const messaging = new FailingStart(tempDir('iat-start-retry-'), 2);
  const logged: string[] = [];
  await messaging.startRegistered({ delaysMs: [1, 2, 3], log: (m) => logged.push(m) });
  await messaging.registration;
  assert.equal(messaging.attempts, 3);
  assert.equal(logged.length, 2);
  const listed = await messaging.listSessions();
  assert.equal(listed.warnings, undefined);
  assert.deepEqual(listed.sessions.map((s) => [s.id, s.self]), [['tab-aaaa-1', true]]);
  messaging.stopSync();
});

test('a start that succeeds first time makes no retry and no warning', async () => {
  const messaging = new FailingStart(tempDir('iat-start-ok-'), 0);
  await messaging.startRegistered({ delaysMs: [1] });
  await messaging.registration;
  assert.equal(messaging.attempts, 1);
  assert.equal((await messaging.listSessions()).warnings, undefined);
  messaging.stopSync();
});

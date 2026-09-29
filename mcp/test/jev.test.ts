import assert from 'node:assert/strict';
import { readFileSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { after, before, beforeEach, test } from 'node:test';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js';
import { ledgerPath } from '../src/jev/ledger.js';
import { DATA_NOTE, Jev, startJev, type JevDeps } from '../src/jev/service.js';
import { parseJevSettings, type JevSettings } from '../src/jev/settings.js';
import { resolveSettings } from '../src/profiles.js';
import { createServer } from '../src/server.js';
import { Service } from '../src/service.js';
import { defaultAnswers, FAKE_KEY, FAKE_MODEL, FakeTypeSafe, ok } from './fakeTypeSafe.js';
import { tempDir } from './tempDir.js';

const fake = new FakeTypeSafe();
const SECRET_STATE = 'state-marker-7f3a do not log me';

before(() => fake.start());
after(() => fake.stop());
beforeEach(() => fake.reset());

const installed = [
  { name: 'claude', installed: true },
  { name: 'codex', installed: true },
  { name: 'gemini', installed: false },
];

function makeJev(over: Omit<Partial<JevDeps>, 'settings'> & { settings?: Partial<JevSettings> } = {}): { jev: Jev; home: string } {
  const home = over.home ?? tempDir('iat-jev-');
  const jev = new Jev({
    home,
    platform: 'linux',
    profiles: async () => installed,
    runCommand: async () => {
      throw new Error('tests never read a real credential store');
    },
    ...over,
    env: { TYPESAFE_API_KEY: FAKE_KEY, TYPESAFE_BASE_URL: fake.url, ...over.env },
    settings: { ...parseJevSettings({ enabled: true }), ...over.settings },
  });
  return { jev, home };
}

function ledger(home: string): Record<string, unknown>[] {
  return readFileSync(ledgerPath(home), 'utf8')
    .trim()
    .split('\n')
    .map((l) => JSON.parse(l));
}

function choiceReply(probabilities: Record<string, number>) {
  const [top] = Object.entries(probabilities).sort((a, b) => b[1] - a[1])[0]!;
  return () => ok({ pick: { type: 'choice', choice: top, confidence: probabilities[top], probabilities } });
}

test('jev settings default to off, and a bad value is a warning that turns Jev off', () => {
  assert.deepEqual(parseJevSettings(undefined), { enabled: false, sure: 0.85, tiers: {}, pricePerMillionInput: 0.042 });
  assert.deepEqual(parseJevSettings({ enabled: true, sure: 0.9, tiers: { 'claude:haiku': 'Small edits' }, pricePerMillionInput: 0.05 }), {
    enabled: true,
    sure: 0.9,
    tiers: { 'claude:haiku': 'Small edits' },
    pricePerMillionInput: 0.05,
  });
  const good = resolveSettings(undefined, JSON.stringify({ defaultAgent: 'codex', jev: { enabled: true } }));
  assert.equal(good.jev.enabled, true);
  assert.deepEqual(good.warnings, []);
  for (const jev of [{ enabled: 'yes' }, { enabled: true, sure: 2 }, { enabled: true, tiers: { 'bad name': 'x' } }, { enabled: true, tiers: { codex: '' } }, { enabled: true, pricePerMillionInput: -1 }, 'on']) {
    const s = resolveSettings(undefined, JSON.stringify({ defaultAgent: 'codex', jev }), 'agents.json', '/h/config.json');
    assert.equal(s.jev.enabled, false, JSON.stringify(jev));
    assert.equal(s.defaultAgent.name, 'codex', 'the rest of config.json still applies');
    assert.match(s.warnings[0]!, /^Ignoring jev in \/h\/config\.json, so Jev is off: /);
  }
});

test('jev_choose adds a none option, keeps the caller order, and says the state is data', async () => {
  const { jev } = makeJev();
  const reply = await jev.choose({
    instruction: 'Which file handles login?',
    options: [
      { id: 'b.ts', description: 'Session storage' },
      { id: 'a.ts', description: 'The login form and its handler' },
    ],
    state: SECRET_STATE,
  });
  const q = fake.seen[0]!.body.questions.pick!;
  assert.equal(q.type, 'choice');
  assert.deepEqual(Object.keys(q.criteria as object), ['b.ts', 'a.ts', 'none']);
  assert.ok(String(q.instructions).startsWith('Which file handles login? '));
  assert.ok(String(q.instructions).includes(DATA_NOTE));
  assert.equal(fake.seen[0]!.body.state, SECRET_STATE);
  assert.equal(reply.model, FAKE_MODEL);
  assert.equal(reply.choice, 'b.ts');
  assert.equal(reply.band, 'sure');
  assert.deepEqual(Object.keys(reply.probabilities), ['b.ts', 'a.ts', 'none']);
  assert.equal(reply.cost_usd, 0.000042);

  await jev.choose({ instruction: 'Pick', options: [{ id: 'x', description: 'X' }, { id: 'y', description: 'Y' }], no_match: false });
  assert.deepEqual(Object.keys(fake.seen[1]!.body.questions.pick!.criteria as object), ['x', 'y']);
  assert.equal(fake.seen[1]!.body.state, '');
});

test('jev_choose bands: sure at or above jev.sure, unsure below, no-match on none, with the runner-up', async () => {
  const { jev } = makeJev({ settings: { sure: 0.8 } });
  const options = [
    { id: 'a', description: 'A' },
    { id: 'b', description: 'B' },
  ];
  fake.respond = choiceReply({ a: 0.8, b: 0.15, none: 0.05 });
  assert.deepEqual((({ band, choice, runner_up }) => ({ band, choice, runner_up }))(await jev.choose({ instruction: 'q', options })), {
    band: 'sure',
    choice: 'a',
    runner_up: 'b',
  });
  fake.respond = choiceReply({ a: 0.3, b: 0.6, none: 0.1 });
  const unsure = await jev.choose({ instruction: 'q', options });
  assert.equal(unsure.band, 'unsure');
  assert.equal(unsure.choice, 'b');
  assert.equal(unsure.runner_up, 'a');
  fake.respond = choiceReply({ a: 0.05, b: 0.05, none: 0.9 });
  const none = await jev.choose({ instruction: 'q', options });
  assert.equal(none.band, 'no-match');
  assert.equal(none.choice, 'none');

  await assert.rejects(jev.choose({ instruction: 'q', options: [{ id: 'none', description: 'N' }] }), /reserved for no match/);
  await assert.rejects(jev.choose({ instruction: 'q', options: [options[0]!, options[0]!] }), /Two options have the id "a"/);
});

test('the server refuses oversized or malformed requests before it calls the API', async () => {
  const { jev, home } = makeJev();
  const labels = (n: number) => Object.fromEntries(Array.from({ length: n }, (_, i) => [`l${i}`, `level ${i}`]));
  const refusals: [Promise<unknown>, RegExp][] = [
    [jev.ask({ state: 's', questions: { c: { type: 'choice', criteria: labels(256) } } }), /256 options; Jev takes 2 to 255/],
    [jev.ask({ state: 's', questions: { c: { type: 'choice', criteria: labels(1) } } }), /1 options; Jev takes 2 to 255/],
    [jev.ask({ state: 's', questions: { s: { type: 'score', criteria: ['low'] as never } } }), /1 levels; Jev takes 2 to 10/],
    [jev.ask({ state: 's', questions: { s: { type: 'score', criteria: Array(11).fill('x') as never } } }), /11 levels/],
    [jev.ask({ state: 'x'.repeat(200_001), questions: { n: { type: 'noul' } } }), /at most 200000/],
    [jev.ask({ state: 's', questions: {} }), /at least one question/],
    [jev.choose({ instruction: 'q', options: Object.entries(labels(255)).map(([id, description]) => ({ id, description })) }), /256 options/],
    [jev.rank({ query: 'q', items: Array.from({ length: 256 }, (_, i) => ({ id: `i${i}`, text: 't' })) }), /at most 255 items/],
  ];
  for (const [call, message] of refusals) await assert.rejects(call, message);
  assert.equal(fake.seen.length, 0);
  assert.throws(() => readFileSync(ledgerPath(home)), /ENOENT/);
});

test('jev_check asks one noul per condition in one request', async () => {
  const { jev } = makeJev();
  let n = 0;
  fake.respond = (body) => ok(defaultAnswers(body, () => [0.9, 0.1][n++]!));
  const reply = await jev.check({
    state: SECRET_STATE,
    conditions: [
      { id: 'deletes', question: 'Does the command delete files?' },
      { id: 'network', question: 'Does the command reach the network?' },
    ],
  });
  assert.equal(fake.seen.length, 1);
  const { questions } = fake.seen[0]!.body;
  assert.deepEqual(Object.keys(questions), ['deletes', 'network']);
  for (const q of Object.values(questions)) {
    assert.equal(q.type, 'noul');
    assert.ok(String(q.instructions).includes(DATA_NOTE));
  }
  assert.deepEqual(reply.conditions, [
    { id: 'deletes', probability: 0.9 },
    { id: 'network', probability: 0.1 },
  ]);
});

test('jev_rank sends every item in one request keyed by id, and orders the reply by probability', async () => {
  const { jev } = makeJev();
  const relevance: Record<string, number> = { c: 0.2, a: 0.7, b: 0.95 };
  fake.respond = (body) => ok(Object.fromEntries(Object.keys(body.questions).map((id) => [id, { type: 'noul', noul: relevance[id] }])));
  const items = [
    { id: 'c', text: 'third' },
    { id: 'a', text: 'first' },
    { id: 'b', text: 'second' },
  ];
  const reply = await jev.rank({ query: 'login bug', items });
  assert.equal(fake.seen.length, 1);
  assert.deepEqual(fake.seen[0]!.body.state, { query: 'login bug', items: { a: 'first', b: 'second', c: 'third' } });
  assert.deepEqual(Object.keys(fake.seen[0]!.body.questions), ['a', 'b', 'c']);
  assert.deepEqual(reply.items, [
    { id: 'b', probability: 0.95 },
    { id: 'a', probability: 0.7 },
    { id: 'c', probability: 0.2 },
  ]);
  assert.deepEqual((await jev.rank({ query: 'login bug', items, top: 1 })).items, [{ id: 'b', probability: 0.95 }]);
});

test('jev_route offers only tiers whose profile is installed, sorted by name', async () => {
  const tiers = { codex: 'A second opinion', 'claude:opus': 'Design across many files', gemini: 'Large context reading', 'claude:haiku': 'Small edits' };
  const { jev } = makeJev({ settings: { tiers } });
  fake.respond = choiceReply({ 'claude:haiku': 0.1, 'claude:opus': 0.7, codex: 0.2 });
  const reply = await jev.route({ task: 'Redesign the routing module across twelve files.' });
  const q = fake.seen[0]!.body.questions.pick!;
  assert.deepEqual(q.criteria, { 'claude:haiku': 'Small edits', 'claude:opus': 'Design across many files', codex: 'A second opinion' });
  assert.equal(fake.seen[0]!.body.state, 'Redesign the routing module across twelve files.');
  assert.equal(reply.tier, 'claude:opus');
  assert.equal(reply.runner_up, 'codex');
  assert.equal(reply.band, 'unsure');
  assert.deepEqual(reply.skipped, ['gemini']);
});

test('jev_route explains a missing or unusable tier list, and skips the call for a single tier', async () => {
  await assert.rejects(makeJev().jev.route({ task: 't' }), /Add jev\.tiers to .*config\.json/);
  await assert.rejects(makeJev({ settings: { tiers: { gemini: 'x' } } }).jev.route({ task: 't' }), /No configured Jev tier has its agent installed \(gemini\)/);
  const single = await makeJev({ settings: { tiers: { codex: 'x', gemini: 'y' } } }).jev.route({ task: 't' });
  assert.deepEqual(single, { tier: 'codex', runner_up: null, band: 'sure', note: 'Only one configured tier has its agent installed, so Jev was not asked.', skipped: ['gemini'] });
  assert.equal(fake.seen.length, 0);
});

test('the ledger records each call without state, questions, answers or the key, and jev_status sums today', async () => {
  const { jev, home } = makeJev({ env: { IDE_AGENT_TABS_AGENT: 'codex', IDE_AGENT_TABS_ID: 'wt-3' } });
  await jev.ask({ state: SECRET_STATE, questions: { secret: { type: 'noul', instructions: 'Is question-marker-99 here?' } } });
  fake.respond = () => ({ status: 401, json: { error: `bad key ${FAKE_KEY}` } });
  await assert.rejects(jev.check({ state: SECRET_STATE, conditions: [{ id: 'x', question: 'q' }] }), /HTTP 401/);

  const lines = ledger(home);
  assert.equal(lines.length, 2);
  assert.deepEqual(Object.keys(lines[0]!), ['at', 'tool', 'agent', 'tab', 'model', 'questions', 'input_tokens', 'ok']);
  assert.deepEqual({ ...lines[0], at: undefined }, { at: undefined, tool: 'jev_ask', agent: 'codex', tab: 'wt-3', model: FAKE_MODEL, questions: 1, input_tokens: 1000, ok: true });
  assert.deepEqual({ ...lines[1], at: undefined }, { at: undefined, tool: 'jev_check', agent: 'codex', tab: 'wt-3', model: 'jev-latest', questions: 1, input_tokens: 0, ok: false, status: 401 });
  const text = readFileSync(ledgerPath(home), 'utf8');
  for (const secret of ['state-marker-7f3a', 'question-marker-99', FAKE_KEY, 'secret']) assert.ok(!text.includes(secret), secret);

  writeFileSync(ledgerPath(home), `${text}${JSON.stringify({ ...lines[0], at: '2020-01-01T00:00:00.000Z', model: 'jev-0.1' })}\nnot json\n`);
  const status = await jev.status();
  assert.deepEqual({ ...status, ledger: undefined }, {
    key: 'env',
    model: 'jev-0.1',
    today: { calls: 2, failed: 1, input_tokens: 1000, cost_usd: 0.000042 },
    sure: 0.85,
    tiers: [],
    ledger: undefined,
  });
});

test('API errors carry the HTTP status and never the key', async () => {
  const { jev, home } = makeJev();
  const ask = () => jev.ask({ state: 's', questions: { n: { type: 'noul' } } });
  const cases: [number, RegExp][] = [
    [401, /rejected the API key \(HTTP 401\)/],
    [422, /refused the request as invalid \(HTTP 422\): questions\.n: bad question/],
    [429, /rate-limited the request \(HTTP 429\)/],
    [529, /overloaded \(HTTP 529\)/],
    [503, /answered HTTP 503/],
  ];
  for (const [status, message] of cases) {
    fake.respond = () => ({
      status,
      json: status === 422 ? { detail: [{ loc: ['body', 'questions', 'n'], msg: 'bad question' }] } : { error: `echo ${FAKE_KEY}` },
      headers: { 'retry-after-ms': '0' },
    });
    await assert.rejects(ask(), (e: Error) => {
      assert.match(e.message, message);
      assert.ok(!e.message.includes(FAKE_KEY), e.message);
      return true;
    });
  }
  assert.deepEqual(ledger(home).map((l) => l.status), [401, 422, 429, 529, 503]);

  const wrongKey = makeJev({ env: { TYPESAFE_API_KEY: 'another-key-123456' } }).jev;
  fake.reset();
  await assert.rejects(wrongKey.ask({ state: 's', questions: { n: { type: 'noul' } } }), /HTTP 401/);
});

test('a missing key is an error that names where the server looked', async () => {
  const { jev, home } = makeJev({ env: { TYPESAFE_API_KEY: '' }, runCommand: async () => ({ code: 1, stdout: '', stderr: '' }) });
  await assert.rejects(jev.check({ state: 's', conditions: [{ id: 'x', question: 'q' }] }), /No TypeSafe API key found.*TYPESAFE_API_KEY.*Secret Service keyring/);
  assert.equal(fake.seen.length, 0);
  assert.throws(() => readFileSync(ledgerPath(home)), /ENOENT/);
  const status = await jev.status();
  assert.equal(status.key, 'missing');
  assert.match(status.key_error!, /TYPESAFE_API_KEY/);
});

async function connect(service: Service, jev?: Jev) {
  const [clientSide, serverSide] = InMemoryTransport.createLinkedPair();
  await createServer(service, jev).connect(serverSide);
  const client = new Client({ name: 'test', version: '1.0.0' });
  await client.connect(clientSide);
  return client;
}

test('the Jev tools and instructions appear only when jev.enabled is true', async () => {
  const home = tempDir('iat-jev-srv-');
  const service = new Service({ home, scriptsDir: home, platform: 'linux', env: { PATH: '' }, callIde: async () => ({}), drivers: [] });

  const off = await startJev(service, { home, env: {}, platform: 'linux' });
  assert.equal(off.jev, undefined);
  assert.match(off.off, /Jev is off\. Set "jev": \{"enabled": true\} in .*config\.json/);
  const plain = await connect(service, off.jev);
  assert.deepEqual((await plain.listTools()).tools.map((t) => t.name).sort(), ['close_tab', 'list_agents', 'list_ides', 'list_tabs', 'open_tab']);
  assert.match(plain.getInstructions()!, /open_tab/);
  assert.doesNotMatch(plain.getInstructions()!, /jev_/);
  await plain.close();

  writeFileSync(path.join(home, 'config.json'), JSON.stringify({ jev: { enabled: true, tiers: { codex: 'Reviews' } } }));
  const on = await startJev(service, { home, env: { TYPESAFE_API_KEY: FAKE_KEY, TYPESAFE_BASE_URL: fake.url }, platform: 'linux' });
  assert.ok(on.jev);
  const client = await connect(service, on.jev);
  const { tools } = await client.listTools();
  const jevTools = tools.filter((t) => t.name.startsWith('jev_'));
  assert.deepEqual(jevTools.map((t) => t.name), ['jev_status', 'jev_ask', 'jev_choose', 'jev_check', 'jev_rank', 'jev_route']);
  for (const t of jevTools) {
    assert.equal(t.annotations?.readOnlyHint, true, t.name);
    assert.equal(t.annotations?.openWorldHint, t.name !== 'jev_status', t.name);
    assert.match(t.description!, /TypeSafe's API/, t.name);
  }
  assert.match(client.getInstructions()!, /isn't proof/);
  assert.match(client.getInstructions()!, /leaves the machine/);

  const result = (await client.callTool({
    name: 'jev_choose',
    arguments: { instruction: 'Which?', options: [{ id: 'x', description: 'X' }], state: { files: ['a'] } },
  })) as { isError?: boolean; content: { text: string }[] };
  assert.equal(result.isError, undefined);
  assert.equal(JSON.parse(result.content[0]!.text).choice, 'x');
  assert.deepEqual(fake.seen[0]!.body.state, { files: ['a'] });
  await client.close();
});

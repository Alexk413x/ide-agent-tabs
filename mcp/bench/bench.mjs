#!/usr/bin/env node
import { execFileSync, spawn } from 'node:child_process';
import http from 'node:http';
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { parseArgs } from 'node:util';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js';

const here = path.dirname(fileURLToPath(import.meta.url));
const { values: opts, positionals } = parseArgs({
  allowPositionals: true,
  options: {
    mode: { type: 'string', default: 'stdio' },
    dist: { type: 'string', default: path.join(here, '..', '..', 'claude-plugin', 'dist') },
    port: { type: 'string', default: '47911' },
    sessions: { type: 'string', default: '1,8' },
    agents: { type: 'string', default: '1,4,8,16' },
    rounds: { type: 'string', default: '10' },
    runs: { type: 'string', default: '5' },
    http: { type: 'string', default: '32' },
    stdio: { type: 'string', default: '8' },
    json: { type: 'boolean', default: false },
  },
});
const command = positionals[0] ?? 'help';
const dist = path.resolve(opts.dist);
const port = Number(opts.port);
const PROTOCOL = '2026-07-28';
const SETTLE_MS = 5_000;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const median = (xs) => {
  const s = [...xs].sort((a, b) => a - b);
  return s.length === 0 ? NaN : s.length % 2 ? s[(s.length - 1) / 2] : (s[s.length / 2 - 1] + s[s.length / 2]) / 2;
};
const p = (xs, q) => [...xs].sort((a, b) => a - b)[Math.min(xs.length - 1, Math.floor(q * xs.length))] ?? NaN;
const fmt = (n) => (Number.isFinite(n) ? n.toFixed(1) : String(n));
const freshHome = () => mkdtempSync(path.join(os.tmpdir(), 'iat-bench-'));
const results = [];
const record = (row) => {
  results.push(row);
  if (!opts.json) console.log(Object.entries(row).map(([k, v]) => `${k}=${typeof v === 'number' ? fmt(v) : v}`).join('  '));
};

function workingSetMb(pid) {
  if (process.platform === 'win32') {
    const out = execFileSync('tasklist', ['/FI', `PID eq ${pid}`, '/FO', 'CSV', '/NH'], { encoding: 'utf8', windowsHide: true });
    const kb = Number(out.split('","').at(-1)?.replace(/[^0-9]/g, ''));
    return kb / 1024;
  }
  return Number(execFileSync('ps', ['-o', 'rss=', '-p', String(pid)], { encoding: 'utf8' }).trim()) / 1024;
}

function cpuSampler() {
  const snap = () => os.cpus().reduce((a, c) => ({ idle: a.idle + c.times.idle, total: a.total + Object.values(c.times).reduce((x, y) => x + y, 0) }), { idle: 0, total: 0 });
  const start = snap();
  return () => {
    const end = snap();
    return 100 * (1 - (end.idle - start.idle) / Math.max(1, end.total - start.total));
  };
}

function baseEnv(home, extra = {}) {
  const env = { ...process.env, IDE_AGENT_TABS_HOME: home, ...extra };
  for (const name of ['IDE_AGENT_TABS_ID', 'IDE_AGENT_TABS_AGENT', 'IDE_AGENT_TABS_MOD', 'CLAUDE_PLUGIN_ROOT']) if (!(name in extra)) delete env[name];
  return env;
}

class StdioSession {
  constructor(home, tab) {
    this.home = home;
    this.tab = tab;
  }
  async open() {
    const started = performance.now();
    this.transport = new StdioClientTransport({
      command: process.execPath,
      args: [path.join(dist, 'mcp-server.mjs')],
      env: baseEnv(this.home, { IDE_AGENT_TABS_ID: this.tab, IDE_AGENT_TABS_AGENT: 'claude' }),
      stderr: 'ignore',
    });
    this.client = new Client({ name: 'claude-code', version: '2.1.293' });
    await this.client.connect(this.transport);
    this.startMs = performance.now() - started;
    return this;
  }
  get pid() {
    return this.transport.pid;
  }
  async call(name, args = {}) {
    const result = await this.client.callTool({ name, arguments: args }, undefined, { timeout: 120_000 });
    const text = result.content?.[0]?.text ?? '';
    if (result.isError) throw new Error(`${name}: ${text}`);
    return text === '' ? {} : JSON.parse(text);
  }
  async close() {
    await this.client.close().catch(() => undefined);
  }
}

const keepAlive = new http.Agent({ keepAlive: true, maxSockets: 256 });

function post(route, headers, body) {
  return new Promise((resolve, reject) => {
    const req = http.request({ host: '127.0.0.1', port, path: route, method: 'POST', agent: keepAlive, headers: { ...headers, 'content-length': Buffer.byteLength(body) } }, (res) => {
      let text = '';
      res.setEncoding('utf8');
      res.on('data', (c) => (text += c));
      res.on('end', () => resolve({ status: res.statusCode, body: text }));
    });
    req.on('error', reject);
    req.end(body);
  });
}

function readToken(home) {
  return readFileSync(path.join(home, 'server', 'token'), 'utf8').trim();
}

class HttpSession {
  constructor(home, tab, index) {
    this.home = home;
    this.tab = tab;
    this.index = index;
    this.client = `bench${String(index).padStart(4, '0')}${Math.random().toString(16).slice(2, 10)}`;
    this.next = 1;
  }
  async open() {
    this.token = readToken(this.home);
    await this.rpc('server/discover', {});
    await this.rpc('tools/list', {});
    return this;
  }
  headers(method, name) {
    return {
      'content-type': 'application/json',
      accept: 'application/json, text/event-stream',
      host: `127.0.0.1:${port}`,
      authorization: `Bearer ${this.token}`,
      'mcp-protocol-version': PROTOCOL,
      'mcp-method': method,
      ...(name ? { 'mcp-name': name } : {}),
      'x-agent-tabs-client': this.client,
      'x-agent-tabs-tab': this.tab,
      'x-agent-tabs-agent': 'claude',
      'x-agent-tabs-pid': String(process.pid),
      'x-agent-tabs-pid-start': String(1_000_000 + this.index),
    };
  }
  async rpc(method, params, inputResponses) {
    const body = {
      jsonrpc: '2.0',
      id: this.next++,
      method,
      params: {
        ...params,
        ...(inputResponses ? { inputResponses } : {}),
        _meta: { 'io.modelcontextprotocol/protocolVersion': PROTOCOL, 'io.modelcontextprotocol/clientCapabilities': { roots: {} }, 'io.modelcontextprotocol/clientInfo': { name: 'claude-code', version: '2.1.293' } },
      },
    };
    const res = await post('/mcp', this.headers(method, params.name), JSON.stringify(body));
    const json = JSON.parse(res.body);
    if (json.error) throw new Error(`${method}: ${res.status} ${json.error.message}`);
    const result = json.result;
    if (result.resultType === 'input_required') {
      const answers = {};
      for (const [key, request] of Object.entries(result.inputRequests ?? {})) {
        if (request.method === 'roots/list') answers[key] = { roots: [{ uri: `file:///${this.home.replace(/\\/g, '/').replace(/^\//, '')}`, name: 'bench' }] };
      }
      return this.rpc(method, params, answers);
    }
    return result;
  }
  async call(name, args = {}) {
    const result = await this.rpc('tools/call', { name, arguments: args });
    const text = result.content?.[0]?.text ?? '';
    if (result.isError) throw new Error(`${name}: ${text}`);
    return text === '' ? {} : JSON.parse(text);
  }
  async close() {}
}

async function health() {
  try {
    const res = await fetch(`http://127.0.0.1:${port}/health`, { headers: { host: `127.0.0.1:${port}` } });
    return res.ok ? await res.json() : undefined;
  } catch {
    return undefined;
  }
}

async function startShared(home) {
  const started = performance.now();
  const child = spawn(process.execPath, [...(process.env.BENCH_NO_FLAGS ? [] : ['--max-semi-space-size=1']), path.join(dist, 'shared-server.mjs'), '--port', String(port)], { env: baseEnv(home), stdio: 'ignore', windowsHide: true });
  for (;;) {
    const h = await health();
    if (h?.pid === child.pid) break;
    if (child.exitCode !== null) throw new Error(`shared server exited with ${child.exitCode}`);
    await sleep(5);
  }
  const ready = performance.now() - started;
  return { child, pid: child.pid, readyMs: ready };
}

async function stopShared(server) {
  server.child.kill();
  await new Promise((r) => (server.child.exitCode !== null ? r() : server.child.once('exit', r)));
}

async function openSessions(mode, home, count, offset = 0) {
  const sessions = [];
  for (let i = 0; i < count; i++) {
    const tab = `bench-${mode}-${String(i + offset).padStart(3, '0')}`;
    sessions.push(mode === 'stdio' ? new StdioSession(home, tab) : new HttpSession(home, tab, i + offset));
  }
  await Promise.all(sessions.map((s) => s.open()));
  return sessions;
}

async function memory() {
  for (const count of opts.sessions.split(',').map(Number)) {
    const home = freshHome();
    let server;
    if (opts.mode === 'http') server = await startShared(home);
    const sessions = await openSessions(opts.mode, home, count);
    await Promise.all(sessions.map((s) => s.call('list_sessions')));
    await sleep(1_500);
    const pids = opts.mode === 'http' ? [server.pid] : sessions.map((s) => s.pid);
    const mb = pids.map(workingSetMb);
    record({ bench: 'memory', mode: opts.mode, sessions: count, processes: pids.length, totalMb: mb.reduce((a, b) => a + b, 0), perProcessMb: median(mb) });
    await Promise.all(sessions.map((s) => s.close()));
    if (server) await stopShared(server);
    rmSync(home, { recursive: true, force: true, maxRetries: 5 });
  }
}

async function idle() {
  const home = freshHome();
  const server = await startShared(home);
  await sleep(1_500);
  record({ bench: 'idle', mode: 'http', state: 'no session yet', mb: workingSetMb(server.pid) });
  const [s] = await openSessions('http', home, 1);
  await s.call('list_sessions');
  await sleep(1_500);
  record({ bench: 'idle', mode: 'http', state: 'after one session', mb: workingSetMb(server.pid) });
  await stopShared(server);
  rmSync(home, { recursive: true, force: true, maxRetries: 5 });
}

async function startup() {
  const times = [];
  for (let i = 0; i < Number(opts.runs); i++) {
    const home = freshHome();
    if (opts.mode === 'http') {
      const server = await startShared(home);
      const started = performance.now();
      const s = new HttpSession(home, 'bench-start', 0);
      s.token = readToken(home);
      await s.rpc('server/discover', {});
      times.push(server.readyMs + (performance.now() - started));
      await stopShared(server);
    } else {
      const [s] = await openSessions('stdio', home, 1);
      times.push(s.startMs);
      await s.close();
    }
    rmSync(home, { recursive: true, force: true, maxRetries: 5 });
  }
  record({ bench: 'startup', mode: opts.mode, runs: times.length, minMs: Math.min(...times), medianMs: median(times), maxMs: Math.max(...times) });
}

async function exchange(sessions, rounds, timings, ledger, all = sessions) {
  const ids = all.map((s) => s.tab);
  await Promise.all(
    sessions.map(async (s, i) => {
      for (let r = 0; r < rounds; r++) {
        const time = async (op, work) => {
          const t = performance.now();
          try {
            const out = await work();
            timings[op].push(performance.now() - t);
            return out;
          } catch (e) {
            ledger.failed.push(`${op}: ${e.message}`);
            return undefined;
          }
        };
        await time('list_sessions', () => s.call('list_sessions'));
        if (ids.length > 1) {
          const to = ids[(i + 1 + (r % (ids.length - 1))) % ids.length];
          const sent = await time('send_message', () => s.call('send_message', { to, text: `round ${r} from ${s.tab}` }));
          if (sent?.id) ledger.sent.set(sent.id, to);
        }
        const read = await time('read_messages', () => s.call('read_messages'));
        for (const m of read?.messages ?? []) ledger.read.push({ id: m.id, by: s.tab });
      }
    }),
  );
}

async function drain(sessions, ledger) {
  const deadline = Date.now() + 30_000;
  while (ledger.read.length < ledger.sent.size && Date.now() < deadline) {
    for (const s of sessions) {
      const read = await s.call('read_messages').catch(() => undefined);
      for (const m of read?.messages ?? []) ledger.read.push({ id: m.id, by: s.tab });
    }
  }
}

function audit(ledger) {
  const seen = new Set();
  let twice = 0;
  let wrong = 0;
  for (const { id, by } of ledger.read) {
    if (seen.has(id)) twice++;
    seen.add(id);
    if (ledger.sent.get(id) !== by) wrong++;
  }
  const lost = [...ledger.sent.keys()].filter((id) => !seen.has(id)).length;
  return { failed: ledger.failed.length, lost, readTwice: twice, wrongReader: wrong };
}

async function latency() {
  for (const count of opts.agents.split(',').map(Number)) {
    const home = freshHome();
    let server;
    if (opts.mode === 'http') server = await startShared(home);
    const sessions = await openSessions(opts.mode, home, Math.max(count, 2));
    await Promise.all(sessions.map((s) => s.call('list_sessions')));
    await sleep(SETTLE_MS);
    const timings = { list_sessions: [], send_message: [], read_messages: [] };
    const ledger = { sent: new Map(), read: [], failed: [] };
    const cpu = cpuSampler();
    await exchange(sessions.slice(0, count), Number(opts.rounds), timings, ledger, sessions);
    const load = cpu();
    await drain(sessions, ledger);
    record({
      bench: 'latency',
      mode: opts.mode,
      agents: count,
      listMs: median(timings.list_sessions),
      sendMs: median(timings.send_message),
      readMs: median(timings.read_messages),
      sendP95Ms: p(timings.send_message, 0.95),
      cpuPct: load,
      ...audit(ledger),
    });
    if (ledger.failed.length) console.error(ledger.failed.slice(0, 5).join('\n'));
    await Promise.all(sessions.map((s) => s.close()));
    if (server) await stopShared(server);
    rmSync(home, { recursive: true, force: true, maxRetries: 5 });
  }
}

async function mixed() {
  const home = freshHome();
  const server = await startShared(home);
  const http = await openSessions('http', home, Number(opts.http));
  const stdio = await openSessions('stdio', home, Number(opts.stdio), Number(opts.http));
  const sessions = [...http, ...stdio];
  await Promise.all(sessions.map((s) => s.call('list_sessions')));
  await sleep(SETTLE_MS);
  const timings = { list_sessions: [], send_message: [], read_messages: [] };
  const ledger = { sent: new Map(), read: [], failed: [] };
  const cpu = cpuSampler();
  await exchange(sessions, Number(opts.rounds), timings, ledger);
  const load = cpu();
  await drain(sessions, ledger);
  record({
    bench: 'mixed',
    http: http.length,
    stdio: stdio.length,
    sendMs: median(timings.send_message),
    readMs: median(timings.read_messages),
    listMs: median(timings.list_sessions),
    sendP95Ms: p(timings.send_message, 0.95),
    serverMb: workingSetMb(server.pid),
    cpuPct: load,
    ...audit(ledger),
  });
  if (ledger.failed.length) console.error(ledger.failed.slice(0, 5).join('\n'));
  await Promise.all(sessions.map((s) => s.close()));
  await stopShared(server);
  rmSync(home, { recursive: true, force: true, maxRetries: 5 });
}

async function cli() {
  for (const args of [['jev', 'status'], ['list-ides']]) {
    const times = [];
    let code = 0;
    for (let i = 0; i < Number(opts.runs); i++) {
      const home = freshHome();
      writeFileSync(path.join(home, 'config.json'), JSON.stringify({ jev: { enabled: true } }));
      const t = performance.now();
      const child = spawn(process.execPath, [path.join(dist, 'mcp-server.mjs'), ...args], { env: baseEnv(home), stdio: ['pipe', 'ignore', 'ignore'], windowsHide: true });
      child.stdin.end('');
      code = await new Promise((r) => child.once('exit', r));
      times.push(performance.now() - t);
      rmSync(home, { recursive: true, force: true, maxRetries: 5 });
    }
    record({ bench: 'cli', command: args.join(' '), exit: code, minMs: Math.min(...times), medianMs: median(times), maxMs: Math.max(...times) });
  }
}

async function coldstarts() {
  const helper = path.join(dist, '..', 'mcp', 'launch', 'headers.mjs');
  let forbidden = 0;
  let noToken = 0;
  const times = [];
  for (let i = 0; i < Number(opts.runs); i++) {
    const home = freshHome();
    const t = performance.now();
    const out = execFileSync(process.execPath, [helper], { env: baseEnv(home, { CLAUDE_CODE_MCP_SERVER_URL: `http://127.0.0.1:${port}/mcp` }), encoding: 'utf8', windowsHide: true });
    const headers = JSON.parse(out);
    if (!headers.Authorization) noToken++;
    const res = await fetch(`http://127.0.0.1:${port}/mcp`, {
      method: 'POST',
      headers: { ...headers, 'content-type': 'application/json', accept: 'application/json', 'mcp-protocol-version': PROTOCOL, 'mcp-method': 'server/discover' },
      body: JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'server/discover', params: { _meta: { 'io.modelcontextprotocol/protocolVersion': PROTOCOL, 'io.modelcontextprotocol/clientCapabilities': {} } } }),
    });
    if (res.status === 403) forbidden++;
    times.push(performance.now() - t);
    const h = await health();
    if (h?.pid) {
      try {
        process.kill(h.pid);
      } catch {}
      while (await health()) await sleep(20);
    }
    rmSync(home, { recursive: true, force: true, maxRetries: 5 });
  }
  record({ bench: 'coldstarts', runs: times.length, forbidden, noToken, medianMs: median(times), maxMs: Math.max(...times) });
}

const commands = { memory, idle, startup, latency, mixed, cli, coldstarts };
if (!commands[command]) {
  console.log(`Usage: node bench/bench.mjs <${Object.keys(commands).join('|')}> [--mode stdio|http] [--dist dir] [--port n]`);
  process.exit(command === 'help' ? 0 : 1);
}
console.error(`# ${command} ${opts.mode} on ${os.cpus()[0]?.model} x${os.cpus().length}, ${os.platform()} ${os.release()}, node ${process.version}`);
await commands[command]();
if (opts.json) console.log(JSON.stringify(results));
process.exit(0);

import assert from 'node:assert/strict';
import http from 'node:http';
import type { AddressInfo } from 'node:net';

export const FAKE_KEY = 'ts-test-key-0123456789abcdef';
export const FAKE_MODEL = 'jev-1.13.0';

export interface SeenRequest {
  headers: http.IncomingHttpHeaders;
  body: {
    model: string;
    state: unknown;
    questions: Record<string, { type: string; instructions?: unknown; criteria?: unknown }>;
  };
}

export interface FakeReply {
  status: number;
  json: unknown;
  headers?: Record<string, string>;
}

type Responder = (body: SeenRequest['body']) => FakeReply;

export function defaultAnswers(body: SeenRequest['body'], noul = () => 0.5): Record<string, unknown> {
  return Object.fromEntries(
    Object.entries(body.questions).map(([name, q]) => {
      if (q.type === 'noul') return [name, { type: 'noul', noul: noul() }];
      if (q.type === 'choice') {
        const labels = Object.keys(q.criteria as object);
        const probabilities = Object.fromEntries(labels.map((l, i) => [l, i === 0 ? 0.9 : 0.1 / (labels.length - 1)]));
        return [name, { type: 'choice', choice: labels[0], confidence: 0.9, probabilities }];
      }
      const levels = (q.criteria as unknown[]).length;
      const probabilities = Object.fromEntries(Array.from({ length: levels }, (_, i) => [String(i), i === 0 ? 1 : 0]));
      return [name, { type: 'score', score: 0, confidence: 1, legend: {}, probabilities }];
    }),
  );
}

export const ok = (answers: Record<string, unknown>, inputTokens = 1000): FakeReply => ({
  status: 200,
  json: { model: FAKE_MODEL, answers, usage: { input_tokens: inputTokens, output_tokens: 0 } },
});

export class FakeTypeSafe {
  readonly seen: SeenRequest[] = [];
  respond: Responder = (body) => ok(defaultAnswers(body));
  private server?: http.Server;
  url = '';

  async start(): Promise<void> {
    this.server = http.createServer((req, res) => {
      let data = '';
      req.on('data', (d) => (data += d));
      req.on('end', () => {
        const send = (reply: FakeReply) => {
          res.writeHead(reply.status, { 'Content-Type': 'application/json', ...reply.headers });
          res.end(JSON.stringify(reply.json));
        };
        try {
          assert.equal(req.method, 'POST');
          assert.equal(req.url, '/v1/systemone');
          assert.equal(req.headers['content-type'], 'application/json');
          const body = JSON.parse(data) as SeenRequest['body'];
          assert.equal(body.model, 'jev-latest');
          if (body.state === null || body.state === undefined) return send({ status: 422, json: { detail: 'state: Field required' } });
          assert.ok(Object.keys(body.questions).length > 0, 'the request carries questions');
          this.seen.push({ headers: req.headers, body });
          if (req.headers.authorization !== `Bearer ${FAKE_KEY}`) return send({ status: 401, json: { error: 'Invalid API key' } });
          send(this.respond(body));
        } catch (e) {
          send({ status: 400, json: { error: `fake server refused the request shape: ${(e as Error).message}` } });
        }
      });
    });
    await new Promise<void>((r) => this.server!.listen(0, '127.0.0.1', r));
    this.url = `http://127.0.0.1:${(this.server.address() as AddressInfo).port}`;
  }

  reset(): void {
    this.seen.length = 0;
    this.respond = (body) => ok(defaultAnswers(body));
  }

  async stop(): Promise<void> {
    await new Promise((r) => this.server?.close(r));
  }
}

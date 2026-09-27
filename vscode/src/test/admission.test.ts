import assert from 'node:assert/strict';
import { test } from 'node:test';
import { newToken } from '../registry';
import { bearerMatches, checkAdmission, isLoopback } from '../request';

const token = newToken();
const json: Record<string, string> = { 'content-type': 'application/json', authorization: `Bearer ${token}` };

function check(address: string | undefined, method = 'POST', headers: Record<string, string> = json) {
  return checkAdmission(address, method, token, name => headers[name]);
}

function without(headers: Record<string, string>, name: string) {
  const copy = { ...headers };
  delete copy[name];
  return copy;
}

test('loopback POST with JSON is admitted', () => {
  assert.equal(check('127.0.0.1'), undefined);
  assert.equal(check('::1'), undefined);
  assert.equal(check('::ffff:127.0.0.1'), undefined);
  assert.equal(check('127.0.0.1', 'POST', { ...json, 'content-type': 'application/json; charset=utf-8' }), undefined);
  assert.equal(check('127.0.0.1', 'POST', { ...json, 'content-type': 'Application/JSON' }), undefined);
});

test('non-loopback address is refused', () => {
  for (const address of ['192.168.1.20', '10.0.0.5', '0.0.0.0', '8.8.8.8', 'fe80::1', '::ffff:192.168.1.2', '::', '1127.0.0.1']) {
    assert.equal(check(address)?.status, 403, address);
  }
});

test('missing address is refused', () => {
  assert.equal(check(undefined)?.status, 403);
  assert.equal(isLoopback(''), false);
});

test('other methods are refused', () => {
  assert.equal(check('127.0.0.1', 'GET')?.status, 405);
  assert.equal(check('127.0.0.1', 'OPTIONS')?.status, 405);
  assert.equal(check('127.0.0.1', 'post')?.status, 405);
});

test('browser requests are refused', () => {
  assert.equal(check('127.0.0.1', 'POST', { ...json, origin: 'https://example.com' })?.status, 403);
  assert.equal(check('127.0.0.1', 'POST', { ...json, origin: 'null' })?.status, 403);
  assert.equal(check('127.0.0.1', 'POST', { ...json, origin: '' })?.status, 403);
  assert.equal(check('127.0.0.1', 'POST', { ...json, referer: 'https://example.com/' })?.status, 403);
});

test('non-JSON content type is refused', () => {
  const auth = without(json, 'content-type');
  assert.equal(check('127.0.0.1', 'POST', auth)?.status, 415);
  assert.equal(check('127.0.0.1', 'POST', { ...auth, 'content-type': 'text/plain' })?.status, 415);
  assert.equal(check('127.0.0.1', 'POST', { ...auth, 'content-type': 'application/x-www-form-urlencoded' })?.status, 415);
});

test('missing or wrong token is refused', () => {
  const noAuth = without(json, 'authorization');
  assert.equal(check('127.0.0.1', 'POST', noAuth)?.status, 401);
  for (const bad of ['', 'Bearer', 'Bearer ', `Basic ${token}`, token, `Bearer ${token.slice(0, -1)}`, `Bearer ${token}0`, `Bearer ${newToken()}`]) {
    assert.equal(check('127.0.0.1', 'POST', { ...noAuth, authorization: bad })?.status, 401, bad);
  }
});

test("token check ignores the scheme's case and surrounding spaces", () => {
  assert.ok(bearerMatches(`bearer ${token}`, token));
  assert.ok(bearerMatches(`  Bearer   ${token}  `, token));
  assert.ok(!bearerMatches(`Bearer ${token.toUpperCase()}`, token));
  assert.ok(!bearerMatches('Bearer ', ''));
  assert.ok(!bearerMatches(undefined, token));
  assert.ok(!bearerMatches(`Bearer ${token}é`, token));
});

test('token is checked after the browser rules and before the content type', () => {
  assert.equal(check('127.0.0.1', 'POST', { origin: 'https://example.com' })?.status, 403);
  assert.equal(check('127.0.0.1', 'POST', {})?.status, 401);
});

test('tokens are 64 hex characters and fresh each time', () => {
  assert.match(token, /^[0-9a-f]{64}$/);
  assert.notEqual(token, newToken());
});

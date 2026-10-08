import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { FIXTURES_FILE, fixturesText } from '../scripts/write-fixtures.js';

test('the JSON fixtures the Python tests compare against match this build', () => {
  assert.equal(readFileSync(FIXTURES_FILE, 'utf8').replace(/\r\n/g, '\n'), fixturesText(), 'run node --import tsx scripts/write-fixtures.ts');
});

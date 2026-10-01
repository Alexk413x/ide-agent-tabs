import assert from 'node:assert/strict';
import { test } from 'node:test';
import { run } from '../src/process.js';

test('a command that exits without reading its input still returns its exit code', async () => {
  const result = await run(process.execPath, ['-e', 'process.exit(3)'], { input: 'x'.repeat(1 << 20) });
  assert.equal(result.code, 3);
});

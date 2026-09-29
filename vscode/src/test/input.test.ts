import assert from 'node:assert/strict';
import { mock, test } from 'node:test';
import { ENTER_DELAY_MS, typeLine } from '../input';

test('types the text, then presses Enter on its own after a pause', () => {
  mock.timers.enable({ apis: ['setTimeout'] });
  try {
    const sent: string[] = [];
    typeLine(data => sent.push(data), 'hello', () => true);
    assert.deepEqual(sent, ['hello']);
    mock.timers.tick(ENTER_DELAY_MS - 1);
    assert.deepEqual(sent, ['hello']);
    mock.timers.tick(1);
    assert.deepEqual(sent, ['hello', '\r']);
  } finally {
    mock.timers.reset();
  }
});

test('skips Enter when the tab closed during the pause', () => {
  mock.timers.enable({ apis: ['setTimeout'] });
  try {
    const sent: string[] = [];
    let open = true;
    typeLine(data => sent.push(data), 'hello', () => open);
    open = false;
    mock.timers.tick(ENTER_DELAY_MS);
    assert.deepEqual(sent, ['hello']);
  } finally {
    mock.timers.reset();
  }
});

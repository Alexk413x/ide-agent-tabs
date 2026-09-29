import assert from 'node:assert/strict';
import { mock, test } from 'node:test';
import { CHAR_DELAY_MS, ENTER_DELAY_MS, typeLine } from '../input';

test('types one character at a time, then presses Enter after a pause', () => {
  mock.timers.enable({ apis: ['setTimeout'] });
  try {
    const sent: string[] = [];
    typeLine(data => sent.push(data), 'hi!', () => true);
    assert.deepEqual(sent, ['h']);
    mock.timers.tick(CHAR_DELAY_MS);
    assert.deepEqual(sent, ['h', 'i']);
    mock.timers.tick(CHAR_DELAY_MS);
    assert.deepEqual(sent, ['h', 'i', '!']);
    mock.timers.tick(ENTER_DELAY_MS - 1);
    assert.deepEqual(sent, ['h', 'i', '!']);
    mock.timers.tick(1);
    assert.deepEqual(sent, ['h', 'i', '!', '\r']);
  } finally {
    mock.timers.reset();
  }
});

test('stops typing, and skips Enter, when the tab closes', () => {
  mock.timers.enable({ apis: ['setTimeout'] });
  try {
    const sent: string[] = [];
    let open = true;
    typeLine(data => sent.push(data), 'hello', () => open);
    mock.timers.tick(CHAR_DELAY_MS);
    open = false;
    for (let i = 0; i < 6; i++) mock.timers.tick(ENTER_DELAY_MS);
    assert.deepEqual(sent, ['h', 'e']);
  } finally {
    mock.timers.reset();
  }
});

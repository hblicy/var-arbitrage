import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const appSource = await readFile(new URL('./App.jsx', import.meta.url), 'utf8');

test('opens the observation pool by default', () => {
  assert.match(appSource, /<details className="observation-pool" open>/);
});

test('does not show manual Variational verification copy', () => {
  assert.doesNotMatch(appSource, /需人工复核 Variational/);
  assert.doesNotMatch(appSource, /需手工确认 Variational 盘口/);
  assert.doesNotMatch(appSource, /请在 Variational 手工确认 1000 USDT/);
});

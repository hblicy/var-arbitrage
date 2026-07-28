import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const appSource = await readFile(new URL('./App.jsx', import.meta.url), 'utf8');

test('opens the observation pool by default', () => {
  assert.match(appSource, /<details className="observation-pool" open>/);
});

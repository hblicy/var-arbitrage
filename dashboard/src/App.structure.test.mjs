import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import { runInNewContext } from 'node:vm';

const appSource = await readFile(new URL('./App.jsx', import.meta.url), 'utf8');

test('refreshes when only a cached market expires', () => {
  const expression = appSource.match(/const getDashboardSignature = ([\s\S]*?);\r?\n/)[1];
  const signature = runInNewContext(`(${expression})`);
  const before = {
    data_version: 1,
    last_update: 'unchanged',
    opportunities: [],
    markets: { BTCUSDT: { bulk: { price: 100 } }, SOLUSDT: { bulk: { price: 200 } } },
    exchange_status: { bulk: { state: 'fresh', last_success_at: 1000 } },
  };
  const after = structuredClone(before);
  after.markets.BTCUSDT.bulk = null;
  assert.notEqual(signature(before), signature(after));
  assert.equal(signature(before), signature(structuredClone(before)));
});

test('opens the observation pool by default', () => {
  assert.match(appSource, /<details className="observation-pool" open>/);
});

test('does not show manual Variational verification copy', () => {
  assert.doesNotMatch(appSource, /需人工复核 Variational/);
  assert.doesNotMatch(appSource, /需手工确认 Variational 盘口/);
  assert.doesNotMatch(appSource, /请在 Variational 手工确认 1000 USDT/);
});

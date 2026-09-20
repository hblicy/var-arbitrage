import test from 'node:test';
import assert from 'node:assert/strict';
import { selectFundingRows, selectFundingDepth, liveDepth, signalState } from './fundingViewModel.mjs';

const row = (key, value, other = {}) => ({ key, symbol: 'BTCUSDT', buy_exchange: 'arcus', sell_exchange: 'bulk', timestamp: 100,
  status: 'triggered', projections: { 8: { net_bps: value }, 1: { net_bps: value === null ? null : -value } }, ...other });

test('sorts the selected holding horizon and keeps unavailable estimates last', () => {
  const source = [row('a', 2), row('b', 5), row('c', null)];
  assert.deepEqual(selectFundingRows(source, 8, new Set(['arcus', 'bulk'])).map(x => x.key), ['b', 'a', 'c']);
  assert.deepEqual(selectFundingRows(source, 1, new Set(['arcus', 'bulk'])).map(x => x.key), ['a', 'b', 'c']);
  assert.deepEqual(source.map(x => x.key), ['a', 'b', 'c']);
});

test('respects selected exchanges', () => {
  assert.equal(selectFundingRows([row('a', 2)], 8, new Set(['arcus'])).length, 0);
});

test('expires live capacity and stale signal status without mutating source', () => {
  const source = row('a', 2, { depth: { expires_at: 102, capacity: { notional_usd: 5000 } } });
  assert.ok(liveDepth(source.depth, 101));
  assert.equal(liveDepth(source.depth, 103), null);
  assert.equal(signalState(source, 131, 30), 'paused');
  assert.equal(source.status, 'triggered');
});

test('replaces expired manual depth with a newer live scan', () => {
  const manual = { checked_at: 100, expires_at: 102, capacity: { notional_usd: 20000 } };
  const automatic = { checked_at: 103, expires_at: 105, capacity: { notional_usd: 30000 } };
  assert.equal(selectFundingDepth(manual, automatic, 104), automatic);
  assert.equal(manual.capacity.notional_usd, 20000);
});

test('uses the newer check when both capacity quotes are live', () => {
  const manual = { checked_at: 101, expires_at: 103 };
  const automatic = { checked_at: 100, expires_at: 102 };
  assert.equal(selectFundingDepth(manual, automatic, 101), manual);
  assert.equal(selectFundingDepth(automatic, manual, 101), manual);
});

test('keeps explicit manual failure and does not revive expired capacity', () => {
  const failed = { reason: 'QUOTE_FETCH_FAILED', capacity: null };
  const automatic = { checked_at: 100, expires_at: 102 };
  assert.equal(selectFundingDepth(failed, automatic, 101), failed);
  assert.equal(liveDepth(selectFundingDepth(null, automatic, 103), 103), null);
  assert.equal(selectFundingDepth(null, automatic, 101), automatic);
  assert.equal(selectFundingDepth(automatic, null, 101), automatic);
});

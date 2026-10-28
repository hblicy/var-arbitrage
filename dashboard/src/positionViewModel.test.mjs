import assert from 'node:assert/strict';
import test from 'node:test';
import { positionViewModel, priceAgeLabel } from './positionViewModel.mjs';

const held = (overrides = {}) => ({
  current_price_long: 100, current_price_short: 101, prices_available: true,
  price_max_age_seconds: 30,
  price_health: {
    long: { status: 'fresh', age_seconds: 2, timestamp_source: 'exchange' },
    short: { status: 'fresh', age_seconds: 4, timestamp_source: 'http_cache' },
  },
  pnl: { notional: 1000, funding_income: 5, total_fees: 1, net_pnl: 4, mtm_pnl: 2, total_pnl: 6 },
  ...overrides,
});

test('position prices age while an HTTP request fails or remains pending', () => {
  const row = held();
  const before = structuredClone(row);
  const fresh = positionViewModel({ one: row }, 10);
  assert.equal(fresh.entries[0][1].price_health.short.age_seconds, 14);
  assert.equal(fresh.totals.total_pnl, 6);
  const stale = positionViewModel({ one: row }, 27);
  assert.equal(stale.entries[0][1].price_health.short.status, 'stale');
  assert.equal(stale.entries[0][1].price_health.long.status, 'fresh');
  assert.equal(stale.entries[0][1].current_price_short, null);
  assert.equal(stale.entries[0][1].pnl.mtm_pnl, null);
  assert.equal(stale.entries[0][1].pnl.total_pnl, null);
  assert.deepEqual(row, before);
});

test('one unavailable position makes the combined total unavailable instead of a partial sum', () => {
  const unavailable = held({ prices_available: false,
    pnl: { notional: 1000, funding_income: 3, total_fees: 1, net_pnl: 2, mtm_pnl: null, total_pnl: null } });
  const { totals } = positionViewModel({ one: held(), two: unavailable }, 0);
  assert.equal(totals.mtm_pnl, null);
  assert.equal(totals.total_pnl, null);
  assert.equal(totals.funding_income, 8);
  assert.equal(totals.total_fees, 2);
});

test('unknown price time cannot be made current by a successful response', () => {
  const { entries } = positionViewModel({ one: held({ price_health: {} }) }, 0);
  assert.equal(entries[0][1].prices_available, false);
  assert.equal(entries[0][1].pnl.total_pnl, null);
});

test('cache and receipt estimates are labelled separately from source quotation time', () => {
  assert.match(priceAgeLabel({ status: 'fresh', age_seconds: 12, timestamp_source: 'http_cache' }), /缓存.*12s/);
  assert.match(priceAgeLabel({ status: 'fresh', age_seconds: 12, timestamp_source: 'received' }), /采集.*12s/);
  assert.match(priceAgeLabel({ status: 'fresh', age_seconds: 12, timestamp_source: 'exchange' }), /报价.*12s/);
  assert.match(priceAgeLabel({ status: 'unknown', age_seconds: null }), /未知/);
  assert.match(priceAgeLabel({ status: 'stale', age_seconds: 65, timestamp_source: 'exchange' }), /过期/);
});

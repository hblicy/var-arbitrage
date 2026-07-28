import assert from 'node:assert/strict';
import test from 'node:test';

import * as opportunityViewModel from './opportunityViewModel.js';

const { partitionOpportunities } = opportunityViewModel;

test('places routes without two-sided depth into the observation pool', () => {
  const executable = {
    symbol: 'BTCUSDT',
    direction: 'binance_long_aster_short',
    details: {
      buy_exchange: 'binance',
      sell_exchange: 'aster',
      entry_check_supported: true,
    },
  };
  const observation = {
    symbol: 'ETHUSDT',
    direction: 'lighter_long_hyperliquid_short',
    details: {
      buy_exchange: 'lighter',
      sell_exchange: 'hyperliquid',
      entry_check_supported: false,
    },
  };

  const result = partitionOpportunities(
    [observation, executable],
    new Set(['binance', 'aster', 'lighter', 'hyperliquid']),
  );

  assert.deepEqual(result.executable, [executable]);
  assert.deepEqual(result.observation, [observation]);
});

test('places the Binance and Variational route into manual review candidates', () => {
  const manualReview = {
    symbol: 'DEXEUSDT',
    direction: 'binance_long_variational_short',
    details: {
      buy_exchange: 'binance',
      sell_exchange: 'variational',
      entry_check_supported: false,
    },
  };

  const result = partitionOpportunities(
    [manualReview],
    new Set(['binance', 'variational']),
  );

  assert.deepEqual(result.manual, [manualReview]);
  assert.deepEqual(result.observation, []);
});

test('prioritizes Binance and Variational over higher-return automatic routes for the same symbol', () => {
  const automatic = {
    symbol: 'BTCUSDT',
    details: {
      projected_24h_bps: 300,
    },
  };
  const manual = {
    symbol: 'BTCUSDT',
    details: {
      projected_24h_bps: 100,
    },
  };

  const result = opportunityViewModel.selectBestReviewCandidates(
    [automatic],
    [manual],
    (opportunity) => opportunity.details.projected_24h_bps,
  );

  assert.equal(result.get('BTCUSDT'), manual);
});

test('excludes routes containing disabled exchanges from both pools', () => {
  const result = partitionOpportunities([
    {
      symbol: 'BTCUSDT',
      details: {
        buy_exchange: 'binance',
        sell_exchange: 'aster',
        entry_check_supported: true,
      },
    },
  ], new Set(['binance']));

  assert.deepEqual(result, { executable: [], manual: [], observation: [] });
});

test('reports every hidden observation route before the user expands the pool', () => {
  const opportunities = Array.from({ length: 21 }, (_, index) => ({
    symbol: `TOKEN${index}USDT`,
  }));

  const preview = opportunityViewModel.getObservationDisplay(opportunities, false);

  assert.equal(preview.visible.length, 20);
  assert.equal(preview.hiddenCount, 1);

  const expanded = opportunityViewModel.getObservationDisplay(opportunities, true);
  assert.equal(expanded.visible.length, 21);
  assert.equal(expanded.hiddenCount, 0);
});

test('shows every observation route by default', () => {
  const opportunities = Array.from({ length: 21 }, (_, index) => ({
    symbol: `TOKEN${index}USDT`,
  }));

  const display = opportunityViewModel.getObservationDisplay(opportunities);

  assert.equal(display.visible.length, 21);
  assert.equal(display.hiddenCount, 0);
});

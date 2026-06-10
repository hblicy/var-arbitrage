import React, { useState, useEffect, useRef, useMemo, useCallback } from 'react';
import PositionsModal from './PositionsModal';

const STORAGE_KEY = 'arbitrage_exchange_prefs';
const MIN_EXCHANGES_REQUIRED = 2;
const POLL_INTERVAL_MS = 10000;
const DEFAULT_EXCHANGES = ['binance', 'variational', 'nado', 'hyperliquid', 'edgex', 'lighter', 'backpack', 'grvt'];
const EMPTY_DASHBOARD_DATA = {
  markets: {},
  opportunities: [],
  reasons: {},
  symbol_max_intervals: {},
  last_update: 'Loading...',
  data_version: 0,
};

const normalizeDashboardData = (json) => ({
  ...EMPTY_DASHBOARD_DATA,
  ...json,
  markets: json?.markets || {},
  opportunities: Array.isArray(json?.opportunities) ? json.opportunities : [],
  reasons: json?.reasons || {},
  symbol_max_intervals: json?.symbol_max_intervals || {},
  data_version: Number(json?.data_version || 0),
});

const getDashboardSignature = (item) => [
  item.data_version,
  item.last_update,
  Object.keys(item.markets || {}).length,
  (item.opportunities || []).length,
].join('|');

const formatExchangeName = (name) => name.charAt(0).toUpperCase() + name.slice(1);

const formatLastUpdate = (value) => {
  if (!value || value === 'Never' || value === 'Loading...') return value || 'Loading...';
  return value.replace(' (Multi-Exchange Mode)', '');
};

const calcDailyYieldPercent = (opportunity) => (
  (opportunity.details.net_spread_bps +
    (opportunity.details.funding_diff_scaled_bps * 24 / (opportunity.base_interval || 8))) / 100
);

const getOpportunityApr = (opportunity) => (
  opportunity.total_apr ?? (
    opportunity.details.funding_diff_scaled_bps * (24 / (opportunity.base_interval || 8)) * 3.65 +
    opportunity.details.net_spread_bps / 100.0
  )
);

const ArbitrageCell = React.memo(function ArbitrageCell({ opportunity }) {
  if (!opportunity) {
    return <div className="no-arb-hint">Insufficient Spread</div>;
  }

  const buyEx = opportunity.details.buy_exchange;
  const sellEx = opportunity.details.sell_exchange;
  const intervals = opportunity.details.native_intervals || {};
  const buyInterval = intervals.buy || 8;
  const sellInterval = intervals.sell || 8;
  const hasMismatch = buyInterval !== sellInterval;
  const dailyYieldPercent = calcDailyYieldPercent(opportunity);
  const totalBps = opportunity.details.net_spread_bps + opportunity.details.funding_diff_scaled_bps;
  const buyVolume = opportunity.details.volumes.buy;
  const sellVolume = opportunity.details.volumes.sell;
  const formatVolume = (volume) => volume > 1000000
    ? `${(volume / 1000000).toFixed(1)}M`
    : `${(volume / 1000).toFixed(0)}K`;

  return (
    <div className="arb-container">
      <div className="arb-action">
        <span className="exchange-pill buy">
          <span style={{ opacity: 0.7 }}>Buy</span> {formatExchangeName(buyEx)}
        </span>
        <span className="arb-arrow">-&gt;</span>
        <span className="exchange-pill sell">
          <span style={{ opacity: 0.7 }}>Sell</span> {formatExchangeName(sellEx)}
        </span>
      </div>
      <div className="arb-details">
        <div className="arb-total-yield">
          {dailyYieldPercent.toFixed(2)}%
          <small>&nbsp; ({totalBps.toFixed(1)} bps)</small>
        </div>
        <div className="arb-breakdown">
          <span>Spread: {opportunity.details.net_spread_bps.toFixed(0)}</span>
          <span>+</span>
          <span>Fund: {opportunity.details.funding_diff_scaled_bps.toFixed(0)}</span>
        </div>
        <div className="arb-volumes" style={{ fontSize: '10px', opacity: 0.6, marginTop: '2px' }}>
          Vol: {formatVolume(buyVolume)} | {formatVolume(sellVolume)}
        </div>
        {(hasMismatch || opportunity.details.suggest_limit_order) && (
          <div className="arb-badges">
            {hasMismatch && (
              <span className="mismatch-badge" title={`Settlement mismatch: ${buyEx} ${buyInterval}h vs ${sellEx} ${sellInterval}h`}>
                ! {buyInterval}h/{sellInterval}h
              </span>
            )}
            {opportunity.details.suggest_limit_order && (
              <span className="limit-order-badge" title="High slippage detected, use limit orders">
                Limit order
              </span>
            )}
          </div>
        )}
      </div>
    </div>
  );
});

const MarketRow = React.memo(function MarketRow({ row, marketSet, exchanges, symbolMaxIntervals }) {
  const { sym, delta, bestSymbolOpp, baseInterval, dailyYield } = row;

  return (
    <tr>
      <td>
        <div className="symbol-cell">
          <div className="coin-icon">{sym.substring(0, 1)}</div>
          <div>
            <span className="sym-name">{sym.replace('USDT', '')}</span>
            <span className="sym-pair">PERP</span>
          </div>
        </div>
      </td>
      {exchanges.map(ex => {
        const market = marketSet[ex];
        const hasData = market && typeof market.funding_rate === 'number';
        const nativeRatePercent = hasData ? market.funding_rate * 100 : 0;
        const nativeInterval = market ? market.native_interval : 8;
        const rowBaseInterval = symbolMaxIntervals?.[sym] || baseInterval || 8;
        const normalizedRate = hasData ? nativeRatePercent * (rowBaseInterval / nativeInterval) : 0;

        let valClass = 'val-pos';
        if (normalizedRate < 0) valClass = 'val-neg';
        if (normalizedRate < -1) valClass = 'val-high-neg';
        if (normalizedRate > 1) valClass = 'val-high-pos';

        return (
          <td key={ex} className={hasData ? 'rate-cell' : 'cell-empty'}>
            {hasData ? (
              <>
                <div className={`rate-primary ${valClass}`}>{normalizedRate.toFixed(4)}%</div>
                <div className="rate-secondary">{nativeInterval}h: {nativeRatePercent.toFixed(4)}%</div>
              </>
            ) : '-'}
          </td>
        );
      })}
      <td className="gap-cell">
        <div className="gap-value">
          <span className={`apr-yield ${dailyYield > 2 ? 'glow' : ''}`}>{dailyYield.toFixed(2)}% <small>Daily</small></span>
          <span className="bps-value">{delta.toFixed(1)} bps / {baseInterval}h</span>
        </div>
      </td>
      <td>
        <ArbitrageCell opportunity={bestSymbolOpp} />
      </td>
    </tr>
  );
});

function App() {
  const [data, setData] = useState(EMPTY_DASHBOARD_DATA);
  const [exchangeStates, setExchangeStates] = useState([]);
  const [error, setError] = useState(null);
  const [isModalOpen, setIsModalOpen] = useState(false);
  const isModalOpenRef = useRef(false);
  const wasModalOpenRef = useRef(false);
  const abortControllerRef = useRef(null);

  const fetchData = useCallback(async ({ force = false } = {}) => {
    abortControllerRef.current?.abort();

    const controller = new AbortController();
    abortControllerRef.current = controller;
    try {
      const response = await fetch('/api/data', { cache: 'no-store', signal: controller.signal });
      if (!response.ok) throw new Error('Network response was not ok');

      const nextData = normalizeDashboardData(await response.json());
      if (abortControllerRef.current !== controller) return;

      setData((prevData) => {
        if (!force && getDashboardSignature(prevData) === getDashboardSignature(nextData)) {
          return prevData;
        }
        return nextData;
      });
      setError(null);
    } catch (err) {
      if (err.name === 'AbortError') return;
      setError('Connection to backend failed. Make sure api.py is running.');
      console.error('Fetch error:', err);
    } finally {
      if (abortControllerRef.current === controller) {
        abortControllerRef.current = null;
      }
    }
  }, []);

  useEffect(() => {
    const fetchExchanges = async () => {
      try {
        const response = await fetch('/api/exchanges');
        if (!response.ok) return;

        const serverExchanges = await response.json();
        let savedPrefs = {};
        try {
          const saved = localStorage.getItem(STORAGE_KEY);
          if (saved) savedPrefs = JSON.parse(saved);
        } catch (e) {
          console.warn('Could not parse saved preferences:', e);
        }

        const merged = serverExchanges.map(ex => ({
          name: ex.name,
          enabled: savedPrefs[ex.name] !== undefined ? savedPrefs[ex.name] : true,
        }));

        setExchangeStates(merged);
      } catch (err) {
        console.error('Failed to fetch exchanges:', err);
      }
    };

    fetchExchanges();
    fetchData({ force: true });

    const intervalId = window.setInterval(() => {
      if (!isModalOpenRef.current && !document.hidden) {
        fetchData();
      }
    }, POLL_INTERVAL_MS);

    return () => {
      abortControllerRef.current?.abort();
      window.clearInterval(intervalId);
    };
  }, [fetchData]);

  useEffect(() => {
    isModalOpenRef.current = isModalOpen;

    if (!isModalOpen && wasModalOpenRef.current) {
      fetchData({ force: true });
    }
    wasModalOpenRef.current = isModalOpen;
  }, [isModalOpen, fetchData]);

  const handleToggle = (name, currentState) => {
    if (currentState) {
      const activeCount = exchangeStates.filter(ex => ex.enabled).length;
      if (activeCount <= MIN_EXCHANGES_REQUIRED) return;
    }

    const newStates = exchangeStates.map(ex =>
      ex.name === name ? { ...ex, enabled: !currentState } : ex
    );
    setExchangeStates(newStates);

    const prefs = {};
    newStates.forEach(ex => { prefs[ex.name] = ex.enabled; });
    localStorage.setItem(STORAGE_KEY, JSON.stringify(prefs));
  };

  const allPossibleExchanges = useMemo(
    () => exchangeStates.length > 0 ? exchangeStates.map(ex => ex.name) : DEFAULT_EXCHANGES,
    [exchangeStates]
  );
  const enabledExchanges = useMemo(
    () => exchangeStates.length > 0 ? exchangeStates.filter(ex => ex.enabled).map(ex => ex.name) : allPossibleExchanges,
    [exchangeStates, allPossibleExchanges]
  );
  const enabledExchangeSet = useMemo(() => new Set(enabledExchanges), [enabledExchanges]);
  const exchanges = enabledExchanges;

  const bestOppBySymbol = useMemo(() => {
    const map = new Map();

    for (const opportunity of data.opportunities) {
      if (
        enabledExchangeSet.has(opportunity.details.buy_exchange?.toLowerCase()) &&
        enabledExchangeSet.has(opportunity.details.sell_exchange?.toLowerCase())
      ) {
        const key = opportunity.symbol.trim().toUpperCase();
        const currentBest = map.get(key);
        if (!currentBest || getOpportunityApr(opportunity) > getOpportunityApr(currentBest)) {
          map.set(key, opportunity);
        }
      }
    }

    return map;
  }, [data.opportunities, enabledExchangeSet]);

  const symbolsWithDelta = useMemo(() => {
    const symbolMaxIntervals = data.symbol_max_intervals || {};

    return Object.keys(data.markets).map(sym => {
      const marketSet = data.markets[sym];
      const bestSymbolOpp = bestOppBySymbol.get(sym.trim().toUpperCase()) || null;

      let baseInterval = symbolMaxIntervals[sym] || 0;
      if (!baseInterval) {
        for (const market of Object.values(marketSet)) {
          if (market?.native_interval) baseInterval = Math.max(baseInterval, market.native_interval);
        }
      }
      baseInterval = baseInterval || 8;

      let minRate = Infinity;
      let maxRate = -Infinity;
      let rateCount = 0;

      for (const ex of exchanges) {
        const market = marketSet[ex];
        if (!market || typeof market.funding_rate !== 'number') continue;

        const scaledRate = market.funding_rate * (baseInterval / (market.native_interval || 8));
        minRate = Math.min(minRate, scaledRate);
        maxRate = Math.max(maxRate, scaledRate);
        rateCount += 1;
      }

      const delta = rateCount > 1 ? (maxRate - minRate) * 10000 : 0;
      const dailyYield = delta * (24 / baseInterval) / 100;
      const bestDailyYieldPercent = bestSymbolOpp ? calcDailyYieldPercent(bestSymbolOpp) : dailyYield;
      const bestDailyYieldBps = bestSymbolOpp ? bestDailyYieldPercent * 100 : null;

      return { sym, delta, bestSymbolOpp, baseInterval, dailyYield, bestDailyYieldPercent, bestDailyYieldBps };
    });
  }, [data.markets, data.symbol_max_intervals, bestOppBySymbol, exchanges]);

  const sortedSymbols = useMemo(() => {
    return [...symbolsWithDelta].sort((a, b) => {
      const getYield = (item) => {
        if (item.bestSymbolOpp) return getOpportunityApr(item.bestSymbolOpp) / 365 || 0;
        return item.dailyYield;
      };
      const yieldA = getYield(a);
      const yieldB = getYield(b);

      if (Math.abs(yieldA - yieldB) > 0.001) return yieldB - yieldA;
      if (b.delta !== a.delta) return b.delta - a.delta;
      return a.sym.localeCompare(b.sym);
    });
  }, [symbolsWithDelta]);

  const visibleSymbols = useMemo(() => sortedSymbols.slice(0, 100), [sortedSymbols]);
  const lastUpdateText = useMemo(() => formatLastUpdate(data.last_update), [data.last_update]);
  const totalMarkets = symbolsWithDelta.length;
  const oppCount = data.opportunities.length;

  return (
    <div className="app-container">
      <header>
        <div className="title-section">
          <h1>Arbitrage Control Center</h1>
          <div className="update-info">
            <span className="dot"></span>
            Last Update: {lastUpdateText} (Multi-Exchange Mode)
          </div>
        </div>

        <div className="filter-controls">
          <div className="filter-label">Exchanges:</div>
          <div className="exchange-toggles">
            {exchangeStates.map(ex => (
              <label key={ex.name} className={`toggle-pill ${ex.enabled ? 'active' : ''}`}>
                <input
                  type="checkbox"
                  checked={ex.enabled}
                  onChange={() => handleToggle(ex.name, ex.enabled)}
                />
                {formatExchangeName(ex.name)}
              </label>
            ))}
          </div>
          <div className="header-actions" style={{ marginLeft: 'auto' }}>
            <button className="manage-btn" onClick={() => setIsModalOpen(true)}>
              Manage Positions
            </button>
          </div>
        </div>
      </header>

      {error && <div className="error-banner">{error}</div>}

      <div className="stats-grid">
        <div className="stats-card">
          <div className="stat-label">Markets Tracked</div>
          <div className="stat-value">{totalMarkets}</div>
        </div>
        <div className="stats-card">
          <div className="stat-label">Live Opportunities</div>
          <div className="stat-value highlight">{oppCount}</div>
        </div>
        <div className="stats-card">
          <div className="stat-label">Best Daily Yield</div>
          <div className="stat-value highlight-green">
            {sortedSymbols.length > 0
              ? sortedSymbols[0].bestSymbolOpp
                ? `${sortedSymbols[0].bestDailyYieldPercent.toFixed(2)}%`
                : sortedSymbols[0].dailyYield > 0
                  ? `${sortedSymbols[0].dailyYield.toFixed(2)}%`
                  : '-'
              : '-'}
          </div>
          <div className="stat-sub">
            {sortedSymbols.length > 0
              ? sortedSymbols[0].bestSymbolOpp
                ? `${sortedSymbols[0].sym} (Daily: ${sortedSymbols[0].bestDailyYieldBps.toFixed(1)} bps)`
                : sortedSymbols[0].dailyYield > 0
                  ? `${sortedSymbols[0].sym} (Funding Only)`
                  : 'No opportunities'
              : 'No opportunities'}
          </div>
        </div>
      </div>

      <div className="data-table-container">
        <table>
          <thead>
            <tr>
              <th className="col-asset">Asset</th>
              {exchanges.map(ex => <th key={ex}>{formatExchangeName(ex)}</th>)}
              <th className="col-gap">Funding Daily</th>
              <th className="col-action">Arbitrage</th>
            </tr>
          </thead>
          <tbody>
            {visibleSymbols.map((row) => (
              <MarketRow
                key={row.sym}
                row={row}
                marketSet={data.markets[row.sym]}
                exchanges={exchanges}
                symbolMaxIntervals={data.symbol_max_intervals}
              />
            ))}
          </tbody>
        </table>
      </div>

      <PositionsModal isOpen={isModalOpen} onClose={() => setIsModalOpen(false)} />
    </div>
  );
}

export default App;

import React, { useState, useEffect, useRef, useMemo, useCallback } from 'react';
import PositionsModal from './PositionsModal';

const STORAGE_KEY = 'arbitrage_exchange_prefs';
const MIN_EXCHANGES_REQUIRED = 2;
const POLL_INTERVAL_MS = 10000;
const DEFAULT_EXCHANGES = ['binance', 'variational', 'nado', 'hyperliquid', 'aster', 'lighter', 'backpack', 'grvt', 'ondoperps'];
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

const EXCHANGE_LABELS = {
  aster: 'Aster',
  ondoperps: 'OndoPerps',
};

const formatExchangeName = (name) => EXCHANGE_LABELS[name] || name.charAt(0).toUpperCase() + name.slice(1);

const formatLastUpdate = (value) => {
  if (!value || value === 'Never' || value === 'Loading...') return value || 'Loading...';
  return value.replace(' (Multi-Exchange Mode)', '');
};

const num = (value, fallback = 0) => (
  Number.isFinite(Number(value)) ? Number(value) : fallback
);

const getBaseInterval = (opportunity) => (
  num(opportunity.details.base_interval, num(opportunity.base_interval, 8)) || 8
);

const calcFundingDailyBps = (opportunity) => {
  const baseInterval = getBaseInterval(opportunity);
  return num(
    opportunity.details.funding_daily_bps,
    num(opportunity.details.funding_diff_24h_bps,
      num(opportunity.details.funding_diff_scaled_bps) * 24 / baseInterval),
  );
};

const calcProjected24hPercent = (opportunity) => (
  num(
    opportunity.details.projected_24h_bps,
    num(opportunity.details.net_spread_bps) + calcFundingDailyBps(opportunity),
  ) / 100
);

const getOpportunityApr = (opportunity) => (
  opportunity.total_apr ?? (
    calcFundingDailyBps(opportunity) * 3.65 +
    opportunity.details.net_spread_bps / 100.0
  )
);

const formatSignedBps = (value) => `${num(value) >= 0 ? '+' : ''}${num(value).toFixed(0)}`;
const formatSignedPercent = (value, digits = 2) => `${num(value) >= 0 ? '+' : ''}${num(value).toFixed(digits)}%`;

const formatCoverTime = (coverHours, spreadBps) => {
  if (num(spreadBps) >= 0) return '无需覆盖';
  if (!Number.isFinite(Number(coverHours))) return '无法估算';
  const hours = Number(coverHours);
  if (hours < 1) return `${Math.round(hours * 60)}m`;
  return `${hours.toFixed(1)}h`;
};

const opportunityTypeLabel = (type) => ({
  aligned: '价差+资金费同向',
  funding_cover: '资金费覆盖',
  price_only: '仅价差',
  watch: '观察',
}[type] || '观察');

const ArbitrageCell = React.memo(function ArbitrageCell({ opportunity }) {
  if (!opportunity) {
    return <div className="no-arb-hint">无有效组合</div>;
  }

  const buyEx = opportunity.details.buy_exchange;
  const sellEx = opportunity.details.sell_exchange;
  const intervals = opportunity.details.native_intervals || {};
  const buyInterval = intervals.buy || 8;
  const sellInterval = intervals.sell || 8;
  const hasMismatch = buyInterval !== sellInterval;
  const projected24hPercent = calcProjected24hPercent(opportunity);
  const priceBps = num(opportunity.details.net_spread_bps);
  const fundingDailyBps = calcFundingDailyBps(opportunity);
  const fundingHourlyBps = num(opportunity.details.funding_hourly_bps, fundingDailyBps / 24);
  const coverHours = opportunity.details.cover_hours;
  const setupType = opportunity.details.opportunity_type || 'watch';
  const buyVolume = opportunity.details.volumes.buy;
  const sellVolume = opportunity.details.volumes.sell;
  const formatVolume = (volume) => !Number.isFinite(Number(volume))
    ? '-'
    : Number(volume) > 1000000
      ? `${(Number(volume) / 1000000).toFixed(1)}M`
      : `${(Number(volume) / 1000).toFixed(0)}K`;

  return (
    <div className="arb-container">
      <div className="arb-action">
        <span className="exchange-pill buy">
          <span style={{ opacity: 0.7 }}>做多</span> {formatExchangeName(buyEx)}
        </span>
        <span className="arb-arrow">-&gt;</span>
        <span className="exchange-pill sell">
          <span style={{ opacity: 0.7 }}>做空</span> {formatExchangeName(sellEx)}
        </span>
      </div>
      <div className="arb-details">
        <div className={`setup-badge ${setupType}`}>{opportunityTypeLabel(setupType)}</div>
        <div className="arb-total-yield">
          {formatSignedPercent(projected24hPercent)}
          <small>&nbsp; 24h估算</small>
        </div>
        <div className="arb-metrics">
          <span className={priceBps >= 0 ? 'metric-good' : 'metric-bad'}>
            价差 {formatSignedBps(priceBps)}
          </span>
          <span className={fundingDailyBps >= 0 ? 'metric-good' : 'metric-bad'}>
            资金费 {formatSignedPercent(fundingDailyBps / 100)}/天
          </span>
          <span className={priceBps < 0 ? 'metric-warn' : 'metric-muted'}>
            覆盖 {formatCoverTime(coverHours, priceBps)}
          </span>
        </div>
        <div className="arb-breakdown">
          <span>{formatSignedBps(fundingHourlyBps)} bps/h</span>
          <span>量 {formatVolume(buyVolume)} | {formatVolume(sellVolume)}</span>
        </div>
        {(hasMismatch || opportunity.details.suggest_limit_order) && (
          <div className="arb-badges">
            {hasMismatch && (
              <span className="mismatch-badge" title={`资金费结算周期不同：${buyEx} ${buyInterval}h vs ${sellEx} ${sellInterval}h`}>
                ! {buyInterval}h/{sellInterval}h
              </span>
            )}
            {opportunity.details.suggest_limit_order && (
              <span className="limit-order-badge" title="检测到滑点风险，建议使用限价单">
                限价单
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
          <span className={`apr-yield ${dailyYield > 2 ? 'glow' : ''}`}>{dailyYield.toFixed(2)}% <small>/天</small></span>
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
      setError('连接后端失败，请确认 api.py 正在运行。');
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
      const bestDailyYieldPercent = bestSymbolOpp ? calcProjected24hPercent(bestSymbolOpp) : dailyYield;
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
          <h1>套利监控中心</h1>
          <div className="update-info">
            <span className="dot"></span>
            更新时间：{lastUpdateText}（多交易所模式）
          </div>
        </div>

        <div className="filter-controls">
          <div className="filter-label">交易所：</div>
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
              持仓管理
            </button>
          </div>
        </div>
      </header>

      {error && <div className="error-banner">{error}</div>}

      <div className="stats-grid">
        <div className="stats-card">
          <div className="stat-label">监控市场</div>
          <div className="stat-value">{totalMarkets}</div>
        </div>
        <div className="stats-card">
          <div className="stat-label">计算路线</div>
          <div className="stat-value highlight">{oppCount}</div>
        </div>
        <div className="stats-card">
          <div className="stat-label">最佳24h收益</div>
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
                ? `${sortedSymbols[0].sym}（24h估算：${sortedSymbols[0].bestDailyYieldBps.toFixed(1)} bps）`
                : sortedSymbols[0].dailyYield > 0
                  ? `${sortedSymbols[0].sym}（仅资金费）`
                  : '暂无机会'
              : '暂无机会'}
          </div>
        </div>
      </div>

      <div className="data-table-container">
        <table>
          <thead>
            <tr>
              <th className="col-asset">币种</th>
              {exchanges.map(ex => <th key={ex}>{formatExchangeName(ex)}</th>)}
              <th className="col-gap">资金费差</th>
              <th className="col-action">套利方向</th>
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

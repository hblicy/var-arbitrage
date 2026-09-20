import React, { useState, useEffect, useRef, useMemo, useCallback } from 'react';
import PositionsModal from './PositionsModal';
import FundingRadar from './FundingRadar';
import {
  getObservationDisplay,
  partitionOpportunities,
  selectBestReviewCandidates,
} from './opportunityViewModel';

const STORAGE_KEY = 'arbitrage_exchange_prefs';
const MIN_EXCHANGES_REQUIRED = 2;
const POLL_INTERVAL_MS = 10000;
const DEFAULT_EXCHANGES = ['binance', 'variational', 'hyperliquid', 'aster', 'lighter', 'arcus', 'bulk', 'risex'];
const EMPTY_DASHBOARD_DATA = {
  markets: {},
  opportunities: [],
  reasons: {},
  symbol_max_intervals: {},
  exchange_status: {},
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
  exchange_status: json?.exchange_status || {},
  data_version: Number(json?.data_version || 0),
});

const getDashboardSignature = (item) => [
  item.data_version,
  item.last_update,
  Object.keys(item.markets || {}).length,
  JSON.stringify(item.markets || {}),
  (item.opportunities || []).length,
  Object.entries(item.exchange_status || {})
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([key, value]) => `${key}:${value.state}:${value.last_success_at || ''}`)
    .join(','),
].join('|');

const EXCHANGE_LABELS = {
  aster: 'Aster',
  arcus: 'Arcus',
  bulk: 'Bulk',
  risex: 'RISEx',
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

const entryCheckKey = (opportunity) => `${opportunity.symbol}:${opportunity.direction}`;

const entryCheckMessage = (entryCheck) => {
  if (!entryCheck) return null;
  if (entryCheck.status === 'checking') return '正在获取双边深度盘口…';
  if (entryCheck.status === 'actionable') {
    return `可人工开仓：收敛净空间 ${formatSignedBps(entryCheck.net_convergence_bps)} bps，安全余量 ${formatSignedBps(entryCheck.safety_margin_bps)} bps（2 秒内有效）`;
  }
  if (entryCheck.status === 'expired') return '复核报价已过期，请重新复核。';
  const reason = {
    DEPTH_UNSUPPORTED: '该交易所组合暂不支持深度复核，只能观察。',
    INSUFFICIENT_DEPTH: '1000 USDT 单腿的盘口深度不足。',
    INSUFFICIENT_EDGE: '扣除往返手续费和安全垫后，收敛空间不足。',
    STALE_QUOTE: '报价已过期。',
    LEG_SKEW: '两边报价时间差过大。',
    QUOTE_FETCH_FAILED: '实时盘口获取失败。',
  }[entryCheck.reason] || '暂不可人工开仓。';
  return `不可开：${reason}`;
};

const entryCheckStatusLabel = (entryCheck) => {
  if (!entryCheck) return '待实时复核';
  if (entryCheck.status === 'checking') return '正在复核';
  if (entryCheck.status === 'actionable') return '实时可开（2 秒）';
  if (entryCheck.status === 'expired') return '报价已过期';
  return '暂不可开';
};

const ArbitrageCell = React.memo(function ArbitrageCell({ opportunity, entryCheck, manualReview, onVerifyEntry }) {
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
          <small>&nbsp; 理论 24h</small>
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
        {!manualReview && (
          <>
            <div className={`execution-state ${entryCheck?.status || 'pending'}`}>
              <span className="execution-state-dot"></span>
              {entryCheckStatusLabel(entryCheck)}
            </div>
            <button
              className="entry-check-btn"
              onClick={() => onVerifyEntry(opportunity)}
              disabled={entryCheck?.status === 'checking'}
            >
              {entryCheck?.status === 'checking' ? '复核中…' : '复核 1000 USDT/腿'}
            </button>
            {entryCheck && (
              <div className={`entry-check-result ${entryCheck.status === 'actionable' ? 'pass' : 'fail'}`}>
                {entryCheckMessage(entryCheck)}
              </div>
            )}
          </>
        )}
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

const ObservationPool = React.memo(function ObservationPool({ opportunities }) {
  const [showAll, setShowAll] = useState(true);
  const { visible, hiddenCount } = getObservationDisplay(opportunities, showAll);

  if (opportunities.length === 0) return null;

  return (
    <details className="observation-pool" open>
      <summary>观察池 · {opportunities.length} 条不可复核路线</summary>
      <p>这些路线暂不支持双边实时深度复核，仅用于跟踪资金费和价差。Variational 使用询价机制，公开报价可能缓存 10 分钟，需到交易所确认实际成交报价。</p>
      <div className="observation-list">
        {visible.map((opportunity) => (
          <div className="observation-row" key={`${opportunity.symbol}:${opportunity.direction}`}>
            <span className="observation-symbol">{opportunity.symbol.replace('USDT', '')}</span>
            <span>{formatExchangeName(opportunity.details.buy_exchange)} 多 → {formatExchangeName(opportunity.details.sell_exchange)} 空</span>
            <span className="observation-yield">理论 24h {formatSignedPercent(calcProjected24hPercent(opportunity))}</span>
            <span className="observation-status">
              {[opportunity.details.buy_exchange, opportunity.details.sell_exchange]
                .some((exchange) => exchange?.toLowerCase() === 'variational')
                ? 'Variational 需实时询价' : '暂未接入深度复核'}
            </span>
          </div>
        ))}
      </div>
      {hiddenCount > 0 && (
        <button className="observation-toggle" type="button" onClick={() => setShowAll(true)}>
          展开其余 {hiddenCount} 条观察路线
        </button>
      )}
      {showAll && opportunities.length > 20 && (
        <button className="observation-toggle" type="button" onClick={() => setShowAll(false)}>
          收起至前 20 条
        </button>
      )}
    </details>
  );
});

const MarketRow = React.memo(function MarketRow({ row, marketSet, exchanges, symbolMaxIntervals, entryChecks, manualReviewKeys, onVerifyEntry }) {
  const { sym, delta, bestSymbolOpp, baseInterval } = row;
  const manualReview = bestSymbolOpp && manualReviewKeys.has(entryCheckKey(bestSymbolOpp));

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
          {bestSymbolOpp ? (
            <>
              <span className="apr-yield">理论 {formatSignedPercent(calcProjected24hPercent(bestSymbolOpp))} <small>/24h</small></span>
              {!manualReview && <span className="bps-value">实时盘口复核前</span>}
            </>
          ) : (
            <>
              <span className="observation-gap">仅观察</span>
              <span className="bps-value">资金费差 {delta.toFixed(1)} bps / {baseInterval}h</span>
            </>
          )}
        </div>
      </td>
      <td>
          <ArbitrageCell
            opportunity={bestSymbolOpp}
            entryCheck={bestSymbolOpp ? entryChecks[entryCheckKey(bestSymbolOpp)] : null}
            manualReview={manualReview}
            onVerifyEntry={onVerifyEntry}
          />
      </td>
    </tr>
  );
});

function App() {
  const [strategyView, setStrategyView] = useState(true);
  const [data, setData] = useState(EMPTY_DASHBOARD_DATA);
  const [exchangeStates, setExchangeStates] = useState([]);
  const [entryChecks, setEntryChecks] = useState({});
  const [error, setError] = useState(null);
  const [isModalOpen, setIsModalOpen] = useState(false);
  const isModalOpenRef = useRef(false);
  const wasModalOpenRef = useRef(false);
  const abortControllerRef = useRef(null);

  const fetchData = useCallback(async ({ force = false } = {}) => {
    if (abortControllerRef.current && !abortControllerRef.current.signal.aborted && !force) return;
    abortControllerRef.current?.abort();

    const controller = new AbortController();
    abortControllerRef.current = controller;
    let timedOut = false;
    const timeout = setTimeout(() => { timedOut = true; controller.abort(); }, 30000);
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
      if (abortControllerRef.current !== controller || (err.name === 'AbortError' && !timedOut)) return;
      setError(timedOut ? '行情数据加载超时，将自动重试。' : '连接后端失败，请确认 api.py 正在运行。');
      console.error('Fetch error:', err);
    } finally {
      clearTimeout(timeout);
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

  const verifyEntry = useCallback(async (opportunity) => {
    const key = entryCheckKey(opportunity);
    setEntryChecks(prev => ({ ...prev, [key]: { status: 'checking' } }));
    try {
      const response = await fetch('/api/entry-check', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          symbol: opportunity.symbol,
          long_exchange: opportunity.details.buy_exchange,
          short_exchange: opportunity.details.sell_exchange,
        }),
      });
      if (!response.ok) throw new Error('entry check failed');
      const result = await response.json();
      setEntryChecks(prev => ({ ...prev, [key]: result }));
      if (result.status === 'actionable') {
        window.setTimeout(() => {
          setEntryChecks(prev => (
            prev[key] === result ? { ...prev, [key]: { status: 'expired' } } : prev
          ));
        }, 2000);
      }
    } catch {
      setEntryChecks(prev => ({ ...prev, [key]: { status: 'unavailable', reason: 'QUOTE_FETCH_FAILED' } }));
    }
  }, []);

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
  const opportunityPools = useMemo(
    () => partitionOpportunities(data.opportunities, enabledExchangeSet),
    [data.opportunities, enabledExchangeSet],
  );
  const automaticOpportunities = opportunityPools.executable;
  const manualOpportunities = opportunityPools.manual;
  const observationOpportunities = opportunityPools.observation;
  const reviewOpportunities = useMemo(
    () => [...automaticOpportunities, ...manualOpportunities],
    [automaticOpportunities, manualOpportunities],
  );
  const manualReviewKeys = useMemo(
    () => new Set(manualOpportunities.map(entryCheckKey)),
    [manualOpportunities],
  );

  const bestOppBySymbol = useMemo(
    () => selectBestReviewCandidates(
      automaticOpportunities,
      manualOpportunities,
      getOpportunityApr,
    ),
    [automaticOpportunities, manualOpportunities],
  );

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
      return { sym, delta, bestSymbolOpp, baseInterval };
    });
  }, [data.markets, data.symbol_max_intervals, bestOppBySymbol, exchanges]);

  const sortedSymbols = useMemo(() => {
    return symbolsWithDelta
      .filter((item) => item.bestSymbolOpp)
      .sort((a, b) => getOpportunityApr(b.bestSymbolOpp) - getOpportunityApr(a.bestSymbolOpp));
  }, [symbolsWithDelta]);

  const visibleSymbols = useMemo(() => sortedSymbols.slice(0, 100), [sortedSymbols]);
  const lastUpdateText = useMemo(() => formatLastUpdate(data.last_update), [data.last_update]);
  const totalMarkets = symbolsWithDelta.length;
  const oppCount = reviewOpportunities.length;
  const actionableCount = automaticOpportunities.reduce((count, opportunity) => (
    entryChecks[entryCheckKey(opportunity)]?.status === 'actionable' ? count + 1 : count
  ), 0);
  const unavailableExchanges = Object.entries(data.exchange_status)
    .filter(([, value]) => value.state !== 'fresh')
    .map(([key, value]) => `${formatExchangeName(key)}（${
      value.state === 'error' ? '抓取失败' : value.state === 'stale' ? '行情过期' : '无行情'
    }）`);

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
        {unavailableExchanges.length > 0 && (
          <div className="stale-banner">
            已排除：{unavailableExchanges.join('、')}；不会沿用旧行情计算机会。
          </div>
        )}

      <nav className="strategy-tabs" aria-label="监控视图">
        <button type="button" aria-pressed={strategyView} onClick={() => setStrategyView(true)}>资金费策略</button>
        <button type="button" aria-pressed={!strategyView} onClick={() => setStrategyView(false)}>原始机会 / 价差复核</button>
      </nav>
      {strategyView ? <FundingRadar enabledExchanges={enabledExchangeSet} /> : <>
      <div className="stats-grid">
        <div className="stats-card">
          <div className="stat-label">监控市场</div>
          <div className="stat-value">{totalMarkets}</div>
        </div>
        <div className="stats-card">
          <div className="stat-label">待复核候选</div>
          <div className="stat-value highlight">{oppCount}</div>
        </div>
        <div className="stats-card">
          <div className="stat-label">实时可开</div>
          <div className={`stat-value ${actionableCount > 0 ? 'highlight-green' : ''}`}>{actionableCount}</div>
          <div className="stat-sub">通过 1000 USDT/腿实时盘口复核</div>
        </div>
      </div>

      <div className="data-table-container">
        <table>
          <thead>
            <tr>
              <th className="col-asset">币种</th>
              {exchanges.map(ex => <th key={ex}>{formatExchangeName(ex)}</th>)}
              <th className="col-gap">执行评估</th>
              <th className="col-action">执行候选</th>
            </tr>
          </thead>
          <tbody>
            {visibleSymbols.length > 0 ? (
              visibleSymbols.map((row) => (
                <MarketRow
                  key={row.sym}
                  row={row}
                  marketSet={data.markets[row.sym]}
                  exchanges={exchanges}
                  symbolMaxIntervals={data.symbol_max_intervals}
                  entryChecks={entryChecks}
                  manualReviewKeys={manualReviewKeys}
                  onVerifyEntry={verifyEntry}
                />
              ))
            ) : (
              <tr>
                <td className="candidate-empty" colSpan={exchanges.length + 3}>
                  暂无待复核候选；其他不可复核路线已放入下方观察池。
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <ObservationPool opportunities={observationOpportunities} />
      </>}

      <PositionsModal isOpen={isModalOpen} onClose={() => setIsModalOpen(false)} />
    </div>
  );
}

export default App;

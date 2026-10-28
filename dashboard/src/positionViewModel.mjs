export function positionViewModel(positions, elapsedSeconds = 0) {
  const entries = Object.entries(positions).map(([id, data]) => {
    const priceHealth = {};
    for (const leg of ['long', 'short']) {
      const health = data.price_health?.[leg];
      const age = Number.isFinite(health?.age_seconds) ? health.age_seconds + Math.max(0, elapsedSeconds) : null;
      let status = health?.status || 'unknown';
      if (status === 'fresh' && age === null) status = 'unknown';
      if (status === 'fresh' && data.price_max_age_seconds > 0 && age > data.price_max_age_seconds) status = 'stale';
      priceHealth[leg] = { ...health, status, age_seconds: age };
    }
    const available = data.prices_available === true && ['long', 'short'].every(leg => priceHealth[leg].status === 'fresh');
    return [id, {
      ...data, prices_available: available, price_health: priceHealth,
      current_price_long: priceHealth.long.status === 'fresh' ? data.current_price_long : null,
      current_price_short: priceHealth.short.status === 'fresh' ? data.current_price_short : null,
      pnl: { ...data.pnl, mtm_pnl: available ? data.pnl?.mtm_pnl ?? null : null,
        total_pnl: available ? data.pnl?.total_pnl ?? null : null },
    }];
  });
  const totals = { notional: 0, funding_income: 0, total_fees: 0, mtm_pnl: 0, total_pnl: 0 };
  for (const [, { pnl }] of entries) {
    for (const key of Object.keys(totals)) {
      if (key === 'mtm_pnl' || key === 'total_pnl') {
        totals[key] = totals[key] === null || pnl[key] == null ? null : totals[key] + pnl[key];
      } else totals[key] += pnl[key] || 0;
    }
  }
  return { entries, totals };
}

export function priceAgeLabel(health) {
  if (health.status === 'missing') return '缺少报价';
  if (health.age_seconds == null || health.status === 'unknown') return '时效未知';
  const source = { http_cache: '缓存估算', received: '采集', exchange: '报价', exchange_ws: '实时报价' }[health.timestamp_source] || '报价';
  return `${health.status === 'stale' ? '已过期 · ' : ''}${source} ${Math.floor(health.age_seconds)}s`;
}

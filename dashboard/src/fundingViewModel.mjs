export function selectFundingRows(rows, hours, exchanges, search = '') {
  return rows.filter(row => exchanges.has(row.buy_exchange) && exchanges.has(row.sell_exchange)
    && row.symbol?.toLowerCase().includes(search.toLowerCase()))
    .sort((a, b) => (b.projections[String(hours)]?.net_bps ?? -Infinity)
      - (a.projections[String(hours)]?.net_bps ?? -Infinity));
}

export const liveDepth = (depth, now) => depth && !depth.reason && depth.expires_at >= now ? depth : null;

export function selectFundingDepth(manual, automatic, now) {
  if (liveDepth(automatic, now) && (!manual || (!manual.reason && automatic.checked_at > manual.checked_at))) {
    return automatic;
  }
  return manual || automatic;
}

export const signalState = (row, now, maxAge) => maxAge && now - row.timestamp > maxAge ? 'paused' : row.status;

export const reasonText = reason => ({
  HISTORY_WARMUP: '历史样本预热中', LOW_NET_RETURN: '预计收益未达标', BOOK_UNAVAILABLE: '缺少双向盘口',
  FUNDING_UNAVAILABLE: '缺少资金费率', STALE_DATA: '行情过期或中断', DEPTH_NOT_CHECKED: '本轮尚未核验容量',
  DEPTH_UNSUPPORTED: '此路线需人工核验盘口', INSUFFICIENT_DEPTH: '盘口深度不足', STALE_QUOTE: '盘口报价过期',
  QUOTE_EXPIRED: '容量报价已过期，请重新复核', LEG_SKEW: '两腿报价时间差过大', QUOTE_FETCH_FAILED: '盘口获取失败',
  EMPTY_BOOK: '盘口为空', INVALID_BOOK: '盘口数据异常', INVALID_MARKET: '行情数据异常',
  INVALID_QUOTE_TIME: '报价时间异常', FUNDING_SCHEDULE_INVALID: '结算时间异常',
  SCAN_INTERRUPTED: '本轮复核中断，重新计时',
}[reason] || reason || '满足当前筛选条件');

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { liveDepth, reasonText, selectFundingDepth, selectFundingRows, signalState } from './fundingViewModel.mjs';
import './FundingRadar.css';

const format = (value, digits = 2) => Number.isFinite(value) ? value.toLocaleString('zh-CN', { maximumFractionDigits: digits, minimumFractionDigits: digits }) : '—';
const signed = value => Number.isFinite(value) ? `${value > 0 ? '+' : ''}${format(value)}` : '—';
const stateNames = { watch: '观察', pending: '持续观察中', triggered: '已达标 · 须复核', paused: '数据暂停', ended: '机会结束' };
const timeText = seconds => Number.isFinite(seconds) ? new Date(seconds * 1000).toLocaleString('zh-CN', { hour12: false }) : '—';

function HistoryChart({ points, field, title, hours, through }) {
  const valid = points.filter(point => Number.isFinite(point[field]));
  if (!valid.length) return <div className="fund-chart-empty">{title}：等待完整分钟样本</div>;
  const end = through || valid.at(-1).time + 60;
  const start = end - hours * 3600;
  const minimum = Math.min(...valid.map(point => point[field]));
  const maximum = Math.max(...valid.map(point => point[field]));
  const padding = Math.max((maximum - minimum) * 0.1, 0.1);
  const low = minimum - padding;
  const high = maximum + padding;
  const path = valid.map((point, index) => {
    const x = 48 + (point.time - start) / (end - start) * 592;
    const y = 155 - (point[field] - low) / (high - low) * 135;
    const command = index === 0 || point.time - valid[index - 1].time > 60 ? 'M' : 'L';
    return `${command}${x.toFixed(2)},${y.toFixed(2)}`;
  }).join(' ');
  return <figure className="fund-chart">
    <figcaption>{title} · 分钟加权均值</figcaption>
    <svg viewBox="0 0 660 185" role="img" aria-label={`${title}最近${hours}小时历史走势，缺失分钟不连线`}>
      <text x="0" y="24">{format(high, 1)}</text><text x="0" y="155">{format(low, 1)}</text>
      <line x1="48" x2="640" y1="155" y2="155" stroke="currentColor" opacity=".25" />
      <path d={path} fill="none" stroke="#60a5fa" strokeWidth="2" />
      <text x="48" y="181">-{hours}h</text><text x="608" y="181">现在</text>
    </svg>
  </figure>;
}

function DepthTable({ depth, now }) {
  const current = liveDepth(depth, now);
  if (!current) return <p className="fund-muted">{reasonText(depth?.reason || 'QUOTE_EXPIRED')}</p>;
  return <>
    <p className="fund-capacity">可见盘口容量：{format(current.capacity?.notional_usd, 0)} USD / 腿
      {current.capacity?.book_limited ? '（达到当前订单簿上限）' : '（受净收益门槛限制）'}
    </p>
    <div className="fund-scroll"><table><thead><tr><th>单腿金额</th><th>共同数量</th><th>开仓多 / 空 VWAP</th><th>平仓多 / 空 VWAP</th><th>预计净额</th></tr></thead>
      <tbody>{current.tiers.map((tier, index) => <tr key={index}>
        <td>{format(tier.notional_usd, 0)} USD</td><td>{format(tier.quantity, 6)}</td>
        <td>{format(tier.entry_buy_vwap, 4)} / {format(tier.entry_sell_vwap, 4)}</td>
        <td>{format(tier.exit_buy_vwap, 4)} / {format(tier.exit_sell_vwap, 4)}</td>
        <td>{tier.reason ? reasonText(tier.reason) : `${signed(tier.net_usd)} USD (${signed(tier.net_bps)} bps)`}</td>
      </tr>)}</tbody></table></div>
    <p className="fund-muted">报价有效期剩余 {Math.max(0, current.expires_at - now).toFixed(1)} 秒；容量是当前深度估算，不是账户余额。</p>
  </>;
}

export default function FundingRadar({ enabledExchanges }) {
  const [packet, setPacket] = useState(null);
  const [error, setError] = useState('');
  const [clock, setClock] = useState(() => performance.now());
  const [chosenHours, setChosenHours] = useState(null);
  const [historyHours, setHistoryHours] = useState(4);
  const [search, setSearch] = useState('');
  const [selectedKey, setSelectedKey] = useState(null);
  const [history, setHistory] = useState(null);
  const [check, setCheck] = useState(null);
  const scanAbort = useRef(null);
  const checkAbort = useRef(null);
  const requestSequence = useRef(0);

  const refresh = useCallback(async () => {
    scanAbort.current?.abort();
    const controller = new AbortController();
    scanAbort.current = controller;
    const started = performance.now();
    try {
      const response = await fetch('/api/funding', { signal: controller.signal, cache: 'no-store' });
      if (!response.ok) throw new Error('资金费策略服务暂不可用');
      const data = await response.json();
      if (!controller.signal.aborted) { setPacket({ data, received: started }); setError(''); }
    } catch (err) { if (err.name !== 'AbortError') setError(err.message); }
  }, []);

  useEffect(() => {
    refresh();
    const interval = setInterval(() => { if (!document.hidden) refresh(); }, 10000);
    const timer = setInterval(() => setClock(performance.now()), 500);
    return () => { clearInterval(interval); clearInterval(timer); scanAbort.current?.abort(); };
  }, [refresh]);

  const data = packet?.data;
  const hours = chosenHours ?? data?.default_hours ?? 8;
  const now = data ? data.server_now + Math.max(0, clock - packet.received) / 1000 : 0;
  const rows = useMemo(() => selectFundingRows(data?.rows || [], hours, enabledExchanges, search), [data, hours, enabledExchanges, search]);
  const selected = rows.find(row => row.key === selectedKey);

  useEffect(() => {
    if (!selectedKey) return;
    const controller = new AbortController();
    const [symbol, long_exchange, short_exchange] = selectedKey.split('|');
    const params = new URLSearchParams({ symbol, long_exchange, short_exchange, hours: historyHours });
    const load = async () => {
      try {
        const response = await fetch(`/api/funding/history?${params}`, { signal: controller.signal, cache: 'no-store' });
        if (!response.ok) throw new Error('历史数据加载失败');
        const result = await response.json();
        if (!controller.signal.aborted) setHistory({ key: selectedKey, hours: historyHours, result });
      } catch (err) {
        if (err.name !== 'AbortError') setHistory({ key: selectedKey, hours: historyHours, error: err.message });
      }
    };
    load();
    const timer = setInterval(load, 30000);
    return () => { controller.abort(); clearInterval(timer); };
  }, [selectedKey, historyHours]);

  useEffect(() => () => { checkAbort.current?.abort(); }, [selectedKey, hours]);

  const verify = async () => {
    if (!selected) return;
    checkAbort.current?.abort();
    const controller = new AbortController();
    checkAbort.current = controller;
    const sequence = ++requestSequence.current;
    setCheck({ key: selected.key, hours, pending: true });
    try {
      const response = await fetch('/api/funding/check', {
        method: 'POST', signal: controller.signal, headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ symbol: selected.symbol, long_exchange: selected.buy_exchange, short_exchange: selected.sell_exchange, hours }),
      });
      if (!response.ok) throw new Error('容量复核失败，请稍后重试');
      const result = await response.json();
      if (!controller.signal.aborted && sequence === requestSequence.current) setCheck({ key: selected.key, hours, result });
    } catch (err) {
      if (sequence === requestSequence.current) {
        setCheck(err.name === 'AbortError' ? null : { key: selected.key, hours, error: err.message });
      }
    }
  };

  const currentHistory = history?.key === selectedKey && history.hours === historyHours ? history : null;
  const currentCheck = check?.key === selectedKey && check.hours === hours ? check : null;
  const projection = selected?.projections[String(hours)];
  const selectedDepth = selectFundingDepth(currentCheck?.result,
    hours === selected?.holding_hours ? selected?.depth : null, now);

  return <section className="fund-radar" aria-label="资金费策略监控">
    <div className="fund-toolbar">
      <div><h2>资金费策略</h2><p className="fund-muted">评估持有期收益、稳定性和计划金额的可成交深度</p></div>
      <label>评估持有期 <select value={hours} onChange={event => setChosenHours(Number(event.target.value))}>
        {[1, 4, 8, 24].map(value => <option key={value} value={value}>{value} 小时</option>)}
      </select></label>
      <input aria-label="搜索资金费标的" placeholder="搜索币种" value={search} onChange={event => setSearch(event.target.value)} />
      <button type="button" onClick={refresh}>刷新</button>
    </div>
    <p className="fund-assumptions">单腿名义金额 {format(data?.notional_usd || 1000, 0)} USD；假设资金费率与退出盘口保持当前值。已计四笔手续费，不以保证金为收益分母，不代表保证收益。</p>
    <p className="fund-muted">通知使用 {data?.default_hours || 8}h 情景、净收益 ≥ {data?.min_net_bps ?? 20} bps、历史覆盖 ≥ {format((data?.min_coverage ?? 0.8)*100, 0)}%、连续 {data?.sustain_seconds ?? 120}s。切换评估时长仅影响本页排序与复核。</p>
    {error && <p className="error-banner" role="alert">{error}</p>}
    {!data && !error && <p role="status">正在加载策略数据…</p>}
    <div className="fund-scroll"><table className="fund-table"><thead><tr>
      <th>标的 / 方向</th><th>{hours}h 预计净收益</th><th>当前费差 / 小时</th><th>价差较历史均值</th><th>历史覆盖</th><th>连续达标 / 状态</th><th>容量</th>
    </tr></thead><tbody>
      {rows.slice(0, 100).map(row => {
        const state = signalState(row, now, data?.market_stale_seconds);
        const forecast = row.projections[String(hours)];
        const depth = hours === row.holding_hours ? liveDepth(row.depth, now) : null;
        return <tr key={row.key} className={selectedKey === row.key ? 'fund-selected' : ''}>
          <td><button className="fund-route" onClick={() => setSelectedKey(row.key)}>{row.symbol.replace('USDT', '')}<small>{row.buy_exchange} 多 / {row.sell_exchange} 空</small></button></td>
          <td className={state !== 'paused' && forecast?.net_usd > 0 ? 'fund-positive' : ''}>{state === 'paused' ? '—' : `${signed(forecast?.net_usd)} USD`}<small>{signed(forecast?.net_bps)} bps · 顶层盘口估算</small></td>
          <td>{signed(row.funding_hourly_bps)} bps</td><td>{signed(row.spread_deviation_bps)} bps</td>
          <td>{format(row.history.coverage*100, 1)}%<small>{format(row.history.valid_seconds/3600)} / {row.history.window_hours || data?.history_hours}h</small></td>
          <td><span className={`fund-state ${state}`}>{stateNames[state]}</span><small>{state === 'paused' ? '行情已中断' : `${format(row.qualified_seconds, 0)} / ${row.required_seconds}s · ${reasonText(row.reason)}`}</small></td>
          <td>{depth?.capacity ? `${format(depth.capacity.notional_usd, 0)} USD` : '待实时复核'}<small>按单腿名义金额</small></td>
        </tr>;
      })}
      {!rows.length && <tr><td colSpan="7" className="fund-empty">暂无可分析路线。等待有效双边行情；历史样本会随采集逐步积累。</td></tr>}
    </tbody></table></div>
    {rows.length > 100 && <p className="fund-muted">显示收益排序前 100 条，可搜索币种或调整交易所筛选。</p>}
    {selected && <section className="fund-detail" aria-label="资金费路线详情">
      <div className="fund-toolbar"><h3>{selected.symbol} · {selected.buy_exchange} 多 / {selected.sell_exchange} 空</h3><button onClick={() => setSelectedKey(null)}>关闭详情</button></div>
      {projection?.reason ? <p>{reasonText(projection.reason)}</p> : <>
        <div className="fund-breakdown">
          {[['预计资金费', projection?.funding_usd], ['开仓价差收益', projection?.entry_spread_usd], ['平仓价差成本', projection?.exit_spread_cost_usd], ['四笔手续费', projection?.fees_usd], [`${hours}h 净收益`, projection?.net_usd]].map(([label, value]) => <div key={label}><span>{label}</span><strong>{signed(value)} USD</strong></div>)}
        </div>
        <p className="fund-muted">预计结算：多腿 {projection?.buy_schedule?.count} 次 / 空腿 {projection?.sell_schedule?.count} 次。下一次：{timeText(projection?.buy_schedule?.next_at)} / {timeText(projection?.sell_schedule?.next_at)}。</p>
        <p className="fund-muted">结算时点依据：多腿 {projection?.buy_schedule?.source === 'exchange' ? '交易所提供' : '按周期推算'}；空腿 {projection?.sell_schedule?.source === 'exchange' ? '交易所提供' : '按周期推算'}。实际费率、结算时间及退出价格可能变化。</p>
      </>}
      <div className="fund-toolbar"><h3>计划金额与盘口容量</h3><button onClick={verify} disabled={currentCheck?.pending}>{currentCheck?.pending ? '复核中…' : `复核 ${hours}h 分档容量`}</button></div>
      {currentCheck?.error && <p role="alert">{currentCheck.error}</p>}
      <DepthTable depth={selectedDepth} now={now} />
      <div className="fund-toolbar"><h3>历史基准</h3><label>回看 <select value={historyHours} onChange={event => setHistoryHours(Number(event.target.value))}>{[1, 4, 8, 24].map(value => <option key={value} value={value}>{value} 小时</option>)}</select></label></div>
      {!currentHistory && <p>正在加载历史…</p>}
      {currentHistory?.error && <p role="alert">{currentHistory.error}</p>}
      {currentHistory?.result && <>
        <p className="fund-muted">有效覆盖 {format((currentHistory.result.summary?.coverage || 0)*100, 1)}%；正费差时间占比 {format((currentHistory.result.summary?.positive_funding_ratio || 0)*100, 1)}%；价差波动 {format(currentHistory.result.summary?.spread_std_bps)} bps。缺失时段不补样本，历史均值不代表必然回归。</p>
        <div className="fund-charts"><HistoryChart points={currentHistory.result.series} field="spread_bps" title="可成交价差（bps）" hours={historyHours} through={currentHistory.result.summary?.through_at} />
          <HistoryChart points={currentHistory.result.series} field="funding_hourly_bps" title="小时资金费差（bps）" hours={historyHours} through={currentHistory.result.summary?.through_at} /></div>
        <h3>最近状态变化</h3><div className="fund-events">{currentHistory.result.events.length ? currentHistory.result.events.map((event, index) => <p key={index}>{timeText(event.occurred_at)} · {stateNames[event.status]} · {reasonText(event.reason)} · 累计 {format(event.qualified_seconds, 0)}s</p>) : <p>暂无事件</p>}</div>
      </>}
    </section>}
  </section>;
}

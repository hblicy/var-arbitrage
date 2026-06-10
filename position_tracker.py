"""Position tracking and exit signal detection.

Tracks user positions and alerts when funding rates flip unfavorably.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from config import Settings

logger = logging.getLogger(__name__)

POSITIONS_FILE = os.path.join(os.path.dirname(__file__), "positions.json")

# In-memory state tracking for unfavorable funding periods
# Key: position_id, Value: datetime when unfavorable state first detected
_unfavorable_since: Dict[str, datetime] = {}


def load_positions() -> Dict[str, Dict]:
    """Load positions from JSON file."""
    if not os.path.exists(POSITIONS_FILE):
        return {}
    try:
        with open(POSITIONS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Error loading positions: {e}")
        return {}


def save_positions(positions: Dict[str, Dict]) -> bool:
    """Save positions to JSON file."""
    try:
        with open(POSITIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(positions, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        logger.error(f"Error saving positions: {e}")
        return False


def add_position(
    symbol: str,
    direction: str,
    long_exchange: str,
    short_exchange: str,
    entry_funding_long: float = 0,
    entry_funding_short: float = 0,
    entry_qty: float = 0,
    entry_price_long: float = 0,
    entry_price_short: float = 0,
) -> Dict:
    """Add a new position to track.
    
    Args:
        symbol: Trading pair, e.g. "DUSKUSDT"
        direction: e.g. "variational_long_binance_short"
        long_exchange: Exchange where you are long
        short_exchange: Exchange where you are short
        entry_funding_long: Funding rate on long side at entry
        entry_funding_short: Funding rate on short side at entry
        entry_qty: Number of coins per leg (e.g. 1000 DUSK)
        entry_price_long: Entry price per coin on long exchange
        entry_price_short: Entry price per coin on short exchange
    
    Returns:
        The created position object
    """
    positions = load_positions()
    
    # Create a unique ID: SYMBOL_LONG_SHORT_TIMESTAMP (秒级精度防碰撞)
    ts_suffix = datetime.now().strftime("%m%d%H%M%S")
    position_id = f"{symbol}_{long_exchange}_{short_exchange}_{ts_suffix}".upper()
    
    position = {
        "id": position_id,
        "symbol": symbol.upper(),
        "direction": direction,
        "long_exchange": long_exchange.lower(),
        "short_exchange": short_exchange.lower(),
        "entry_time": datetime.now().isoformat(),
        "entry_funding": {
            "long": entry_funding_long,
            "short": entry_funding_short,
        },
        "entry_qty": entry_qty,
        "entry_price_long": entry_price_long,
        "entry_price_short": entry_price_short,
    }
    
    positions[position_id] = position
    save_positions(positions)
    logger.info(f"Added position: {position_id} (qty={entry_qty}, long_p={entry_price_long}, short_p={entry_price_short})")
    return position


def remove_position(position_id: str) -> bool:
    """Remove a position by its unique ID.
    
    Returns:
        True if removed, False if not found
    """
    positions = load_positions()
    position_id = position_id.upper()
    
    if position_id in positions:
        del positions[position_id]
        save_positions(positions)
        # Also clear unfavorable tracking for this position
        _unfavorable_since.pop(position_id, None)
        logger.info(f"Removed position: {position_id}")
        return True
    return False


def get_all_positions() -> Dict[str, Dict]:
    """Get all tracked positions."""
    return load_positions()


def estimate_position_pnl(
    pos: Dict,
    long_taker_bps: float = 5.0,
    short_taker_bps: float = 5.0,
) -> Optional[Dict[str, Any]]:
    """Estimate cumulative P&L for a position.
    
    Funding income = cumulative_funding (真实结算累计值, 离散结算点入账).
    不再使用连续线性插值估算未结算部分, 避免 1h/4h 结算的周期误差.
    
    Args:
        pos: Position dict with entry_qty, entry_price_long/short, entry_time, entry_funding
        long_taker_bps: Taker fee on long exchange (bps)
        short_taker_bps: Taker fee on short exchange (bps)
    
    Returns:
        Dict with cost, income, net_pnl, is_profitable; or None if no qty/price/funding
    """
    entry_qty = pos.get("entry_qty", 0)
    # Robust fallback: check for None or 0 to handle various storage states
    entry_price_long = pos.get("entry_price_long") or pos.get("entry_price_short") or pos.get("entry_price", 0)
    entry_price_short = pos.get("entry_price_short") or pos.get("entry_price_long") or pos.get("entry_price", 0)
        
    if not entry_qty or (not entry_price_long and not entry_price_short):
        return None
    
    cur_price_long = pos.get("current_price_long")
    cur_price_short = pos.get("current_price_short")

    price_to_use_long = cur_price_long if cur_price_long else entry_price_long
    price_to_use_short = cur_price_short if cur_price_short else entry_price_short

    # 按照最新市值计算名义价值 (Notional Value)
    notional_long = entry_qty * price_to_use_long
    notional_short = entry_qty * price_to_use_short
    avg_notional = (notional_long + notional_short) / 2
    
    # Trading fees: open + close, both legs each time
    # 开仓手续费按开仓价，平仓预估手续费按当前价
    fee_open_long = (entry_qty * entry_price_long) * long_taker_bps / 10000
    fee_open_short = (entry_qty * entry_price_short) * short_taker_bps / 10000
    fee_close_long = notional_long * long_taker_bps / 10000
    fee_close_short = notional_short * short_taker_bps / 10000
    total_fees = fee_open_long + fee_open_short + fee_close_long + fee_close_short
    
    # Funding income: 仅使用真实结算累计值 (离散结算点入账)
    # 不再做连续线性插值估算, 避免 1h/4h 结算的周期误差和跳变
    funding_income = pos.get("cumulative_funding", 0)
    
    # hours_held for display
    entry_time_str = pos.get("entry_time", "")
    try:
        entry_time = datetime.fromisoformat(entry_time_str)
        hours_held = max(0, (datetime.now() - entry_time).total_seconds() / 3600)
    except (ValueError, TypeError):
        hours_held = 0
    
    net_pnl = funding_income - total_fees
    
    return {
        "notional": round(avg_notional, 2),
        "total_fees": round(total_fees, 2),
        "funding_income": round(funding_income, 2),
        "net_pnl": round(net_pnl, 2),
        "is_profitable": net_pnl > 0,
        "hours_held": round(hours_held, 1),
    }


# ══════════════════════════════════════════════════════════════════
#  Funding Rate Accumulator — 精确时间窗口结算
#  :55 快照 → :01 结算
# ══════════════════════════════════════════════════════════════════

def _prev_settlement_utc(interval_h: int, utc_now: datetime) -> datetime:
    """计算上一个 UTC 对齐的结算整点。

    Examples (UTC):
        interval=1h, now=08:23 → 08:00
        interval=4h, now=09:10 → 08:00
        interval=8h, now=17:30 → 16:00
    """
    interval = max(interval_h, 1)
    hour = utc_now.hour
    prev_hour = (hour // interval) * interval
    return utc_now.replace(hour=prev_hour, minute=0, second=0, microsecond=0)


def _next_settlement_utc(interval_h: int, utc_now: datetime) -> datetime:
    """计算下一个 UTC 对齐的结算整点。

    Examples (UTC):
        interval=1h, now=08:23 → 09:00
        interval=4h, now=09:10 → 12:00
        interval=8h, now=17:30 → 00:00 (next day)
    """
    interval = max(interval_h, 1)
    hour = utc_now.hour
    next_hour = ((hour // interval) + 1) * interval
    result = utc_now.replace(minute=0, second=0, microsecond=0)
    if next_hour >= 24:
        result = result + timedelta(days=next_hour // 24)
        next_hour = next_hour % 24
    return result.replace(hour=next_hour)


def _is_snapshot_window(interval_h: int, utc_now: datetime) -> bool:
    """判断当前是否处于结算前 5 分钟的快照窗口 (X:55 ~ X:00)。

    Example: interval=1h, now=08:56 UTC → next settle=09:00 → 距离4分钟 → True
    """
    next_settle = _next_settlement_utc(interval_h, utc_now)
    minutes_until = (next_settle - utc_now).total_seconds() / 60
    return 0 < minutes_until <= 5


def _is_settle_window(interval_h: int, utc_now: datetime) -> bool:
    """判断当前是否处于结算后 5 分钟的结算窗口 (X:00 ~ X:05)。

    Example: interval=1h, now=09:02 UTC → prev settle=09:00 → 距离2分钟 → True
    """
    prev_settle = _prev_settlement_utc(interval_h, utc_now)
    minutes_since = (utc_now - prev_settle).total_seconds() / 60
    return 0 <= minutes_since <= 5


def _calc_funding_payment(rate: float, notional: float, is_long: bool) -> float:
    """计算单腿资金费支付。

    Long side: rate > 0 → 多方付给空方 → 亏损 (负值)
    Short side: rate > 0 → 空方收取 → 收入 (正值)
    """
    if is_long:
        return -rate * notional  # 多方正费率 = 支出
    else:
        return rate * notional   # 空方正费率 = 收入


def update_pre_settlement_rates(exchanges_data: Dict[str, Dict]) -> None:
    """每 30 秒调用。持续更新 current_funding 用于显示,
    并在结算前 5 分钟窗口内锁定 pre_settlement_rate 快照。
    """
    from datetime import timezone

    positions = load_positions()
    if not positions:
        return

    utc_now = datetime.now(timezone.utc).replace(tzinfo=None)  # naive UTC
    changed = False

    for p_id, pos in positions.items():
        symbol = pos.get("symbol", p_id.split("_")[0] if "_" in p_id else p_id).upper()
        long_ex = pos.get("long_exchange", "").lower()
        short_ex = pos.get("short_exchange", "").lower()

        for leg, ex_name in [("long", long_ex), ("short", short_ex)]:
            market = exchanges_data.get(ex_name, {}).get(symbol)
            if not market or getattr(market, 'funding_rate', None) is None:
                continue

            rate_raw = market.funding_rate
            interval_h = getattr(market, 'native_interval_hours', 8) or 8

            # ── 始终更新 current_funding (8h 标准化, 用于 PnL 估算显示) ──
            scaled = rate_raw * (8 / interval_h)
            cur_key = f"current_funding_{leg}"
            if pos.get(cur_key) != scaled:
                pos[cur_key] = scaled
                changed = True

            # ── 只在 :55 快照窗口内锁定 pre_settlement_rate ──
            if _is_snapshot_window(interval_h, utc_now):
                snap_key = f"pre_settlement_rate_{leg}"
                snap_time_key = f"snapshot_time_{leg}"
                snap_interval_key = f"snapshot_interval_{leg}"
                # 更新快照 (窗口内每 30s 都覆盖, 确保用的是最新值)
                pos[snap_key] = rate_raw
                pos[snap_time_key] = utc_now.isoformat()
                pos[snap_interval_key] = interval_h
                changed = True
                logger.debug(
                    "📸 %s [%s/%s] 快照费率: %.6f (interval=%dh)",
                    symbol, leg, ex_name, rate_raw, interval_h,
                )

    if changed:
        save_positions(positions)


def _utc_to_local_naive(utc_dt: datetime) -> datetime:
    """将 Naive UTC 转换为 Naive Local Time"""
    from datetime import timezone
    aware_utc = utc_dt.replace(tzinfo=timezone.utc)
    local_aware = aware_utc.astimezone()
    return local_aware.replace(tzinfo=None)


def accumulate_funding(
    exchanges_data: Dict[str, Dict],
) -> int:
    """每 30 秒由 api.py 调用 (monitor.py 做兜底)。
    
    正常路径: 在结算后 5 分钟窗口内, 使用 :55 快照的费率进行记账。
    补记路径: 如果错过了正常窗口, 逐周期补记, 使用当前实时费率。

    Returns:
        本次新增的结算记录数
    """
    from datetime import timezone

    positions = load_positions()
    if not positions:
        return 0

    utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
    local_now = datetime.now()  # 用于和 last_settlement (local) 比较
    settlements_recorded = 0
    changed = False

    for p_id, pos in positions.items():
        symbol = pos.get("symbol", "").upper()
        long_ex = pos.get("long_exchange", "").lower()
        short_ex = pos.get("short_exchange", "").lower()
        entry_qty = pos.get("entry_qty", 0)

        if not entry_qty:
            continue

        # 初始化结构
        if "funding_history" not in pos:
            pos["funding_history"] = []
            pos["cumulative_funding"] = 0.0
            changed = True

        # 处理两条腿
        for leg, ex_name, price_key, is_long in [
            ("long", long_ex, "entry_price_long", True),
            ("short", short_ex, "entry_price_short", False),
        ]:
            market = exchanges_data.get(ex_name, {}).get(symbol)
            if not market:
                continue

            interval_h = getattr(market, 'native_interval_hours', 8) or 8
            last_key = f"last_settlement_{leg}"

            # ── 确定需要结算的周期列表 ──
            # 统一用 local time 做比较, 消除时区混淆
            prev_settle_utc = _prev_settlement_utc(interval_h, utc_now)
            prev_settle_local = _utc_to_local_naive(prev_settle_utc)
            
            in_settle_window = _is_settle_window(interval_h, utc_now)
            
            # 解析上次结算时间 (local time)
            last_settle_str = pos.get(last_key, "")
            last_settle_local = None
            if last_settle_str:
                try:
                    last_settle_local = datetime.fromisoformat(last_settle_str)
                except (ValueError, TypeError):
                    pass
            
            # 如果没有上次结算记录, 使用 entry_time 作为起点
            if last_settle_local is None:
                entry_time_str = pos.get("entry_time", "")
                if entry_time_str:
                    try:
                        last_settle_local = datetime.fromisoformat(entry_time_str)
                    except (ValueError, TypeError):
                        continue
                else:
                    continue

            # ── 计算从 last_settle 到 now 之间有多少个待结算周期 ──
            interval_seconds = interval_h * 3600
            elapsed = (local_now - last_settle_local).total_seconds()
            
            if elapsed < interval_seconds:
                # 没有超过一个周期, 检查是否在正常结算窗口内
                if not in_settle_window:
                    continue
                # 在结算窗口内, 但上次结算已经 >= 本轮结算点, 说明已记过
                if last_settle_local >= prev_settle_local:
                    continue
                # 正常结算: 只结算 1 期
                periods_to_settle = [prev_settle_local]
            else:
                # 超过了一个周期 — 需要补记
                # 从 last_settle 之后的下一个结算点开始, 逐个补到 prev_settle_local
                periods_to_settle = []
                # 用 local time 对齐找到 last_settle 之后的第一个结算点
                cursor_utc = _prev_settlement_utc(interval_h, utc_now)
                # 往回倒, 找到所有 > last_settle_local 且 <= prev_settle_local 的结算点
                cursor_local = _utc_to_local_naive(cursor_utc)
                
                # 先收集 prev_settle_local 及之前的所有漏记结算点
                check_local = prev_settle_local
                while check_local > last_settle_local:
                    periods_to_settle.append(check_local)
                    # 往前推一个周期
                    check_local = check_local - timedelta(hours=interval_h)
                
                periods_to_settle.reverse()  # 按时间正序
                
                # 限制最多补 24 期 (防止异常拖死系统)
                if len(periods_to_settle) > 24:
                    logger.warning("🚨 %s [%s/%s] 漏记超24期, 只补最近24期", symbol, leg, ex_name)
                    periods_to_settle = periods_to_settle[-24:]
            
            if not periods_to_settle:
                continue

            # ── 越权保护: 只有在结算前开仓的持仓才需要缴纳 ──
            entry_time_str = pos.get("entry_time", "")
            entry_time_local = None
            if entry_time_str:
                try:
                    entry_time_local = datetime.fromisoformat(entry_time_str)
                except (ValueError, TypeError):
                    pass

            # ── 对每个待结算周期逐一入账 ──
            for settle_point in periods_to_settle:
                # 越权保护: 如果开仓晚于此结算点, 跳过
                if entry_time_local and entry_time_local >= settle_point:
                    continue
                
                # 防重复
                if last_settle_local and last_settle_local >= settle_point:
                    continue

                is_catchup = (settle_point != prev_settle_local) or not in_settle_window

                # ── 取费率 ──
                rate = None
                snap_key = f"pre_settlement_rate_{leg}"
                snap_interval_key = f"snapshot_interval_{leg}"
                snap_time_key = f"snapshot_time_{leg}"

                # 只有正常结算(非补记、且是当前周期)才使用快照
                if not is_catchup and snap_key in pos:
                    snap_interval = pos.get(snap_interval_key, interval_h)
                    if snap_interval != interval_h:
                        pos.pop(snap_key, None)
                        pos.pop(snap_interval_key, None)
                        pos.pop(snap_time_key, None)
                        changed = True
                    else:
                        snap_time_str = pos.get(snap_time_key, "")
                        try:
                            snap_time = datetime.fromisoformat(snap_time_str)
                            if (prev_settle_utc - snap_time).total_seconds() > 15 * 60:
                                pass  # 过期, 不使用
                            else:
                                rate = pos[snap_key]
                        except:
                            pass

                if rate is None:
                    rate_raw = getattr(market, 'funding_rate', None)
                    if rate_raw is not None:
                        rate = rate_raw
                        if is_catchup:
                            logger.warning("🚨 %s [%s/%s] 补记结算点 %s, 使用实时费率: %.8f",
                                           symbol, leg, ex_name, settle_point.strftime("%m-%d %H:%M"), rate)
                        else:
                            logger.warning("⚠️ %s [%s/%s] 退化使用实时费率: %.8f", symbol, leg, ex_name, rate)
                    else:
                        continue

                # 名义价值用最新市价
                cur_price = getattr(market, 'price', None)
                entry_price = pos.get(price_key, 0) or 0
                price_to_use = cur_price if cur_price else entry_price
                notional = entry_qty * price_to_use

                if notional <= 0:
                    continue

                payment = _calc_funding_payment(rate, notional, is_long)

                record = {
                    "time": datetime.now().isoformat(),
                    "settle_local_time": settle_point.isoformat(),
                    "leg": leg,
                    "exchange": ex_name,
                    "rate": round(rate, 8),
                    "interval_h": interval_h,
                    "notional": round(notional, 2),
                    "payment": round(payment, 4),
                    "is_catchup": is_catchup,
                }
                pos["funding_history"].append(record)
                pos["cumulative_funding"] = round(
                    pos.get("cumulative_funding", 0) + payment, 4
                )
                pos[last_key] = settle_point.isoformat()
                last_settle_local = settle_point  # 同步更新内存变量

                # 清除已用快照 (只在正常结算后清除)
                if not is_catchup:
                    pos.pop(snap_key, None)
                    pos.pop(snap_interval_key, None)
                    pos.pop(snap_time_key, None)

                settlements_recorded += 1
                changed = True

                tag = "🚨补记" if is_catchup else "💰"
                logger.info(
                    "%s %s [%s/%s] 资金费结算: rate=%.6f, notional=$%.2f, payment=$%.4f, 累计=$%.4f",
                    tag, symbol, leg, ex_name, rate, notional, payment,
                    pos.get("cumulative_funding", 0),
                )

    if changed:
        save_positions(positions)

    if settlements_recorded:
        logger.info("资金费结算: 本次记录 %d 笔", settlements_recorded)

    return settlements_recorded


def check_exit_signals(
    exchanges_data: Dict[str, Dict],
    cfg: Optional["Settings"] = None,
) -> List[Dict[str, Any]]:
    """Check if any positions need to be closed due to unfavorable funding.
    
    Exit signal condition:
    - Net funding becomes negative (you are losing money on funding)
    - The unfavorable state must persist for at least `threshold_minutes` (from config)
    
    Args:
        exchanges_data: Dict of exchange_name -> {symbol: MarketDatum}
        cfg: Settings object for threshold configuration
    
    Returns:
        List of exit signal alerts
    """
    positions = load_positions()
    alerts = []
    
    # Get threshold from config, default to 5 minutes
    threshold_minutes = 5
    if cfg and hasattr(cfg, 'thresholds'):
        threshold_minutes = getattr(cfg.thresholds, 'funding_reversal_threshold_minutes', 5)
    threshold_duration = timedelta(minutes=threshold_minutes)
    now = datetime.now()
    
    for p_id, pos in positions.items():
        # Fallback for old records that might rely on key as symbol
        symbol = pos.get("symbol", p_id.split("_")[0] if "_" in p_id else p_id).upper()

        long_ex = pos.get("long_exchange", "").lower()
        short_ex = pos.get("short_exchange", "").lower()
        
        # Get current funding rates
        long_data = exchanges_data.get(long_ex, {}).get(symbol)
        short_data = exchanges_data.get(short_ex, {}).get(symbol)
        
        if not long_data or not short_data:
            logger.debug(f"Missing data for {symbol}: long_ex={long_ex}, short_ex={short_ex}")
            continue
        
        # Extract funding rates (handle both MarketDatum objects and dicts)
        # Extract funding rates and intervals
        if hasattr(long_data, 'funding_rate'):
            long_rate = long_data.funding_rate or 0
            short_rate = short_data.funding_rate or 0
            long_interval = getattr(long_data, 'native_interval_hours', 8) or 8
            short_interval = getattr(short_data, 'native_interval_hours', 8) or 8
        else:
            long_rate = long_data.get('funding_rate', 0) or 0
            short_rate = short_data.get('funding_rate', 0) or 0
            long_interval = long_data.get('native_interval_hours', 8) or 8
            short_interval = short_data.get('native_interval_hours', 8) or 8
        
        # Normalize everything to 8h basis for consistent alerting
        # regardless of whether the native interval is 1h, 4h or 8h.
        base_interval = 8
        
        # Rate per 8h = rate * (8 / native_interval)
        long_rate_scaled = long_rate * (8 / long_interval)
        short_rate_scaled = short_rate * (8 / short_interval)
        
        # Net funding yield (8h Basis)
        # You are Long A, Short B. Profit = Short_Rate - Long_Rate
        net_funding_scaled = short_rate_scaled - long_rate_scaled
        
        # Check if funding is unfavorable
        # Get threshold from config (default -33.3 bps ≈ 24h 亏损 1%)
        exit_threshold_bps = -33.3
        if cfg and hasattr(cfg, 'thresholds'):
            exit_threshold_bps = getattr(cfg.thresholds, 'exit_min_net_funding_bps', -33.3)
            
        # Convert bps to decimal: -33.3 bps = -0.00333
        is_unfavorable = net_funding_scaled < (exit_threshold_bps / 10000.0)
        
        if is_unfavorable:
            # Track when unfavorable state started
            if p_id not in _unfavorable_since:
                _unfavorable_since[p_id] = now
                logger.info(f"Position {p_id}: Funding turned unfavorable, starting {threshold_minutes}min countdown")
            
            # Check if threshold duration has passed
            unfavorable_start = _unfavorable_since[p_id]
            elapsed = now - unfavorable_start
            
            if elapsed >= threshold_duration:
                # Estimate P&L if qty/price available
                pnl_info = None
                if cfg:
                    long_taker = getattr(cfg.exchanges.get(long_ex), 'taker_bps', 5.0) if hasattr(cfg, 'exchanges') else 5.0
                    short_taker = getattr(cfg.exchanges.get(short_ex), 'taker_bps', 5.0) if hasattr(cfg, 'exchanges') else 5.0
                    
                    # Inject current funding rates (normalized to 8h basis, same as entry_funding)
                    pos_copy = pos.copy()
                    pos_copy["current_funding_long"] = long_rate_scaled
                    pos_copy["current_funding_short"] = short_rate_scaled

                    # Inject current prices for MTM calculation
                    if hasattr(long_data, 'price'):
                        pos_copy["current_price_long"] = long_data.price
                    elif isinstance(long_data, dict):
                        pos_copy["current_price_long"] = long_data.get('price')
                    if hasattr(short_data, 'price'):
                        pos_copy["current_price_short"] = short_data.price
                    elif isinstance(short_data, dict):
                        pos_copy["current_price_short"] = short_data.get('price')

                    pnl_info = estimate_position_pnl(
                        pos_copy,
                        long_taker_bps=long_taker,
                        short_taker_bps=short_taker,
                    )

                    # Add MTM (mark-to-market) PnL to match dashboard
                    if pnl_info:
                        entry_qty = pos.get("entry_qty", 0)
                        entry_price_long = pos.get("entry_price_long", 0)
                        entry_price_short = pos.get("entry_price_short", 0)
                        cur_price_long = pos_copy.get("current_price_long")
                        cur_price_short = pos_copy.get("current_price_short")

                        mtm_pnl = 0.0
                        if entry_qty and entry_price_long and entry_price_short and cur_price_long and cur_price_short:
                            mtm_pnl = (cur_price_long - entry_price_long) * entry_qty + \
                                      (entry_price_short - cur_price_short) * entry_qty

                        pnl_info["mtm_pnl"] = round(mtm_pnl, 2)
                        pnl_info["total_pnl"] = round(pnl_info["net_pnl"] + mtm_pnl, 2)
                
                # Threshold exceeded - trigger alert
                alert_data = {
                    "symbol": symbol,
                    "direction": pos.get("direction", "unknown"),
                    "long_exchange": long_ex,
                    "short_exchange": short_ex,
                    "current_funding": {
                        "long": long_rate,
                        "short": short_rate,
                    },
                    "native_intervals": {
                        "buy": long_interval,
                        "sell": short_interval,
                    },
                    "base_interval": base_interval,
                    "net_funding_bps": net_funding_scaled * 10000,
                    "entry_funding": pos.get("entry_funding", {}),
                    "entry_time": pos.get("entry_time", ""),
                    "unfavorable_duration_minutes": elapsed.total_seconds() / 60,
                    "reason": "FUNDING_NEGATIVE_SPREAD",
                    "current_price_long": pos_copy.get("current_price_long"),
                    "current_price_short": pos_copy.get("current_price_short"),
                }
                if pnl_info:
                    alert_data["pnl"] = pnl_info
                alerts.append(alert_data)
                logger.warning(
                    f"Exit signal for {symbol}: Net {base_interval}h Funding = {net_funding_scaled*100:.4f}% "
                    f"(持续不利 {elapsed.total_seconds()/60:.1f} min, threshold={threshold_minutes}min)"
                    + (f" | PnL: ${pnl_info['net_pnl']:.2f}" if pnl_info else "")
                )
            else:
                remaining = (threshold_duration - elapsed).total_seconds() / 60
                logger.debug(f"Position {p_id}: Unfavorable for {elapsed.total_seconds()/60:.1f}min, {remaining:.1f}min until alert")
        else:
            # Funding recovered - clear the tracking
            if p_id in _unfavorable_since:
                elapsed = now - _unfavorable_since[p_id]
                logger.info(f"Position {p_id}: Funding recovered after {elapsed.total_seconds()/60:.1f}min (no alert triggered)")
                del _unfavorable_since[p_id]
    
    return alerts


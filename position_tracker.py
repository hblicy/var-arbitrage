"""Position tracking and exit signal detection (SQLite-backed).

Tracks user positions and alerts when funding rates flip unfavorably.
Multi-user: each position belongs to an owner_id.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional, TYPE_CHECKING

from db import (
    init_db,
    create_position,
    delete_position,
    get_all_positions_raw,
    get_positions,
    update_position,
    update_position_health,
)

if TYPE_CHECKING:
    from config import Settings

logger = logging.getLogger(__name__)

# In-memory state tracking for unfavorable funding periods
# Key: position_id, Value: datetime when unfavorable state first detected
_unfavorable_since: Dict[str, datetime] = {}


def _parse_json_field(val: Any, default: Any = None) -> Any:
    """Parse a JSON string field from the database, handling legacy data."""
    if val is None:
        return default
    if isinstance(val, (list, dict)):
        return val
    if isinstance(val, str):
        try:
            return json.loads(val)
        except (json.JSONDecodeError, TypeError):
            return default
    return default


# ══════════════════════════════════════════════════════════════════
# Position CRUD — delegates to db.py
# ══════════════════════════════════════════════════════════════════

def init_tracker():
    """Initialize the database at startup."""
    init_db()


def load_positions() -> Dict[str, Dict]:
    """Backward-compatible alias for the old JSON loader."""
    return get_all_positions_raw()


def save_positions(positions: Dict[str, Dict]) -> bool:
    """Backward-compatible saver for legacy tests/scripts."""
    try:
        for p_id, pos in positions.items():
            update_position(p_id, **pos)
        return True
    except Exception as e:
        logger.error("Error saving positions: %s", e)
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
    owner_id: str = "",
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
        owner_id: ID of the user who owns this position

    Returns:
        The created position object (as dict)
    """
    position = create_position(
        symbol=symbol,
        direction=direction,
        long_exchange=long_exchange,
        short_exchange=short_exchange,
        entry_funding_long=entry_funding_long,
        entry_funding_short=entry_funding_short,
        entry_qty=entry_qty,
        entry_price_long=entry_price_long,
        entry_price_short=entry_price_short,
        owner_id=owner_id,
    )
    logger.info(f"Added position: {position['id']} (owner={owner_id}, qty={entry_qty})")
    return position


def remove_position(position_id: str) -> bool:
    """Mark a position as closed (soft delete).

    Returns:
        True if removed, False if not found
    """
    # Also clear unfavorable tracking
    _unfavorable_since.pop(position_id, None)
    result = delete_position(position_id)
    if result:
        logger.info(f"Removed position: {position_id}")
    return result


def get_all_positions() -> Dict[str, Dict]:
    """Get all open positions as a dict keyed by position_id."""
    return load_positions()


def get_positions_for_owner(owner_id: str) -> List[Dict]:
    """Get all open positions belonging to a specific owner."""
    return get_positions(owner_id=owner_id, status="open")


def _safe_update_position_health(position_id: str, **fields) -> None:
    """Update DB health state without breaking alert calculation."""
    try:
        update_position_health(position_id, **fields)
    except Exception as e:
        logger.debug("Skip position health update for %s: %s", position_id, e)


# ══════════════════════════════════════════════════════════════════
# PnL Estimation
# ══════════════════════════════════════════════════════════════════

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
    fee_open_long = (entry_qty * entry_price_long) * long_taker_bps / 10000
    fee_open_short = (entry_qty * entry_price_short) * short_taker_bps / 10000
    fee_close_long = notional_long * long_taker_bps / 10000
    fee_close_short = notional_short * short_taker_bps / 10000
    total_fees = fee_open_long + fee_open_short + fee_close_long + fee_close_short

    # Funding income: 仅使用真实结算累计值 (离散结算点入账)
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
    """计算上一个 UTC 对齐的结算整点。"""
    interval = max(interval_h, 1)
    hour = utc_now.hour
    prev_hour = (hour // interval) * interval
    return utc_now.replace(hour=prev_hour, minute=0, second=0, microsecond=0)


def _next_settlement_utc(interval_h: int, utc_now: datetime) -> datetime:
    """计算下一个 UTC 对齐的结算整点。"""
    interval = max(interval_h, 1)
    hour = utc_now.hour
    next_hour = ((hour // interval) + 1) * interval
    result = utc_now.replace(minute=0, second=0, microsecond=0)
    if next_hour >= 24:
        result = result + timedelta(days=next_hour // 24)
        next_hour = next_hour % 24
    return result.replace(hour=next_hour)


def _is_snapshot_window(interval_h: int, utc_now: datetime) -> bool:
    """判断当前是否处于结算前 5 分钟的快照窗口 (X:55 ~ X:00)。"""
    next_settle = _next_settlement_utc(interval_h, utc_now)
    minutes_until = (next_settle - utc_now).total_seconds() / 60
    return 0 < minutes_until <= 5


def _is_settle_window(interval_h: int, utc_now: datetime) -> bool:
    """判断当前是否处于结算后 5 分钟的结算窗口 (X:00 ~ X:05)。"""
    prev_settle = _prev_settlement_utc(interval_h, utc_now)
    minutes_since = (utc_now - prev_settle).total_seconds() / 60
    return 0 <= minutes_since <= 5


def _calc_funding_payment(rate: float, notional: float, is_long: bool) -> float:
    """计算单腿资金费支付。

    Long side: rate > 0 → 多方付给空方 → 亏损 (负值)
    Short side: rate > 0 → 空方收取 → 收入 (正值)
    """
    if is_long:
        return -rate * notional
    else:
        return rate * notional


def _utc_to_local_naive(utc_dt: datetime) -> datetime:
    """将 Naive UTC 转换为 Naive Local Time"""
    from datetime import timezone
    aware_utc = utc_dt.replace(tzinfo=timezone.utc)
    local_aware = aware_utc.astimezone()
    return local_aware.replace(tzinfo=None)


def update_pre_settlement_rates(exchanges_data: Dict[str, Dict]) -> None:
    """每 30 秒调用。持续更新 current_funding 用于显示,
    并在结算前 5 分钟窗口内锁定 pre_settlement_rate 快照。
    """
    from datetime import timezone

    positions = get_all_positions()
    if not positions:
        return

    utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
    changed_positions: Dict[str, Dict] = {}

    for p_id, pos in positions.items():
        symbol = pos.get("symbol", p_id.split("_")[0] if "_" in p_id else p_id).upper()
        long_ex = pos.get("long_exchange", "").lower()
        short_ex = pos.get("short_exchange", "").lower()

        pos_changed = False

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
                pos_changed = True

            # ── 只在 :55 快照窗口内锁定 pre_settlement_rate ──
            if _is_snapshot_window(interval_h, utc_now):
                snap_key = f"pre_settlement_rate_{leg}"
                snap_time_key = f"snapshot_time_{leg}"
                snap_interval_key = f"snapshot_interval_{leg}"
                pos[snap_key] = rate_raw
                pos[snap_time_key] = utc_now.isoformat()
                pos[snap_interval_key] = interval_h
                pos_changed = True
                logger.debug(
                    "📸 %s [%s/%s] 快照费率: %.6f (interval=%dh)",
                    symbol, leg, ex_name, rate_raw, interval_h,
                )

        if pos_changed:
            changed_positions[p_id] = pos

    # Batch update to DB
    for p_id, pos in changed_positions.items():
        update_position(
            p_id,
            current_funding_long=pos.get("current_funding_long"),
            current_funding_short=pos.get("current_funding_short"),
            pre_settlement_rate_long=pos.get("pre_settlement_rate_long"),
            pre_settlement_rate_short=pos.get("pre_settlement_rate_short"),
            snapshot_time_long=pos.get("snapshot_time_long"),
            snapshot_time_short=pos.get("snapshot_time_short"),
            snapshot_interval_long=pos.get("snapshot_interval_long"),
            snapshot_interval_short=pos.get("snapshot_interval_short"),
        )


def accumulate_funding(
    exchanges_data: Dict[str, Dict],
) -> int:
    """每 30 秒由 api.py 调用.

    正常路径: 在结算后 5 分钟窗口内, 使用 :55 快照的费率进行记账。
    补记路径: 如果错过了正常窗口, 逐周期补记, 使用当前实时费率。

    Returns:
        本次新增的结算记录数
    """
    from datetime import timezone

    positions = get_all_positions()
    if not positions:
        return 0

    utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
    local_now = datetime.now()
    settlements_recorded = 0
    changed_positions: Dict[str, Dict] = {}

    for p_id, pos in positions.items():
        symbol = pos.get("symbol", "").upper()
        long_ex = pos.get("long_exchange", "").lower()
        short_ex = pos.get("short_exchange", "").lower()
        entry_qty = pos.get("entry_qty", 0)

        if not entry_qty:
            continue

        # 初始化 funding_history from JSON string
        pos["funding_history"] = _parse_json_field(pos.get("funding_history"), [])
        if "cumulative_funding" not in pos:
            pos["cumulative_funding"] = 0.0

        pos_changed = False

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
                if not in_settle_window:
                    continue
                if last_settle_local >= prev_settle_local:
                    continue
                periods_to_settle = [prev_settle_local]
            else:
                periods_to_settle = []
                cursor_utc = _prev_settlement_utc(interval_h, utc_now)
                cursor_local = _utc_to_local_naive(cursor_utc)

                check_local = prev_settle_local
                while check_local > last_settle_local:
                    periods_to_settle.append(check_local)
                    check_local = check_local - timedelta(hours=interval_h)

                periods_to_settle.reverse()

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
                if entry_time_local and entry_time_local >= settle_point:
                    continue
                if last_settle_local and last_settle_local >= settle_point:
                    continue

                is_catchup = (settle_point != prev_settle_local) or not in_settle_window

                # ── 取费率 ──
                rate = None
                snap_key = f"pre_settlement_rate_{leg}"
                snap_interval_key = f"snapshot_interval_{leg}"
                snap_time_key = f"snapshot_time_{leg}"

                if not is_catchup and snap_key in pos:
                    snap_interval = pos.get(snap_interval_key, interval_h)
                    if snap_interval != interval_h:
                        pos.pop(snap_key, None)
                        pos.pop(snap_interval_key, None)
                        pos.pop(snap_time_key, None)
                        pos_changed = True
                    else:
                        snap_time_str = pos.get(snap_time_key, "")
                        try:
                            snap_time = datetime.fromisoformat(snap_time_str)
                            if (prev_settle_utc - snap_time).total_seconds() > 15 * 60:
                                pass
                            else:
                                rate = pos[snap_key]
                        except Exception:
                            pass

                if rate is None:
                    rate_raw = getattr(market, 'funding_rate', None)
                    if rate_raw is not None:
                        rate = rate_raw
                        if is_catchup:
                            logger.warning(
                                "🚨 %s [%s/%s] 补记结算点 %s, 使用实时费率: %.8f",
                                symbol, leg, ex_name, settle_point.strftime("%m-%d %H:%M"), rate,
                            )
                        else:
                            logger.warning(
                                "⚠️ %s [%s/%s] 退化使用实时费率: %.8f",
                                symbol, leg, ex_name, rate,
                            )
                    else:
                        continue

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
                last_settle_local = settle_point
                pos_changed = True

                if not is_catchup:
                    pos.pop(snap_key, None)
                    pos.pop(snap_interval_key, None)
                    pos.pop(snap_time_key, None)

                settlements_recorded += 1

                tag = "🚨补记" if is_catchup else "💰"
                logger.info(
                    "%s %s [%s/%s] 资金费结算: rate=%.6f, notional=$%.2f, payment=$%.4f, 累计=$%.4f",
                    tag, symbol, leg, ex_name, rate, notional, payment,
                    pos.get("cumulative_funding", 0),
                )

        if pos_changed:
            changed_positions[p_id] = pos

    # Batch update to DB
    for p_id, pos in changed_positions.items():
        update_position(
            p_id,
            cumulative_funding=pos.get("cumulative_funding"),
            funding_history=pos.get("funding_history"),
            last_settlement_long=pos.get("last_settlement_long"),
            last_settlement_short=pos.get("last_settlement_short"),
            pre_settlement_rate_long=pos.get("pre_settlement_rate_long"),
            pre_settlement_rate_short=pos.get("pre_settlement_rate_short"),
            snapshot_time_long=pos.get("snapshot_time_long"),
            snapshot_time_short=pos.get("snapshot_time_short"),
            snapshot_interval_long=pos.get("snapshot_interval_long"),
            snapshot_interval_short=pos.get("snapshot_interval_short"),
        )

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
        List of exit signal alerts, each including owner_id for routing
    """
    positions = get_all_positions()
    alerts = []

    # Get threshold from config, default to 5 minutes
    threshold_minutes = 5
    if cfg and hasattr(cfg, 'thresholds'):
        threshold_minutes = getattr(cfg.thresholds, 'funding_reversal_threshold_minutes', 5)
    threshold_duration = timedelta(minutes=threshold_minutes)
    now = datetime.now()

    for p_id, pos in positions.items():
        # Skip closed positions
        if pos.get("status") == "closed":
            continue

        symbol = pos.get("symbol", p_id.split("_")[0] if "_" in p_id else p_id).upper()
        long_ex = pos.get("long_exchange", "").lower()
        short_ex = pos.get("short_exchange", "").lower()
        owner_id = pos.get("owner_id", "")

        # Get current funding rates
        long_data = exchanges_data.get(long_ex, {}).get(symbol)
        short_data = exchanges_data.get(short_ex, {}).get(symbol)

        if not long_data or not short_data:
            logger.debug(f"Missing data for {symbol}: long_ex={long_ex}, short_ex={short_ex}")
            continue

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

        base_interval = 8
        long_rate_scaled = long_rate * (8 / long_interval)
        short_rate_scaled = short_rate * (8 / short_interval)
        net_funding_scaled = short_rate_scaled - long_rate_scaled

        # Get threshold from config
        exit_threshold_bps = -33.3
        if cfg and hasattr(cfg, 'thresholds'):
            exit_threshold_bps = getattr(cfg.thresholds, 'exit_min_net_funding_bps', -33.3)

        is_unfavorable = net_funding_scaled < (exit_threshold_bps / 10000.0)

        if is_unfavorable:
            if p_id not in _unfavorable_since:
                _unfavorable_since[p_id] = now
                logger.info(f"Position {p_id}: Funding turned unfavorable, starting {threshold_minutes}min countdown")

            unfavorable_start = _unfavorable_since[p_id]
            elapsed = now - unfavorable_start

            if elapsed >= threshold_duration:
                # Update health status
                if owner_id:
                    _safe_update_position_health(
                        p_id,
                        health_status="reversal",
                        reversal_reason="FUNDING_NEGATIVE_SPREAD",
                        last_signal_at=now.isoformat(),
                    )

                pnl_info = None
                if cfg:
                    long_taker = getattr(cfg.exchanges.get(long_ex), 'taker_bps', 5.0) if hasattr(cfg, 'exchanges') else 5.0
                    short_taker = getattr(cfg.exchanges.get(short_ex), 'taker_bps', 5.0) if hasattr(cfg, 'exchanges') else 5.0

                    pos_copy = pos.copy()
                    pos_copy["current_funding_long"] = long_rate_scaled
                    pos_copy["current_funding_short"] = short_rate_scaled

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

                alert_data = {
                    "position_id": p_id,
                    "symbol": symbol,
                    "direction": pos.get("direction", "unknown"),
                    "long_exchange": long_ex,
                    "short_exchange": short_ex,
                    "owner_id": owner_id,
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
                    "entry_funding": {
                        "long": pos.get("entry_funding_long", 0),
                        "short": pos.get("entry_funding_short", 0),
                    },
                    "entry_time": pos.get("entry_time", ""),
                    "unfavorable_duration_minutes": elapsed.total_seconds() / 60,
                    "reason": "FUNDING_NEGATIVE_SPREAD",
                    "current_price_long": pos_copy.get("current_price_long") if pnl_info else None,
                    "current_price_short": pos_copy.get("current_price_short") if pnl_info else None,
                }
                if pnl_info:
                    alert_data["pnl"] = pnl_info
                alerts.append(alert_data)
                logger.warning(
                    f"Exit signal for {symbol} (owner={owner_id}): Net {base_interval}h Funding = "
                    f"{net_funding_scaled*100:.4f}% (持续不利 {elapsed.total_seconds()/60:.1f}min, "
                    f"threshold={threshold_minutes}min)"
                    + (f" | PnL: ${pnl_info['net_pnl']:.2f}" if pnl_info else "")
                )
            else:
                remaining = (threshold_duration - elapsed).total_seconds() / 60
                logger.debug(f"Position {p_id}: Unfavorable for {elapsed.total_seconds()/60:.1f}min, {remaining:.1f}min until alert")
        else:
            # Funding recovered
            if p_id in _unfavorable_since:
                elapsed = now - _unfavorable_since[p_id]
                logger.info(f"Position {p_id}: Funding recovered after {elapsed.total_seconds()/60:.1f}min (no alert triggered)")
                del _unfavorable_since[p_id]
                # Reset health status
                if owner_id:
                    _safe_update_position_health(p_id, health_status="healthy", reversal_reason=None, last_signal_at=None)

    return alerts


def check_exit_signals_for_owner(
    exchanges_data: Dict[str, Dict],
    owner_id: str,
    cfg: Optional["Settings"] = None,
) -> List[Dict[str, Any]]:
    """Check exit signals for positions belonging to a specific owner only."""
    positions = get_positions_for_owner(owner_id)
    alerts = []

    threshold_minutes = 5
    if cfg and hasattr(cfg, 'thresholds'):
        threshold_minutes = getattr(cfg.thresholds, 'funding_reversal_threshold_minutes', 5)
    threshold_duration = timedelta(minutes=threshold_minutes)
    now = datetime.now()

    for pos in positions:
        p_id = pos["id"]
        if pos.get("status") == "closed":
            continue

        symbol = pos.get("symbol", p_id.split("_")[0] if "_" in p_id else p_id).upper()
        long_ex = pos.get("long_exchange", "").lower()
        short_ex = pos.get("short_exchange", "").lower()

        long_data = exchanges_data.get(long_ex, {}).get(symbol)
        short_data = exchanges_data.get(short_ex, {}).get(symbol)

        if not long_data or not short_data:
            continue

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

        base_interval = 8
        long_rate_scaled = long_rate * (8 / long_interval)
        short_rate_scaled = short_rate * (8 / short_interval)
        net_funding_scaled = short_rate_scaled - long_rate_scaled

        exit_threshold_bps = -33.3
        if cfg and hasattr(cfg, 'thresholds'):
            exit_threshold_bps = getattr(cfg.thresholds, 'exit_min_net_funding_bps', -33.3)

        is_unfavorable = net_funding_scaled < (exit_threshold_bps / 10000.0)

        if is_unfavorable:
            if p_id not in _unfavorable_since:
                _unfavorable_since[p_id] = now

            unfavorable_start = _unfavorable_since[p_id]
            elapsed = now - unfavorable_start

            if elapsed >= threshold_duration:
                alert_data = {
                    "position_id": p_id,
                    "symbol": symbol,
                    "direction": pos.get("direction", "unknown"),
                    "long_exchange": long_ex,
                    "short_exchange": short_ex,
                    "owner_id": owner_id,
                    "current_funding": {"long": long_rate, "short": short_rate},
                    "native_intervals": {"buy": long_interval, "sell": short_interval},
                    "base_interval": base_interval,
                    "net_funding_bps": net_funding_scaled * 10000,
                    "entry_funding": {
                        "long": pos.get("entry_funding_long", 0),
                        "short": pos.get("entry_funding_short", 0),
                    },
                    "entry_time": pos.get("entry_time", ""),
                    "unfavorable_duration_minutes": elapsed.total_seconds() / 60,
                    "reason": "FUNDING_NEGATIVE_SPREAD",
                }
                alerts.append(alert_data)
                update_position_health(
                    p_id, health_status="reversal",
                    reversal_reason="FUNDING_NEGATIVE_SPREAD",
                    last_signal_at=now.isoformat(),
                )
            else:
                update_position_health(p_id, health_status="watch", reversal_reason="FUNDING_UNFAVORABLE", last_signal_at=None)
        else:
            if p_id in _unfavorable_since:
                del _unfavorable_since[p_id]
            update_position_health(p_id, health_status="healthy", reversal_reason=None, last_signal_at=None)

    return alerts

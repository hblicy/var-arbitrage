"""Arbitrage evaluation logic."""
from __future__ import annotations

from typing import Dict, Iterable, List, Tuple

from config import Settings, settings
from models import ArbitrageOpportunity, MarketDatum
def analyse_markets(
    symbols: Iterable[str],
    exchanges_data: Dict[str, Dict[MarketDatum]],
    cfg: Settings | None = None,
) -> Tuple[List[ArbitrageOpportunity], Dict[str, str], Dict[str, int]]:
    """
    Analyze arbitrage opportunities across multiple exchanges.
    Returns: (List of opportunities, map of symbol -> status_reason)
    """
    cfg = cfg or settings
    opportunities: List[ArbitrageOpportunity] = []
    reasons: Dict[str, str] = {}
    symbol_max_intervals: Dict[str, int] = {}
    
    exchange_names = list(exchanges_data.keys())
    if len(exchange_names) < 2:
        return [], {}, {}

    for sym in symbols:
        reasons[sym] = "NO_DATA"
        # Find maximum interval for this symbol among available exchanges
        avail_intervals = [
            exchanges_data[name][sym].native_interval_hours 
            for name in exchange_names 
            if sym in exchanges_data[name]
        ]
        if not avail_intervals:
            continue
        
        max_interval = max(avail_intervals)
        symbol_max_intervals[sym] = max_interval

        # Generate all unique pairs of exchanges for comparison
        found_any_pair = False
        for i in range(len(exchange_names)):
            for j in range(i + 1, len(exchange_names)):
                name_a = exchange_names[i]
                name_b = exchange_names[j]
                
                m_a = exchanges_data[name_a].get(sym)
                m_b = exchanges_data[name_b].get(sym)
                
                if not m_a or not m_b:
                    continue

                found_any_pair = True
                # Safety: Ignore symbols with zero/missing prices (phantom data)
                if m_a.price <= 0 or m_b.price <= 0:
                    reasons[sym] = "ZERO_PRICE"
                    continue

                low_volume = not _passes_volume_filter(m_a, m_b, cfg)
                if low_volume:
                    reasons[sym] = "LOW_VOLUME"
                    # Strictly filter out low volume pairs (ghost coins)
                    continue

                # Price Deviation Filter: Detect identically named but different tokens
                price_dev_pct = abs(m_a.price - m_b.price) / min(m_a.price, m_b.price) * 100
                if price_dev_pct > cfg.thresholds.max_price_deviation_pct:
                    reasons[sym] = "PRICE_DEVIATION_TOO_HIGH"
                    continue

                # Direction 1: Buy A, Sell B
                opp1 = _evaluate_direction(
                    symbol=sym,
                    buy=m_a,
                    sell=m_b,
                    direction=f"{name_a}_long_{name_b}_short",
                    base_interval=max_interval,
                    cfg=cfg,
                    low_volume=low_volume,
                    buy_exchange=name_a,
                    sell_exchange=name_b,
                )
                if opp1:
                    opportunities.append(opp1)

                # Direction 2: Buy B, Sell A
                opp2 = _evaluate_direction(
                    symbol=sym,
                    buy=m_b,
                    sell=m_a,
                    direction=f"{name_b}_long_{name_a}_short",
                    base_interval=max_interval,
                    cfg=cfg,
                    low_volume=low_volume,
                    buy_exchange=name_b,
                    sell_exchange=name_a,
                )
                if opp2:
                    opportunities.append(opp2)
        
        # If we have data but no opportunity was found by _evaluate_direction
        if found_any_pair and not any(o.symbol == sym for o in opportunities):
            if reasons[sym] == "NO_DATA":
                reasons[sym] = "NO_OPPORTUNITY"

    return opportunities, reasons, symbol_max_intervals


def _passes_volume_filter(leg_a: MarketDatum, leg_b: MarketDatum, cfg: Settings) -> bool:
    """
    检查两个交易平台该币种的 24h 交易额是否达到门槛。
    只要其中一边低于门槛，就视为流动性不足。
    """
    # 优先取全局门槛
    threshold = cfg.thresholds.min_volume_usd
    
    # 检查 Leg A
    vol_a = leg_a.volume_24h
    if vol_a is not None and vol_a < threshold:
        return False
        
    # 检查 Leg B
    vol_b = leg_b.volume_24h
    if vol_b is not None and vol_b < threshold:
        return False

    # 特殊情况：如果两边交易量都是 None (极少见)，暂时允许通过，
    # 或者如果只有一边是 None，另一边达到门槛，也允许通过。
    return True


def _evaluate_direction(
    symbol: str,
    buy: MarketDatum,
    sell: MarketDatum,
    direction: str,
    base_interval: int,
    cfg: Settings,
    low_volume: bool = False,
    buy_exchange: str = "",
    sell_exchange: str = "",
) -> ArbitrageOpportunity | None:
    try:
        # 1. Price spread. Prefer executable top-of-book prices:
        #    long leg pays ask, short leg receives bid. If one side has no
        #    book data, apply the configured fallback slippage to that leg.
        buy_exec_price, buy_price_source, buy_fallback_slippage = _buy_execution_price(buy, cfg)
        sell_exec_price, sell_price_source, sell_fallback_slippage = _sell_execution_price(sell, cfg)
        gross_bps = (_spread_ratio(sell_exec_price, buy_exec_price) - 1.0) * 10_000
        reference_spread_bps = (_spread_ratio(sell.price, buy.price) - 1.0) * 10_000
    except ZeroDivisionError:
        return None

    # Calculate fee cost
    # Fetch taker fees for specific exchanges from config if possible
    buy_fee = 5.0
    sell_fee = 5.0
    exchanges = cfg.exchanges
    if buy_exchange.lower() in exchanges:
        buy_fee = exchanges[buy_exchange.lower()].taker_bps
    if sell_exchange.lower() in exchanges:
        sell_fee = exchanges[sell_exchange.lower()].taker_bps
    
    estimated_slippage_bps = max(0.0, reference_spread_bps - gross_bps)
    fallback_slippage_bps = buy_fallback_slippage + sell_fallback_slippage
    total_slippage_bps = estimated_slippage_bps

    fee_penalty = buy_fee + sell_fee
    net_spread_bps = gross_bps - fee_penalty
    
    # Determine if limit order should be suggested
    # Suggest limit order if:
    # 1. Spread is thin (slippage > half of net profit)
    # 2. Or if estimated slippage is above a threshold (e.g., 20 bps)
    suggest_limit_order = (
        total_slippage_bps > 20
        or fallback_slippage_bps > 0
        or (net_spread_bps > 0 and total_slippage_bps > net_spread_bps / 2)
    )

    # 2. Funding Yield Scaling
    # Normalize BOTH sides to base_interval
    funding_diff = None
    buy_scaled = None
    sell_scaled = None
    
    if buy.funding_rate is not None and sell.funding_rate is not None:
        # Scale: rate * (base_interval / native_interval)
        buy_scaled = buy.funding_rate * (base_interval / buy.native_interval_hours)
        sell_scaled = sell.funding_rate * (base_interval / sell.native_interval_hours)
        funding_diff = sell_scaled - buy_scaled

    # 3. Total Net Yield (Spread + Scaled Funding)
    funding_bps = (funding_diff * 10000) if funding_diff is not None else 0
    total_net_bps = net_spread_bps + funding_bps

    # Normalize funding to 24h for consistent threshold comparison
    funding_bps_24h = funding_bps * (24 / base_interval) if base_interval > 0 else 0

    # Decision Criteria: Trigger for dashboard if meets ANY of the following from config
    cond1 = net_spread_bps >= cfg.thresholds.dashboard_min_spread_bps
    cond2 = funding_bps >= cfg.thresholds.dashboard_min_funding_bps
    cond3 = total_net_bps >= cfg.thresholds.dashboard_min_total_bps
    
    is_triggered = cond1 or cond2 or cond3
    
    if not is_triggered or total_net_bps < -50:
        return None
        
    if net_spread_bps < -100: # Allow up to 1% price loss if funding compensates
        return None

    recommendation = _build_recommendation(
        direction,
        buy,
        sell,
        net_spread_bps,
        funding_bps_24h,
        cfg,
        suggest_limit_order,
        executable_gross_bps=gross_bps,
        reference_spread_bps=reference_spread_bps,
    )
    
    details = {
        "symbol": symbol,
        "base_interval": base_interval,
        "buy_exchange": buy.exchange,
        "sell_exchange": sell.exchange,
        "buy_price": buy.price,
        "sell_price": sell.price,
        "buy_execution_price": buy_exec_price,
        "sell_execution_price": sell_exec_price,
        "buy_price_source": buy_price_source,
        "sell_price_source": sell_price_source,
        "reference_spread_bps": reference_spread_bps,
        "executable_spread_bps": gross_bps,
        "gross_spread_bps": gross_bps,
        "net_spread_bps": net_spread_bps,
        "total_net_bps": total_net_bps,
        "fees_bps": fee_penalty,
        "slippage_bps": total_slippage_bps,
        "fallback_slippage_bps": fallback_slippage_bps,
        "suggest_limit_order": suggest_limit_order,
        "buy_bid_ask": {
            "bid": buy.best_bid,
            "ask": buy.best_ask,
        },
        "sell_bid_ask": {
            "bid": sell.best_bid,
            "ask": sell.best_ask,
        },
        "funding_long_native": buy.funding_rate,
        "funding_short_native": sell.funding_rate,
        "funding_long_scaled_bps": (buy_scaled * 10000) if buy_scaled is not None else None,
        "funding_short_scaled_bps": (sell_scaled * 10000) if sell_scaled is not None else None,
        "funding_diff_scaled_bps": funding_bps,
        "funding_diff_24h_bps": funding_bps_24h,
        "native_intervals": {
            "buy": buy.native_interval_hours,
            "sell": sell.native_interval_hours,
        },
        "volumes": {
            "buy": buy.volume_24h,
            "sell": sell.volume_24h,
        },
        "low_volume": low_volume,
    }

    return ArbitrageOpportunity(
        symbol=symbol,
        direction=direction,
        entry_exchange=buy.exchange or "",
        exit_exchange=sell.exchange or "",
        gross_spread_bps=gross_bps,
        net_spread_bps=net_spread_bps,
        funding_diff=funding_diff,
        recommendation=recommendation,
        details=details,
    )


def _spread_ratio(sell_price: float, buy_price: float) -> float:
    return sell_price / buy_price


def _has_valid_book(market: MarketDatum) -> bool:
    bid = market.best_bid
    ask = market.best_ask
    return bid is not None and ask is not None and bid > 0 and ask > 0 and bid <= ask


def _buy_execution_price(market: MarketDatum, cfg: Settings) -> Tuple[float, str, float]:
    if _has_valid_book(market):
        return float(market.best_ask), "best_ask", 0.0
    fallback = cfg.fees.slippage_bps
    return market.price * (1 + fallback / 10_000), "price_plus_fallback_slippage", fallback


def _sell_execution_price(market: MarketDatum, cfg: Settings) -> Tuple[float, str, float]:
    if _has_valid_book(market):
        return float(market.best_bid), "best_bid", 0.0
    fallback = cfg.fees.slippage_bps
    return market.price * (1 - fallback / 10_000), "price_minus_fallback_slippage", fallback


def _build_recommendation(
    direction: str,
    buy: MarketDatum,
    sell: MarketDatum,
    net_bps: float,
    funding_bps_24h: float,
    cfg: Settings,
    suggest_limit_order: bool = False,
    executable_gross_bps: float | None = None,
    reference_spread_bps: float | None = None,
) -> str:
    if executable_gross_bps is None:
        try:
            executable_gross_bps = (sell.price / buy.price - 1.0) * 10_000
        except ZeroDivisionError:
            executable_gross_bps = 0
    if reference_spread_bps is None:
        reference_spread_bps = executable_gross_bps

    limit_hint = " suggest limit order" if suggest_limit_order else ""
    return (
        f"{buy.exchange} long / {sell.exchange} short {buy.symbol} | "
        f"executable spread {executable_gross_bps:.1f} bps "
        f"(reference {reference_spread_bps:.1f} bps, net after fees {net_bps:.1f} bps)"
        f"; 24h funding edge {funding_bps_24h:.2f} bps{limit_hint}"
    )


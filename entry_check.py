"""Pure calculations for manual cross-exchange entry verification."""
from __future__ import annotations

from typing import Any, Iterable, Mapping


def entry_check_supported(
    long_exchange: str,
    short_exchange: str,
    collectors: Mapping[str, Any],
) -> bool:
    long_collector = collectors.get(long_exchange.lower())
    short_collector = collectors.get(short_exchange.lower())
    return bool(
        long_collector
        and short_collector
        and callable(getattr(long_collector, "fetch_order_book", None))
        and callable(getattr(short_collector, "fetch_order_book", None))
    )


def annotate_entry_check_support(opportunities: Iterable[Any], collectors: Mapping[str, Any]) -> None:
    for opportunity in opportunities:
        opportunity.details["entry_check_supported"] = entry_check_supported(
            opportunity.details.get("buy_exchange", ""),
            opportunity.details.get("sell_exchange", ""),
            collectors,
        )


def _levels_vwap(levels: Iterable[Iterable[float]], quantity: float) -> float | None:
    remaining = quantity
    total = 0.0
    for level in levels:
        if len(level) < 2:
            continue
        price = float(level[0])
        size = float(level[1])
        if price <= 0 or size <= 0:
            continue
        filled = min(remaining, size)
        total += filled * price
        remaining -= filled
        if remaining <= max(1e-12, quantity * 1e-12):
            return total / quantity
    return None


def evaluate_entry_quote(
    *,
    buy_book: Mapping[str, Iterable[Iterable[float]]],
    sell_book: Mapping[str, Iterable[Iterable[float]]],
    notional_usd: float,
    buy_fee_bps: float,
    sell_fee_bps: float,
    safety_buffer_bps: float,
    buy_quoted_at: float,
    sell_quoted_at: float,
    checked_at: float,
    max_quote_age_ms: int,
    max_leg_skew_ms: int,
) -> dict:
    if notional_usd <= 0:
        raise ValueError("notional_usd must be positive")

    buy_asks = list(buy_book.get("asks", []))
    sell_bids = list(sell_book.get("bids", []))
    if not buy_asks or not sell_bids:
        return {"status": "unavailable", "reason": "EMPTY_BOOK"}

    best_buy = float(buy_asks[0][0])
    best_sell = float(sell_bids[0][0])
    if best_buy <= 0 or best_sell <= 0:
        return {"status": "unavailable", "reason": "INVALID_BOOK"}

    quote_age_ms = max(0.0, checked_at - min(buy_quoted_at, sell_quoted_at)) * 1_000
    if quote_age_ms > max_quote_age_ms:
        return {"status": "unavailable", "reason": "STALE_QUOTE", "quote_age_ms": quote_age_ms}

    leg_skew_ms = abs(buy_quoted_at - sell_quoted_at) * 1_000
    if leg_skew_ms > max_leg_skew_ms:
        return {"status": "unavailable", "reason": "LEG_SKEW", "leg_skew_ms": leg_skew_ms}

    hedge_quantity = min(notional_usd / best_buy, notional_usd / best_sell)
    buy_vwap = _levels_vwap(buy_asks, hedge_quantity)
    sell_vwap = _levels_vwap(sell_bids, hedge_quantity)
    if buy_vwap is None or sell_vwap is None:
        return {"status": "unavailable", "reason": "INSUFFICIENT_DEPTH"}

    gross_convergence_bps = (sell_vwap / buy_vwap - 1.0) * 10_000
    round_trip_fee_bps = 2 * (buy_fee_bps + sell_fee_bps)
    net_convergence_bps = gross_convergence_bps - round_trip_fee_bps
    safety_margin_bps = net_convergence_bps - safety_buffer_bps

    return {
        "status": "actionable" if safety_margin_bps >= 0 else "unavailable",
        "reason": None if safety_margin_bps >= 0 else "INSUFFICIENT_EDGE",
        "hedge_quantity": hedge_quantity,
        "buy_vwap": buy_vwap,
        "sell_vwap": sell_vwap,
        "gross_convergence_bps": gross_convergence_bps,
        "round_trip_fee_bps": round_trip_fee_bps,
        "net_convergence_bps": net_convergence_bps,
        "safety_margin_bps": safety_margin_bps,
        "quote_age_ms": quote_age_ms,
        "leg_skew_ms": leg_skew_ms,
    }

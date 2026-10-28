"""State transitions for exchange market snapshots."""
from __future__ import annotations

import math
from typing import Any, Dict


def stamp_market_timestamps(data: Dict[str, Any], fetched_at: float) -> None:
    for market in data.values():
        if market.fetched_at is None:
            market.fetched_at = fetched_at
        if market.timestamp is None and market.timestamp_source != "unknown":
            market.timestamp = fetched_at
            market.timestamp_source = "received"
        elif market.timestamp_source is None:
            market.timestamp_source = "exchange"


def market_price_health(market: Any, *, now: float, max_age: float) -> dict:
    timestamp = getattr(market, "timestamp", None)
    fetched_at = getattr(market, "fetched_at", None)
    source = getattr(market, "timestamp_source", None) or ("exchange" if timestamp is not None else "unknown")
    valid_time = isinstance(timestamp, (int, float)) and math.isfinite(timestamp) and 0 <= timestamp <= now
    age = now - timestamp if valid_time else None
    price = getattr(market, "price", None)
    valid_price = isinstance(price, (int, float)) and math.isfinite(price) and price > 0
    if not valid_price:
        status = "missing"
    elif not valid_time or source == "unknown":
        status = "unknown"
    else:
        status = "stale" if max_age and age > max_age else "fresh"
    return {
        "status": status,
        "age_seconds": round(age, 3) if age is not None else None,
        "timestamp": timestamp if valid_time else None,
        "fetched_at": fetched_at if isinstance(fetched_at, (int, float)) and math.isfinite(fetched_at) else None,
        "timestamp_source": source,
    }


def replace_exchange_snapshot(
    raw_exchanges_data: Dict[str, dict],
    exchange_status: Dict[str, dict],
    *,
    key: str,
    data: dict,
    fetched_at: float,
    error: str | None,
) -> None:
    previous = exchange_status.get(key, {})
    if error is None:
        stamp_market_timestamps(data, fetched_at)

    raw_exchanges_data[key] = data
    exchange_status[key] = {
        "state": "error" if error else ("fresh" if data else "empty"),
        "market_count": len(data),
        "last_attempt_at": fetched_at,
        "last_success_at": fetched_at if error is None else previous.get("last_success_at"),
        "error": error,
    }


def dashboard_snapshot(
    source: Dict[str, Any],
    *,
    market_stale_seconds: float,
    now: float,
) -> Dict[str, Any]:
    statuses = {
        key: dict(value)
        for key, value in source.get("exchange_status", {}).items()
    }
    stale_exchanges = set()
    raw_data = source.get("analysis_exchanges_data", source.get("raw_exchanges_data", {}))
    stale_markets = {
        (symbol, exchange)
        for exchange, data in raw_data.items()
        for symbol, market in data.items()
        if market_stale_seconds and (
            market.timestamp is None or now - market.timestamp > market_stale_seconds
        )
    }
    for key, status in statuses.items():
        if status.get("state") != "fresh":
            continue
        last_success_at = status.get("last_success_at")
        data = raw_data.get(key, {})
        if market_stale_seconds and (
            last_success_at is None or now - last_success_at > market_stale_seconds
            or (data and all((symbol, key) in stale_markets for symbol in data))
        ):
            status["state"] = "stale"
            stale_exchanges.add(key)

    markets = {
        symbol: {
            exchange: None if exchange in stale_exchanges or (symbol, exchange) in stale_markets else market
            for exchange, market in market_set.items()
        }
        for symbol, market_set in source.get("markets", {}).items()
    }
    opportunities = [
        opportunity
        for opportunity in source.get("opportunities", [])
        if str(opportunity.get("details", {}).get("buy_exchange", "")).lower() not in stale_exchanges
        and str(opportunity.get("details", {}).get("sell_exchange", "")).lower() not in stale_exchanges
        and all(
            (opportunity.get("symbol"), str(opportunity.get("details", {}).get(leg, "")).lower()) not in stale_markets
            for leg in ("buy_exchange", "sell_exchange")
        )
    ]
    reasons = dict(source.get("reasons", {}))
    remaining_symbols = {opportunity.get("symbol") for opportunity in opportunities}
    for symbol, _ in stale_markets:
        if symbol in markets and symbol not in remaining_symbols:
            reasons[symbol] = "STALE_DATA"

    return {
        "markets": markets,
        "opportunities": opportunities,
        "reasons": reasons,
        "symbol_max_intervals": source.get("symbol_max_intervals", {}),
        "last_update": source.get("last_update", "Never"),
        "data_version": source.get("data_version", 0),
        "exchange_status": statuses,
    }

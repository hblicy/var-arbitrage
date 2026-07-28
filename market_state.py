"""State transitions for exchange market snapshots."""
from __future__ import annotations

from typing import Any, Dict


def stamp_market_timestamps(data: Dict[str, Any], fetched_at: float) -> None:
    for market in data.values():
        market.timestamp = fetched_at


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
    for key, status in statuses.items():
        if status.get("state") != "fresh":
            continue
        last_success_at = status.get("last_success_at")
        if market_stale_seconds and (
            last_success_at is None or now - last_success_at > market_stale_seconds
        ):
            status["state"] = "stale"
            stale_exchanges.add(key)

    markets = {
        symbol: {
            exchange: None if exchange in stale_exchanges else market
            for exchange, market in market_set.items()
        }
        for symbol, market_set in source.get("markets", {}).items()
    }
    opportunities = [
        opportunity
        for opportunity in source.get("opportunities", [])
        if str(opportunity.get("details", {}).get("buy_exchange", "")).lower() not in stale_exchanges
        and str(opportunity.get("details", {}).get("sell_exchange", "")).lower() not in stale_exchanges
    ]

    return {
        "markets": markets,
        "opportunities": opportunities,
        "reasons": source.get("reasons", {}),
        "symbol_max_intervals": source.get("symbol_max_intervals", {}),
        "last_update": source.get("last_update", "Never"),
        "data_version": source.get("data_version", 0),
        "exchange_status": statuses,
    }

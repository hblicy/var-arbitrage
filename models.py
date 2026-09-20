"""Shared data structures for the arbitrage monitor."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class MarketDatum:
    symbol: str
    price: float
    funding_rate: Optional[float]
    volume_24h: Optional[float] = None
    timestamp: Optional[float] = None
    exchange: Optional[str] = None
    native_interval_hours: int = 8  # Default to 8h
    best_bid: Optional[float] = None  # Best bid price for slippage calculation
    best_ask: Optional[float] = None  # Best ask price for slippage calculation
    next_funding_time: Optional[float] = None  # Next settlement timestamp (UTC ms)
    next_funding_time_source: str = "exchange"


@dataclass
class ArbitrageOpportunity:
    symbol: str
    direction: str  # e.g. "nado_long_variational_short"
    entry_exchange: str
    exit_exchange: str
    gross_spread_bps: float
    net_spread_bps: float
    funding_diff: Optional[float]
    recommendation: str
    details: dict

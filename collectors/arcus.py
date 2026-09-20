"""Arcus public perpetual markets and L2 snapshots."""
from __future__ import annotations

import logging
import math
import time
from typing import Dict, Iterable

from collectors.base import MarketCollector
from collectors.decorators import with_retry
from collectors.session_pool import session_pool
from config import ExchangeSettings, settings as app_settings
from models import MarketDatum

logger = logging.getLogger(__name__)


class ArcusCollector(MarketCollector):
    def __init__(self, settings: ExchangeSettings) -> None:
        super().__init__(settings)
        self._client = session_pool.get_session(
            name="arcus", base_url=settings.base_url, timeout=settings.timeout,
            headers=settings.extra_headers,
        )
        self._symbols: Dict[str, str] = {}

    async def aclose(self) -> None:
        await session_pool.close_session("arcus")

    async def _load_markets(self) -> list[dict]:
        response = await self._client.get("/v1/markets")
        response.raise_for_status()
        rows = response.json()["markets"]
        if not isinstance(rows, list):
            raise ValueError("Arcus markets must be a list")
        active = [row for row in rows if row["status"] == "ONLINE" and row["type"] == "PERPETUAL"
                  and row["quoteAsset"] == "USD" and row["marketDisplayName"] not in self.settings.excluded_symbols]
        self._symbols = {
            self.settings.symbol_overrides.get(row["marketDisplayName"], row["baseAsset"] + "USDT"):
            row["marketDisplayName"] for row in active
        }
        return active

    @with_retry(max_retries=3)
    async def fetch_markets(self, symbols: Iterable[str]) -> Dict[str, MarketDatum]:
        requested = set(symbols)
        collected_at = time.time()
        rows = await self._load_markets()
        tasks = []
        for row in rows:
            symbol = self.settings.symbol_overrides.get(row["marketDisplayName"], row["baseAsset"] + "USDT")
            if (requested and symbol not in requested) or symbol in self.settings.excluded_symbols:
                continue
            volume = _number(row["volume24hNotional"])
            if self.settings.min_volume_usd is not None and volume is not None and volume < self.settings.min_volume_usd:
                continue
            tasks.append(self._market(symbol, row, volume))
        markets = await self._gather_with_semaphore(tasks)
        # Prices and funding precede the book requests, so include that wait in their age.
        for market in markets:
            market.timestamp = collected_at
        logger.info("Fetched %d markets from Arcus", len(markets))
        return {market.symbol: market for market in markets}

    async def _market(self, symbol: str, row: dict, volume: float | None) -> MarketDatum:
        book = await self.fetch_order_book(symbol, 1)
        price = _number(row["lastTradePrice"])
        if price is None or price <= 0:
            price = _number(row["markPrice"])
        if price is None or price <= 0:
            raise ValueError(f"Arcus {symbol}: invalid price")
        next_funding = _number(row.get("nextFundingAt"))
        return MarketDatum(
            symbol=symbol, price=price, funding_rate=_number(row.get("nextFundingRate")),
            volume_24h=volume, exchange=self.settings.name, native_interval_hours=1,
            best_bid=book["bids"][0][0] if book["bids"] else None,
            best_ask=book["asks"][0][0] if book["asks"] else None,
            next_funding_time=next_funding * 1000 if next_funding is not None else None,
        )

    async def fetch_order_book(self, symbol: str, limit: int) -> dict:
        if symbol not in self._symbols:
            await self._load_markets()
        if symbol not in self._symbols:
            raise ValueError(f"Arcus unsupported market: {symbol}")
        if limit <= 0:
            raise ValueError("Arcus book limit must be positive")
        response = await self._client.get(
            f"/v1/l2OrderBook/{self._symbols[symbol]}", params={"nLevels": min(limit, 100)},
        )
        response.raise_for_status()
        payload = response.json()
        # Arcus L2 timestamps are microseconds; nextFundingAt is seconds.
        timestamp = _number(payload["timestamp"])
        if timestamp is None:
            raise ValueError(f"Arcus {symbol}: missing book timestamp")
        age_ms = (time.time() - timestamp / 1e6) * 1000
        if age_ms > app_settings.entry_check.max_quote_age_ms or age_ms < -5000:
            raise ValueError(f"Arcus {symbol}: stale order book ({age_ms:.0f} ms)")
        book = {side: _levels(payload[side], reverse=side == "bids") for side in ("bids", "asks")}
        if book["bids"] and book["asks"] and book["bids"][0][0] >= book["asks"][0][0]:
            raise ValueError(f"Arcus {symbol}: crossed order book")
        book["timestamp"] = timestamp / 1e6
        return book


def _number(value) -> float | None:
    if value is None:
        return None
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"Arcus invalid numeric value: {value!r}")
    return result


def _levels(rows: list, *, reverse: bool) -> list[list[float]]:
    levels = []
    for row in rows:
        price, size = _number(row[0]), _number(row[1])
        if price is None or size is None or price <= 0 or size <= 0:
            raise ValueError("Arcus invalid order book level")
        levels.append([price, size])
    return sorted(levels, key=lambda level: level[0], reverse=reverse)

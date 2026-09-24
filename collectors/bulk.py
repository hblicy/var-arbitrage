"""BULK public REST market data; funding rates are hourly fractions."""
from __future__ import annotations

import logging
import math
import time
from typing import Dict, Iterable

from curl_cffi.requests.exceptions import HTTPError

from collectors.base import MarketCollector
from collectors.decorators import with_retry
from collectors.session_pool import session_pool
from config import ExchangeSettings, settings as app_settings
from models import MarketDatum

logger = logging.getLogger(__name__)


class _StaleMarketData(ValueError):
    pass


class BulkCollector(MarketCollector):
    def __init__(self, settings: ExchangeSettings) -> None:
        super().__init__(settings)
        self._client = session_pool.get_session(
            name="bulk", base_url=settings.base_url, timeout=settings.timeout,
            headers=settings.extra_headers,
        )
        self._symbols: Dict[str, str] = {}

    async def aclose(self) -> None:
        await session_pool.close_session("bulk")

    async def _load_markets(self) -> None:
        response = await self._client.get("/api/v1/exchangeInfo")
        response.raise_for_status()
        rows = response.json()
        if not isinstance(rows, list):
            raise ValueError("Bulk exchangeInfo must be a list")
        self._symbols = {
            self.settings.symbol_overrides.get(row["symbol"], row["baseAsset"] + "USDT"): row["symbol"]
            for row in rows if row["status"] == "TRADING" and row["quoteAsset"] == "USD"
            and row["symbol"] not in self.settings.excluded_symbols
        }

    @with_retry(max_retries=3)
    async def fetch_markets(self, symbols: Iterable[str]) -> Dict[str, MarketDatum]:
        requested = set(symbols)
        await self._load_markets()

        async def fresh_market(symbol, native):
            try:
                return await self._market(symbol, native)
            except _StaleMarketData as exc:
                logger.warning("Skipping Bulk market %s: %s", symbol, exc)
                return None

        markets = await self._gather_with_semaphore([
            fresh_market(symbol, native) for symbol, native in self._symbols.items()
            if (not requested or symbol in requested) and symbol not in self.settings.excluded_symbols
        ])
        result = {market.symbol: market for market in markets if market is not None}
        logger.info("Fetched %d markets from Bulk", len(result))
        return result

    async def _market(self, symbol: str, native: str) -> MarketDatum | None:
        collected_at = time.time()
        response = await self._client.get(f"/api/v1/ticker/{native}")
        response.raise_for_status()
        row = response.json()
        if row["symbol"] != native:
            raise ValueError(f"Bulk {symbol}: mismatched ticker symbol")
        # Live REST timestamps are nanoseconds, despite older millisecond examples.
        timestamp = _number(row["timestamp"])
        if timestamp is None:
            raise ValueError(f"Bulk {symbol}: missing ticker timestamp")
        age = time.time() - timestamp / 1e9
        if (app_settings.schedule.market_stale_seconds and age > app_settings.schedule.market_stale_seconds) or age < -5:
            raise _StaleMarketData(f"Bulk {symbol}: stale ticker ({age:.1f}s)")
        volume = _number(row["quoteVolume"])
        if self.settings.min_volume_usd is not None and volume is not None and volume < self.settings.min_volume_usd:
            return None
        price = _number(row["lastPrice"])
        if price is None or price <= 0:
            price = _number(row["markPrice"])
        if price is None or price <= 0:
            raise ValueError(f"Bulk {symbol}: invalid price")
        try:
            book = await self.fetch_order_book(symbol, 1)
        except HTTPError as exc:
            if exc.response is None or exc.response.status_code != 404:
                raise
            logger.warning("Skipping Bulk market %s: order book unavailable (HTTP 404)", symbol)
            return None
        return MarketDatum(
            symbol=symbol, price=price, funding_rate=_number(row.get("fundingRate")),
            timestamp=min(collected_at, timestamp / 1e9),
            volume_24h=volume, exchange=self.settings.name, native_interval_hours=1,
            best_bid=book["bids"][0][0] if book["bids"] else None,
            best_ask=book["asks"][0][0] if book["asks"] else None,
            next_funding_time=(math.floor(timestamp / 1e9 / 3600) + 1) * 3600 * 1000,
            next_funding_time_source="utc_estimate",
        )

    async def fetch_order_book(self, symbol: str, limit: int) -> dict:
        if symbol not in self._symbols:
            await self._load_markets()
        if symbol not in self._symbols:
            raise ValueError(f"Bulk unsupported market: {symbol}")
        if limit <= 0:
            raise ValueError("Bulk book limit must be positive")
        response = await self._client.get("/api/v1/l2book", params={
            "type": "l2book", "coin": self._symbols[symbol], "nlevels": min(limit, 1000),
        })
        response.raise_for_status()
        payload = response.json()
        if payload["symbol"] != self._symbols[symbol] or payload["updateType"] != "snapshot":
            raise ValueError(f"Bulk {symbol}: invalid order book snapshot")
        timestamp = _number(payload["timestamp"])
        if timestamp is None:
            raise ValueError(f"Bulk {symbol}: missing book timestamp")
        age_ms = (time.time() - timestamp / 1e9) * 1000
        if age_ms > app_settings.entry_check.max_quote_age_ms or age_ms < -5000:
            raise _StaleMarketData(f"Bulk {symbol}: stale order book ({age_ms:.0f} ms)")
        bids, asks = payload["levels"]
        book = {"bids": _levels(bids, reverse=True), "asks": _levels(asks, reverse=False)}
        if book["bids"] and book["asks"] and book["bids"][0][0] >= book["asks"][0][0]:
            raise ValueError(f"Bulk {symbol}: crossed order book")
        book["timestamp"] = timestamp / 1e9
        return book


def _number(value) -> float | None:
    if value is None:
        return None
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"Bulk invalid numeric value: {value!r}")
    return result


def _levels(rows: list, *, reverse: bool) -> list[list[float]]:
    levels = []
    for row in rows:
        price, size = _number(row["px"]), _number(row["sz"])
        if price is None or size is None or price <= 0 or size <= 0:
            raise ValueError("Bulk invalid order book level")
        levels.append([price, size])
    return sorted(levels, key=lambda level: level[0], reverse=reverse)

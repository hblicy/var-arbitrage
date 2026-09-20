"""RISEx public REST markets and order books."""
from __future__ import annotations

import logging
import math
import time
from typing import Dict, Iterable

from collectors.base import MarketCollector
from collectors.decorators import with_retry
from collectors.session_pool import session_pool
from config import ExchangeSettings
from models import MarketDatum

logger = logging.getLogger(__name__)


class RisexCollector(MarketCollector):
    def __init__(self, settings: ExchangeSettings) -> None:
        super().__init__(settings)
        self._client = session_pool.get_session(
            name="risex", base_url=settings.base_url, timeout=settings.timeout,
            headers=settings.extra_headers,
        )
        self._market_ids: Dict[str, str] = {}

    async def aclose(self) -> None:
        await session_pool.close_session("risex")

    def _symbol(self, row: dict) -> str:
        native = row["config"]["name"]
        return self.settings.symbol_overrides.get(native, native.rsplit("/", 1)[0] + "USDT")

    async def _load_markets(self) -> list[dict]:
        response = await self._client.get("/v1/markets", params={"force_refresh": "true"})
        response.raise_for_status()
        rows = response.json()["data"]["markets"]
        if not isinstance(rows, list):
            raise ValueError("RISEx markets must be a list")
        active = [row for row in rows if row["active"] and row["config"]["unlocked"]
                  and not row.get("reduce_only", False) and not row.get("post_only", False)
                  and row["quote_asset_symbol"] == "USDC"
                  and row["config"]["name"] not in self.settings.excluded_symbols]
        self._market_ids = {self._symbol(row): str(row["market_id"]) for row in active}
        return active

    @with_retry(max_retries=3)
    async def fetch_markets(self, symbols: Iterable[str]) -> Dict[str, MarketDatum]:
        requested = set(symbols)
        collected_at = time.time()
        rows = await self._load_markets()
        tasks = []
        for row in rows:
            symbol = self._symbol(row)
            if (requested and symbol not in requested) or symbol in self.settings.excluded_symbols:
                continue
            volume = _number(row["quote_volume_24h"])
            if self.settings.min_volume_usd is not None and volume is not None and volume < self.settings.min_volume_usd:
                continue
            tasks.append(self._market(symbol, row, volume))
        markets = await self._gather_with_semaphore(tasks)
        # Prices and funding precede the book requests, so include that wait in their age.
        for market in markets:
            market.timestamp = collected_at
        logger.info("Fetched %d markets from RISEx", len(markets))
        return {market.symbol: market for market in markets}

    async def _market(self, symbol: str, row: dict, volume: float | None) -> MarketDatum:
        interval_ns = int(row["funding_interval"])
        if interval_ns <= 0 or interval_ns % 3_600_000_000_000:
            raise ValueError(f"RISEx {symbol}: unsupported funding interval {interval_ns} ns")
        price = _number(row["last_price"])
        if price is None or price <= 0:
            price = _number(row["mark_price"])
        if price is None or price <= 0:
            raise ValueError(f"RISEx {symbol}: invalid price")
        next_funding = _number(row.get("next_funding_time"))
        book = await self.fetch_order_book(symbol, 1)
        return MarketDatum(
            symbol=symbol, price=price, funding_rate=_number(row.get("current_funding_rate")),
            volume_24h=volume, exchange=self.settings.name,
            native_interval_hours=interval_ns // 3_600_000_000_000,
            best_bid=book["bids"][0][0] if book["bids"] else None,
            best_ask=book["asks"][0][0] if book["asks"] else None,
            next_funding_time=next_funding / 1e6 if next_funding is not None else None,
        )

    async def fetch_order_book(self, symbol: str, limit: int) -> dict:
        if symbol not in self._market_ids:
            await self._load_markets()
        if symbol not in self._market_ids:
            raise ValueError(f"RISEx unsupported market: {symbol}")
        if limit <= 0:
            raise ValueError("RISEx book limit must be positive")
        response = await self._client.get("/v1/orderbook", params={
            "market_id": self._market_ids[symbol], "limit": min(limit, 250),
        })
        response.raise_for_status()
        payload = response.json()["data"]
        if str(payload["market_id"]) != self._market_ids[symbol]:
            raise ValueError(f"RISEx {symbol}: mismatched order book market ID")
        book = {side: _levels(payload[side], reverse=side == "bids") for side in ("bids", "asks")}
        if book["bids"] and book["asks"] and book["bids"][0][0] >= book["asks"][0][0]:
            raise ValueError(f"RISEx {symbol}: crossed order book")
        return book


def _number(value) -> float | None:
    if value is None:
        return None
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"RISEx invalid numeric value: {value!r}")
    return result


def _levels(rows: list, *, reverse: bool) -> list[list[float]]:
    levels = []
    for row in rows:
        price, size = _number(row["price"]), _number(row["quantity"])
        if price is None or size is None or price <= 0 or size <= 0:
            raise ValueError("RISEx invalid order book level")
        levels.append([price, size])
    return sorted(levels, key=lambda level: level[0], reverse=reverse)

"""Collector for Aster perpetual futures public REST data."""
from __future__ import annotations

import logging
from typing import Dict, Iterable, Optional

from config import ExchangeSettings
from collectors.base import MarketCollector
from collectors.decorators import with_retry
from collectors.session_pool import session_pool
from models import MarketDatum

logger = logging.getLogger(__name__)


class AsterCollector(MarketCollector):
    def __init__(self, settings: ExchangeSettings) -> None:
        super().__init__(settings)
        self._client = session_pool.get_session(
            name="aster",
            base_url=self.settings.base_url,
            timeout=self.settings.timeout,
            headers=self.settings.extra_headers,
        )
        self._interval_map: Dict[str, int] = {}

    def _normalise_symbol(self, raw_symbol: str) -> Optional[str]:
        """Aster USDT perpetual symbols already match the unified format."""
        if raw_symbol in self.settings.symbol_overrides:
            return self.settings.symbol_overrides[raw_symbol]
        if raw_symbol.endswith("USDT") and not raw_symbol.startswith("SHIELD"):
            return raw_symbol
        return None

    @with_retry(max_retries=3, backoff_base=2, default_return={})
    async def fetch_markets(self, symbols: Iterable[str]) -> Dict[str, MarketDatum]:
        """Fetch Aster ticker, funding and top-of-book snapshots."""
        symbol_set = set(symbols)
        endpoint = self.settings.price_endpoint

        ticker_resp = await self._client.get(endpoint.path)
        ticker_resp.raise_for_status()
        tickers_raw = ticker_resp.json()

        funding_endpoint = self.settings.funding_endpoint
        if funding_endpoint is None:
            logger.warning("Aster funding endpoint is not configured.")
            return {}

        premium_resp = await self._client.get(funding_endpoint.path)
        premium_resp.raise_for_status()
        premium_raw = premium_resp.json()

        book_resp = await self._client.get("/fapi/v1/ticker/bookTicker")
        book_resp.raise_for_status()
        book_raw = book_resp.json()

        await self._refresh_intervals()

        if not isinstance(tickers_raw, list):
            logger.warning("Aster returned unexpected ticker payload.")
            return {}

        premium_items = premium_raw if isinstance(premium_raw, list) else []
        book_items = book_raw if isinstance(book_raw, list) else []
        premium_map = {item.get("symbol"): item for item in premium_items if item.get("symbol")}
        book_map = {item.get("symbol"): item for item in book_items if item.get("symbol")}

        markets: Dict[str, MarketDatum] = {}
        for item in tickers_raw:
            raw_symbol = item.get(endpoint.symbol_key)
            if not raw_symbol or raw_symbol in self.settings.excluded_symbols:
                continue

            normalised_symbol = self._normalise_symbol(raw_symbol)
            if not normalised_symbol:
                continue

            if symbol_set and normalised_symbol not in symbol_set:
                continue

            premium = premium_map.get(raw_symbol, {})
            book = book_map.get(raw_symbol, {})

            price = _positive_float(item.get(endpoint.price_key))
            if price is None:
                price = _positive_float(premium.get("markPrice"))
            if price is None:
                continue

            volume = _safe_float(item.get(endpoint.volume_key))
            min_volume = self.settings.min_volume_usd
            if min_volume is not None and volume is not None and volume < min_volume:
                continue

            markets[normalised_symbol] = MarketDatum(
                symbol=normalised_symbol,
                price=price,
                funding_rate=_safe_float(premium.get(funding_endpoint.funding_key)),
                volume_24h=volume,
                exchange=self.settings.name,
                native_interval_hours=self._interval_map.get(raw_symbol, 8),
                best_bid=_positive_float(book.get("bidPrice")),
                best_ask=_positive_float(book.get("askPrice")),
                next_funding_time=_safe_float(premium.get("nextFundingTime")),
            )

        logger.info("Fetched %d markets from Aster.", len(markets))
        return markets

    async def _refresh_intervals(self) -> None:
        try:
            resp = await self._client.get("/fapi/v1/fundingInfo")
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, list):
                return
            self._interval_map = {
                item["symbol"]: int(item.get("fundingIntervalHours") or 8)
                for item in data
                if item.get("symbol")
            }
        except Exception as exc:
            logger.warning("Failed to refresh Aster funding intervals: %s", exc)

    async def aclose(self):
        pass


def _safe_float(value) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _positive_float(value) -> Optional[float]:
    parsed = _safe_float(value)
    return parsed if parsed is not None and parsed > 0 else None

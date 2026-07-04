"""Collector for OndoPerps public REST market data."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Dict, Iterable, Optional

from config import ExchangeSettings
from collectors.base import MarketCollector
from collectors.decorators import with_retry
from collectors.session_pool import session_pool
from models import MarketDatum

logger = logging.getLogger(__name__)


class OndoPerpsCollector(MarketCollector):
    def __init__(self, settings: ExchangeSettings) -> None:
        super().__init__(settings)
        self._client = session_pool.get_session(
            name="ondoperps",
            base_url=self.settings.base_url,
            timeout=self.settings.timeout,
            headers=self.settings.extra_headers,
        )
        self._symbol_re = re.compile(self.settings.symbol_pattern) if self.settings.symbol_pattern else None

    def _normalise_symbol(self, raw_symbol: str) -> Optional[str]:
        """Convert Ondo market symbols like CRCL-USD.P to CRCLUSDT."""
        if raw_symbol in self.settings.symbol_overrides:
            return self.settings.symbol_overrides[raw_symbol]

        if self._symbol_re:
            match = self._symbol_re.match(raw_symbol)
            if match:
                return f"{match.group(1).upper()}USDT"

        return None

    @with_retry(max_retries=3, backoff_base=2, default_return={})
    async def fetch_markets(self, symbols: Iterable[str]) -> Dict[str, MarketDatum]:
        """Fetch OndoPerps contracts with price, top-of-book and funding."""
        symbol_set = set(symbols)
        endpoint = self.settings.price_endpoint

        resp = await self._client.request(
            method=endpoint.method,
            url=endpoint.path,
            params=endpoint.params,
        )
        resp.raise_for_status()
        payload = resp.json()

        data = payload
        for key in endpoint.response_path:
            data = data[key]

        markets: Dict[str, MarketDatum] = {}
        if not isinstance(data, list):
            logger.warning("OndoPerps returned unexpected contracts payload.")
            return markets

        for item in data:
            raw_symbol = item.get(endpoint.symbol_key)
            if not raw_symbol or raw_symbol in self.settings.excluded_symbols:
                continue

            normalised_symbol = self._normalise_symbol(raw_symbol)
            if not normalised_symbol:
                continue

            if symbol_set and normalised_symbol not in symbol_set:
                continue

            if item.get("disabled") is True:
                continue

            price = _safe_float(item.get(endpoint.price_key))
            if not price:
                price = _safe_float(item.get("markPrice")) or _safe_float(item.get("indexPrice"))
            if not price or price <= 0:
                continue

            volume = _safe_float(item.get(endpoint.volume_key)) if endpoint.volume_key else None
            min_volume = self.settings.min_volume_usd
            if min_volume is not None and volume is not None and volume < min_volume:
                continue

            funding = _safe_float(item.get(endpoint.funding_key))
            if funding is None:
                funding = _safe_float(item.get("fundingRate"))

            markets[normalised_symbol] = MarketDatum(
                symbol=normalised_symbol,
                price=price,
                funding_rate=funding,
                volume_24h=volume,
                exchange=self.settings.name,
                native_interval_hours=1,
                best_bid=_positive_float(item.get("bid")),
                best_ask=_positive_float(item.get("ask")),
                next_funding_time=_parse_iso_ms(item.get("nextFundingRateTimestamp")),
            )

        logger.info("Fetched %d markets from OndoPerps.", len(markets))
        return markets

    async def aclose(self):
        pass


def _safe_float(value) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _positive_float(value) -> Optional[float]:
    parsed = _safe_float(value)
    return parsed if parsed and parsed > 0 else None


def _parse_iso_ms(value) -> Optional[float]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).timestamp() * 1000
    except ValueError:
        return None

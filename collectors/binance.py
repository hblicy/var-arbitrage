"""Collector for Binance USD-M Futures data."""
from __future__ import annotations

import logging
from typing import Dict, Iterable, Optional

from curl_cffi import requests

from config import ExchangeSettings
from collectors.base import MarketCollector
from collectors.decorators import with_retry
from models import MarketDatum

logger = logging.getLogger(__name__)


class BinanceCollector(MarketCollector):
    def __init__(self, settings: ExchangeSettings) -> None:
        super().__init__(settings)
        # Reverting to dedicated session for Binance for maximum stability
        self._client = requests.AsyncSession(
            base_url=self.settings.base_url,
            timeout=self.settings.timeout,
            impersonate="chrome"
        )
        self._interval_map: Dict[str, int] = {}
        self._last_refresh = 0

    async def _refresh_intervals(self):
        """Fetch fundingIntervalHours from fundingInfo."""
        import time
        if time.time() - self._last_refresh < 3600 and self._interval_map:
            return
            
        try:
            logger.info("Refreshing Binance fundingInfo for intervals...")
            resp = await self._client.get("/fapi/v1/fundingInfo")
            resp.raise_for_status()
            data = resp.json()
            for item in data:
                sym = item["symbol"]
                # Uses fundingIntervalHours specifically
                self._interval_map[sym] = item.get("fundingIntervalHours", 8)
            self._last_refresh = time.time()
            logger.info(f"Refreshed intervals for {len(self._interval_map)} Binance symbols.")
        except Exception as e:
            logger.error(f"Failed to refresh Binance intervals: {e}")

    def _normalise_symbol(self, raw_symbol: str) -> Optional[str]:
        """Convert exchange symbol to unified symbol (e.g., BTCUSDT)."""
        # Binance Futures symbols are typically BTCUSDT already.
        if raw_symbol in self.settings.symbol_overrides:
            return self.settings.symbol_overrides[raw_symbol]
        return raw_symbol

    @with_retry(max_retries=3, backoff_base=2, default_return={})
    async def fetch_markets(self, symbols: Iterable[str]) -> Dict[str, MarketDatum]:
        """Fetch prices and funding rates from Binance Futures public API."""
        await self._refresh_intervals()
        symbol_list = list(symbols)
        
        # 1. Fetch Prices + Volume from 24hr ticker (includes quoteVolume)
        price_resp = await self._client.get("/fapi/v1/ticker/24hr")
        price_resp.raise_for_status()
        prices_raw = price_resp.json()

        # 2. Fetch Funding Rates
        funding_resp = await self._client.get(self.settings.funding_endpoint.path)
        funding_resp.raise_for_status()
        funding_raw = funding_resp.json()

        # Map funding rates by symbol for easy lookup
        funding_map = {}
        next_funding_map = {}
        for item in funding_raw:
            sym = item.get(self.settings.funding_endpoint.symbol_key)
            if sym and self.settings.funding_endpoint.funding_key in item:
                funding_map[sym] = float(item[self.settings.funding_endpoint.funding_key])
            if sym and "nextFundingTime" in item:
                next_funding_map[sym] = float(item["nextFundingTime"])

        markets: Dict[str, MarketDatum] = {}
        for item in prices_raw:
            raw_sym = item.get("symbol")
            if not raw_sym:
                continue

            norm_sym = self._normalise_symbol(raw_sym)
            if not norm_sym:
                continue

            # Filter by tracked symbols if provided
            if symbol_list and norm_sym not in symbol_list:
                continue

            # Safety: Check timestamp freshness (Binance sometimes returns stale ghost data for delisted tokens)
            import time
            current_ms = time.time() * 1000
            ticker_time = item.get("closeTime", 0)
            if ticker_time > 0 and (current_ms - ticker_time) > 3600 * 1000: # Older than 1 hour
                continue

            price = float(item.get("lastPrice", 0))
            funding = funding_map.get(raw_sym)
            
            # Extract bid/ask for slippage calculation
            best_bid = float(item.get("bidPrice", 0)) if item.get("bidPrice") else None
            best_ask = float(item.get("askPrice", 0)) if item.get("askPrice") else None
            
            # Extract 24h volume (quoteVolume = USDT volume)
            volume_24h = float(item.get("quoteVolume", 0)) if item.get("quoteVolume") else 0.0
            
            # Use dynamic interval
            native_interval = self._interval_map.get(raw_sym, 8)
            
            markets[norm_sym] = MarketDatum(
                symbol=norm_sym,
                price=price,
                funding_rate=funding,
                volume_24h=volume_24h,
                exchange=self.settings.name,
                native_interval_hours=native_interval,
                best_bid=best_bid,
                best_ask=best_ask,
                next_funding_time=next_funding_map.get(raw_sym),
            )
        
        logger.info(f"Fetched {len(markets)} markets from Binance.")

        return markets

    async def aclose(self):
        await self._client.close()

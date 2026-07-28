"""Collector for Backpack exchange data."""
from __future__ import annotations

import logging
import re
from typing import Dict, Iterable, Optional

from curl_cffi import requests

from config import ExchangeSettings
from collectors.base import MarketCollector
from collectors.decorators import with_retry
from collectors.session_pool import session_pool
from models import MarketDatum

logger = logging.getLogger(__name__)


class BackpackCollector(MarketCollector):
    def __init__(self, settings: ExchangeSettings) -> None:
        super().__init__(settings)
        # Use shared session pool
        self._client = session_pool.get_session(
            name="backpack",
            base_url=self.settings.base_url,
            timeout=self.settings.timeout,
        )
        self._symbol_re = re.compile(self.settings.symbol_pattern) if self.settings.symbol_pattern else None

    def _normalise_symbol(self, raw_symbol: str) -> Optional[str]:
        """Convert Backpack symbol (e.g., BTC_USDC_PERP) to unified symbol (e.g., BTCUSDT)."""
        if raw_symbol in self.settings.symbol_overrides:
            return self.settings.symbol_overrides[raw_symbol]
        
        if self._symbol_re:
            match = self._symbol_re.match(raw_symbol)
            if match:
                # Backpack uses USDC as collateral but we unify to USDT naming convention for core logic
                return f"{match.group(1)}USDT"
        
        return None

    @with_retry(max_retries=3, backoff_base=2)
    async def fetch_markets(self, symbols: Iterable[str]) -> Dict[str, MarketDatum]:
        """Fetch prices and funding rates from Backpack public API."""
        symbol_list = list(symbols)
        
        # 1. Fetch Tickers
        ticker_resp = await self._client.get(self.settings.price_endpoint.path)
        ticker_resp.raise_for_status()
        tickers_raw = ticker_resp.json()

        # 2. Fetch Funding Rates (if endpoint is configured)
        funding_map = {}
        if self.settings.funding_endpoint:
            try:
                funding_resp = await self._client.get(self.settings.funding_endpoint.path)
                funding_resp.raise_for_status()
                funding_raw = funding_resp.json()

                # Map funding rates by symbol
                for item in funding_raw:
                    sym = item.get("symbol")
                    rate = item.get("fundingRate")
                    if sym and rate is not None:
                        try:
                            funding_map[sym] = float(rate)
                        except (ValueError, TypeError):
                            pass
            except Exception as fe:
                logger.warning("Failed to fetch Backpack funding rates: %s", fe)

        markets: Dict[str, MarketDatum] = {}
        for item in tickers_raw:
            raw_sym = item.get("symbol")
            if not raw_sym:
                continue

            norm_sym = self._normalise_symbol(raw_sym)
            if not norm_sym:
                continue

            # Filter by tracked symbols if provided
            if symbol_list and norm_sym not in symbol_list:
                continue

            try:
                price = float(item.get("lastPrice", 0))
                volume = float(item.get("quoteVolume", 0))
            except (ValueError, TypeError):
                continue
            
            # Extract bid/ask for slippage calculation
            try:
                best_bid = float(item.get("bestBid")) if item.get("bestBid") not in (None, "", 0) else None
            except (ValueError, TypeError):
                best_bid = None
            
            try:
                best_ask = float(item.get("bestAsk")) if item.get("bestAsk") not in (None, "", 0) else None
            except (ValueError, TypeError):
                best_ask = None
                
            funding = funding_map.get(raw_sym)
            
            # Backpack funding is typically every 1 hour (as per user clarification)
            markets[norm_sym] = MarketDatum(
                symbol=norm_sym,
                price=price,
                funding_rate=funding,
                volume_24h=volume,
                exchange=self.settings.name,
                native_interval_hours=1,
                best_bid=best_bid,
                best_ask=best_ask,
            )
        
        logger.info(f"Fetched {len(markets)} markets from Backpack.")
        return markets

    async def aclose(self):
        # Shared session pool handles closing
        pass

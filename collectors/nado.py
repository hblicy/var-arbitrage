"""Collector for Nado exchange market data via REST API."""
from __future__ import annotations

import logging
import re
from typing import Dict, Iterable, Optional

from curl_cffi import requests

from collectors.base import MarketCollector
from collectors.decorators import with_retry
from collectors.session_pool import session_pool
from config import ExchangeSettings
from models import MarketDatum

logger = logging.getLogger(__name__)


class NadoCollector(MarketCollector):
    """Fetches market information from the Nado Protocol using REST API via curl_cffi."""

    def __init__(self, settings: ExchangeSettings) -> None:
        super().__init__(settings)
        # Use shared session pool
        self._session = session_pool.get_session(
            name="nado",
            base_url=self.settings.base_url,
            timeout=self.settings.timeout,
        )

    def _normalise_symbol_api(self, raw_symbol: str) -> Optional[str]:
        """Convert API symbol (e.g. BTC-PERP_USDT0) to internal (BTCUSDT)."""
        if raw_symbol in self.settings.symbol_overrides:
            return self.settings.symbol_overrides[raw_symbol]
        
        if self.settings.symbol_pattern:
            match = re.match(self.settings.symbol_pattern, raw_symbol)
            if match:
                asset = match.group(1).upper()
                return f"{asset}USDT"
            
        return raw_symbol

    @with_retry(max_retries=3, backoff_base=2, default_return={})
    async def fetch_markets(self, symbols: Iterable[str]) -> Dict[str, MarketDatum]:
        """Fetch market data using REST API + Indexer Funding."""
        target_symbols = list(symbols)
        auto_discover = len(target_symbols) == 0
        endpoint = self.settings.price_endpoint

        # 1. Fetch Prices/Volumes from Tickers
        logger.debug("Fetching Nado tickers: %s", endpoint.path)
        ticker_resp = await self._session.get(endpoint.path, params=endpoint.params)
        ticker_resp.raise_for_status()
        ticker_data = ticker_resp.json()

        if isinstance(ticker_data, dict):
            ticker_list = list(ticker_data.values())
        elif isinstance(ticker_data, list):
            ticker_list = ticker_data
        else:
            return {}

        # 2. Fetch Funding Rates from Indexer (POST /v1)
        # Collect all product IDs from ticker_list
        product_ids = [int(item.get("product_id")) for item in ticker_list if item.get("product_id")]
        funding_map = {}
        if product_ids:
            try:
                funding_resp = await self._session.post(
                    "/v1", 
                    json={"funding_rates": {"product_ids": product_ids}}
                )
                if funding_resp.status_code == 200:
                    funding_data = funding_resp.json()
                    for pid_str, info in funding_data.items():
                        rate_x18 = info.get("funding_rate_x18")
                        if rate_x18:
                            # Convert X18 to decimal and normalize to HOURLY rate (Indexer returns DAILY rate)
                            funding_map[int(pid_str)] = float(rate_x18) / 1e18 / 24.0
            except Exception as fe:
                logger.warning(f"Failed to fetch Nado funding: {fe}")

        markets: Dict[str, MarketDatum] = {}
        for item in ticker_list:
            raw_sym = item.get(endpoint.symbol_key)
            if not raw_sym:
                continue
            
            norm_sym = self._normalise_symbol_api(raw_sym)
            if not norm_sym:
                continue

            if not auto_discover and norm_sym not in target_symbols:
                continue

            price = float(item.get(endpoint.price_key, 0))
            volume = float(item.get(endpoint.volume_key, 0))
            pid = item.get("product_id")
            
            # Nado Reports instantaneous rate. 
            # If we want to align with SDK's hourly normalized (funding / 24.0), 
            # we need to know what x18 represents.
            # In Vertex/Nado, it's usually the instantaneous rate.
            funding = (funding_map.get(int(pid)) or 0.0) if pid else 0.0
            
            # Extract bid/ask for slippage calculation
            try:
                best_bid = float(item.get("bid")) if item.get("bid") not in (None, "", 0) else None
            except (ValueError, TypeError):
                best_bid = None
            
            try:
                best_ask = float(item.get("ask")) if item.get("ask") not in (None, "", 0) else None
            except (ValueError, TypeError):
                best_ask = None

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
        
        logger.info("Fetched %d markets from Nado (including funding).", len(markets))
        return markets

    async def aclose(self) -> None:
        # Shared session pool handles closing
        pass

"""Collector for Lighter exchange market data."""
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


class LighterCollector(MarketCollector):
    """Fetches market information from Lighter."""

    def __init__(self, settings: ExchangeSettings) -> None:
        super().__init__(settings)
        # Use shared session pool with custom headers
        self._session = session_pool.get_session(
            name="lighter",
            base_url=self.settings.base_url,
            timeout=self.settings.timeout,
            headers={
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "Origin": "https://app.lighter.xyz",
                "Referer": "https://app.lighter.xyz/",
                "Sec-Ch-Ua": '"Not A(Brand";v="99", "Google Chrome";v="121", "Chromium";v="121"',
                "Sec-Ch-Ua-Mobile": "?0",
                "Sec-Ch-Ua-Platform": '"Windows"',
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "cross-site",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
            }
        )

    def _normalise_symbol_api(self, raw_symbol: str) -> Optional[str]:
        """Convert Lighter symbol (e.g. BTC) to internal (BTCUSDT)."""
        if raw_symbol in self.settings.symbol_overrides:
            return self.settings.symbol_overrides[raw_symbol]
        
        # Remove any _USDC etc if present
        clean_sym = raw_symbol.split('_')[0]
        
        if self.settings.symbol_pattern:
            match = re.match(self.settings.symbol_pattern, clean_sym)
            if match:
                asset = match.group(1).upper()
                return f"{asset}USDT"
            
        return f"{clean_sym.upper()}USDT"

    @with_retry(max_retries=3, backoff_base=2)
    async def fetch_markets(self, symbols: Iterable[str]) -> Dict[str, MarketDatum]:
        """Fetch market data from Lighter."""
        target_symbols = list(symbols)
        auto_discover = len(target_symbols) == 0
        
        markets: Dict[str, MarketDatum] = {}
        
        # 1. Fetch Price & Volume from /api/v1/orderBookDetails
        resp = await self._session.get("/api/v1/orderBookDetails")
        resp.raise_for_status()
        ob_payload = resp.json()
        ob_data = ob_payload.get("order_book_details", [])
        
        # 2. Fetch Funding Rates from alternative public endpoint /api/v1/funding-rates
        funding_resp = await self._session.get("/api/v1/funding-rates")
        funding_resp.raise_for_status()
        funding_payload = funding_resp.json()
        funding_list = funding_payload.get("funding_rates", [])
        
        # Map by symbol since market_id might differ across exchanges in this list
        # Filter specifically for 'lighter' exchange
        funding_map = {}
        for item in funding_list:
            if isinstance(item, dict) and item.get("exchange") == "lighter":
                funding_map[item.get("symbol")] = float(item.get("rate", 0))
        
        for item in ob_data:
            # We want perpetual markets
            if item.get("market_type") != "perp":
                continue
            
            raw_sym = item.get("symbol")
            norm_sym = self._normalise_symbol_api(raw_sym)
            
            if not auto_discover and norm_sym not in target_symbols:
                continue
            
            # Lighter uses 'last_trade_price' and 'daily_quote_token_volume'
            # Check for best_bid and best_ask in orderBookDetails
            price_str = item.get("last_trade_price")
            if not price_str:
                continue
            price = float(price_str)
            
            # Extract bid/ask for slippage calculation
            try:
                best_bid = float(item.get("best_bid")) if item.get("best_bid") not in (None, "", 0) else None
            except (ValueError, TypeError):
                best_bid = None
            
            try:
                best_ask = float(item.get("best_ask")) if item.get("best_ask") not in (None, "", 0) else None
            except (ValueError, TypeError):
                best_ask = None
            
            # funding_map key is 'BTC' etc.
            funding_rate = funding_map.get(raw_sym, 0.0)
            
            volume_str = item.get("daily_quote_token_volume")
            volume = float(volume_str) if volume_str else 0.0
            
            markets[norm_sym] = MarketDatum(
                symbol=norm_sym,
                price=price,
                # API returns 8h basis rate, but UI shows 1h. 
                # We normalize it here to 1h to match UI secondary display.
                funding_rate=funding_rate / 8.0,
                volume_24h=volume,
                exchange=self.settings.name,
                native_interval_hours=1,
                best_bid=best_bid,
                best_ask=best_ask,
            )
            
        logger.info("Fetched %d markets from Lighter.", len(markets))
        return markets

    async def aclose(self) -> None:
        # Shared session pool handles closing
        pass

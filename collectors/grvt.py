"""Collector for GRVT exchange market data via REST API."""
from __future__ import annotations

import logging
import asyncio
from typing import Dict, Iterable, Optional, List

from curl_cffi import requests

from collectors.base import MarketCollector
from collectors.session_pool import session_pool
from config import ExchangeSettings
from models import MarketDatum

logger = logging.getLogger(__name__)


class GrvtCollector(MarketCollector):
    """Fetches market information from GRVT using REST API."""

    def __init__(self, settings: ExchangeSettings) -> None:
        super().__init__(settings)
        # Use shared session pool
        self._session = session_pool.get_session(
            name="grvt",
            base_url=self.settings.base_url,
            timeout=self.settings.timeout,
        )
        self._instruments: List[str] = []
        self._instrument_intervals: Dict[str, int] = {}  # instrument -> funding_interval_hours
        self._last_instrument_fetch = 0

    def _normalise_symbol(self, raw_symbol: str) -> Optional[str]:
        """Convert GRVT symbol (e.g. BTC_USDT_Perp) to internal (BTCUSDT)."""
        if raw_symbol in self.settings.symbol_overrides:
            return self.settings.symbol_overrides[raw_symbol]
        
        # Example: BTC_USDT_Perp -> BTCUSDT
        parts = raw_symbol.split('_')
        if len(parts) >= 2:
            return f"{parts[0]}{parts[1]}"
            
        return raw_symbol

    async def _fetch_all_instruments(self) -> List[str]:
        """Fetch all perpetual instruments from GRVT."""
        resp = await self._session.post("/full/v1/instruments", json={})
        if resp.status_code != 200:
            raise RuntimeError(f"GRVT instruments request failed: HTTP {resp.status_code}")
        data = resp.json()
        if not isinstance(data, dict) or not isinstance(data.get("result"), list):
            raise ValueError("GRVT returned invalid instruments payload")

        instruments = []
        intervals = {}
        for item in data["result"]:
            if item.get("kind") == "PERPETUAL":
                inst_name = item["instrument"]
                instruments.append(inst_name)
                intervals[inst_name] = int(item.get("funding_interval_hours", 8))
        self._instrument_intervals = intervals
        return instruments

    async def fetch_markets(self, symbols: Iterable[str]) -> Dict[str, MarketDatum]:
        """Fetch market data for all instruments."""
        # Update instrument list every hour or if empty
        import time
        if not self._instruments or (
            time.time() - self._last_instrument_fetch > self.settings.metadata_refresh_seconds
        ):
            self._instruments = await self._fetch_all_instruments()
            self._last_instrument_fetch = time.time()

        if not self._instruments:
            return {}

        markets: Dict[str, MarketDatum] = {}
        
        # Poll each instrument (could be optimized with a batch ticker if GRVT supported it)
        # For now, let's limit the concurrency
        tasks = []
        for inst in self._instruments:
            tasks.append(self._fetch_single_market(inst))
        
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        for res in results:
            if isinstance(res, MarketDatum):
                markets[res.symbol] = res
            elif isinstance(res, Exception):
                logger.debug(f"Error fetching GRVT market: {res}")

        return markets

    async def _fetch_single_market(self, instrument: str) -> Optional[MarketDatum]:
        """Fetch price and funding for a single instrument."""
        try:
            # Use /full/v1/ticker which contains current predicted funding_rate
            # (not /full/v1/funding which returns historical settled rates)
            ticker_resp = await self._session.post("/full/v1/ticker", json={"instrument": instrument})
            
            if ticker_resp.status_code != 200:
                return None
                
            ticker_data = ticker_resp.json().get("result", {})
            price = float(ticker_data.get("mark_price", 0))
            if price == 0:
                return None
                
            norm_sym = self._normalise_symbol(instrument)
            
            # GRVT returns funding_rate as percentage (e.g., -0.69 for -0.69%)
            # API already returns the native interval rate, no conversion needed.
            raw_rate = float(ticker_data.get("funding_rate", 0))
            funding = raw_rate / 100.0

            # Get the correct funding interval for this instrument
            interval = self._instrument_intervals.get(instrument, 8)

            # Try to get 24h volume from ticker
            buy_vol = float(ticker_data.get("buy_volume_24h_q", 0))
            sell_vol = float(ticker_data.get("sell_volume_24h_q", 0))
            volume = buy_vol + sell_vol

            # Extract bid/ask for slippage calculation
            best_bid = float(ticker_data.get("best_bid", 0)) if ticker_data.get("best_bid") else None
            best_ask = float(ticker_data.get("best_ask", 0)) if ticker_data.get("best_ask") else None

            return MarketDatum(
                symbol=norm_sym,
                price=price,
                funding_rate=funding,
                volume_24h=volume,
                exchange=self.settings.name,
                native_interval_hours=interval,
                best_bid=best_bid,
                best_ask=best_ask,
            )
        except Exception as e:
            logger.debug(f"Error in _fetch_single_market for {instrument}: {e}")
            return None

    async def aclose(self) -> None:
        # Shared session pool handles closing
        pass

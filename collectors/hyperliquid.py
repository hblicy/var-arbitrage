"""Collector for Hyperliquid exchange market data.

Uses the public Hyperliquid info API:
  POST https://api.hyperliquid.xyz/info
  {"type": "metaAndAssetCtxs"}

Returns perpetual metadata (universe) and asset contexts (mark price,
funding rate, open interest, 24h volume, impact prices) in one call.

Funding rate is per-hour (1h settlement cycle).
"""
from __future__ import annotations

import logging
from typing import Dict, Iterable, Optional

from collectors.base import MarketCollector
from collectors.decorators import with_retry
from collectors.session_pool import session_pool
from config import ExchangeSettings
from models import MarketDatum

logger = logging.getLogger(__name__)


class HyperliquidCollector(MarketCollector):
    """Fetches market information from Hyperliquid."""

    def __init__(self, settings: ExchangeSettings) -> None:
        super().__init__(settings)
        self._session = session_pool.get_session(
            name="hyperliquid",
            base_url=self.settings.base_url,
            timeout=self.settings.timeout,
        )

    def _normalise_symbol(self, raw_name: str) -> str:
        """Convert Hyperliquid asset name (e.g. BTC) to internal (BTCUSDT).
        
        Hyperliquid uses bare asset names like 'BTC', 'ETH', 'SOL'.
        We append 'USDT' to match our unified symbol format.
        """
        if raw_name in self.settings.symbol_overrides:
            return self.settings.symbol_overrides[raw_name]
        return f"{raw_name.upper()}USDT"

    @with_retry(max_retries=3, backoff_base=2, default_return={})
    async def fetch_markets(self, symbols: Iterable[str]) -> Dict[str, MarketDatum]:
        """Fetch all perpetual market data from Hyperliquid in one API call."""
        target_symbols = list(symbols)
        auto_discover = len(target_symbols) == 0

        # Single POST request fetches everything: metadata + asset contexts
        resp = await self._session.post(
            "/info",
            json={"type": "metaAndAssetCtxs"},
        )
        resp.raise_for_status()
        payload = resp.json()

        # Response: [meta, [ctx0, ctx1, ...]]
        # meta["universe"] is a list of {name, szDecimals, maxLeverage, ...}
        # ctxs[i] corresponds to meta["universe"][i]
        meta = payload[0]
        ctxs = payload[1]
        universe = meta.get("universe", [])

        markets: Dict[str, MarketDatum] = {}

        for i, asset_info in enumerate(universe):
            if i >= len(ctxs):
                break

            raw_name = asset_info.get("name", "")
            norm_sym = self._normalise_symbol(raw_name)

            # Check blacklist
            if norm_sym in self.settings.excluded_symbols:
                continue

            if not auto_discover and norm_sym not in target_symbols:
                continue

            ctx = ctxs[i]

            # Parse mark price
            mark_px_str = ctx.get("markPx")
            if not mark_px_str:
                continue
            price = float(mark_px_str)
            if price <= 0:
                continue

            # Parse funding rate (Hyperliquid = 1h settlement)
            funding_str = ctx.get("funding", "0")
            funding_1h = float(funding_str) if funding_str else 0.0

            # Parse 24h notional volume (USD)
            vol_str = ctx.get("dayNtlVlm", "0")
            volume_24h = float(vol_str) if vol_str else 0.0

            # Parse impact prices as bid/ask proxy
            # impactPxs: [best_bid_approx, best_ask_approx]
            best_bid = None
            best_ask = None
            impact_pxs = ctx.get("impactPxs")
            if impact_pxs and isinstance(impact_pxs, list) and len(impact_pxs) >= 2:
                try:
                    best_bid = float(impact_pxs[0])
                    best_ask = float(impact_pxs[1])
                except (ValueError, TypeError):
                    pass

            markets[norm_sym] = MarketDatum(
                symbol=norm_sym,
                price=price,
                funding_rate=funding_1h,
                volume_24h=volume_24h,
                exchange=self.settings.name,
                native_interval_hours=1,  # Hyperliquid settles funding hourly
                best_bid=best_bid,
                best_ask=best_ask,
            )

        logger.info("Fetched %d markets from Hyperliquid.", len(markets))
        return markets

    async def aclose(self) -> None:
        # Shared session pool handles closing
        pass

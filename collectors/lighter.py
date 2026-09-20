"""Collector for Lighter exchange market data."""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from collections import deque
from time import monotonic
from typing import Dict, Iterable, Optional
from urllib.parse import urlsplit, urlunsplit

import websocket

from collectors.base import MarketCollector
from collectors.decorators import with_retry
from collectors.session_pool import session_pool
from config import ExchangeSettings, settings as app_settings
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
        self._market_ids: Dict[str, int] = {}
        self._rest_requests = deque()
        self._scan_depth_requests = 0

    async def _rest_get(self, path: str, *, params=None, depth: bool = False):
        now = monotonic()
        while self._rest_requests and self._rest_requests[0][0] <= now - 60:
            self._rest_requests.popleft()
        # A rolling minute can include one extra scan's delayed requests when
        # scan latency falls. Budget for that overlap, with two REST calls per
        # scan reserved for metadata/funding (six depth calls at a 10s interval).
        interval = max(1, app_settings.schedule.interval_seconds)
        overlapping_scans = math.ceil(60 / interval) + 1
        depth_per_scan = max(0, 60 // overlapping_scans - 2)
        if len(self._rest_requests) >= 60 or (depth and self._scan_depth_requests >= depth_per_scan):
            raise ValueError('Lighter REST request budget exhausted; retry on a later scan')
        # No await before reservation: concurrent callers share one atomic budget.
        # Failed requests count too, since the server may have received them.
        self._rest_requests.append((now, depth))
        if depth:
            self._scan_depth_requests += 1
        return await self._session.get(path, params=params)

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
        collected_at = time.time()
        
        # 1. Fetch Price & Volume from /api/v1/orderBookDetails
        ob_data = await self._load_market_details()
        
        # 2. Fetch Funding Rates from alternative public endpoint /api/v1/funding-rates
        funding_resp = await self._rest_get("/api/v1/funding-rates")
        funding_resp.raise_for_status()
        funding_payload = funding_resp.json()
        funding_list = funding_payload.get("funding_rates", [])
        
        # Map by symbol since market_id might differ across exchanges in this list
        # Filter specifically for 'lighter' exchange
        funding_map = {}
        for item in funding_list:
            if isinstance(item, dict) and item.get("exchange") == "lighter":
                raw_rate = item.get('rate')
                rate = float(raw_rate) if raw_rate not in (None, '') else None
                if rate is not None and not math.isfinite(rate):
                    raise ValueError(f"Lighter {item.get('symbol')}: invalid funding rate")
                funding_map[item.get("symbol")] = rate

        stats, quoted_at = await asyncio.to_thread(self._fetch_market_stats)
        
        for item in ob_data:
            # We want perpetual markets
            if item.get("market_type") != "perp":
                continue
            
            raw_sym = item.get("symbol")
            norm_sym = self._normalise_symbol_api(raw_sym)
            if norm_sym not in self._market_ids:
                continue
            
            if not auto_discover and norm_sym not in target_symbols:
                continue
            
            # Lighter uses 'last_trade_price' and 'daily_quote_token_volume'
            price_str = item.get("last_trade_price")
            if not price_str:
                continue
            price = float(price_str)
            
            quote = stats.get(str(item['market_id']))
            best_bid = best_ask = None
            if quote is None:
                logger.warning('Lighter %s: missing top-of-book snapshot', norm_sym)
            else:
                if quote.get('market_id') != item['market_id'] or quote.get('symbol') != raw_sym:
                    raise ValueError(f'Lighter {norm_sym}: mismatched market stats')
                bid, ask = quote.get('best_bid_price'), quote.get('best_ask_price')
                if bid is not None and ask is not None:
                    bid, ask = float(bid), float(ask)
                    if not math.isfinite(bid) or not math.isfinite(ask) or bid < 0 or ask < 0:
                        raise ValueError(f'Lighter {norm_sym}: invalid top-of-book prices')
                    if bid > 0 and ask > 0:
                        if bid >= ask:
                            raise ValueError(f'Lighter {norm_sym}: crossed top-of-book prices')
                        best_bid, best_ask = bid, ask
            
            # funding_map key is 'BTC' etc.
            funding_rate = funding_map.get(raw_sym)
            if funding_rate is None:
                logger.warning('Lighter %s: funding rate unavailable', norm_sym)
            
            volume_str = item.get("daily_quote_token_volume")
            volume = float(volume_str) if volume_str else 0.0
            
            markets[norm_sym] = MarketDatum(
                symbol=norm_sym,
                price=price,
                # API returns 8h basis rate, but UI shows 1h. 
                # We normalize it here to 1h to match UI secondary display.
                funding_rate=funding_rate / 8.0 if funding_rate is not None else None,
                volume_24h=volume,
                timestamp=min(collected_at, quoted_at),
                exchange=self.settings.name,
                native_interval_hours=1,
                best_bid=best_bid,
                best_ask=best_ask,
            )
            
        logger.info("Fetched %d markets from Lighter.", len(markets))
        # Follow successful scan boundaries, not request timing: normal network
        # jitter must not carry the previous scan's depth quota into this one.
        # The rolling REST window above still caps all scans/retries at 60/min.
        self._scan_depth_requests = 0
        return markets

    def _fetch_market_stats(self) -> tuple[dict, float]:
        base = urlsplit(self.settings.base_url)
        url = self.settings.ws_url or urlunsplit((
            'wss' if base.scheme == 'https' else 'ws', base.netloc, '/stream', 'readonly=true', '',
        ))
        deadline = time.monotonic() + self.settings.timeout
        socket = websocket.create_connection(url, timeout=self.settings.timeout)
        try:
            socket.send(json.dumps({'type': 'subscribe', 'channel': 'market_stats/all'}))
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('Lighter market stats snapshot timed out')
                socket.settimeout(remaining)
                message = socket.recv()
                if not message:
                    raise ConnectionError('Lighter market stats stream closed before snapshot')
                payload = json.loads(message)
                if payload.get('type') == 'ping':
                    socket.send(json.dumps({'type': 'pong'}))
                    continue
                if payload.get('type') == 'error':
                    raise ValueError(f"Lighter market stats error: {payload.get('error')}")
                if payload.get('type') != 'subscribed/market_stats' or payload.get('channel') != 'market_stats:all':
                    continue
                raw_time = payload.get('timestamp')
                if not isinstance(raw_time, (int, float)) or not math.isfinite(raw_time):
                    raise ValueError('Lighter market stats: invalid snapshot timestamp')
                timestamp = raw_time / 1000
                age_ms = (time.time() - timestamp) * 1000
                if age_ms > app_settings.entry_check.max_quote_age_ms or age_ms < -5000:
                    raise ValueError(f'Lighter market stats: stale snapshot ({age_ms:.0f} ms)')
                stats = payload.get('market_stats')
                if not isinstance(stats, dict):
                    raise ValueError('Lighter market stats: invalid snapshot')
                return stats, timestamp
        finally:
            socket.close()

    async def _load_market_details(self) -> list:
        response = await self._rest_get('/api/v1/orderBookDetails')
        response.raise_for_status()
        payload = response.json()
        if payload.get('code') != 200:
            raise ValueError(f"Lighter market details error: {payload.get('code')}")
        rows = payload['order_book_details']
        self._market_ids = {
            self._normalise_symbol_api(row['symbol']): int(row['market_id']) for row in rows
            if row['market_type'] == 'perp' and row['status'] == 'active'
            and row['symbol'] not in self.settings.excluded_symbols
            and self._normalise_symbol_api(row['symbol']) not in self.settings.excluded_symbols
        }
        return rows

    async def fetch_order_book(self, symbol: str, limit: int) -> dict:
        if limit <= 0:
            raise ValueError('Lighter book limit must be positive')
        if symbol not in self._market_ids:
            await self._load_market_details()
        if symbol not in self._market_ids or symbol in self.settings.excluded_symbols:
            raise ValueError(f'Lighter unsupported market: {symbol}')
        # This REST snapshot has no source timestamp; include the full request
        # duration in quote age instead of stamping a slow response as fresh.
        timestamp = time.time()
        response = await self._rest_get('/api/v1/orderBookOrders', depth=True, params={
            'market_id': self._market_ids[symbol], 'limit': min(limit, 250),
        })
        response.raise_for_status()
        payload = response.json()
        if payload.get('code') != 200:
            raise ValueError(f"Lighter {symbol}: order book error {payload.get('code')}")
        age_ms = (time.time() - timestamp) * 1000
        if age_ms > app_settings.entry_check.max_quote_age_ms or age_ms < -5000:
            raise ValueError(f'Lighter {symbol}: stale order book ({age_ms:.0f} ms)')
        book = {}
        for side in ('bids', 'asks'):
            levels = []
            for row in payload[side]:
                price, size = float(row['price']), float(row['remaining_base_amount'])
                if not math.isfinite(price) or not math.isfinite(size) or price <= 0 or size < 0:
                    raise ValueError(f'Lighter {symbol}: invalid book level')
                if size > 0:
                    levels.append([price, size])
            book[side] = sorted(levels, key=lambda level: level[0], reverse=side == 'bids')[:limit]
        if book['bids'] and book['asks'] and book['bids'][0][0] >= book['asks'][0][0]:
            raise ValueError(f'Lighter {symbol}: crossed order book')
        book['timestamp'] = timestamp
        return book

    async def aclose(self) -> None:
        # Shared session pool handles closing
        pass

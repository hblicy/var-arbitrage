"""Collector for EdgeX exchange market data via WebSocket."""
from __future__ import annotations

import logging
import asyncio
import json
import math
import ssl
import time
from typing import Dict, Iterable, Optional

import websockets

from collectors.base import MarketCollector
from config import ExchangeSettings
from models import MarketDatum

logger = logging.getLogger(__name__)

class EdgeXCollector(MarketCollector):
    """Fetches market information from EdgeX via WebSocket."""

    def __init__(self, settings: ExchangeSettings) -> None:
        super().__init__(settings)
        self._cache: Dict[str, MarketDatum] = {}
        self._cache_lock = asyncio.Lock()
        self._ws_task: Optional[asyncio.Task] = None
        self._contracts: Dict[str, dict] = {} # contract_id -> metadata dict
        self._subscribed_ids: set[str] = set() # Track already subscribed IDs
        self._running = False
        
    async def _start_ws(self):
        """Start the WebSocket connection loop."""
        if self._ws_task and not self._ws_task.done():
            return
            
        self._running = True
        self._ws_task = asyncio.create_task(self._ws_loop())
        logger.info("EdgeX WebSocket task started.")

    async def _ws_loop(self):
        """Main WebSocket loop with reconnection logic."""
        
        # SSL Context for wss
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Origin": "https://pro.edgex.exchange"
        }
        if self.settings.extra_headers:
            headers.update(self.settings.extra_headers)

        while self._running:
            try:
                ws_url = self.settings.ws_url
                if not ws_url:
                    logger.error("No WS URL configured for EdgeX.")
                    break
                    
                logger.info(f"Connecting to EdgeX WS: {ws_url}")
                async with websockets.connect(ws_url, ssl=ssl_context, extra_headers=headers) as websocket:
                    # Clear subscription state for the new connection
                    self._subscribed_ids.clear()
                    logger.info("EdgeX WS Connected.")
                    
                    # 1. Subscribe to Metadata to discover contracts
                    sub_meta = {"type": "subscribe", "channel": "metadata"}
                    await websocket.send(json.dumps(sub_meta))
                    
                    # 2. Main message loop
                    async for message in websocket:
                        try:
                            data = json.loads(message)
                            msg_type = data.get("type")
                            
                            if msg_type == "ping":
                                await websocket.send(json.dumps({"type": "pong"}))
                                continue
                                
                            if msg_type == "quote-event" or (isinstance(data, dict) and "content" in data):
                                # Check for metadata content
                                content = data.get("content", {})
                                
                                if "data" in content:
                                    items = content["data"]
                                    
                                    if items and isinstance(items, list) and len(items) > 0:
                                        target_obj = items[0]
                                        if "contractList" in target_obj:
                                            await self._handle_metadata(target_obj["contractList"], websocket)
                                        elif "contract" in target_obj:
                                            await self._handle_metadata(target_obj["contract"], websocket)
                                        elif "contractId" in target_obj:
                                            # Direct list of contracts - likely contains price data too!
                                            await self._handle_metadata(items, websocket)
                                            # Also treat as ticker data
                                            for item in items:
                                                await self._handle_ticker({"data": item})
                                        else:
                                            logger.warning(f"Unknown metadata structure. keys: {target_obj.keys()}")
                                    else:
                                        pass
                                else:
                                    pass
                                
                            if msg_type == "ticker":
                                await self._handle_ticker(data)
                                
                        except Exception as e:
                            logger.error(f"Error processing EdgeX WS message: {e}")
                            
            except Exception as e:
                logger.error(f"EdgeX WS Connection failed: {e}. Retrying in 5s...")
                await asyncio.sleep(5)

    async def _handle_metadata(self, contracts: list, websocket):
        """Process metadata and subscribe to tickers."""
        new_subscriptions = []
        for c in contracts:
            cid = str(c.get("contractId"))
            if cid:
                self._contracts[cid] = c
                if cid not in self._subscribed_ids:
                    new_subscriptions.append(cid)
                    self._subscribed_ids.add(cid)
        
        if not new_subscriptions:
            logger.debug(f"EdgeX Metadata updated. Total contracts: {len(self._contracts)}")
            return

        logger.info(f"EdgeX Metadata received. Subscribing to {len(new_subscriptions)} new contracts.")
        
        # Batch subscribe to tickers
        count = 0
        for cid in new_subscriptions:
            sub_msg = {"type": "subscribe", "channel": f"ticker.{cid}"}
            await websocket.send(json.dumps(sub_msg))
            count += 1
            if count % 10 == 0:
                await asyncio.sleep(0.001)
                
        logger.debug(f"EdgeX: Successfully triggered {count} new subscriptions.")

    async def _handle_ticker(self, data: dict):
        """Update cache with ticker data."""
        # Ticker format from debug:
        # { "type": "ticker", "appId": "...", "data": { ... } }
        # data keys: contractId, lastPrice, fundingRate, etc.
        
        inner = data.get("data", {})
        cid = str(inner.get("contractId"))
        
        if cid not in self._contracts:
            return

        meta = self._contracts[cid]
        name = meta.get("contractName")
        
        # Filter 1: Metadata flags (ghost coins usually have enableDisplay=False)
        if not meta.get("enableDisplay", True) or not meta.get("enableTrade", True):
            return

        norm_sym = self._normalise_symbol(name)
        
        # Filter 2: Exclusions
        if norm_sym in self.settings.excluded_symbols:
            return
        # Also filter TEMP tokens dynamically
        if "TEMP" in norm_sym:
            return
            
        price = float(inner.get("lastPrice", 0))
        if price == 0:
            return

        funding = float(inner.get("fundingRate", 0))
        # Try to parse volume from multiple possible fields, prioritizing "value" (turnover/notional)
        volume = 0.0
        for key in ["value", "turnover", "volume", "size"]:
            if key in inner and inner[key] is not None:
                try:
                    volume = float(inner[key])
                    # If we found a valid value, break. 
                    # Note: "value" is usually USD volume, "volume" might be base asset amount.
                    # We prefer USD volume for the threshold check.
                    if volume > 0:
                        break
                except (ValueError, TypeError):
                    continue
        
        # Filter 3: Volume threshold (avoid illiquid/ghost coins)
        # Fallback to 100k if not specified
        min_vol = self.settings.min_volume_usd if self.settings.min_volume_usd is not None else 100000.0
        if volume < min_vol:
            return
        
        if "AVNT" in norm_sym:
            # Metadata inspection showed Paradox/EdgeX fields
            logger.debug(f"EdgeX {norm_sym} debug info preserved.")
        
        # Extract bid/ask for slippage calculation
        try:
            best_bid = float(inner.get("bidPrice")) if inner.get("bidPrice") not in (None, "", 0) else None
        except (ValueError, TypeError):
            best_bid = None
        
        try:
            best_ask = float(inner.get("askPrice")) if inner.get("askPrice") not in (None, "", 0) else None
        except (ValueError, TypeError):
            best_ask = None
        
        datum = MarketDatum(
            symbol=norm_sym,
            price=price,
            funding_rate=funding,
            volume_24h=volume,
            exchange=self.settings.name,
            native_interval_hours=4,  # Fixed 4h as requested
            best_bid=best_bid,
            best_ask=best_ask,
        )
        
        async with self._cache_lock:
            self._cache[norm_sym] = datum

    def _normalise_symbol(self, raw_symbol: str) -> Optional[str]:
        # Reuse logic from REST collector
        if raw_symbol in self.settings.symbol_overrides:
            return self.settings.symbol_overrides[raw_symbol]
        if raw_symbol.endswith("USD"):
             return raw_symbol[:-3] + "USDT"
        return raw_symbol

    async def fetch_markets(self, symbols: Iterable[str]) -> Dict[str, MarketDatum]:
        """Return the current cache of market data."""
        # Ensure WS is running
        if not self._ws_task or self._ws_task.done():
            await self._start_ws()
            # Wait for data warm-up (up to 5 seconds)
            logger.info("EdgeX WS started/restarted. Waiting for cache warm-up...")
            for _ in range(50):
                if self._cache:
                    break
                await asyncio.sleep(0.1)
        
        async with self._cache_lock:
            # Filter if specific symbols requested, else return all
            target_symbols = set(symbols)
            if not target_symbols:
                return self._cache.copy()
            
            return {k: v for k, v in self._cache.items() if k in target_symbols}

    async def aclose(self) -> None:
        """Close resources."""
        self._running = False
        if self._ws_task:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass

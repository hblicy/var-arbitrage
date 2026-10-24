"""QFEX public contract metadata, market snapshots and L2 books."""
import asyncio
import json
import logging
import os
import time
from datetime import datetime

import websocket

from collectors.base import MarketCollector
from collectors.decorators import with_retry
from collectors.perpetual_utils import number, order_book, positive, quote_currency, symbol_key, timestamp_ms
from collectors.session_pool import session_pool
from models import MarketDatum

logger = logging.getLogger(__name__)


def _timestamp(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('QFEX timestamp has no timezone')
    return timestamp_ms(parsed.timestamp() * 1000)


class QfexCollector(MarketCollector):
    def __init__(self, settings):
        super().__init__(settings)
        self._client = session_pool.get_session('qfex', settings.base_url, timeout=settings.timeout)
        self._symbols = {}
        self._metadata_at = 0

    async def aclose(self):
        await session_pool.close_session('qfex')

    async def _load_markets(self):
        if self._symbols and time.monotonic() - self._metadata_at < self.settings.metadata_refresh_seconds:
            return
        response = await self._client.get('/refdata')
        response.raise_for_status()
        symbols, fees = {}, {}
        for row in response.json()['data']:
            if row['status'] != 'ACTIVE':
                continue
            base = row['base_asset']
            category = row['product_category']
            if category == 'COMMODITY':
                base = {'GOLD': 'XAU', 'SILVER': 'XAG'}.get(base, base)
            symbol = symbol_key(self.settings, row['symbol'], base, row['quote_asset'])
            if row['symbol'] in self.settings.excluded_symbols or symbol in self.settings.excluded_symbols:
                continue
            if symbol in symbols:
                raise ValueError(f'QFEX duplicate symbol mapping: {symbol}')
            symbols[symbol] = row
            fees[symbol] = self.settings.taker_bps if os.getenv('QFEX_TAKER_FEE_BPS') is not None else (
                self.settings.category_taker_bps.get(category, self.settings.taker_bps))
        self._symbols = symbols
        self.settings.symbol_taker_bps = fees
        self._metadata_at = time.monotonic()

    def _fetch_snapshot(self):
        deadline = time.monotonic() + self.settings.timeout
        socket = websocket.create_connection(self.settings.ws_url, timeout=self.settings.timeout)
        try:
            socket.send(json.dumps({'type': 'subscribe', 'channels': ['market_stats'], 'symbols': ['*']}))
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('QFEX market snapshot timed out')
                socket.settimeout(remaining)
                raw = socket.recv()
                if not raw:
                    raise ConnectionError('QFEX market stream closed before snapshot')
                payload = json.loads(raw)
                if payload.get('type') == 'error':
                    raise ValueError(f'QFEX market stream error: {payload}')
                if payload.get('type') == 'market_stats' and payload.get('symbol') == '*':
                    return payload
        finally:
            socket.close()

    @with_retry(max_retries=3)
    async def fetch_markets(self, symbols):
        await self._load_markets()
        requested = set(symbols)
        collected_at = time.time()
        response = await self._client.get('/md/contracts')
        response.raise_for_status()
        contracts = response.json()['data']
        snapshot = await asyncio.to_thread(self._fetch_snapshot)
        snapshot_at = _timestamp(snapshot['time'])
        quotes = {row['symbol']: row for row in snapshot['bbos']}
        marks = {row['symbol']: row for row in snapshot.get('mark_prices', [])}
        native_map = {row['symbol']: (symbol, row) for symbol, row in self._symbols.items()}
        markets = {}
        for row in contracts:
            native = row['ticker_id']
            if row['product_type'] != 'Perpetual' or native not in native_map:
                continue
            symbol, meta = native_map[native]
            if requested and symbol not in requested:
                continue
            quote = quote_currency(meta['quote_asset'])
            volume = number(row.get('target_volume')) if quote == 'USD' else None
            if volume is not None and volume < (self.settings.min_volume_usd or 0):
                continue
            price = number(row['last_price'])
            if price is None or price <= 0:
                price = number(marks.get(native, {}).get('price'))
            if price is None or price <= 0:
                logger.warning('Skipping QFEX %s: no traded or mark price', native)
                continue
            bbo = quotes.get(native)
            bid = ask = None
            timestamp = min(collected_at, snapshot_at)
            if bbo is not None:
                timestamp = min(timestamp, _timestamp(bbo['time']))
                book = order_book(bbo['bid'], bbo['ask'], timestamp, 1)
                bid = book['bids'][0][0] if book['bids'] else None
                ask = book['asks'][0][0] if book['asks'] else None
            next_funding = number(row.get('next_funding_rate_timestamp'))
            markets[symbol] = MarketDatum(symbol, price, number(row.get('next_funding_rate')), volume,
                timestamp, self.settings.name, positive(row['funding_interval_minutes'], 'QFEX funding interval') / 60,
                bid, ask, next_funding * 1000 if next_funding is not None else None,
                quote_currency=quote, funding_hours=meta.get('market_hours') or {})
        logger.info('Fetched %d markets from QFEX', len(markets))
        return markets

    async def fetch_order_book(self, symbol, limit):
        if limit <= 0:
            raise ValueError('QFEX book limit must be positive')
        if symbol not in self._symbols:
            await self._load_markets()
        if symbol not in self._symbols or symbol in self.settings.excluded_symbols:
            raise ValueError(f'QFEX unsupported market: {symbol}')
        native = self._symbols[symbol]['symbol']
        response = await self._client.get(f'/md/orderbook/{native}')
        response.raise_for_status()
        payload = response.json()
        if payload['ticker_id'] != native:
            raise ValueError('QFEX mismatched order book symbol')
        book = order_book(payload['bids'], payload['asks'], timestamp_ms(payload['timestamp'], book=True), limit)
        book['quote_currency'] = quote_currency(self._symbols[symbol]['quote_asset'])
        return book

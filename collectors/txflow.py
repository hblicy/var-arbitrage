"""TxFlow batch funding subscription with rate-limited public L2 snapshots."""
import asyncio
import json
import logging
import time

import websocket

from collectors.base import MarketCollector
from collectors.decorators import with_retry
from collectors.perpetual_utils import StalePublicData, number, order_book, positive, quote_currency, symbol_key, timestamp_ms
from collectors.session_pool import session_pool
from models import MarketDatum
from config import settings as app_settings

logger = logging.getLogger(__name__)


class TxflowCollector(MarketCollector):
    def __init__(self, settings):
        super().__init__(settings)
        self._client = session_pool.get_session('txflow', settings.base_url, timeout=settings.timeout,
                                                headers=settings.extra_headers)
        self._symbols = {}
        self._metadata_at = 0
        self._request_lock = asyncio.Lock()
        self._next_request_at = 0

    async def aclose(self):
        await session_pool.close_session('txflow')

    async def _info(self, kind, **fields):
        # Share the 40/s budget across scans and manual depth checks (official limit: 50/s).
        async with self._request_lock:
            delay = self._next_request_at - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._next_request_at = time.monotonic() + .025
        response = await self._client.post('/info', json={'type': kind, **fields})
        response.raise_for_status()
        payload = response.json()
        if payload['success'] is not True or payload['code'] != 0:
            raise ValueError(f"TxFlow {kind}: {payload['code']} {payload.get('message')}")
        return payload['data']

    async def _load_markets(self):
        if self._symbols and time.monotonic() - self._metadata_at < self.settings.metadata_refresh_seconds:
            return
        payload = await self._info('perpMeta', includeDelisted=False)
        symbols = {}
        for row in payload['universe']:
            if row['delisted'] or row['haltTrading']:
                continue
            symbol = symbol_key(self.settings, row['name'], row['baseCurrency'], row['quoteCurrency'])
            if symbol in self.settings.excluded_symbols or row['name'] in self.settings.excluded_symbols:
                continue
            if symbol in symbols:
                raise ValueError(f'TxFlow duplicate symbol mapping: {symbol}')
            symbols[symbol] = row
        self._symbols = symbols
        self._metadata_at = time.monotonic()

    def _fetch_snapshot(self):
        deadline = time.monotonic() + self.settings.timeout
        socket = websocket.create_connection(self.settings.ws_url, timeout=self.settings.timeout)
        try:
            socket.send(json.dumps({'method': 'subscribe', 'type': 'allAssetCtxs',
                                    'subscription': {'type': 'allAssetCtxs'}}))
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('TxFlow funding snapshot timed out')
                socket.settimeout(remaining)
                raw = socket.recv()
                if not raw:
                    raise ConnectionError('TxFlow market stream closed before snapshot')
                payload = json.loads(raw)
                if payload.get('channel') == 'error':
                    raise ValueError(f'TxFlow market stream error: {payload}')
                if payload.get('channel') == 'allAssetCtxs':
                    return payload['data']['ctxs']
        finally:
            socket.close()

    @with_retry(max_retries=3)
    async def fetch_markets(self, symbols):
        await self._load_markets()
        requested = set(symbols)
        selected = {symbol: row for symbol, row in self._symbols.items() if not requested or symbol in requested}
        if not selected:
            return {}
        collected_at = time.time()
        stats = await self._info('webData2', user='0x0000000000000000000000000000000000000000')
        tickers = {int(row['instrumentId']): row for row in stats['assetCtxs']}
        contexts = await asyncio.to_thread(self._fetch_snapshot)
        by_id = {int(row['coin']): row['ctx'] for row in contexts}
        markets = {}
        for symbol, meta in selected.items():
            index = int(meta['index'])
            if index not in by_id:
                logger.warning('Skipping TxFlow %s: no funding context', symbol)
                continue
            ctx, ticker = by_id[index], tickers.get(index, {})
            timestamp = min(collected_at, timestamp_ms(ctx['blockTime']))
            quote = quote_currency(meta['quoteCurrency'])
            volume = number(ticker.get('dayNtlVlm')) if quote == 'USD' else None
            if volume is not None and volume < (self.settings.min_volume_usd or 0):
                continue
            price = number(ticker.get('lastPrice')) or number(ctx.get('markPx'))
            if price is None or price <= 0:
                logger.warning('Skipping TxFlow %s: no traded or mark price', symbol)
                continue
            bid = ask = None
            next_time, prev_time = number(ctx.get('nextFundingTime')), number(ctx.get('prevFundingTime'))
            interval = positive(next_time-prev_time, 'TxFlow funding interval') / 3600000 if (
                next_time is not None and prev_time is not None) else 1
            funding = number(ctx.get('funding'))
            # TxFlow node funding, like marketTicker.fundingRate, is a percentage.
            markets[symbol] = MarketDatum(symbol, price, funding / 100 if funding is not None else None,
                volume, timestamp, self.settings.name, interval, bid, ask, next_time, quote_currency=quote)
        async def quote_market(item):
            if not requested and item.volume_24h is not None and item.volume_24h < app_settings.thresholds.min_volume_usd:
                return
            try:
                book = await self.fetch_order_book(item.symbol, 1)
            except StalePublicData as exc:
                logger.warning('TxFlow %s BBO unavailable: %s', item.symbol, exc)
                return
            item.best_bid = book['bids'][0][0] if book['bids'] else None
            item.best_ask = book['asks'][0][0] if book['asks'] else None
            item.timestamp = min(item.timestamp, book['timestamp'])
        await self._gather_with_semaphore([quote_market(item) for item in markets.values()])
        logger.info('Fetched %d markets from TxFlow', len(markets))
        return markets

    async def fetch_order_book(self, symbol, limit):
        if limit <= 0:
            raise ValueError('TxFlow book limit must be positive')
        if symbol not in self._symbols:
            await self._load_markets()
        if symbol not in self._symbols or symbol in self.settings.excluded_symbols:
            raise ValueError(f'TxFlow unsupported market: {symbol}')
        index = str(self._symbols[symbol]['index'])
        payload = await self._info('l2Book', coin=index)
        if payload['coin'] != self._symbols[symbol]['name']:
            raise ValueError('TxFlow mismatched order book instrument')
        bids, asks = ([[row['px'], row['sz']] for row in side] for side in payload['levels'])
        book = order_book(bids, asks, timestamp_ms(payload['time'], book=True), limit)
        book['quote_currency'] = quote_currency(self._symbols[symbol]['quoteCurrency'])
        return book

"""PopDEX public perpetual metadata, paged tickers and L2 snapshots."""
import logging
import asyncio
import time

from collectors.base import MarketCollector
from collectors.decorators import with_retry
from collectors.perpetual_utils import number, order_book, positive, quote_currency, symbol_key, timestamp_ms
from collectors.session_pool import session_pool
from models import MarketDatum

logger = logging.getLogger(__name__)


class PopdexCollector(MarketCollector):
    def __init__(self, settings):
        super().__init__(settings)
        self._client = session_pool.get_session('popdex', settings.base_url, timeout=settings.timeout)
        self._symbols = {}
        self._metadata_at = 0

    async def aclose(self):
        await session_pool.close_session('popdex')

    async def _get(self, path, params=None):
        response = await self._client.get(path, params=params)
        response.raise_for_status()
        payload = response.json()
        if str(payload['code']) != '200':
            raise ValueError(f"PopDEX {path}: {payload['code']} {payload.get('msg')}")
        return payload

    async def _load_markets(self):
        if self._symbols and time.monotonic() - self._metadata_at < self.settings.metadata_refresh_seconds:
            return
        payload = await self._get('/api/v1/config/symbols')
        symbols = {}
        for row in payload['data']:
            if row['category'] != 'Futures' or row['status'] != 'Trading':
                continue
            symbol = symbol_key(self.settings, row['symbol'], row['baseToken'], row['quoteToken'])
            if symbol in self.settings.excluded_symbols or row['symbol'] in self.settings.excluded_symbols:
                continue
            if symbol in symbols:
                raise ValueError(f'PopDEX duplicate symbol mapping: {symbol}')
            symbols[symbol] = row
        self._symbols = symbols
        self._metadata_at = time.monotonic()

    async def _get_pages(self, path, params):
        rows, seen, cursor, timestamps = [], set(), None, []
        while True:
            query = {**params, 'limit': 100}
            if cursor is not None:
                query['cursor'] = cursor
            payload = await self._get(path, query)
            page = payload['data']
            if not isinstance(page, list):
                raise ValueError(f'PopDEX {path}: data must be a list')
            if 'updatedTime' in payload:
                timestamps.append(timestamp_ms(payload['updatedTime']))
            for row in page:
                if row['symbol'] in seen:
                    raise ValueError(f"PopDEX repeated symbol in {path}: {row['symbol']}")
                seen.add(row['symbol'])
                rows.append(row)
            if len(seen) >= int(payload['total']):
                return rows, min(timestamps) if timestamps else None
            next_cursor = payload.get('cursor')
            if not page or not next_cursor or next_cursor == cursor:
                raise ValueError(f'PopDEX incomplete pagination: {path}')
            cursor = next_cursor

    @with_retry(max_retries=3)
    async def fetch_markets(self, symbols):
        await self._load_markets()
        requested = set(symbols)
        by_native = {row['symbol']: (symbol, row) for symbol, row in self._symbols.items()}
        markets = {}
        collected_at = time.time()
        (rows, _), (rates, funding_at) = await asyncio.gather(
            self._get_pages('/api/v1/public/market/tickers', {'category': 'Futures'}),
            self._get_pages('/api/v1/market/funding-rate', {}),
        )
        if funding_at is None:
            raise ValueError('PopDEX missing funding snapshot timestamp')
        funding = {row['symbol']: row for row in rates}
        for row in rows:
            native = row['symbol']
            if native not in by_native or row['status'] != 'Trading':
                continue
            symbol, meta = by_native[native]
            if requested and symbol not in requested:
                continue
            timestamp = min(timestamp_ms(row['updatedTime']), funding_at)
            quote = quote_currency(meta['quoteToken'])
            volume = number(row.get('turnover24h')) if quote == 'USD' else None
            if volume is not None and volume < (self.settings.min_volume_usd or 0):
                continue
            rate = funding.get(native)
            interval = positive(rate['fundingRateInterval'] if rate else meta['fundingInterval'], 'PopDEX funding interval')
            next_time = positive(rate['nextUpdate'], 'PopDEX next funding time') if rate else None
            bid = number(row.get('bid1Price')) if (number(row.get('bid1Size')) or 0) > 0 else None
            ask = number(row.get('ask1Price')) if (number(row.get('ask1Size')) or 0) > 0 else None
            markets[symbol] = MarketDatum(symbol, positive(row['lastPrice'], 'PopDEX price'),
                number(rate.get('fundingRate')) if rate else None, volume, min(collected_at, timestamp), self.settings.name,
                interval, bid or None, ask or None,
                next_time, quote_currency=quote)
        logger.info('Fetched %d markets from PopDEX', len(markets))
        return markets

    async def fetch_order_book(self, symbol, limit):
        if limit <= 0:
            raise ValueError('PopDEX book limit must be positive')
        if symbol not in self._symbols:
            await self._load_markets()
        if symbol not in self._symbols or symbol in self.settings.excluded_symbols:
            raise ValueError(f'PopDEX unsupported market: {symbol}')
        payload = await self._get('/api/v1/public/market/orderbook', {
            'category': 'Futures', 'symbol': self._symbols[symbol]['symbol'], 'limit': min(limit, 100),
        })
        book = order_book(payload['data']['bids'], payload['data']['asks'],
                          timestamp_ms(payload['updatedTime'], book=True), limit)
        book['quote_currency'] = quote_currency(self._symbols[symbol]['quoteToken'])
        return book

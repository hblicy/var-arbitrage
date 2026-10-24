"""Validation shared by the TxFlow, QFEX and PopDEX public readers."""
import math
import time

from config import settings as app_settings


class StalePublicData(ValueError):
    pass


def number(value):
    if value is None or value == '':
        return None
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f'Non-finite market number: {value!r}')
    return result


def positive(value, label):
    result = number(value)
    if result is None or result <= 0:
        raise ValueError(f'Invalid {label}: {value!r}')
    return result


def timestamp_ms(value, *, book=False):
    timestamp = positive(value, 'source timestamp') / 1000
    age = time.time() - timestamp
    limit = app_settings.entry_check.max_quote_age_ms / 1000 if book else app_settings.schedule.market_stale_seconds
    if (limit and age > limit) or age < -5:
        raise StalePublicData(f'stale {"book" if book else "market"} ({age:.3f}s)')
    return timestamp


def order_book(bids, asks, timestamp, limit):
    book = {'timestamp': timestamp}
    for name, rows in (('bids', bids), ('asks', asks)):
        levels = []
        for row in rows:
            price = positive(row[0], 'book price')
            size = number(row[1])
            if size is None or size < 0:
                raise ValueError(f'Invalid book quantity: {row[1]!r}')
            if size:
                levels.append([price, size])
        book[name] = sorted(levels, key=lambda level: level[0], reverse=name == 'bids')[:limit]
    if book['bids'] and book['asks'] and book['bids'][0][0] >= book['asks'][0][0]:
        raise ValueError('Crossed order book')
    return book


def quote_currency(raw):
    return 'USD' if raw.upper() in ('USD', 'USDT', 'USDC') else raw.upper()


def symbol_key(settings, native, base, quote):
    symbol = settings.symbol_overrides.get(native)
    if symbol is None:
        symbol = base.upper() + ('USDT' if quote_currency(quote) == 'USD' else quote.upper())
    return symbol

"""Cashflow estimates for equal-quantity, two-leg funding positions."""
from __future__ import annotations

import math

from entry_check import _levels_vwap

HORIZONS = (1, 4, 8, 24)
DEPTH_BUDGETS = (1000, 5000, 10000)


def settlement_schedule(market, now: float, hours: int) -> dict:
    period = market.native_interval_hours * 3600
    if not math.isfinite(period) or period <= 0:
        raise ValueError('Invalid funding interval')
    supplied = market.next_funding_time
    if supplied is None:
        first = (math.floor(now / period) + 1) * period
        source = 'utc_estimate'
    else:
        first = supplied / 1000
        if not math.isfinite(first) or first <= 0:
            raise ValueError('Invalid next funding time')
        source = market.next_funding_time_source
        if first <= now:
            first += (math.floor((now - first) / period) + 1) * period
            source = 'rolled_forward'
    end = now + hours * 3600
    count = max(0, math.floor((end - first) / period) + 1)
    return {'count': count, 'next_at': first, 'source': source}


def project_funding(buy, sell, *, now, hours, notional, buy_fee, sell_fee, prices=None, quantity=None) -> dict:
    if hours not in HORIZONS or not math.isfinite(notional) or notional <= 0:
        raise ValueError('Invalid holding horizon or notional')
    if any(not math.isfinite(fee) or fee < 0 for fee in (buy_fee, sell_fee)):
        raise ValueError('Invalid fee')
    base = {'hours': hours, 'notional_usd': notional, 'exit_assumption': 'unchanged_books',
            'funding_assumption': 'constant_rates', 'net_bps': None, 'net_usd': None}
    if buy.funding_rate is None or sell.funding_rate is None:
        return {**base, 'reason': 'FUNDING_UNAVAILABLE'}
    values = (buy.price, sell.price, buy.funding_rate, sell.funding_rate)
    if any(not math.isfinite(v) for v in values) or min(buy.price, sell.price) <= 0:
        return {**base, 'reason': 'INVALID_MARKET'}
    prices = prices or (buy.best_ask, sell.best_bid, buy.best_bid, sell.best_ask)
    if any(p is None or not math.isfinite(p) or p <= 0 for p in prices):
        return {**base, 'reason': 'BOOK_UNAVAILABLE'}
    entry_buy, entry_sell, exit_buy, exit_sell = prices
    if entry_buy < exit_buy or entry_sell > exit_sell:
        return {**base, 'reason': 'INVALID_BOOK'}
    try:
        buy_schedule = settlement_schedule(buy, now, hours)
        sell_schedule = settlement_schedule(sell, now, hours)
    except ValueError:
        return {**base, 'reason': 'FUNDING_SCHEDULE_INVALID'}
    quantity = quantity if quantity is not None else notional / max(entry_buy, entry_sell)
    funding_long = -quantity * buy.price * buy.funding_rate * buy_schedule['count']
    funding_short = quantity * sell.price * sell.funding_rate * sell_schedule['count']
    entry_spread = quantity * (entry_sell - entry_buy)
    exit_cost = quantity * (exit_sell - exit_buy)
    fees = quantity * ((entry_buy + exit_buy) * buy_fee + (entry_sell + exit_sell) * sell_fee) / 10000
    net = funding_long + funding_short + entry_spread - exit_cost - fees
    return {**base, 'reason': None, 'quantity': quantity, 'net_usd': net, 'net_bps': net / notional * 10000,
            'funding_usd': funding_long + funding_short, 'funding_long_usd': funding_long,
            'funding_short_usd': funding_short, 'entry_spread_usd': entry_spread,
            'exit_spread_cost_usd': exit_cost, 'fees_usd': fees,
            'buy_schedule': buy_schedule, 'sell_schedule': sell_schedule,
            'entry_buy_vwap': entry_buy, 'entry_sell_vwap': entry_sell,
            'exit_buy_vwap': exit_buy, 'exit_sell_vwap': exit_sell}


def depth_scenarios(buy, sell, buy_book, sell_book, *, now, hours, budgets=DEPTH_BUDGETS,
                    buy_fee, sell_fee, min_net_bps) -> dict:
    sides = [buy_book.get('asks', []), sell_book.get('bids', []),
             buy_book.get('bids', []), sell_book.get('asks', [])]
    if any(not side for side in sides):
        return {'reason': 'EMPTY_BOOK', 'tiers': [], 'capacity': None}
    for i, side in enumerate(sides):
        if any(len(level) < 2 or any(not math.isfinite(v) or v <= 0 for v in level[:2]) for level in side):
            return {'reason': 'INVALID_BOOK', 'tiers': [], 'capacity': None}
        sides[i] = sorted(side, key=lambda level: level[0], reverse=i in (1, 2))
    if sides[0][0][0] < sides[2][0][0] or sides[1][0][0] > sides[3][0][0]:
        return {'reason': 'INVALID_BOOK', 'tiers': [], 'capacity': None}
    reference = max(sides[0][0][0], sides[1][0][0])
    max_quantity = min(sum(level[1] for level in side) for side in sides)

    def evaluate(q):
        prices = tuple(_levels_vwap(side, q) for side in sides)
        return project_funding(buy, sell, now=now, hours=hours, notional=q * max(prices[:2]),
                               buy_fee=buy_fee, sell_fee=sell_fee, prices=prices, quantity=q)

    tiers = []
    for budget in budgets:
        if evaluate(max_quantity)['notional_usd'] + 1e-8 < budget:
            tiers.append({'notional_usd': budget, 'reason': 'INSUFFICIENT_DEPTH', 'net_bps': None, 'net_usd': None})
            continue
        low, high = 0.0, max_quantity
        for _ in range(50):
            mid = (low + high) / 2
            if evaluate(mid)['notional_usd'] <= budget:
                low = mid
            else:
                high = mid
        tiers.append(evaluate(low))
    maximum = evaluate(max_quantity)
    if maximum['reason'] is not None:
        return {'reason': maximum['reason'], 'tiers': tiers, 'capacity': None}
    book_limited = maximum['net_bps'] >= min_net_bps
    if book_limited:
        low = max_quantity
    else:
        low, high = 0.0, max_quantity
        for _ in range(50):
            mid = (low + high) / 2
            if evaluate(mid)['net_bps'] >= min_net_bps:
                low = mid
            else:
                high = mid
    if low * reference < 0.01:
        low = 0.0
    return {'reason': None, 'tiers': tiers, 'capacity': {
        'quantity': low, 'notional_usd': evaluate(low)['notional_usd'] if low else 0.0, 'book_limited': book_limited,
    }}

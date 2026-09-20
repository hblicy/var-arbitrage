"""Funding signals shared by the API and command-line scan loop."""
from __future__ import annotations

import asyncio
import copy
import logging
import math
import time

from curl_cffi.requests.exceptions import RequestException

from funding_strategy import DEPTH_BUDGETS, HORIZONS, depth_scenarios, project_funding
from strategy_history import StrategyHistory

logger = logging.getLogger(__name__)


def route_key(symbol, buy, sell):
    return f'{symbol.upper()}|{buy.lower()}|{sell.lower()}'


class FundingMonitor:
    def __init__(self, cfg, history=None):
        self.cfg = cfg
        self.history = history if history is not None else StrategyHistory(cfg.strategy.history_path)
        self.rows = []
        self.updated = None
        self._requests = asyncio.Semaphore(5)

    def _fresh(self, market, now):
        age = self.cfg.schedule.market_stale_seconds
        return market is not None and market.timestamp is not None and (
            not age or now - market.timestamp <= age
        )

    async def _book(self, collector, symbol):
        async with self._requests:
            try:
                book = await collector.fetch_order_book(symbol, self.cfg.entry_check.book_depth_limit)
            except (RequestException, TimeoutError, ValueError) as exc:
                logger.warning('Funding depth unavailable for %s: %s', symbol, exc, exc_info=True)
                return {'reason': 'QUOTE_FETCH_FAILED'}
            quoted_at = book.get('timestamp', time.time())
            if not isinstance(quoted_at, (int, float)) or not math.isfinite(quoted_at):
                return {'reason': 'INVALID_QUOTE_TIME'}
            return {'reason': None, 'book': book, 'quoted_at': quoted_at}

    def _depth(self, buy, sell, buy_key, sell_key, hours, books, now):
        if any(item['reason'] for item in books):
            return {'reason': next(item['reason'] for item in books if item['reason']), 'tiers': [], 'capacity': None}
        times = [item['quoted_at'] for item in books]
        age_ms = (now-min(times))*1000
        if age_ms > self.cfg.entry_check.max_quote_age_ms or max(times)-now > 5:
            return {'reason': 'STALE_QUOTE', 'tiers': [], 'capacity': None}
        if abs(times[0]-times[1])*1000 > self.cfg.entry_check.max_leg_skew_ms:
            return {'reason': 'LEG_SKEW', 'tiers': [], 'capacity': None}
        budgets = sorted(set((*DEPTH_BUDGETS, self.cfg.entry_check.notional_usd)))
        result = depth_scenarios(buy, sell, books[0]['book'], books[1]['book'], now=now,
                                 hours=hours, budgets=budgets,
                                 buy_fee=self.cfg.exchanges[buy_key].taker_bps,
                                 sell_fee=self.cfg.exchanges[sell_key].taker_bps,
                                 min_net_bps=self.cfg.strategy.min_net_bps)
        result.update(checked_at=now, expires_at=min(times)+self.cfg.entry_check.max_quote_age_ms/1000,
                      quote_age_ms=max(0, age_ms), hours=hours)
        if time.time() > result['expires_at']:
            return {'reason': 'STALE_QUOTE', 'tiers': [], 'capacity': None}
        return result

    async def check(self, symbol, buy_key, sell_key, hours, data, collectors):
        buy_key, sell_key = buy_key.lower(), sell_key.lower()
        if hours not in HORIZONS or buy_key == sell_key:
            raise ValueError('Invalid holding horizon or exchange pair')
        if buy_key not in self.cfg.exchanges or sell_key not in self.cfg.exchanges:
            raise ValueError('Unknown exchange')
        buy, sell = data.get(buy_key, {}).get(symbol), data.get(sell_key, {}).get(symbol)
        if not all(self._fresh(market, time.time()) for market in (buy, sell)):
            return {'reason': 'STALE_DATA', 'tiers': [], 'capacity': None}
        pair = [collectors.get(key) for key in (buy_key, sell_key)]
        if any(not callable(getattr(collector, 'fetch_order_book', None)) for collector in pair):
            return {'reason': 'DEPTH_UNSUPPORTED', 'tiers': [], 'capacity': None}
        tasks = [asyncio.create_task(self._book(collector, symbol)) for collector in pair]
        try:
            books = await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        now = time.time()
        if not all(self._fresh(market, now) for market in (buy, sell)):
            return {'reason': 'STALE_DATA', 'tiers': [], 'capacity': None}
        return self._depth(buy, sell, buy_key, sell_key, hours, books, now)

    async def process(self, opportunities, data, collectors, *, on_triggered=None):
        now = time.time()
        policy = self.cfg.strategy
        max_gap = max(self.cfg.schedule.interval_seconds * 3, 1)
        self.history.record({
            route_key(opp.symbol, opp.details['buy_exchange'], opp.details['sell_exchange']): {
                'spread': opp.gross_spread_bps, 'funding': opp.details['funding_hourly_bps'],
                'timestamp': min(data[opp.details['buy_exchange'].lower()][opp.symbol].timestamp,
                                 data[opp.details['sell_exchange'].lower()][opp.symbol].timestamp),
            } for opp in opportunities if opp.funding_diff is not None
            and opp.details.get('buy_price_source') == 'best_ask'
            and opp.details.get('sell_price_source') == 'best_bid'
        }, now, max_gap=max_gap, max_age=self.cfg.schedule.market_stale_seconds)
        history = self.history.summaries(now, policy.history_hours)
        rows = []
        for opp in opportunities:
            buy_key, sell_key = opp.details['buy_exchange'].lower(), opp.details['sell_exchange'].lower()
            buy, sell = data[buy_key][opp.symbol], data[sell_key][opp.symbol]
            key = route_key(opp.symbol, buy_key, sell_key)
            projections = {str(hours): project_funding(buy, sell, now=now, hours=hours,
                           notional=self.cfg.entry_check.notional_usd,
                           buy_fee=self.cfg.exchanges[buy_key].taker_bps,
                           sell_fee=self.cfg.exchanges[sell_key].taker_bps) for hours in HORIZONS}
            summary = history.get(key, {'coverage': 0, 'valid_seconds': 0, 'window_hours': policy.history_hours})
            mean = summary.get('spread_mean_bps')
            row = {'key': key, 'symbol': opp.symbol, 'buy_exchange': buy_key, 'sell_exchange': sell_key,
                   'holding_hours': policy.holding_hours, 'notional_usd': self.cfg.entry_check.notional_usd,
                   'timestamp': min(buy.timestamp, sell.timestamp), 'projections': projections,
                   'history': summary, 'spread_bps': opp.gross_spread_bps,
                   'funding_hourly_bps': opp.details['funding_hourly_bps'] if opp.funding_diff is not None else None,
                   'spread_deviation_bps': opp.gross_spread_bps-mean if mean is not None else None,
                   'depth': {'reason': 'DEPTH_NOT_CHECKED', 'tiers': [], 'capacity': None},
                   'status': 'watch', 'reason': 'DEPTH_NOT_CHECKED', 'qualified_seconds': 0,
                   'required_seconds': policy.sustain_seconds}
            rows.append((opp, row, buy, sell))
        candidates = sorted((item for item in rows if
            item[1]['projections'][str(policy.holding_hours)]['reason'] is None
            and item[1]['projections'][str(policy.holding_hours)]['net_bps'] >= policy.min_net_bps
            and (item[1]['funding_hourly_bps'] or 0) > 0
            and item[1]['history']['coverage'] >= policy.min_coverage),
            key=lambda item: item[1]['projections'][str(policy.holding_hours)]['net_bps'], reverse=True)
        requests = {}
        supported = []
        for _, row, _, _ in candidates:
            keys = (row['buy_exchange'], row['sell_exchange'])
            if any(not callable(getattr(collectors.get(key), 'fetch_order_book', None)) for key in keys):
                row['depth']['reason'] = 'DEPTH_UNSUPPORTED'
                continue
            supported.append(row['key'])
            if len(supported) > 20:
                continue
            for key in keys:
                requests[(key, row['symbol'])] = collectors[key]
        candidate_keys = set(supported[:20])
        self.rows = [row for _, row, _, _ in rows]
        self.updated = now
        active = {row['key'] for row in self.rows}
        for key in list(self.history.states):
            if key not in active:
                self.history.advance(key, now, False, False, 'STALE_DATA',
                                     required_seconds=policy.sustain_seconds, max_gap=max_gap)
        # Each route consumes shared book tasks as soon as its own two legs finish.
        book_tasks = {key: asyncio.create_task(self._book(collector, key[1]))
                      for key, collector in requests.items()}
        completed = set()

        async def finish_route(item):
            opp, row, buy, sell = item
            if row['key'] in candidate_keys:
                keys = [(key, row['symbol']) for key in (row['buy_exchange'], row['sell_exchange'])]
                books = await asyncio.gather(*(book_tasks[key] for key in keys))
                row['depth'] = self._depth(buy, sell, row['buy_exchange'], row['sell_exchange'],
                                           policy.holding_hours, books, time.time())
            checked = time.time()
            projection = row['projections'][str(policy.holding_hours)]
            valid = self._fresh(buy, checked) and self._fresh(sell, checked)
            if not valid:
                reason = 'STALE_DATA'
            elif projection['reason']:
                reason = projection['reason']
            elif (row['funding_hourly_bps'] or 0) <= 0 or projection['net_bps'] < policy.min_net_bps:
                reason = 'LOW_NET_RETURN'
            elif row['history']['coverage'] < policy.min_coverage:
                reason = 'HISTORY_WARMUP'
            else:
                depth = row['depth']
                if depth.get('expires_at', math.inf) < checked:
                    depth = row['depth'] = {'reason': 'STALE_QUOTE', 'tiers': [], 'capacity': None}
                reason = depth['reason']
                if not reason:
                    tier = min(depth['tiers'], key=lambda tier: abs(tier['notional_usd']-self.cfg.entry_check.notional_usd))
                    reason = tier['reason'] or ('LOW_NET_RETURN' if tier['net_bps'] < policy.min_net_bps else None)
            valid = valid and reason not in ('STALE_QUOTE', 'LEG_SKEW', 'QUOTE_FETCH_FAILED', 'INVALID_QUOTE_TIME')
            state = self.history.advance(row['key'], checked, reason is None, valid, reason,
                                         required_seconds=policy.sustain_seconds, max_gap=max_gap,
                                         valid_until=row['timestamp']+self.cfg.schedule.market_stale_seconds
                                         if self.cfg.schedule.market_stale_seconds else None)
            row.update(state)
            opp.details['funding_strategy'] = row
            self.updated = checked
            completed.add(row['key'])
            if state['status'] == 'triggered' and on_triggered is not None:
                await on_triggered([opp])

        route_tasks = [asyncio.create_task(finish_route(item)) for item in rows]
        try:
            await asyncio.gather(*route_tasks)
        finally:
            tasks = [*route_tasks, *book_tasks.values()]
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            interrupted_at = time.time()
            for opp, row, _, _ in rows:
                if row['key'] in completed:
                    continue
                row.update(self.history.advance(row['key'], interrupted_at, False, False, 'SCAN_INTERRUPTED',
                                                 required_seconds=policy.sustain_seconds, max_gap=max_gap))
                row['depth'] = {'reason': 'SCAN_INTERRUPTED', 'tiers': [], 'capacity': None}
                opp.details['funding_strategy'] = row
                self.updated = interrupted_at
                logger.warning('Funding check interrupted for %s; continuous timer reset', row['key'])
            self.history.flush_events()

    def snapshot(self, now):
        rows = copy.deepcopy(self.rows)
        for row in rows:
            if self.cfg.schedule.market_stale_seconds and now-row['timestamp'] > self.cfg.schedule.market_stale_seconds:
                row.update(status='paused', reason='STALE_DATA', qualified_seconds=0)
                row['depth'] = {'reason': 'STALE_DATA', 'tiers': [], 'capacity': None}
            elif row['depth'].get('expires_at', math.inf) < now:
                row['depth'] = {'reason': 'QUOTE_EXPIRED', 'tiers': [], 'capacity': None}
        return {'rows': rows, 'updated': self.updated, 'server_now': now,
                'market_stale_seconds': self.cfg.schedule.market_stale_seconds,
                'default_hours': self.cfg.strategy.holding_hours,
                'history_hours': self.cfg.strategy.history_hours, 'min_net_bps': self.cfg.strategy.min_net_bps,
                'min_coverage': self.cfg.strategy.min_coverage, 'sustain_seconds': self.cfg.strategy.sustain_seconds,
                'notional_usd': self.cfg.entry_check.notional_usd}

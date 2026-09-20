"""Contracts for Hyperliquid and Lighter public depth snapshots."""
import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from collectors.factory import create_collector
from config import Settings
from entry_check import entry_check_supported, evaluate_entry_quote
from analyzer import analyse_markets
from funding_monitor import FundingMonitor
from market_state import stamp_market_timestamps
from models import MarketDatum
from strategy_history import StrategyHistory

NOW = 1789905245.0


class Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return copy.deepcopy(self.payload)


class TestDepthCollectors(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        clock = patch('time.time', return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)
        retry = patch('collectors.decorators.asyncio.sleep', new_callable=AsyncMock)
        retry.start()
        self.addCleanup(retry.stop)
        self.stats = {'channel': 'market_stats:all', 'type': 'subscribed/market_stats',
                      'timestamp': int(NOW * 1000), 'market_stats': {
                          '1': {'symbol': 'BTC', 'market_id': 1,
                                'best_bid_price': '99', 'best_ask_price': '101'}}}
        self.sockets = []
        self.funding_rows = [{'exchange': 'lighter', 'symbol': 'BTC', 'rate': '0'}]

        def connect(*args, **kwargs):
            socket = Mock()
            messages = iter([{'type': 'connected'}, {'type': 'ping'}, self.stats])
            socket.recv.side_effect = lambda: json.dumps(next(messages))
            self.sockets.append(socket)
            return socket

        websocket = patch('websocket.create_connection', side_effect=connect)
        websocket.start()
        self.addCleanup(websocket.stop)

    def collector(self, key):
        with patch('collectors.session_pool.session_pool.get_session'):
            collector = create_collector(key, Settings().exchanges[key])
        session = AsyncMock()
        collector._session = session
        if key == 'hyperliquid':
            metadata = [{'universe': [{'name': 'BTC'}, {'name': 'OLD', 'isDelisted': True}]},
                        [{'markPx': '100', 'funding': '0.0001', 'dayNtlVlm': '1000000'}] * 2]
            book = {'coin': 'BTC', 'time': int(NOW * 1000),
                    'levels': [[{'px': '98', 'sz': '2'}, {'px': '99', 'sz': '3'}],
                               [{'px': '102', 'sz': '2'}, {'px': '101', 'sz': '3'}]]}

            async def post(path, json):
                self.assertEqual(path, '/info')
                if json['type'] == 'l2Book':
                    self.assertEqual(json['coin'], 'BTC')
                    return Response(book)
                return Response(metadata)

            session.post.side_effect = post
        else:
            metadata = {'code': 200, 'order_book_details': [
                {'symbol': 'BTC', 'market_id': 1, 'market_type': 'perp', 'status': 'active',
                 'last_trade_price': 100, 'daily_quote_token_volume': 1000000},
                {'symbol': 'OLD', 'market_id': 2, 'market_type': 'perp', 'status': 'inactive'},
                {'symbol': 'SPOT', 'market_id': 3, 'market_type': 'spot', 'status': 'active'}]}
            book = {'code': 200, 'bids': [
                {'price': '98', 'remaining_base_amount': '2', 'initial_base_amount': '200'},
                {'price': '99', 'remaining_base_amount': '3', 'initial_base_amount': '300'}],
                'asks': [{'price': '102', 'remaining_base_amount': '2', 'initial_base_amount': '200'},
                         {'price': '101', 'remaining_base_amount': '3', 'initial_base_amount': '300'}]}

            async def get(path, params=None):
                if path == '/api/v1/orderBookDetails':
                    return Response(metadata)
                if path == '/api/v1/funding-rates':
                    return Response({'funding_rates': self.funding_rows})
                self.assertEqual(path, '/api/v1/orderBookOrders')
                self.assertEqual(params['market_id'], 1)
                self.assertLessEqual(params['limit'], 250)
                return Response(book)

            session.get.side_effect = get
        return collector, book

    async def test_both_collectors_enable_depth_review(self):
        collectors = {key: self.collector(key)[0] for key in ('hyperliquid', 'lighter')}
        self.assertTrue(entry_check_supported('hyperliquid', 'lighter', collectors))
        self.assertFalse(entry_check_supported('variational', 'lighter', collectors))

    async def test_cold_start_maps_native_market_and_sorts_remaining_depth(self):
        for key in ('hyperliquid', 'lighter'):
            with self.subTest(key=key):
                collector, _ = self.collector(key)
                book = await collector.fetch_order_book('BTCUSDT', 1000)
                self.assertEqual(book['bids'], [[99, 3], [98, 2]])
                self.assertEqual(book['asks'], [[101, 3], [102, 2]])
                self.assertEqual(book['timestamp'], NOW)
                result = evaluate_entry_quote(
                    buy_book=book, sell_book=book, notional_usd=1000,
                    buy_fee_bps=0, sell_fee_bps=0, safety_buffer_bps=0,
                    buy_quoted_at=NOW, sell_quoted_at=NOW, checked_at=NOW,
                    max_quote_age_ms=2000, max_leg_skew_ms=1000)
                self.assertEqual(result['reason'], 'INSUFFICIENT_DEPTH')

    async def test_empty_books_stay_empty_and_invalid_prices_fail(self):
        for key in ('hyperliquid', 'lighter'):
            collector, payload = self.collector(key)
            side = payload['levels'][0] if key == 'hyperliquid' else payload['bids']
            side[0]['px' if key == 'hyperliquid' else 'price'] = 'nan'
            with self.assertRaises(ValueError):
                await collector.fetch_order_book('BTCUSDT', 100)
            if key == 'hyperliquid':
                payload['levels'] = [[], []]
            else:
                payload['bids'], payload['asks'] = [], []
            book = await collector.fetch_order_book('BTCUSDT', 100)
            self.assertEqual((book['bids'], book['asks']), ([], []))

    async def test_unknown_excluded_and_inactive_symbols_never_fetch_a_book(self):
        for key in ('hyperliquid', 'lighter'):
            collector, _ = self.collector(key)
            for symbol in ('BADUSDT', 'OLDUSDT', 'SPOTUSDT'):
                with self.assertRaisesRegex(ValueError, 'unsupported market'):
                    await collector.fetch_order_book(symbol, 10)
            collector.settings.excluded_symbols = ['BTCUSDT']
            with self.assertRaisesRegex(ValueError, 'unsupported market'):
                await collector.fetch_order_book('BTCUSDT', 10)

    async def test_overrides_and_warm_metadata_reuse(self):
        for key in ('hyperliquid', 'lighter'):
            collector, _ = self.collector(key)
            collector.settings.symbol_overrides = {'BTC': 'XBTUSDT'}
            await collector.fetch_markets(['XBTUSDT'])
            collector._session.reset_mock()
            await collector.fetch_order_book('XBTUSDT', 10)
            calls = collector._session.post.await_args_list if key == 'hyperliquid' else collector._session.get.await_args_list
            self.assertEqual(len(calls), 1)

    async def test_hyperliquid_checks_identity_and_source_time(self):
        collector, payload = self.collector('hyperliquid')
        for field, value in [('coin', 'ETH'), ('time', (NOW - 60) * 1000),
                             ('time', (NOW + 10) * 1000), ('time', None)]:
            original = payload[field]
            payload[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                await collector.fetch_order_book('BTCUSDT', 10)
            payload[field] = original

    async def test_lighter_rejects_error_payload_and_uses_request_start_time(self):
        collector, payload = self.collector('lighter')
        payload['code'] = 429
        with self.assertRaisesRegex(ValueError, '429'):
            await collector.fetch_order_book('BTCUSDT', 10)
        payload['code'] = 200
        with patch('collectors.lighter.time.time', side_effect=[NOW, NOW + 3]):
            with self.assertRaisesRegex(ValueError, 'stale'):
                await collector.fetch_order_book('BTCUSDT', 10)

    async def test_crossed_books_and_invalid_limits_are_rejected(self):
        for key in ('hyperliquid', 'lighter'):
            collector, payload = self.collector(key)
            with self.assertRaises(ValueError):
                await collector.fetch_order_book('BTCUSDT', 0)
            side = payload['levels'][0] if key == 'hyperliquid' else payload['bids']
            side[0]['px' if key == 'hyperliquid' else 'price'] = '105'
            with self.assertRaisesRegex(ValueError, 'crossed'):
                await collector.fetch_order_book('BTCUSDT', 10)

    async def test_upstream_errors_propagate(self):
        for key in ('hyperliquid', 'lighter'):
            collector, _ = self.collector(key)
            await collector.fetch_markets(['BTCUSDT'])
            method = collector._session.post if key == 'hyperliquid' else collector._session.get
            method.side_effect = TimeoutError('upstream timeout')
            with self.assertRaisesRegex(TimeoutError, 'upstream timeout'):
                await collector.fetch_order_book('BTCUSDT', 10)

    async def test_lighter_scan_uses_one_bulk_snapshot_without_per_market_depth_requests(self):
        collector, _ = self.collector('lighter')
        markets = await collector.fetch_markets(['BTCUSDT'])
        market = markets['BTCUSDT']
        self.assertEqual((market.best_bid, market.best_ask), (99, 101))
        self.assertEqual(market.timestamp, NOW)
        self.assertEqual(len(self.sockets), 1)
        self.sockets[0].close.assert_called_once()
        sent = [json.loads(call.args[0]) for call in self.sockets[0].send.call_args_list]
        self.assertIn({'type': 'subscribe', 'channel': 'market_stats/all'}, sent)
        self.assertIn({'type': 'pong'}, sent)
        self.assertFalse(any('orderBookOrders' in str(call) for call in collector._session.get.await_args_list))

    async def test_lighter_snapshot_is_not_reused_when_a_market_disappears(self):
        collector, _ = self.collector('lighter')
        self.assertEqual((await collector.fetch_markets([]))['BTCUSDT'].best_bid, 99)
        self.stats['market_stats'] = {}
        with self.assertLogs('collectors.lighter', level='WARNING'):
            market = (await collector.fetch_markets([]))['BTCUSDT']
        self.assertIsNone(market.best_bid)
        self.assertIsNone(market.best_ask)

    async def test_lighter_snapshot_rejects_stale_source_and_mismatched_market(self):
        for change in ('stale', 'symbol', 'invalid_price'):
            collector, _ = self.collector('lighter')
            original = copy.deepcopy(self.stats)
            if change == 'stale':
                self.stats['timestamp'] = int((NOW - 60) * 1000)
            elif change == 'symbol':
                self.stats['market_stats']['1']['symbol'] = 'ETH'
            else:
                self.stats['market_stats']['1']['best_bid_price'] = 'nan'
            with self.subTest(change=change), self.assertRaises(ValueError):
                await collector.fetch_markets([])
            self.stats = original
        for socket in self.sockets:
            socket.close.assert_called_once()

    async def test_lighter_scan_preserves_metadata_age_while_waiting_for_quotes(self):
        collector, _ = self.collector('lighter')
        original_get = collector._session.get.side_effect
        with patch('time.time', return_value=NOW) as clock:
            async def delayed_metadata(path, params=None):
                result = await original_get(path, params)
                if path == '/api/v1/orderBookDetails':
                    clock.return_value = NOW + 10
                    self.stats['timestamp'] = int((NOW + 10) * 1000)
                return result
            collector._session.get.side_effect = delayed_metadata
            market = (await collector.fetch_markets([]))['BTCUSDT']
        self.assertEqual(market.timestamp, NOW)

    async def test_lighter_empty_top_side_does_not_become_a_tradable_book(self):
        collector, _ = self.collector('lighter')
        self.stats['market_stats']['1']['best_bid_price'] = '0'
        market = (await collector.fetch_markets([]))['BTCUSDT']
        self.assertIsNone(market.best_bid)
        self.assertIsNone(market.best_ask)

    async def test_lighter_snapshot_transport_failure_closes_connection_and_propagates(self):
        collector, _ = self.collector('lighter')
        for failure in ('', TimeoutError('snapshot timeout')):
            socket = Mock()
            if isinstance(failure, Exception):
                socket.recv.side_effect = failure
            else:
                socket.recv.return_value = failure
            with patch('websocket.create_connection', return_value=socket):
                with self.assertRaises((ConnectionError, TimeoutError)):
                    await collector.fetch_markets([])
            self.assertEqual(socket.close.call_count, 3)

    async def test_lighter_scan_reaches_funding_history_automatic_depth_and_trigger(self):
        await self._funding_scenario(expect_trigger=True)

    async def test_lighter_missing_funding_never_triggers_or_records_valid_history(self):
        self.funding_rows = []
        await self._funding_scenario(expect_trigger=False)

    async def test_lighter_missing_null_and_zero_rates_remain_distinct(self):
        for record, expected in [({}, None), ({'rate': None}, None), ({'rate': ''}, None),
                                 ({'rate': '0'}, 0), ({'rate': '-0.0008'}, -0.0001)]:
            self.funding_rows = [{'exchange': 'lighter', 'symbol': 'BTC', **record}]
            collector, _ = self.collector('lighter')
            with self.subTest(record=record):
                market = (await collector.fetch_markets(['BTCUSDT']))['BTCUSDT']
                self.assertEqual(market.funding_rate, expected)

    async def test_lighter_depth_budget_preserves_scans_under_concurrent_load(self):
        collector, _ = self.collector('lighter')
        clock_value = 1000.0
        with patch('collectors.lighter.monotonic', side_effect=lambda: clock_value):
            for cycle in range(6):
                clock_value = 1000.0 + cycle * 10
                await collector.fetch_markets(['BTCUSDT'])
                results = await asyncio.gather(
                    *(collector.fetch_order_book('BTCUSDT', 10) for _ in range(20)),
                    return_exceptions=True,
                )
                self.assertEqual(sum(isinstance(r, dict) for r in results), 6)
                self.assertTrue(all(isinstance(r, ValueError) and 'budget' in str(r)
                                    for r in results if not isinstance(r, dict)))
            self.assertEqual(collector._session.get.await_count, 48)
            clock_value = 1060.01
            await collector.fetch_markets(['BTCUSDT'])
            self.assertIsInstance(await collector.fetch_order_book('BTCUSDT', 10), dict)

    async def test_lighter_failed_requests_still_consume_shared_budget(self):
        collector, _ = self.collector('lighter')
        await collector.fetch_markets(['BTCUSDT'])
        original_get = collector._session.get.side_effect
        collector._session.get.reset_mock()
        collector._session.get.side_effect = TimeoutError('upstream timeout')
        for _ in range(6):
            with self.assertRaises(TimeoutError):
                await collector.fetch_order_book('BTCUSDT', 10)
        with self.assertRaisesRegex(ValueError, 'budget'):
            await collector.fetch_order_book('BTCUSDT', 10)
        self.assertEqual(collector._session.get.await_count, 6)
        collector._session.get.side_effect = original_get
        await collector.fetch_markets(['BTCUSDT'])

    async def test_lighter_metadata_retries_cannot_exceed_total_rolling_budget(self):
        collector, _ = self.collector('lighter')
        with patch('collectors.lighter.monotonic', return_value=2000) as clock:
            for _ in range(60):
                await collector._load_market_details()
            with self.assertRaisesRegex(ValueError, 'budget'):
                await collector.fetch_markets(['BTCUSDT'])
            with self.assertRaisesRegex(ValueError, 'budget'):
                await collector.fetch_order_book('BTCUSDT', 10)
            self.assertEqual(collector._session.get.await_count, 60)
            clock.return_value = 2060.01
            await collector.fetch_markets(['BTCUSDT'])
            self.assertEqual(collector._session.get.await_count, 62)

    async def test_lighter_scan_jitter_does_not_reset_qualified_routes(self):
        await self._scan_jitter_scenario([2 if cycle % 2 == 0 else 1 for cycle in range(18)])

    async def test_lighter_minute_boundary_jitter_does_not_reset_qualified_routes(self):
        await self._scan_jitter_scenario([2] * 6 + [1] * 18)

    async def _scan_jitter_scenario(self, offsets):
        collector, _ = self.collector('lighter')
        symbols = [f'T{i}USDT' for i in range(8)]
        cfg = Settings()
        metadata = {'code': 200, 'order_book_details': [
            {'symbol': f'T{i}', 'market_id': i, 'market_type': 'perp', 'status': 'active',
             'last_trade_price': 100, 'daily_quote_token_volume': 1000000} for i in range(8)]}
        requests = []
        now = NOW

        async def get(path, params=None):
            requests.append((now, path))
            if path == '/api/v1/orderBookDetails':
                return Response(metadata)
            if path == '/api/v1/funding-rates':
                return Response({'funding_rates': [
                    {'exchange': 'lighter', 'symbol': f'T{i}', 'rate': '0'} for i in range(8)]})
            return Response({'code': 200,
                             'bids': [{'price': '99.99', 'remaining_base_amount': '1000'}],
                             'asks': [{'price': '100', 'remaining_base_amount': '1000'}]})

        collector._session.get.side_effect = get
        self.stats['market_stats'] = {
            str(i): {'symbol': f'T{i}', 'market_id': i,
                     'best_bid_price': '99.99', 'best_ask_price': '100'} for i in range(8)}
        coverage = {f'{symbol}|lighter|binance': {'coverage': 1, 'spread_mean_bps': 0}
                    for symbol in symbols}
        reference = AsyncMock()
        with tempfile.TemporaryDirectory() as directory:
            history = StrategyHistory(str(Path(directory) / 'history.db'))
            monitor = FundingMonitor(cfg, history)
            with patch('time.time', side_effect=lambda: now), \
                    patch('collectors.lighter.monotonic', side_effect=lambda: now), \
                    patch.object(history, 'summaries', return_value=coverage):
                qualified_keys = None
                for cycle, offset in enumerate(offsets):
                    now = NOW + cycle * 10
                    self.stats['timestamp'] = int(now * 1000)
                    markets = await collector.fetch_markets(symbols)
                    now += offset
                    data = {'lighter': markets, 'binance': {
                        symbol: MarketDatum(symbol, 100, 0.003, 1000000, now,
                                            'Binance', 1, 100, 100.01) for symbol in symbols}}
                    reference.fetch_order_book.return_value = {
                        'bids': [[100, 1000]], 'asks': [[100.01, 1000]], 'timestamp': now}
                    await monitor.process(analyse_markets(symbols, data, cfg)[0], data,
                                          {'lighter': collector, 'binance': reference})
                    rows = [r for r in monitor.rows if r['buy_exchange'] == 'lighter']
                    qualified = [r for r in rows if r['reason'] is None]
                    self.assertEqual(len(qualified), 6,
                                     f'cycle {cycle}: {[r["reason"] for r in rows]}')
                    keys = {r['key'] for r in qualified}
                    if qualified_keys is None:
                        qualified_keys = keys
                    self.assertEqual(keys, qualified_keys)
                    rejected = [r for r in rows if r['key'] not in keys]
                    self.assertEqual(len(rejected), 2)
                    self.assertTrue(all(r['reason'] == 'QUOTE_FETCH_FAILED' for r in rejected))
                    self.assertTrue(all(r['qualified_seconds'] == 0 for r in rejected))
                self.assertTrue(all(row['status'] == 'triggered' for row in qualified))
                self.assertTrue(all(row['qualified_seconds'] >= 120 for row in qualified))
        # Check every rolling window, including requests near scan boundaries.
        for at, _ in requests:
            self.assertLessEqual(sum(at - 60 < previous <= at for previous, _ in requests), 60)

    async def test_lighter_new_scans_do_not_reset_the_rolling_minute_budget(self):
        collector, _ = self.collector('lighter')
        with patch('collectors.lighter.monotonic', return_value=1000):
            for _ in range(7):
                await collector.fetch_markets(['BTCUSDT'])
                await asyncio.gather(*(collector.fetch_order_book('BTCUSDT', 10) for _ in range(6)))
            await collector.fetch_markets(['BTCUSDT'])
            await asyncio.gather(*(collector.fetch_order_book('BTCUSDT', 10) for _ in range(2)))
            self.assertEqual(collector._session.get.await_count, 60)
            with self.assertRaisesRegex(ValueError, 'budget'):
                await collector.fetch_markets(['BTCUSDT'])
            with self.assertRaisesRegex(ValueError, 'budget'):
                await collector.fetch_order_book('BTCUSDT', 10)
            self.assertEqual(collector._session.get.await_count, 60)

    async def _funding_scenario(self, *, expect_trigger):
        collector, book = self.collector('lighter')
        self.stats['market_stats']['1'].update(best_bid_price='99.99', best_ask_price='100')
        book['bids'] = [{'price': '99.99', 'remaining_base_amount': '1000'}]
        book['asks'] = [{'price': '100', 'remaining_base_amount': '1000'}]
        cfg = Settings()
        cfg.strategy.history_hours = 1
        cfg.strategy.min_coverage = 0.002
        cfg.strategy.sustain_seconds = 10
        reference_collector = AsyncMock()
        notified = AsyncMock()
        with tempfile.TemporaryDirectory() as directory:
            history = StrategyHistory(str(Path(directory) / 'history.db'))
            monitor = FundingMonitor(cfg, history)
            for offset in range(0, 81, 10):
                now = NOW + offset
                with patch('time.time', return_value=now), patch('collectors.lighter.monotonic', return_value=now):
                    self.stats['timestamp'] = int(now * 1000)
                    markets = await collector.fetch_markets(['BTCUSDT'])
                    stamp_market_timestamps(markets, now)
                    reference = MarketDatum('BTCUSDT', 100, 0.001, 1000000, now,
                                            'Binance', 1, 100, 100.01)
                    reference_collector.fetch_order_book.return_value = {
                        'bids': [[100, 1000]], 'asks': [[100.01, 1000]], 'timestamp': now}
                    data = {'lighter': markets, 'binance': {'BTCUSDT': reference}}
                    opportunities = analyse_markets(['BTCUSDT'], data, cfg)[0]
                    await monitor.process(opportunities, data,
                                          {'lighter': collector, 'binance': reference_collector},
                                          on_triggered=notified)
            row = next(row for row in monitor.rows if row['buy_exchange'] == 'lighter')
            if expect_trigger:
                self.assertIsNone(row['projections']['8']['reason'])
                self.assertGreater(row['history']['coverage'], 0)
                self.assertGreater(row['depth']['capacity']['notional_usd'], 1000)
                self.assertEqual(row['status'], 'triggered')
                notified.assert_awaited()
            else:
                self.assertEqual(row['projections']['8']['reason'], 'FUNDING_UNAVAILABLE')
                self.assertEqual(row['history']['coverage'], 0)
                self.assertNotEqual(row['status'], 'triggered')
                notified.assert_not_awaited()

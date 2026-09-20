import asyncio
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from analyzer import analyse_markets
from config import Settings
from models import MarketDatum
from strategy_history import StrategyHistory
from funding_monitor import FundingMonitor

NOW = 1789871500.0


class MonitorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Settings()
        self.cfg.strategy.sustain_seconds = 10
        self.store = StrategyHistory(str(Path(self.tmp.name) / 'strategy.db'))
        self.monitor = FundingMonitor(self.cfg, self.store)
        self.data = {
            'arcus': {'BTCUSDT': MarketDatum('BTCUSDT', 100, 0, 1000000, timestamp=NOW, exchange='Arcus',
                        native_interval_hours=1, best_bid=99.99, best_ask=100, next_funding_time=(NOW+600)*1000)},
            'bulk': {'BTCUSDT': MarketDatum('BTCUSDT', 100, 0.001, 1000000, timestamp=NOW, exchange='Bulk',
                        native_interval_hours=1, best_bid=100, best_ask=100.01, next_funding_time=(NOW+600)*1000)},
        }
        self.collectors = {key: AsyncMock() for key in self.data}
        self.collectors['arcus'].fetch_order_book.return_value = {'bids': [[99.99, 1000]], 'asks': [[100, 1000]], 'timestamp': NOW}
        self.collectors['bulk'].fetch_order_book.return_value = {'bids': [[100, 1000]], 'asks': [[100.01, 1000]], 'timestamp': NOW}
        self.history = {'BTCUSDT|arcus|bulk': {'coverage': 1, 'spread_mean_bps': 0, 'funding_mean_hourly_bps': 10}}

    async def cycle(self, now=NOW):
        for market_set in self.data.values():
            market_set['BTCUSDT'].timestamp = now
        for collector in self.collectors.values():
            collector.fetch_order_book.return_value['timestamp'] = now
        with patch('time.time', return_value=now):
            opps = analyse_markets(['BTCUSDT'], self.data, self.cfg)[0]
            await self.monitor.process(opps, self.data, self.collectors)
        return opps

    async def test_qualified_funding_requires_history_depth_and_duration(self):
        with patch.object(self.store, 'summaries', return_value=self.history):
            first = await self.cycle()
            self.assertEqual(first[0].details['funding_strategy']['status'], 'pending')
            second = await self.cycle(NOW+10)
            self.assertEqual(second[0].details['funding_strategy']['status'], 'triggered')
            self.assertGreater(second[0].details['funding_strategy']['depth']['capacity']['notional_usd'], 1000)

    async def test_warmup_and_thin_depth_never_trigger(self):
        await self.cycle()
        self.assertEqual(self.monitor.rows[0]['reason'], 'HISTORY_WARMUP')
        with patch.object(self.store, 'summaries', return_value=self.history):
            self.collectors['bulk'].fetch_order_book.return_value['asks'] = [[100.01, 1]]
            await self.cycle(NOW+10)
        self.assertEqual(self.monitor.rows[0]['reason'], 'INSUFFICIENT_DEPTH')
        self.assertNotEqual(self.monitor.rows[0]['status'], 'triggered')

    async def test_expired_market_gap_restarts_qualification(self):
        with patch.object(self.store, 'summaries', return_value=self.history):
            for markets in self.data.values():
                markets['BTCUSDT'].timestamp = NOW - 29
            with patch('time.time', return_value=NOW):
                opps = analyse_markets(['BTCUSDT'], self.data, self.cfg)[0]
                await self.monitor.process(opps, self.data, self.collectors)
            self.assertEqual(self.monitor.snapshot(NOW+5)['rows'][0]['status'], 'paused')
            await self.cycle(NOW+10)
            self.assertEqual(self.monitor.rows[0]['status'], 'pending')
            self.assertEqual(self.monitor.rows[0]['qualified_seconds'], 0)
            await self.cycle(NOW+20)
            self.assertEqual(self.monitor.rows[0]['status'], 'triggered')

    async def test_cli_empty_cycle_resets_history_and_signal(self):
        from monitor import run_cycle
        self.cfg.tracked_symbols = []
        notifier = AsyncMock()
        for gap in ('empty', 'no_common_symbol'):
            with self.subTest(gap=gap), patch('monitor.update_pre_settlement_rates'), \
                    patch.object(self.store, 'summaries', return_value=self.history):
                for offset in (0, 10, 20):
                    for key, collector in self.collectors.items():
                        self.data[key]['BTCUSDT'].timestamp = NOW+offset
                        collector.fetch_order_book.return_value['timestamp'] = NOW+offset
                        collector.fetch_markets.return_value = self.data[key]
                        if offset == 10:
                            collector.fetch_markets.return_value = {} if gap == 'empty' else {
                                key+'USDT': self.data[key]['BTCUSDT']}
                    with patch('time.time', return_value=NOW+offset):
                        await run_cycle(self.cfg, self.collectors, notifier, self.monitor)
                    if offset == 10:
                        self.assertEqual(self.store.states['BTCUSDT|arcus|bulk']['status'], 'paused')
                        self.assertEqual(self.store.previous, {})
                self.assertEqual(self.monitor.rows[0]['status'], 'pending')
                self.assertEqual(self.monitor.rows[0]['qualified_seconds'], 0)
        notifier.send.assert_not_awaited()

    async def test_fast_route_notifies_before_unrelated_slow_book(self):
        self.cfg.strategy.sustain_seconds = 0
        clock = [NOW]
        release_slow = asyncio.Event()
        notified = asyncio.Event()
        delivered = []
        for markets in self.data.values():
            markets['SLOWUSDT'] = copy.deepcopy(markets['BTCUSDT'])
            markets['SLOWUSDT'].symbol = 'SLOWUSDT'
        for key, collector in self.collectors.items():
            book = copy.deepcopy(collector.fetch_order_book.return_value)
            async def fetch(symbol, limit, book=book):
                if symbol == 'SLOWUSDT':
                    await release_slow.wait()
                    raise TimeoutError('unrelated slow market')
                return book
            collector.fetch_order_book.side_effect = fetch
        async def notify(opportunities):
            for opp in opportunities:
                row = opp.details['funding_strategy']
                self.assertGreaterEqual(row['depth']['expires_at'], clock[0])
                delivered.append(opp.symbol)
            notified.set()
        history = {**self.history, 'SLOWUSDT|arcus|bulk': {'coverage': 1}}
        with patch('time.time', side_effect=lambda: clock[0]), \
                patch.object(self.store, 'summaries', return_value=history):
            opps = analyse_markets(['BTCUSDT', 'SLOWUSDT'], self.data, self.cfg)[0]
            task = asyncio.create_task(self.monitor.process(opps, self.data, self.collectors, on_triggered=notify))
            try:
                await asyncio.wait_for(notified.wait(), timeout=1)
                self.assertFalse(task.done())
                self.assertEqual(delivered, ['BTCUSDT'])
                self.assertEqual(self.monitor.snapshot(NOW)['rows'][0]['status'], 'triggered')
            finally:
                clock[0] = NOW+3
                release_slow.set()
                await task
        fast = next(row for row in self.monitor.rows if row['key'] == 'BTCUSDT|arcus|bulk')
        slow = next(row for row in self.monitor.rows if row['key'] == 'SLOWUSDT|arcus|bulk')
        self.assertEqual(fast['status'], 'triggered')
        self.assertEqual(slow['reason'], 'QUOTE_FETCH_FAILED')
        self.assertEqual(self.monitor.snapshot(NOW+3)['rows'][0]['depth']['reason'], 'QUOTE_EXPIRED')

    async def test_shared_market_book_is_fetched_once(self):
        self.cfg.strategy.sustain_seconds = 0
        self.data['binance'] = copy.deepcopy(self.data['bulk'])
        self.data['binance']['BTCUSDT'].exchange = 'Binance'
        self.collectors['binance'] = AsyncMock()
        self.collectors['binance'].fetch_order_book.return_value = copy.deepcopy(
            self.collectors['bulk'].fetch_order_book.return_value)
        history = {**self.history, 'BTCUSDT|arcus|binance': {'coverage': 1}}
        notify = AsyncMock()
        with patch('time.time', return_value=NOW), patch.object(self.store, 'summaries', return_value=history):
            opps = analyse_markets(['BTCUSDT'], self.data, self.cfg)[0]
            await self.monitor.process(opps, self.data, self.collectors, on_triggered=notify)
        for collector in self.collectors.values():
            collector.fetch_order_book.assert_awaited_once()
        self.assertEqual(notify.await_count, 2)

    async def test_cancelled_scan_drains_inflight_books(self):
        started = asyncio.Event()
        active = set()
        async def fetch(symbol, limit):
            task = asyncio.current_task()
            active.add(task)
            if len(active) == 2:
                started.set()
            try:
                await asyncio.Event().wait()
            finally:
                active.remove(task)
        for collector in self.collectors.values():
            collector.fetch_order_book.side_effect = fetch
        with patch('time.time', return_value=NOW), patch.object(self.store, 'summaries', return_value=self.history):
            opps = analyse_markets(['BTCUSDT'], self.data, self.cfg)[0]
            task = asyncio.create_task(self.monitor.process(opps, self.data, self.collectors))
            try:
                await asyncio.wait_for(started.wait(), timeout=1)
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        self.assertEqual(active, set())
        state = self.store.states['BTCUSDT|arcus|bulk']
        self.assertEqual(state['status'], 'paused')
        self.assertEqual(state['reason'], 'SCAN_INTERRUPTED')
        self.assertEqual(state['qualified_seconds'], 0)

    async def test_failed_scan_resets_counter_before_recovery(self):
        self.cfg.strategy.sustain_seconds = 120
        route = 'BTCUSDT|arcus|bulk'
        with patch.object(self.store, 'summaries', return_value=self.history):
            for offset in range(0, 101, 10):
                await self.cycle(NOW+offset)
        self.assertEqual(self.store.states[route]['qualified_seconds'], 100)
        for markets in self.data.values():
            markets['BTCUSDT'].timestamp = NOW+110
            markets['BADUSDT'] = copy.deepcopy(markets['BTCUSDT'])
            markets['BADUSDT'].symbol = 'BADUSDT'
        waiting = asyncio.Event()
        async def fetch(symbol, limit):
            if symbol == 'BADUSDT':
                await waiting.wait()
                raise KeyError('timestamp')
            waiting.set()
            await asyncio.Event().wait()
        for collector in self.collectors.values():
            collector.fetch_order_book.side_effect = fetch
        history = {**self.history, 'BADUSDT|arcus|bulk': {'coverage': 1}}
        notify = AsyncMock()
        with patch('time.time', return_value=NOW+110), patch.object(self.store, 'summaries', return_value=history):
            opps = analyse_markets(['BTCUSDT', 'BADUSDT'], self.data, self.cfg)[0]
            with self.assertRaisesRegex(KeyError, 'timestamp'):
                await self.monitor.process(opps, self.data, self.collectors, on_triggered=notify)
        state = self.store.states[route]
        self.assertEqual(state['status'], 'paused')
        self.assertFalse(state['qualifies'])
        self.assertEqual(state['qualified_seconds'], 0)
        for symbol in ('BTCUSDT', 'BADUSDT'):
            row = next(row for row in self.monitor.snapshot(NOW+110)['rows']
                       if row['key'] == f'{symbol}|arcus|bulk')
            self.assertEqual(row['reason'], 'SCAN_INTERRUPTED')
            self.assertIsNone(row['depth']['capacity'])
        self.assertEqual(self.store.events(route)[0]['status'], 'paused')
        for markets in self.data.values():
            del markets['BADUSDT']
            markets['BTCUSDT'].timestamp = NOW+120
        for collector in self.collectors.values():
            collector.fetch_order_book.side_effect = None
            collector.fetch_order_book.return_value['timestamp'] = NOW+120
        with patch('time.time', return_value=NOW+120), patch.object(self.store, 'summaries', return_value=self.history):
            await self.monitor.process(analyse_markets(['BTCUSDT'], self.data, self.cfg)[0],
                                       self.data, self.collectors, on_triggered=notify)
        self.assertEqual(self.store.states[route]['status'], 'pending')
        self.assertEqual(self.store.states[route]['qualified_seconds'], 0)
        notify.assert_not_awaited()

    async def test_failure_keeps_already_verified_route(self):
        self.cfg.strategy.sustain_seconds = 0
        verified = asyncio.Event()
        for markets in self.data.values():
            markets['BADUSDT'] = copy.deepcopy(markets['BTCUSDT'])
            markets['BADUSDT'].symbol = 'BADUSDT'
        for collector in self.collectors.values():
            book = copy.deepcopy(collector.fetch_order_book.return_value)
            async def fetch(symbol, limit, book=book):
                if symbol == 'BADUSDT':
                    await verified.wait()
                    raise KeyError('timestamp')
                return book
            collector.fetch_order_book.side_effect = fetch
        async def notify(opportunities):
            verified.set()
        history = {**self.history, 'BADUSDT|arcus|bulk': {'coverage': 1}}
        with patch('time.time', return_value=NOW), patch.object(self.store, 'summaries', return_value=history):
            opps = analyse_markets(['BTCUSDT', 'BADUSDT'], self.data, self.cfg)[0]
            with self.assertRaisesRegex(KeyError, 'timestamp'):
                await self.monitor.process(opps, self.data, self.collectors, on_triggered=notify)
        self.assertEqual(self.store.states['BTCUSDT|arcus|bulk']['status'], 'triggered')
        self.assertEqual(self.store.states['BADUSDT|arcus|bulk']['status'], 'paused')

    async def test_unexpected_book_failure_propagates_and_drains_sibling(self):
        started = asyncio.Event()
        stopped = asyncio.Event()
        async def blocked(symbol, limit):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        async def broken(symbol, limit):
            await started.wait()
            raise RuntimeError('unexpected parser failure')
        self.collectors['arcus'].fetch_order_book.side_effect = broken
        self.collectors['bulk'].fetch_order_book.side_effect = blocked
        with patch('time.time', return_value=NOW), patch.object(self.store, 'summaries', return_value=self.history):
            opps = analyse_markets(['BTCUSDT'], self.data, self.cfg)[0]
            with self.assertRaisesRegex(RuntimeError, 'unexpected parser failure'):
                await self.monitor.process(opps, self.data, self.collectors)
        self.assertTrue(stopped.is_set())

    async def test_manual_failure_drains_sibling_and_releases_slots(self):
        for attempt in range(3):
            with self.subTest(attempt=attempt):
                started, stopped = asyncio.Event(), asyncio.Event()
                error = KeyError('timestamp')
                async def blocked(symbol, limit):
                    started.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        await asyncio.sleep(0)
                        stopped.set()
                async def broken(symbol, limit):
                    await started.wait()
                    raise error
                self.collectors['arcus'].fetch_order_book.side_effect = broken
                self.collectors['bulk'].fetch_order_book.side_effect = blocked
                with patch('time.time', return_value=NOW):
                    with self.assertRaises(KeyError) as raised:
                        await self.monitor.check('BTCUSDT', 'arcus', 'bulk', 8, self.data, self.collectors)
                self.assertIs(raised.exception, error)
                self.assertTrue(stopped.is_set())
                self.assertEqual(self.monitor._requests._value, 5)
        for collector in self.collectors.values():
            collector.fetch_order_book.side_effect = None
        with patch('time.time', return_value=NOW):
            result = await asyncio.wait_for(
                self.monitor.check('BTCUSDT', 'arcus', 'bulk', 8, self.data, self.collectors), timeout=1)
        self.assertIsNone(result['reason'])

    async def test_manual_cancellation_drains_both_books(self):
        started = asyncio.Event()
        active = set()
        async def blocked(symbol, limit):
            task = asyncio.current_task()
            active.add(task)
            if len(active) == 2:
                started.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                active.remove(task)
        for collector in self.collectors.values():
            collector.fetch_order_book.side_effect = blocked
        with patch('time.time', return_value=NOW):
            task = asyncio.create_task(self.monitor.check('BTCUSDT', 'arcus', 'bulk', 8, self.data, self.collectors))
            try:
                await asyncio.wait_for(started.wait(), timeout=1)
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        self.assertEqual(active, set())
        self.assertEqual(self.monitor._requests._value, 5)

    async def test_cli_notifies_qualified_route_only_once(self):
        from monitor import run_cycle
        self.cfg.strategy.sustain_seconds = 0
        self.cfg.tracked_symbols = ['BTCUSDT']
        notifier = AsyncMock()
        for key, collector in self.collectors.items():
            collector.fetch_markets.return_value = self.data[key]
        with patch('time.time', return_value=NOW), patch('monitor.update_pre_settlement_rates'), \
                patch.object(self.store, 'summaries', return_value=self.history):
            await run_cycle(self.cfg, self.collectors, notifier, self.monitor)
        notifier.send.assert_awaited_once()
        self.assertEqual(notifier.send.await_args.args[0][0].details['funding_strategy']['status'], 'triggered')

    async def test_api_scan_notifies_qualified_route_only_once(self):
        import api
        self.cfg.strategy.sustain_seconds = 0
        notifier = AsyncMock()
        for key, collector in self.collectors.items():
            collector.fetch_markets.return_value = self.data[key]
        latest = {'raw_exchanges_data': self.data, 'exchange_status': {}, 'last_update': 'test'}
        with patch('time.time', return_value=NOW), patch.object(api, 'settings', self.cfg), \
                patch.object(api, 'funding_monitor', self.monitor), patch.object(api, 'notifier', notifier), \
                patch.object(api, 'latest_data', latest), patch.object(api, 'collectors_hub', self.collectors), \
                patch.object(api, 'update_pre_settlement_rates'), patch.object(api, 'accumulate_funding'), \
                patch.object(api, 'check_exit_signals', return_value=[]), \
                patch.object(self.store, 'summaries', return_value=self.history):
            opps = analyse_markets(['BTCUSDT'], self.data, self.cfg)[0]
            with patch.object(api, 'perform_analysis', new_callable=AsyncMock, return_value=opps), \
                    patch('api.asyncio.sleep', new_callable=AsyncMock, side_effect=asyncio.CancelledError):
                with self.assertRaises(asyncio.CancelledError):
                    await api.update_data_loop()
        notifier.send.assert_awaited_once()
        self.assertEqual(notifier.send.await_args.args[0][0].details['funding_strategy']['status'], 'triggered')

    async def test_source_stale_during_gather_is_rejected(self):
        self.collectors['arcus'].fetch_order_book.return_value['timestamp'] = NOW - 3
        with patch('time.time', return_value=NOW):
            result = await self.monitor.check('BTCUSDT', 'arcus', 'bulk', 8, self.data, self.collectors)
        self.assertEqual(result['reason'], 'STALE_QUOTE')

    async def test_unsupported_routes_do_not_consume_depth_slots(self):
        with patch('time.time', return_value=NOW):
            target = analyse_markets(['BTCUSDT'], self.data, self.cfg)[0][0]
            self.data['hyperliquid'] = self.data['bulk']
            high = copy.deepcopy(target)
            high.details['sell_exchange'] = 'Hyperliquid'
            unsupported = [copy.deepcopy(high) for _ in range(20)]
            history = {**self.history, 'BTCUSDT|arcus|hyperliquid': {'coverage': 1}}
            with patch.object(self.store, 'summaries', return_value=history):
                await self.monitor.process([*unsupported, target], self.data, self.collectors)
        self.collectors['bulk'].fetch_order_book.assert_awaited_once()
        self.assertIsNone(target.details['funding_strategy']['depth']['reason'])

    async def test_missing_routes_pause_and_reset_existing_signals(self):
        with patch.object(self.store, 'summaries', return_value=self.history):
            await self.cycle()
            await self.cycle(NOW+10)
            with patch('time.time', return_value=NOW+20):
                await self.monitor.process([], {}, self.collectors)
            self.assertEqual(self.store.states['BTCUSDT|arcus|bulk']['status'], 'paused')
            await self.cycle(NOW+25)
            self.assertEqual(self.monitor.rows[0]['qualified_seconds'], 0)

    async def test_snapshot_cannot_present_expired_capacity_as_live(self):
        with patch.object(self.store, 'summaries', return_value=self.history):
            await self.cycle()
            await self.cycle(NOW+10)
        original = copy.deepcopy(self.monitor.rows)
        result = self.monitor.snapshot(NOW+13)
        self.assertEqual(result['rows'][0]['depth']['reason'], 'QUOTE_EXPIRED')
        self.assertIsNone(result['rows'][0]['depth']['capacity'])
        self.assertEqual(self.monitor.rows, original)

    async def test_upstream_error_is_visible(self):
        self.collectors['bulk'].fetch_order_book.side_effect = TimeoutError('upstream timeout')
        with patch('time.time', return_value=NOW):
            result = await self.monitor.check('BTCUSDT', 'arcus', 'bulk', 8, self.data, self.collectors)
        self.assertEqual(result['reason'], 'QUOTE_FETCH_FAILED')

    async def test_api_exposes_strategy_and_validates_horizon(self):
        import api
        import httpx
        with patch('time.time', return_value=NOW), patch.object(api, 'funding_monitor', self.monitor, create=True), \
                patch.object(api, 'latest_data', {'raw_exchanges_data': self.data}), \
                patch.object(api, 'collectors_hub', self.collectors):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url='http://test') as client:
                response = await client.get('/api/funding')
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()['default_hours'], 8)
                body = {'symbol': 'BTCUSDT', 'long_exchange': 'arcus', 'short_exchange': 'bulk', 'hours': 1}
                result = await client.post('/api/funding/check', json=body)
                self.assertEqual(result.status_code, 200)
                self.assertIsNone(result.json()['reason'])
                body['hours'] = 3
                self.assertEqual((await client.post('/api/funding/check', json=body)).status_code, 422)
                history = await client.get('/api/funding/history', params={k:v for k,v in body.items() if k!='hours'})
                self.assertEqual(history.status_code, 200)
                self.assertEqual(history.json()['series'], [])
                body['hours'] = 8
                self.assertEqual((await client.get('/api/funding/history', params=body)).status_code, 200)

    async def test_quote_expiring_during_calculation_is_rejected(self):
        from funding_strategy import depth_scenarios
        clock = [NOW]
        def slow_depth(*args, **kwargs):
            result = depth_scenarios(*args, **kwargs)
            clock[0] += 3
            return result
        with patch('time.time', side_effect=lambda: clock[0]), \
                patch('funding_monitor.depth_scenarios', side_effect=slow_depth):
            result = await self.monitor.check('BTCUSDT', 'arcus', 'bulk', 8, self.data, self.collectors)
        self.assertEqual(result['reason'], 'STALE_QUOTE')
        self.assertIsNone(result['capacity'])

    async def test_notifier_cannot_send_unqualified_strategy(self):
        from notifier import WeChatNotifier
        from config import NotificationSettings
        opp = (await self.cycle())[0]
        opp.details['entry_check_supported'] = True
        with patch.object(WeChatNotifier, '_load_state', return_value={}), \
                patch.object(WeChatNotifier, '_save_state'), \
                patch.object(WeChatNotifier, '_post', new_callable=AsyncMock, return_value=True) as post:
            notifier = WeChatNotifier(NotificationSettings(wechat_webhook='https://example.invalid', notify_minute_offset=0))
            await notifier.send([opp])
            post.assert_not_awaited()
            opp.details['funding_strategy']['status'] = 'triggered'
            opp.details['funding_strategy']['depth']['expires_at'] = 0
            await notifier.send([opp])
            post.assert_not_awaited()
            opp.details['funding_strategy']['depth']['expires_at'] = float('inf')
            await notifier.send([opp])
            post.assert_awaited_once()
            self.assertIn('收益情景', post.await_args.args[1])

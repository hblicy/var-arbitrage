"""Position quotes must expire independently of request success and other venues."""
import asyncio
import copy
import logging
import logging.handlers
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from collectors.variational import VariationalCollector
from config import Settings
from market_state import dashboard_snapshot, replace_exchange_snapshot, stamp_market_timestamps
from models import MarketDatum


NOW = 1793170800.0


def import_api():
    with patch('logging.handlers.RotatingFileHandler', side_effect=lambda *a, **k: logging.NullHandler()):
        import api
    return api


class PositionPriceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.api = import_api()
        self.cfg = Settings()
        self.cfg.schedule.market_stale_seconds = 30
        self.position = {
            'id': 'held-q', 'symbol': 'QUSDT', 'long_exchange': 'binance',
            'short_exchange': 'variational', 'entry_qty': 1000,
            'entry_price_long': .06, 'entry_price_short': .06,
            'entry_time': '2026-10-28T00:00:00', 'cumulative_funding': 5,
            'health_status': 'healthy', 'current_price_long': .052,
            'current_price_short': .051,
        }
        self.markets = {
            'binance': {'QUSDT': MarketDatum('QUSDT', .057, 0, timestamp=NOW-2)},
            'variational': {'QUSDT': MarketDatum('QUSDT', .055, 0, timestamp=NOW-3)},
        }
        self.live = {}

    async def response(self):
        with patch.object(self.api, 'settings', self.cfg), \
                patch.object(self.api, 'live_position_prices', self.live), \
                patch.object(self.api, 'latest_data', {'raw_exchanges_data': self.markets}), \
                patch('db.get_positions', return_value=[self.position]), \
                patch('time.time', return_value=NOW):
            return (await self.api.get_positions_with_pnl(
                {'id': 'test-user', 'role': 'trader'}, all_users=False, owner=None))['held-q']

    async def test_fresh_quotes_keep_existing_mtm_formula_and_report_each_leg_age(self):
        original = copy.deepcopy(self.position)
        row = await self.response()
        self.assertAlmostEqual(row['pnl']['mtm_pnl'], 2)
        self.assertEqual(row['pnl']['total_pnl'], round(row['pnl']['net_pnl']+2, 2))
        self.assertEqual(row['price_health']['long']['age_seconds'], 2)
        self.assertEqual(row['price_health']['short']['age_seconds'], 3)
        self.assertEqual(row['price_max_age_seconds'], 30)
        self.assertTrue(row['prices_available'])
        self.assertEqual(self.position, original)

    async def test_stale_leg_cannot_produce_current_price_mtm_or_combined_pnl(self):
        self.markets['variational']['QUSDT'].timestamp = NOW-120
        row = await self.response()
        self.assertIsNone(row['current_price_short'])
        self.assertEqual(row['current_price_long'], .057)
        self.assertEqual(row['price_health']['short']['status'], 'stale')
        self.assertEqual(row['price_health']['short']['age_seconds'], 120)
        self.assertFalse(row['prices_available'])
        self.assertIsNone(row['pnl']['mtm_pnl'])
        self.assertIsNone(row['pnl']['total_pnl'])
        self.assertEqual(row['pnl']['funding_income'], 5)

    async def test_missing_market_never_reuses_persisted_price(self):
        self.markets = {}
        row = await self.response()
        self.assertIsNone(row['current_price_long'])
        self.assertIsNone(row['current_price_short'])
        self.assertEqual(row['price_health']['long']['status'], 'missing')
        self.assertIsNone(row['pnl']['total_pnl'])

    async def test_unknown_or_invalid_times_cannot_claim_freshness(self):
        for timestamp in (None, NOW+60, float('nan'), float('inf')):
            with self.subTest(timestamp=timestamp):
                self.markets['variational']['QUSDT'].timestamp = timestamp
                row = await self.response()
                self.assertEqual(row['price_health']['short']['status'], 'unknown')
                self.assertIsNone(row['current_price_short'])
                self.assertIsNone(row['pnl']['mtm_pnl'])

    async def test_invalid_prices_cannot_reach_pnl_or_json(self):
        for price in (0, -1, float('nan'), float('inf')):
            with self.subTest(price=price):
                self.markets['variational']['QUSDT'].price = price
                row = await self.response()
                self.assertEqual(row['price_health']['short']['status'], 'missing')
                self.assertIsNone(row['current_price_short'])
                self.assertIsNone(row['pnl']['mtm_pnl'])

    async def test_zero_age_limit_does_not_make_missing_time_valid(self):
        self.cfg.schedule.market_stale_seconds = 0
        self.markets['variational']['QUSDT'].timestamp = None
        row = await self.response()
        self.assertIsNone(row['pnl']['total_pnl'])
        self.assertEqual(row['price_health']['short']['status'], 'unknown')

    async def test_missing_entry_prices_do_not_fabricate_zero_mtm(self):
        self.position['entry_price_long'] = 0
        row = await self.response()
        self.assertIsNone(row['pnl']['mtm_pnl'])
        self.assertIsNone(row['pnl']['total_pnl'])

    async def test_live_price_overrides_cached_price_without_changing_funding_interval(self):
        self.markets['variational']['QUSDT'].timestamp = NOW-90
        self.markets['variational']['QUSDT'].native_interval_hours = 4
        self.markets['variational']['QUSDT'].funding_rate = .001
        self.live['QUSDT'] = MarketDatum('QUSDT', .054, None, timestamp=NOW-1,
                                         timestamp_source='exchange_ws', fetched_at=NOW)
        with patch.object(self.api, 'estimate_position_pnl', wraps=self.api.estimate_position_pnl) as estimate:
            row = await self.response()
        self.assertEqual(estimate.call_args.args[0]['current_funding_short'], .002)
        self.assertEqual(row['current_price_short'], .054)
        self.assertEqual(row['price_health']['short']['timestamp_source'], 'exchange_ws')
        self.assertEqual(row['pnl']['mtm_pnl'], 3)

    async def test_expired_stream_does_not_silently_fall_back_to_cached_price(self):
        self.live['QUSDT'] = MarketDatum('QUSDT', .054, None, timestamp=NOW-90,
                                         timestamp_source='exchange_ws', fetched_at=NOW-89)
        row = await self.response()
        self.assertEqual(row['price_health']['short']['status'], 'stale')
        self.assertIsNone(row['current_price_short'])
        self.assertIsNone(row['pnl']['total_pnl'])

    async def test_http_contract_serializes_unknown_prices_as_null_and_filters_owner(self):
        import httpx
        self.markets['variational']['QUSDT'].timestamp = float('nan')
        with patch.object(self.api, 'settings', self.cfg), \
                patch.object(self.api, 'latest_data', {'raw_exchanges_data': self.markets}), \
                patch.object(self.api, 'live_position_prices', {}), \
                patch('db.get_positions', return_value=[self.position]) as read, \
                patch('time.time', return_value=NOW), \
                patch.dict(self.api.app.dependency_overrides,
                           {self.api.get_current_user: lambda: {'id': 'test-user', 'role': 'trader'}}):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.api.app), base_url='http://test') as client:
                response = await client.get('/api/positions/pnl')
        self.assertEqual(response.status_code, 200)
        read.assert_called_once_with(owner_id='test-user', status='open')
        row = response.json()['held-q']
        self.assertIsNone(row['price_health']['short']['timestamp'])
        self.assertIsNone(row['current_price_short'])
        self.assertIsNone(row['pnl']['total_pnl'])


class VariationalPriceTimeTests(unittest.IsolatedAsyncioTestCase):
    async def fetch(self, headers):
        cfg = Settings().variational
        cfg.scrape_mode = 'api'
        collector = VariationalCollector(cfg)
        response = SimpleNamespace(headers=headers, raise_for_status=lambda: None,
            json=lambda: [{'asset': 'Q', 'price': '.055', 'funding_rate': '.1', 'funding_interval_s': 14400}])
        session = SimpleNamespace(request=AsyncMock(return_value=response))
        with patch.object(collector, '_get_session', new_callable=AsyncMock, return_value=session), \
                patch('time.time', return_value=NOW):
            return (await collector.fetch_markets(['QUSDT']))['QUSDT']

    async def test_http_cache_age_is_not_replaced_by_poll_completion_time(self):
        market = await self.fetch({'Age': '55', 'Date': 'Wed, 28 Oct 2026 07:00:00 GMT'})
        stamp_market_timestamps({'QUSDT': market}, NOW+20)
        self.assertEqual(market.timestamp, NOW-55)
        self.assertEqual(market.fetched_at, NOW)
        self.assertEqual(market.timestamp_source, 'http_cache')

    async def test_invalid_or_absent_cache_metadata_stays_unknown(self):
        for headers in ({}, {'Age': 'invalid'}, {'Age': '-5'}, {'Age': 'nan'}):
            with self.subTest(headers=headers):
                market = await self.fetch(headers)
                stamp_market_timestamps({'QUSDT': market}, NOW+20)
                self.assertIsNone(market.timestamp)
                self.assertEqual(market.timestamp_source, 'unknown')

    async def test_cached_date_can_make_a_quote_older_than_the_age_header(self):
        market = await self.fetch({'Age': '2', 'Date': 'Wed, 28 Oct 2026 06:58:00 GMT'})
        self.assertEqual(market.timestamp, NOW-120)


class IndependentPriceCollectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_analysis_monitor_and_exit_use_one_snapshot_across_notification_wait(self):
        api = import_api()
        cfg = Settings()
        cfg.tracked_symbols = ['QUSDT']
        original = {
            'binance': {'QUSDT': MarketDatum('QUSDT', 100, .001, 1000000, timestamp=NOW,
                                            exchange='Binance', best_bid=99, best_ask=101)},
            'variational': {'QUSDT': MarketDatum('QUSDT', 101, .002, 1000000, timestamp=NOW,
                                                exchange='Variational', best_bid=100, best_ask=102)},
        }
        latest = {'raw_exchanges_data': dict(original), 'exchange_status': {}, 'last_update': 'test'}

        async def replace_while_sending(_alerts):
            latest['raw_exchanges_data']['variational'] = {}

        monitor = SimpleNamespace(process=AsyncMock())
        notifier = SimpleNamespace(send=AsyncMock(side_effect=replace_while_sending))
        with patch.object(api, 'settings', cfg), patch.object(api, 'latest_data', latest), \
                patch.object(api, 'collectors_hub', {}), patch.object(api, 'funding_monitor', monitor), \
                patch.object(api, 'notifier', notifier), patch('time.time', return_value=NOW), \
                patch.object(api, 'select_binance_variational_alerts', return_value=['alert']), \
                patch.object(api, 'update_pre_settlement_rates'), patch.object(api, 'accumulate_funding'), \
                patch.object(api, 'check_exit_signals', return_value=[]) as exits, \
                patch('api.asyncio.sleep', new_callable=AsyncMock, side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):
                await api.update_data_loop()
        used = monitor.process.await_args.args[1]
        self.assertIs(used['variational'], original['variational'])
        self.assertIs(exits.call_args.args[0], used)
        self.assertIs(latest['analysis_exchanges_data'], used)
        self.assertEqual(latest['raw_exchanges_data']['variational'], {})

    def test_new_raw_prices_do_not_renew_old_dashboard_analysis(self):
        old = {'binance': {'QUSDT': MarketDatum('QUSDT', 100, 0, timestamp=NOW-41)}}
        latest = {
            'raw_exchanges_data': dict(old), 'analysis_exchanges_data': old,
            'exchange_status': {}, 'markets': {'QUSDT': {'binance': {'price': 100}}},
            'opportunities': [{'symbol': 'QUSDT', 'details': {'buy_exchange': 'binance', 'sell_exchange': 'binance'}}],
        }
        replace_exchange_snapshot(latest['raw_exchanges_data'], latest['exchange_status'], key='binance',
                                  data={'QUSDT': MarketDatum('QUSDT', 200, 0)}, fetched_at=NOW, error=None)
        packet = dashboard_snapshot(latest, market_stale_seconds=30, now=NOW)
        self.assertIsNone(packet['markets']['QUSDT']['binance'])
        self.assertEqual(packet['opportunities'], [])

    async def test_fast_exchange_keeps_publishing_while_other_exchange_is_blocked(self):
        api = import_api()
        cfg = Settings()
        cfg.schedule.interval_seconds = .02
        calls = 0
        twice = asyncio.Event()
        blocked = asyncio.Event()
        slow_cancelled = asyncio.Event()

        async def fast(_symbols):
            nonlocal calls
            calls += 1
            if calls >= 2:
                twice.set()
            return {'QUSDT': MarketDatum('QUSDT', float(calls), 0)}

        async def slow(_symbols):
            blocked.set()
            try:
                await asyncio.Event().wait()
            finally:
                slow_cancelled.set()

        latest = {'raw_exchanges_data': {}, 'exchange_status': {}, 'last_update': 'test'}
        collectors = {'binance': SimpleNamespace(fetch_markets=fast),
                      'variational': SimpleNamespace(fetch_markets=slow)}
        with patch.object(api, 'settings', cfg), patch.object(api, 'latest_data', latest), \
                patch.object(api, 'collectors_hub', collectors), \
                patch.object(api, 'perform_analysis', new_callable=AsyncMock, return_value=[]), \
                patch.object(api, 'funding_monitor', None), patch.object(api, 'notifier', AsyncMock()), \
                patch.object(api, 'update_pre_settlement_rates'), patch.object(api, 'accumulate_funding'), \
                patch.object(api, 'check_exit_signals', return_value=[]):
            task = asyncio.create_task(api.update_data_loop())
            try:
                await asyncio.wait_for(blocked.wait(), timeout=1)
                await asyncio.wait_for(twice.wait(), timeout=.5)
                self.assertGreaterEqual(latest['raw_exchanges_data']['binance']['QUSDT'].price, 2)
                market = latest['raw_exchanges_data']['binance']['QUSDT']
                self.assertIsNotNone(market.timestamp)
                self.assertLess(time.time()-market.timestamp, .5)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(slow_cancelled.is_set())


class VariationalPriceStreamTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.collector = VariationalCollector(Settings().variational)
        self.channels = {'instrument_price:P-Q-USDC-3600': 'QUSDT'}
        self.quotes = {}
        self.message = {'channel': 'instrument_price:P-Q-USDC-3600',
                        'pricing': {'price': '.05541', 'timestamp': '2026-10-28T06:59:59Z'}}

    def apply(self, message):
        with patch('time.time', return_value=NOW):
            return self.collector._apply_price_message(message, self.channels, self.quotes)

    def test_public_price_uses_exchange_time_and_ignores_repeated_old_updates(self):
        self.assertTrue(self.apply(self.message))
        quote = self.quotes['QUSDT']
        self.assertEqual(quote.timestamp, NOW-1)
        self.assertEqual(quote.timestamp_source, 'exchange_ws')
        self.assertEqual(quote.price, .05541)
        self.assertFalse(self.apply(self.message))
        old = copy.deepcopy(self.message)
        old['pricing']['timestamp'] = '2026-10-28T06:59:50Z'
        old['pricing']['price'] = '.1'
        self.assertFalse(self.apply(old))
        self.assertIs(self.quotes['QUSDT'], quote)

    async def test_scrape_fallback_does_not_replace_authoritative_stream_instruments(self):
        metadata = [{'asset': 'BTC', 'price': '100', 'funding_rate': '.1'}]
        with patch.object(self.collector, '_get_session', new_callable=AsyncMock), \
                patch.object(self.collector, '_try_api', new_callable=AsyncMock, side_effect=[metadata, None]), \
                patch.object(self.collector, '_scrape_markets', new_callable=AsyncMock,
                             return_value=[{'asset': 'BTC-PERP', 'price': '99'}]):
            await self.collector.fetch_markets(['BTCUSDT'])
            await self.collector.fetch_markets(['BTCUSDT'])
        self.assertEqual(self.collector._price_instruments['BTCUSDT']['underlying'], 'BTC')

    def test_wrong_symbol_invalid_price_and_future_time_are_rejected(self):
        for payload in ([], None, {'channel': []}, {'channel': None}):
            self.assertFalse(self.apply(payload))
        for field, value in [('channel', 'instrument_price:P-BTC-USDC-3600'),
                             ('price', 'nan'), ('price', '-1'), ('timestamp', 'invalid'),
                             ('timestamp', '2026-10-28T08:00:00Z'),
                             ('timestamp', '2026-10-28T06:59:59')]:
            message = copy.deepcopy(self.message)
            if field == 'channel':
                message[field] = value
            else:
                message['pricing'][field] = value
            self.assertFalse(self.apply(message))
        self.assertEqual(self.quotes, {})

    async def test_stream_subscribes_only_held_markets_and_closes_on_cancellation(self):
        import websocket
        self.collector._price_instruments = {
            'QUSDT': {'underlying': 'Q', 'instrument_type': 'perpetual_future',
                       'settlement_asset': 'USDC', 'funding_interval_s': 3600},
        }
        message = copy.deepcopy(self.message)
        from datetime import datetime, timezone
        message['pricing']['timestamp'] = datetime.now(timezone.utc).isoformat()

        class Socket:
            closed = False
            delivered = False

            def __init__(self):
                self.sent = []

            def send(self, value):
                self.sent.append(json.loads(value))

            def settimeout(self, _timeout):
                pass

            def recv(self):
                if not self.delivered:
                    self.delivered = True
                    return json.dumps(message)
                time.sleep(.01)
                raise websocket.WebSocketTimeoutException()

            def close(self, **_kwargs):
                self.closed = True

        for mode in ('connected', 'connect_error', 'idle'):
            with self.subTest(mode=mode):
                self.quotes.clear()
                socket = Socket()
                idle = Socket()
                idle.delivered = True
                self.collector.settings.timeout = .05 if mode == 'idle' else 10
                attempts = {'connected': [socket], 'connect_error': [websocket.WebSocketConnectionClosedException(), socket],
                            'idle': [idle, socket]}[mode]
                with patch('collectors.variational.websocket.create_connection', side_effect=attempts) as connect:
                    task = asyncio.create_task(self.collector.stream_position_prices(lambda: {'QUSDT'}, self.quotes))
                    try:
                        async def received():
                            while 'QUSDT' not in self.quotes:
                                await asyncio.sleep(.01)
                        await asyncio.wait_for(received(), timeout=2)
                    finally:
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await task
                self.assertTrue(socket.closed)
                self.assertEqual(connect.call_count, 1 if mode == 'connected' else 2)
                if mode == 'idle':
                    self.assertTrue(idle.closed)
                self.assertEqual(socket.sent[0]['action'], 'subscribe')
                self.assertEqual(len(socket.sent[0]['instruments']), 1)
                self.assertEqual(socket.sent[0]['instruments'][0]['funding_interval_s'], 3600)


if __name__ == '__main__':
    unittest.main()

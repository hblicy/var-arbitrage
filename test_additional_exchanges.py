"""Contracts for the three public integrations and their RWA calculations."""
import copy
import json
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from analyzer import analyse_markets
from collectors.factory import create_collector
from config import Settings
from funding_strategy import FundingScheduleUncertain, funding_window_active, project_funding, settlement_schedule
from models import MarketDatum


NOW = datetime(2026, 10, 23, 22, tzinfo=timezone.utc).timestamp()
HOURS = {'tz': 'America/New_York', **{
    day: [{'open': '00:00:00', 'close': '19:59:59', 'session': 'open'}]
    for day in ('monday', 'tuesday', 'wednesday', 'thursday', 'friday')
}}


def market(**changes):
    item = MarketDatum('AAPLUSDT', 100, .001, 1e7, timestamp=NOW,
                       native_interval_hours=1, best_bid=99, best_ask=101,
                       next_funding_time=(NOW + 3600) * 1000)
    for key, value in changes.items():
        setattr(item, key, value)
    return item


class RwaCalculations(unittest.TestCase):
    def setUp(self):
        clock = patch('time.time', return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)

    def test_three_exchanges_are_registered(self):
        self.assertTrue({'txflow', 'qfex', 'popdex'} <= set(Settings().exchanges))

    def test_fees_follow_the_symbol_instead_of_one_exchange_flat_fee(self):
        cfg = Settings()
        cfg.arcus.symbol_taker_bps = {'AAPLUSDT': 10}
        data = {'arcus': {'AAPLUSDT': market()}, 'risex': {'AAPLUSDT': market()}}
        opportunities = analyse_markets(['AAPLUSDT'], data, cfg)[0]
        long_arcus = next(o for o in opportunities if o.direction.startswith('arcus_long'))
        self.assertAlmostEqual(long_arcus.net_spread_bps, (99 / 101 - 1) * 10000 - 13)

    def test_native_quote_currency_cannot_pair_with_usd_even_with_an_override(self):
        cfg = Settings()
        data = {'arcus': {'AAPLUSDT': market(quote_currency='KRW')},
                'risex': {'AAPLUSDT': market(quote_currency='USD')}}
        self.assertEqual(analyse_markets(['AAPLUSDT'], data, cfg)[0], [])

    def test_non_usd_projection_does_not_report_native_amounts_as_dollars(self):
        result = project_funding(market(quote_currency='KRW'), market(quote_currency='KRW'),
                                 now=NOW, hours=8, notional=1000, buy_fee=5, sell_fee=5)
        self.assertEqual(result['reason'], 'NON_USD_QUOTE')
        self.assertIsNone(result['net_usd'])

    def test_funding_count_excludes_weekend_and_closed_hours(self):
        item = market(funding_hours=HOURS)
        # Friday 18:00 ET: two full windows until 20:00, then no weekend funding.
        self.assertEqual(settlement_schedule(item, NOW, 24)['count'], 2)

    def test_partial_session_window_cannot_be_assumed_to_pay_a_full_rate(self):
        hours = copy.deepcopy(HOURS)
        hours['friday'] = [{'open': '00:00:00', 'close': '18:29:59', 'session': 'open'}]
        result = project_funding(market(funding_hours=hours), market(), now=NOW,
                                 hours=1, notional=1000, buy_fee=5, sell_fee=5)
        self.assertEqual(result['reason'], 'FUNDING_SCHEDULE_UNCERTAIN')

    def test_raw_opportunity_daily_estimate_also_observes_market_hours(self):
        data = {'arcus': {'AAPLUSDT': market(funding_rate=0)},
                'risex': {'AAPLUSDT': market(funding_hours=HOURS)}}
        row = analyse_markets(['AAPLUSDT'], data, Settings())[0][0]
        self.assertAlmostEqual(row.details['funding_diff_24h_bps'], 20)
        self.assertIsNone(row.details['cover_hours'])

    def test_raw_opportunity_does_not_invent_a_partial_window_payment(self):
        hours = copy.deepcopy(HOURS)
        hours['friday'][0]['close'] = '18:29:59'
        data = {'arcus': {'AAPLUSDT': market(funding_rate=0)},
                'risex': {'AAPLUSDT': market(funding_hours=hours)}}
        row = analyse_markets(['AAPLUSDT'], data, Settings())[0][0]
        self.assertIsNone(row.details['projected_24h_bps'])
        self.assertEqual(row.details['funding_projection_reason'], 'FUNDING_SCHEDULE_UNCERTAIN')

    def test_session_counts_follow_daylight_saving_transitions(self):
        hours = {'tz': 'America/New_York', 'sunday': [
            {'open': '00:00:00', 'close': '03:59:59', 'session': 'open'}]}
        for date, expected in [(datetime(2026, 3, 8, 5, tzinfo=timezone.utc), 3),
                               (datetime(2026, 11, 1, 4, tzinfo=timezone.utc), 5)]:
            with self.subTest(date=date):
                now = date.timestamp()
                item = market(funding_hours=hours, next_funding_time=(now+3600)*1000)
                self.assertEqual(settlement_schedule(item, now, 8)['count'], expected)

    def test_overnight_session_uses_the_previous_days_opening(self):
        now = datetime(2026, 10, 24, 3, tzinfo=timezone.utc).timestamp()
        hours = {'tz': 'America/New_York', 'friday': [
            {'open': '20:00:00', 'close': '01:59:59', 'session': 'open'}]}
        item = market(funding_hours=hours, next_funding_time=(now+3600)*1000)
        self.assertEqual(settlement_schedule(item, now, 8)['count'], 3)

    def test_unconfirmed_schedule_stops_the_forecast(self):
        result = project_funding(market(funding_hours={}), market(), now=NOW,
                                 hours=8, notional=1000, buy_fee=0, sell_fee=0)
        self.assertEqual(result['reason'], 'FUNDING_SCHEDULE_INVALID')

    def test_native_currency_pairs_keep_basis_point_comparisons(self):
        data = {'arcus': {'AAPLUSDT': market(quote_currency='SEK')},
                'risex': {'AAPLUSDT': market(quote_currency='SEK')}}
        self.assertEqual(len(analyse_markets(['AAPLUSDT'], data, Settings())[0]), 2)

    def test_missing_rwa_rate_cannot_become_zero_in_the_daily_estimate(self):
        data = {'arcus': {'AAPLUSDT': market(funding_rate=0)},
                'risex': {'AAPLUSDT': market(funding_hours=HOURS, funding_rate=None)}}
        row = analyse_markets(['AAPLUSDT'], data, Settings())[0][0]
        self.assertIsNone(row.details['funding_diff_24h_bps'])
        self.assertEqual(row.details['funding_projection_reason'], 'FUNDING_UNAVAILABLE')
        self.assertIsNone(row.details['funding_hourly_bps'])

    def test_missing_continuous_rate_cannot_become_a_known_daily_return(self):
        data = {'txflow': {'AAPLUSDT': market(funding_rate=None)},
                'popdex': {'AAPLUSDT': market()}}
        row = analyse_markets(['AAPLUSDT'], data, Settings())[0][0]
        self.assertIsNone(row.details['projected_24h_bps'])
        self.assertEqual(row.details['funding_projection_reason'], 'FUNDING_UNAVAILABLE')

    def test_reopening_rate_is_unknown_even_when_forecast_starts_while_open(self):
        now = datetime(2026, 10, 26, 22, tzinfo=timezone.utc).timestamp()
        item = market(funding_hours=HOURS, next_funding_time=(now+3600)*1000)
        result = project_funding(item, market(), now=now, hours=24, notional=1000, buy_fee=0, sell_fee=0)
        self.assertEqual(result['reason'], 'FUNDING_SCHEDULE_UNCERTAIN')

    def test_ledger_window_rejects_partial_or_unknown_sessions(self):
        hours = copy.deepcopy(HOURS)
        hours['friday'][0]['close'] = '18:29:59'
        with self.assertRaises(FundingScheduleUncertain):
            funding_window_active(market(funding_hours=hours), NOW+3600)
        with self.assertRaisesRegex(ValueError, 'Missing funding market hours'):
            funding_window_active(market(funding_hours={}), NOW+3600)

    def test_restart_catchup_does_not_charge_closed_rwa_windows(self):
        import position_tracker as tracker
        class Frozen(datetime):
            @classmethod
            def now(cls, tz=None):
                value = datetime(2026, 10, 26, 6, 1, tzinfo=timezone.utc)
                return value.astimezone(tz) if tz else value.astimezone().replace(tzinfo=None)
        entry = datetime(2026, 10, 24, tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
        item = market(funding_hours=HOURS, next_funding_time=datetime(
            2026, 10, 26, 7, tzinfo=timezone.utc).timestamp()*1000)
        pos = {'symbol': 'AAPLUSDT', 'long_exchange': 'qfex', 'short_exchange': 'risex',
               'entry_qty': 10, 'entry_price_long': 100, 'entry_time': entry.isoformat(),
               'last_settlement_long': entry.isoformat(), 'cumulative_funding': 0, 'funding_history': []}
        with patch.object(tracker, 'datetime', Frozen), \
                patch.object(tracker, 'get_all_positions', return_value={'p': pos}), \
                patch.object(tracker, 'update_position'):
            count = tracker.accumulate_funding({'qfex': {'AAPLUSDT': item}})
        self.assertEqual(count, 2)
        self.assertEqual(pos['cumulative_funding'], -2)

    def test_exit_alerts_use_rwa_settlement_counts_for_both_entry_points(self):
        import position_tracker as tracker
        class Frozen(datetime):
            @classmethod
            def now(cls, tz=None):
                value = datetime.fromtimestamp(NOW, timezone.utc)
                return value.astimezone(tz) if tz else value.astimezone().replace(tzinfo=None)
        data = {'qfex': {'AAPLUSDT': market(funding_hours=HOURS, funding_rate=.002)},
                'risex': {'AAPLUSDT': market(funding_rate=.0012)}}
        pos = {'id': 'p', 'symbol': 'AAPLUSDT', 'long_exchange': 'qfex', 'short_exchange': 'risex'}
        cfg = Settings()
        cfg.thresholds.funding_reversal_threshold_minutes = 0
        with patch.object(tracker, 'datetime', Frozen), \
                patch.object(tracker, 'get_all_positions', return_value={'p': pos}), \
                patch.object(tracker, 'get_positions_for_owner', return_value=[pos]), \
                patch.object(tracker, 'update_position_health'), patch.dict(tracker._unfavorable_since, {}, clear=True):
            self.assertEqual(tracker.check_exit_signals(data, cfg), [])
            self.assertEqual(tracker.check_exit_signals_for_owner(data, 'test', cfg), [])
            data['qfex']['AAPLUSDT'].funding_hours = {}
            for check in (lambda: tracker.check_exit_signals(data, cfg),
                          lambda: tracker.check_exit_signals_for_owner(data, 'test', cfg)):
                tracker._unfavorable_since['p'] = Frozen.now()
                self.assertEqual(check(), [])
                self.assertNotIn('p', tracker._unfavorable_since)


class Response:
    def __init__(self, payload):
        self.status_code = 200
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return copy.deepcopy(self.payload)


class AdditionalCollectors(unittest.IsolatedAsyncioTestCase):
    async def test_bulk_is_absent_from_the_exchange_api_and_new_positions(self):
        import api
        from fastapi import HTTPException
        self.assertNotIn('bulk', {row['name'] for row in await api.get_exchanges()})
        for leg in ('long_exchange', 'short_exchange'):
            pair = {'long_exchange': 'arcus', 'short_exchange': 'binance', leg: 'bulk'}
            pos = api.PositionCreate(symbol='BTCUSDT', **pair)
            with self.subTest(leg=leg), patch.object(api, 'add_position') as add:
                with self.assertRaises(HTTPException) as caught:
                    await api.create_position(pos, {'id': 'test-user', 'username': 'test'})
                self.assertEqual(caught.exception.status_code, 400)
                add.assert_not_called()

    async def test_existing_bulk_position_records_remain_readable(self):
        import api
        legacy = {'symbol': 'BTCUSDT', 'long_exchange': 'bulk', 'short_exchange': 'binance'}
        with patch('db.get_positions', return_value=[legacy]) as read:
            rows = await api.get_positions({'id': 'test-user', 'role': 'trader'},
                all_users=False, owner=None, status_filter='open')
        self.assertEqual(rows, [legacy])
        read.assert_called_once_with(owner_id='test-user', status='open')

    async def test_existing_bulk_position_fee_estimates_keep_the_original_rate(self):
        import os
        import api
        cfg = Settings()
        cfg.binance.taker_bps = 5
        for override, bulk_fee in ((None, 3.5), ('1', 1), ('0', 0), ('invalid', 3.5)):
            for bulk_leg in ('long', 'short'):
                long_ex, short_ex = ('bulk', 'binance') if bulk_leg == 'long' else ('binance', 'bulk')
                pos = {'id': 'legacy', 'symbol': 'BTCUSDT', 'long_exchange': long_ex,
                       'short_exchange': short_ex, 'entry_qty': 100, 'entry_price_long': 100,
                       'entry_price_short': 100, 'entry_time': '2026-10-24T00:00:00', 'cumulative_funding': 18}
                original = dict(pos)
                with self.subTest(override=override, bulk_leg=bulk_leg), patch.dict(os.environ), \
                        patch.object(api, 'settings', cfg), \
                        patch.dict(api.latest_data, {'raw_exchanges_data': {}}), \
                        patch('db.get_positions', return_value=[pos]):
                    if override is None:
                        os.environ.pop('BULK_TAKER_FEE_BPS', None)
                    else:
                        os.environ['BULK_TAKER_FEE_BPS'] = override
                    rows = await api.get_positions_with_pnl({'id': 'test-user', 'role': 'trader'},
                        all_users=False, owner=None)
                    expected_fees = 2 * (bulk_fee + 5)
                    self.assertEqual(rows['legacy']['pnl']['total_fees'], expected_fees)
                    self.assertEqual(rows['legacy']['pnl']['net_pnl'], 18-expected_fees)
                    self.assertEqual(pos, original)
                    self.assertNotIn('bulk', set(cfg.exchanges))

    def setUp(self):
        self.clock = patch('time.time', return_value=NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.retry = patch('collectors.decorators.asyncio.sleep', new_callable=AsyncMock)
        self.retry.start()
        self.addCleanup(self.retry.stop)

    def collector(self, key):
        cfg = Settings()
        self.assertIn(key, cfg.exchanges)
        with patch('collectors.session_pool.session_pool.get_session'):
            collector = create_collector(key, cfg.exchanges[key])
        return collector

    async def test_popdex_loads_every_page_and_uses_quote_turnover(self):
        collector = self.collector('popdex')
        specs = [{'symbolId': str(i), 'symbol': symbol, 'baseToken': base,
                  'quoteToken': 'USDT', 'category': 'Futures', 'status': 'Trading',
                  'fundingInterval': '1', 'assetClass': 'Crypto'}
                 for i, symbol, base in [(1, 'BTCUSDT', 'BTC'), (2, 'ETHUSDT', 'ETH')]]
        rows = [{'symbol': spec['symbol'], 'symbolId': spec['symbolId'], 'lastPrice': '100',
                 'fundingRate': '0', 'turnover24h': '1000000', 'volume24h': '10',
                 'bid1Price': '99', 'ask1Price': '101', 'bid1Size': '1', 'ask1Size': '1',
                 'updatedTime': str(int(NOW * 1000)),
                 'status': 'Trading'} for spec in specs]

        async def get(path, params=None):
            if path == '/api/v1/config/symbols':
                return Response({'code': '200', 'data': specs})
            if path == '/api/v1/market/funding-rate':
                return Response({'code': '200', 'data': [
                    {'symbol': row['symbol'], 'fundingRate': '0', 'fundingRateInterval': '1',
                     'nextUpdate': str(int((NOW+1800)*1000))} for row in specs],
                    'total': '2', 'cursor': '2', 'updatedTime': NOW*1000})
            self.assertEqual(path, '/api/v1/public/market/tickers')
            second = params.get('cursor') == '1'
            return Response({'code': '200', 'data': [rows[int(second)]],
                             'cursor': '2' if second else '1', 'total': '2', 'limit': '1'})

        collector._client = AsyncMock()
        collector._client.get.side_effect = get
        result = await collector.fetch_markets([])
        self.assertEqual(set(result), {'BTCUSDT', 'ETHUSDT'})
        self.assertEqual(result['BTCUSDT'].funding_rate, 0)
        self.assertEqual(result['BTCUSDT'].volume_24h, 1000000)
        self.assertEqual(result['BTCUSDT'].native_interval_hours, 1)
        self.assertEqual(result['BTCUSDT'].next_funding_time_source, 'exchange')
        self.assertEqual(result['BTCUSDT'].next_funding_time, (NOW+1800)*1000)
        self.assertEqual(result['BTCUSDT'].best_bid, 99)
        self.assertEqual(result['BTCUSDT'].best_ask, 101)

    async def test_popdex_zero_or_missing_top_sizes_are_not_executable_quotes(self):
        collector = self.collector('popdex')
        collector._symbols = {'BTCUSDT': {'symbol': 'BTCUSDT', 'quoteToken': 'USDT', 'fundingInterval': '1'}}
        row = {'symbol': 'BTCUSDT', 'status': 'Trading', 'lastPrice': '100', 'updatedTime': NOW*1000,
               'bid1Price': '99', 'ask1Price': '101', 'bid1Size': '0'}
        async def pages(path, params):
            return ([row], NOW) if 'tickers' in path else ([], NOW)
        with patch.object(collector, '_load_markets', new_callable=AsyncMock), \
                patch.object(collector, '_get_pages', side_effect=pages):
            result = await collector.fetch_markets([])
        self.assertIsNone(result['BTCUSDT'].best_bid)
        self.assertIsNone(result['BTCUSDT'].best_ask)

    async def test_popdex_business_error_is_not_an_empty_success(self):
        collector = self.collector('popdex')
        collector._client = AsyncMock()
        collector._client.get.return_value = Response({'code': '50002', 'msg': 'Parameter verification failed'})
        with self.assertRaisesRegex(ValueError, '50002'):
            await collector.fetch_markets([])

    async def test_popdex_book_uses_top_level_source_timestamp(self):
        collector = self.collector('popdex')
        collector._symbols = {'BTCUSDT': {'symbol': 'BTCUSDT', 'quoteToken': 'USDT'}}
        collector._client = AsyncMock()
        collector._client.get.return_value = Response({'code': '200', 'updatedTime': str(int(NOW * 1000)),
            'data': {'bids': [['99', '0'], ['98', '2']], 'asks': [['101', '3']]}})
        book = await collector.fetch_order_book('BTCUSDT', 50)
        self.assertEqual(book['bids'], [[98, 2]])
        self.assertEqual(book['timestamp'], NOW)
        collector._client.get.return_value.payload['updatedTime'] = str(int((NOW - 10) * 1000))
        with self.assertRaisesRegex(ValueError, 'stale'):
            await collector.fetch_order_book('BTCUSDT', 50)

    async def test_qfex_snapshot_preserves_local_quote_and_product_fees(self):
        collector = self.collector('qfex')
        metadata = [{'symbol': native, 'base_asset': base, 'quote_asset': quote,
                     'status': 'ACTIVE', 'product_category': category, 'market_hours': HOURS}
                    for native, base, quote, category in [('AAPL-USD', 'AAPL', 'USD', 'EQUITY'),
                        ('GOLD-USD', 'GOLD', 'USD', 'COMMODITY'), ('SIVE-SEK', 'SIVE', 'SEK', 'EQUITY')]]
        contracts = [{'ticker_id': row['symbol'], 'base_currency': row['base_asset'],
                      'target_currency': row['quote_asset'], 'last_price': '100', 'target_volume': '1000000',
                      'product_type': 'Perpetual', 'funding_rate': '.99', 'next_funding_rate': '0',
                      'funding_interval_minutes': 60, 'next_funding_rate_timestamp': NOW+3600}
                     for row in metadata]
        async def get(path, params=None):
            return Response({'data': metadata if path == '/refdata' else contracts})
        collector._client = AsyncMock()
        collector._client.get.side_effect = get
        iso = datetime.fromtimestamp(NOW, timezone.utc).isoformat()
        snapshot = {'type': 'market_stats', 'time': iso,
                    'bbos': [{'symbol': row['symbol'], 'time': iso, 'bid': [['99', '2']],
                              'ask': [['101', '3']]} for row in metadata]}
        with patch.object(collector, '_fetch_snapshot', return_value=snapshot):
            result = await collector.fetch_markets([])
        self.assertEqual(set(result), {'AAPLUSDT', 'XAUUSDT', 'SIVESEK'})
        self.assertEqual(result['SIVESEK'].quote_currency, 'SEK')
        self.assertIsNone(result['SIVESEK'].volume_24h)
        self.assertEqual(result['AAPLUSDT'].funding_rate, 0)
        self.assertEqual(result['AAPLUSDT'].funding_hours, HOURS)
        self.assertEqual(result['AAPLUSDT'].next_funding_time, (NOW+3600)*1000)
        self.assertEqual(collector.settings.symbol_taker_bps['AAPLUSDT'], 10)
        self.assertEqual(collector.settings.symbol_taker_bps['XAUUSDT'], 5)

    async def test_txflow_percent_funding_and_numeric_coin_do_not_use_list_positions(self):
        collector = self.collector('txflow')
        metadata = {'universe': [{'index': 81, 'name': 'AAPL-USDC', 'baseCurrency': 'AAPL',
                                  'quoteCurrency': 'USDC', 'delisted': False, 'haltTrading': False},
                                 {'index': 1, 'name': 'BTC-USDC', 'baseCurrency': 'BTC',
                                  'quoteCurrency': 'USDC', 'delisted': False, 'haltTrading': False}]}
        stats = {'assetCtxs': [{'instrumentId': 1, 'lastPrice': '100', 'dayNtlVlm': '1000000'},
                              {'instrumentId': 81, 'lastPrice': '200', 'dayNtlVlm': '2000000'}]}
        async def post(path, json):
            if json['type'] == 'l2Book':
                native = next(row['name'] for row in metadata['universe'] if str(row['index']) == json['coin'])
                return Response({'success': True, 'code': 0, 'data': {'coin': native, 'time': NOW*1000,
                    'levels': [[{'px': '99', 'sz': '2'}], [{'px': '101', 'sz': '3'}]]}})
            return Response({'success': True, 'code': 0,
                             'data': metadata if json['type'] == 'perpMeta' else stats})
        collector._client = AsyncMock()
        collector._client.post.side_effect = post
        contexts = [{'coin': str(index), 'ctx': {'funding': '-0.00125', 'markPx': '100',
                     'nextFundingTime': (NOW+3600)*1000, 'prevFundingTime': NOW*1000,
                     'blockTime': NOW*1000}} for index in (1, 81)]
        with patch.object(collector, '_fetch_snapshot', return_value=contexts):
            result = await collector.fetch_markets([])
        self.assertEqual(result['AAPLUSDT'].price, 200)
        self.assertAlmostEqual(result['BTCUSDT'].funding_rate, -.0000125)
        self.assertEqual(result['BTCUSDT'].native_interval_hours, 1)
        self.assertEqual(result['BTCUSDT'].best_bid, 99)
        self.assertEqual(result['BTCUSDT'].next_funding_time_source, 'exchange')

    async def test_manual_entry_never_labels_a_native_currency_book_as_usd(self):
        import api
        book = {'bids': [[99, 20]], 'asks': [[101, 20]], 'timestamp': NOW, 'quote_currency': 'SEK'}
        collectors = {key: AsyncMock() for key in ('arcus', 'risex')}
        for collector in collectors.values():
            collector.fetch_order_book.return_value = book
        with patch.object(api, 'collectors_hub', collectors):
            result = await api.evaluate_manual_entry('SIVESEK', 'arcus', 'risex')
        self.assertEqual(result['reason'], 'NON_USD_QUOTE')

    async def test_native_currency_position_cannot_enter_the_usd_pnl_ledger(self):
        import api
        from fastapi import HTTPException
        data = {'qfex': {'SIVESEK': market(quote_currency='SEK')}}
        pos = api.PositionCreate(symbol='SIVESEK', long_exchange='qfex', short_exchange='risex')
        with patch.dict(api.latest_data, {'raw_exchanges_data': data}), patch.object(api, 'add_position') as add:
            with self.assertRaises(HTTPException) as caught:
                await api.create_position(pos, {'id': 'test-user', 'username': 'test'})
        self.assertEqual(caught.exception.detail, 'NON_USD_POSITION_UNSUPPORTED')
        add.assert_not_called()

    async def test_native_currency_position_is_rejected_without_live_data(self):
        import api
        from fastapi import HTTPException
        pos = api.PositionCreate(symbol='SIVESEK', long_exchange='qfex', short_exchange='popdex')
        for data in ({}, {'qfex': {}}):
            with self.subTest(data=data), patch.dict(api.latest_data, {'raw_exchanges_data': data}), \
                    patch.object(api, 'add_position') as add:
                with self.assertRaises(HTTPException) as caught:
                    await api.create_position(pos, {'id': 'test-user', 'username': 'test'})
                self.assertEqual(caught.exception.detail, 'NON_USD_POSITION_UNSUPPORTED')
                add.assert_not_called()

    async def test_usd_qfex_position_remains_available_without_live_data(self):
        import api
        pos = api.PositionCreate(symbol='AAPLUSDT', long_exchange='qfex', short_exchange='popdex')
        with patch.dict(api.latest_data, {'raw_exchanges_data': {}}), patch.object(api, 'add_position') as add:
            add.return_value = {'id': 'test-position'}
            result = await api.create_position(pos, {'id': 'test-user', 'username': 'test'})
        self.assertEqual(result['status'], 'success')
        add.assert_called_once()

    async def test_dashboard_response_keeps_the_native_quote_currency(self):
        import api
        import httpx
        data = {'qfex': {'SIVESEK': market(symbol='SIVESEK', quote_currency='SEK', exchange='QFEX')}}
        with patch.dict(api.latest_data, {'raw_exchanges_data': data}), patch.object(api, 'collectors_hub', {}):
            await api.perform_analysis()
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url='http://test') as client:
                response = await client.get('/api/data')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['markets']['SIVESEK']['qfex'].get('quote_currency'), 'SEK')

    async def test_txflow_large_universe_does_not_stop_after_one_hundred_quotes(self):
        collector = self.collector('txflow')
        collector._symbols = {f'TEST{i}USDT': {'index': i, 'name': f'TEST{i}-USDC',
                              'baseCurrency': f'TEST{i}', 'quoteCurrency': 'USDC'} for i in range(236)}
        stats = {'assetCtxs': [{'instrumentId': i, 'lastPrice': '100', 'dayNtlVlm': '10000000'} for i in range(236)]}
        contexts = [{'coin': str(i), 'ctx': {'funding': '0', 'blockTime': NOW*1000,
                     'nextFundingTime': (NOW+3600)*1000, 'prevFundingTime': NOW*1000}} for i in range(236)]
        async def info(kind, **fields):
            if kind == 'webData2':
                return stats
            self.assertEqual(kind, 'l2Book')
            return {'coin': f"TEST{fields['coin']}-USDC", 'time': NOW*1000,
                    'levels': [[{'px': '99', 'sz': '2'}], [{'px': '101', 'sz': '3'}]]}
        with patch.object(collector, '_load_markets', new_callable=AsyncMock), \
                patch.object(collector, '_fetch_snapshot', return_value=contexts), \
                patch.object(collector, '_info', side_effect=info):
            rows = await collector.fetch_markets([])
        self.assertEqual(len(rows), 236)
        self.assertEqual(sum(bool(row.best_bid and row.best_ask) for row in rows.values()), 236)

    async def test_txflow_l2_display_name_is_valid_but_another_market_is_rejected(self):
        collector = self.collector('txflow')
        collector._symbols = {'BTCUSDT': {'index': 1, 'name': 'BTC-USDC', 'baseCurrency': 'BTC', 'quoteCurrency': 'USDC'}}
        collector._client = AsyncMock()
        payload = {'success': True, 'code': 0, 'data': {'coin': 'BTC-USDC', 'time': NOW*1000,
            'levels': [[{'px': '99', 'sz': '2'}], [{'px': '101', 'sz': '3'}]]}}
        collector._client.post.return_value = Response(payload)
        book = await collector.fetch_order_book('BTCUSDT', 50)
        self.assertEqual(book['bids'], [[99, 2]])
        self.assertEqual(collector._client.post.await_args.kwargs['json']['coin'], '1')
        collector._client.post.return_value.payload['data']['coin'] = 'ETH-USDC'
        with self.assertRaisesRegex(ValueError, 'mismatched'):
            await collector.fetch_order_book('BTCUSDT', 50)


if __name__ == '__main__':
    unittest.main()

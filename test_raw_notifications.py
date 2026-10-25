import asyncio
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from config import NotificationSettings, Settings, ThresholdSettings
from funding_monitor import FundingMonitor
from models import ArbitrageOpportunity, MarketDatum
from notifier import WeChatNotifier, select_binance_variational_alerts
from strategy_history import StrategyHistory


NOW = 1792888200.0


class RawNotificationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Settings()
        self.cfg.tracked_symbols = ['TUSDT']
        self.cfg.thresholds.notify_min_spread_bps = 100
        self.cfg.thresholds.notify_min_funding_bps = 50
        self.cfg.thresholds.notify_min_total_bps = 100
        self.cfg.thresholds.notify_max_cover_hours = 24
        self.cfg.notifications = NotificationSettings(
            wechat_webhook='https://example.invalid/mock',
            notification_exchanges=['binance', 'variational'],
            notify_minute_offset=0, cooldown_seconds=1800,
        )
        self.data = {
            'variational': {'TUSDT': MarketDatum(
                'TUSDT', 100, -.02, 421000, timestamp=NOW, exchange='Variational',
                native_interval_hours=1, next_funding_time=(NOW+600)*1000)},
            'binance': {'TUSDT': MarketDatum(
                'TUSDT', 96.42, -.006384, 180400000, timestamp=NOW, exchange='Binance',
                native_interval_hours=1, best_bid=96.42, best_ask=96.43,
                next_funding_time=(NOW+600)*1000)},
        }
        self.collectors = {
            key: SimpleNamespace(fetch_markets=AsyncMock(return_value=data))
            for key, data in self.data.items()
        }
        self.collectors['binance'].fetch_order_book = AsyncMock()
        self.store = StrategyHistory(str(Path(self.tmp.name) / 'strategy.db'))
        self.monitor = FundingMonitor(self.cfg, self.store)
        self.enterContext(patch.object(WeChatNotifier, '_load_state', return_value={}))
        self.enterContext(patch.object(WeChatNotifier, '_save_state'))
        self.enterContext(patch('time.time', return_value=NOW))
        self.notifier = WeChatNotifier(self.cfg.notifications)
        self.notifier._post = AsyncMock(return_value=True)

    async def api_cycle(self):
        import api

        latest = {'raw_exchanges_data': {}, 'exchange_status': {}, 'last_update': 'test'}
        with patch.object(api, 'settings', self.cfg), \
                patch.object(api, 'funding_monitor', self.monitor), \
                patch.object(api, 'notifier', self.notifier), \
                patch.object(api, 'latest_data', latest), \
                patch.object(api, 'collectors_hub', self.collectors), \
                patch.object(api, 'bump_data_version'), \
                patch.object(api, 'update_pre_settlement_rates'), \
                patch.object(api, 'accumulate_funding'), \
                patch.object(api, 'check_exit_signals', return_value=[]), \
                patch('api.asyncio.sleep', new_callable=AsyncMock, side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):
                await api.update_data_loop()

    def assert_raw_alert(self):
        self.notifier._post.assert_awaited_once()
        content = self.notifier._post.await_args.args[1]
        self.assertIn('TUSDT', content)
        self.assertIn('Variational 做多 / Binance 做空', content)
        self.assertIn('原始机会', content)
        self.assertIn('未经深度核验', content)
        self.assertNotIn('可人工开仓', content)
        self.assertTrue(all(row['status'] != 'triggered' for row in self.monitor.rows))
        self.collectors['binance'].fetch_order_book.assert_not_awaited()

    async def test_api_restores_raw_alert_during_strategy_warmup(self):
        await self.api_cycle()
        self.assert_raw_alert()

    async def test_cli_restores_raw_alert_with_funding_monitor_enabled(self):
        from monitor import run_cycle

        with patch('monitor.update_pre_settlement_rates'):
            await run_cycle(self.cfg, self.collectors, self.notifier, self.monitor)
        self.assert_raw_alert()

    async def test_raw_alert_keeps_existing_cooldown(self):
        await self.api_cycle()
        await self.api_cycle()
        self.assert_raw_alert()
        self.assertEqual(self.notifier._last_sent['TUSDT_variational_long_binance_short'], NOW)
        with patch('time.time', return_value=NOW+1800):
            for markets in self.data.values():
                markets['TUSDT'].timestamp = NOW+1800
            await self.api_cycle()
        self.assertEqual(self.notifier._post.await_count, 2)

    async def test_whitelist_requires_both_exchanges(self):
        self.cfg.notifications.notification_exchanges = ['binance']
        await self.api_cycle()
        self.notifier._post.assert_not_awaited()

    async def test_empty_whitelist_allows_raw_alert(self):
        self.cfg.notifications.notification_exchanges = []
        await self.api_cycle()
        self.assert_raw_alert()

    async def test_raw_alert_keeps_notification_time_window(self):
        self.cfg.notifications.notify_minute_offset = 10
        with patch('notifier.datetime') as clock:
            clock.now.return_value = datetime(2026, 10, 25, 12, 5)
            await self.api_cycle()
        self.notifier._post.assert_not_awaited()

    async def test_failed_raw_alert_does_not_start_cooldown(self):
        self.notifier._post.side_effect = [False, True]
        await self.api_cycle()
        self.assertEqual(self.notifier._last_sent, {})
        await self.api_cycle()
        self.assertEqual(self.notifier._post.await_count, 2)
        self.assertEqual(self.notifier._last_sent['TUSDT_variational_long_binance_short'], NOW)

    async def test_api_applies_original_threshold_configuration(self):
        for setting, value in (
            ('notify_min_funding_bps', 10000),
            ('notify_min_total_bps', 10000),
            ('notify_max_cover_hours', 1),
        ):
            with self.subTest(setting=setting), patch.object(self.cfg.thresholds, setting, value):
                await self.api_cycle()
                self.notifier._post.assert_not_awaited()

    async def test_stale_market_does_not_send_raw_alert(self):
        self.data['variational']['TUSDT'].timestamp = NOW-31
        await self.api_cycle()
        self.notifier._post.assert_not_awaited()


class RawThresholdTests(unittest.TestCase):
    def setUp(self):
        self.thresholds = ThresholdSettings(
            notify_min_spread_bps=100, notify_min_funding_bps=50,
            notify_min_total_bps=100, notify_max_cover_hours=24,
        )

    def opportunity(self, spread=-358, **details):
        return ArbitrageOpportunity(
            symbol='TUSDT', direction='variational_long_binance_short',
            entry_exchange='variational', exit_exchange='binance',
            gross_spread_bps=spread, net_spread_bps=spread, funding_diff=.013616,
            recommendation='WATCH', details={
                'buy_exchange': 'Variational', 'sell_exchange': 'Binance',
                'funding_daily_bps': 3267.84, 'projected_24h_bps': 2909.84,
                'cover_hours': 2.6, 'native_intervals': {'buy': 1, 'sell': 1},
                **details,
            },
        )

    def test_same_rules_support_both_exchange_directions(self):
        for buy, sell in (('Variational', 'Binance'), ('Binance', 'Variational')):
            with self.subTest(buy=buy):
                opp = self.opportunity(buy_exchange=buy, sell_exchange=sell)
                self.assertEqual(select_binance_variational_alerts([opp], self.thresholds), [opp])

    def test_aligned_thresholds_include_exact_boundary(self):
        opp = self.opportunity(spread=100, funding_daily_bps=50, projected_24h_bps=150)
        self.assertEqual(select_binance_variational_alerts([opp], self.thresholds), [opp])
        for field in ('notify_min_spread_bps', 'notify_min_funding_bps', 'notify_min_total_bps'):
            with self.subTest(field=field), patch.object(self.thresholds, field, {
                'notify_min_spread_bps': 100.01,
                'notify_min_funding_bps': 50.01,
                'notify_min_total_bps': 150.01,
            }[field]):
                self.assertEqual(select_binance_variational_alerts([opp], self.thresholds), [])

    def test_funding_cover_thresholds_include_exact_boundary(self):
        opp = self.opportunity(spread=-100, funding_daily_bps=200, projected_24h_bps=100, cover_hours=24)
        self.assertEqual(select_binance_variational_alerts([opp], self.thresholds), [opp])
        for change in (
            {'cover_hours': 24.01}, {'cover_hours': None},
            {'funding_daily_bps': 49.99}, {'projected_24h_bps': 99.99},
        ):
            with self.subTest(change=change):
                candidate = self.opportunity(**change)
                self.assertEqual(select_binance_variational_alerts([candidate], self.thresholds), [])

    def test_small_positive_spread_does_not_use_funding_cover_rule(self):
        opp = self.opportunity(spread=99.99)
        self.assertEqual(select_binance_variational_alerts([opp], self.thresholds), [])

    def test_settlement_direction_filter_is_preserved(self):
        for intervals, expected in (({'buy': 4, 'sell': 1}, False), ({'buy': 1, 'sell': 4}, True)):
            with self.subTest(intervals=intervals):
                opp = self.opportunity(native_intervals=intervals)
                self.assertEqual(bool(select_binance_variational_alerts([opp], self.thresholds)), expected)

    def test_missing_funding_or_uncertain_projection_does_not_send(self):
        missing_funding = self.opportunity()
        missing_funding.funding_diff = None
        candidates = [missing_funding, self.opportunity(funding_daily_bps=None),
                      self.opportunity(projected_24h_bps=None)]
        self.assertEqual(select_binance_variational_alerts(candidates, self.thresholds), [])

    def test_other_exchange_pairs_do_not_enter_raw_notification_path(self):
        for buy, sell in (('Variational', 'Aster'), ('Binance', 'TxFlow')):
            with self.subTest(buy=buy, sell=sell):
                opp = self.opportunity(buy_exchange=buy, sell_exchange=sell)
                self.assertEqual(select_binance_variational_alerts([opp], self.thresholds), [])

import tempfile
import unittest
from pathlib import Path

from strategy_history import StrategyHistory


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / 'history.db')
        self.store = StrategyHistory(self.path)

    def record(self, now, spread=10, funding=2, route='BTCUSDT|a|b'):
        self.store.record({route: {'spread': spread, 'funding': funding, 'timestamp': now}}, now, max_gap=30)

    def test_weighted_minutes_and_window_coverage(self):
        self.record(0, 10, 2)
        self.record(10, 30, -2)
        self.record(30, 30, -2)
        self.record(60, 30, -2)
        stats = self.store.summaries(60, 1)['BTCUSDT|a|b']
        self.assertAlmostEqual(stats['spread_mean_bps'], (10*10 + 30*50)/60)
        self.assertAlmostEqual(stats['positive_funding_ratio'], 10/60)
        self.assertAlmostEqual(stats['coverage'], 60/3600)
        self.assertGreater(stats['spread_std_bps'], 0)

    def test_gap_and_missing_route_do_not_fill_history(self):
        self.record(0)
        self.record(10)
        self.record(100)
        self.store.record({}, 110, max_gap=30)
        self.record(120)
        stats = self.store.summaries(180, 1)['BTCUSDT|a|b']
        self.assertEqual(stats['valid_seconds'], 10)

    def test_restart_recovers_history_without_crediting_downtime(self):
        self.record(0)
        self.record(10)
        self.store = StrategyHistory(self.path)
        self.record(30)
        stats = self.store.summaries(60, 1)['BTCUSDT|a|b']
        self.assertEqual(stats['valid_seconds'], 10)
        self.assertEqual(len(self.store.series('BTCUSDT|a|b', 60, 1)), 1)

    def test_signal_requires_continuity_and_records_events(self):
        args = dict(required_seconds=20, max_gap=15)
        self.assertEqual(self.store.advance('r', 0, True, True, None, **args)['status'], 'pending')
        self.assertEqual(self.store.advance('r', 10, True, True, None, **args)['qualified_seconds'], 10)
        self.assertEqual(self.store.advance('r', 20, True, True, None, **args)['status'], 'triggered')
        self.assertEqual(self.store.advance('r', 21, False, False, 'STALE_DATA', **args)['status'], 'paused')
        recovered = self.store.advance('r', 25, True, True, None, **args)
        self.assertEqual(recovered['status'], 'pending')
        self.assertEqual(recovered['qualified_seconds'], 0)
        self.assertEqual(len(self.store.events('r')), 4)

    def test_gaps_restart_continuous_timer_and_false_ends(self):
        args = dict(required_seconds=10, max_gap=15)
        self.store.advance('r', 0, True, True, None, **args)
        self.assertEqual(self.store.advance('r', 100, True, True, None, **args)['qualified_seconds'], 0)
        self.store.advance('r', 110, True, True, None, **args)
        self.assertEqual(self.store.advance('r', 111, False, True, 'LOW_NET_RETURN', **args)['status'], 'ended')

    def test_retention_removes_old_minutes_and_events(self):
        self.record(0)
        self.record(10)
        self.store.advance('r', 0, False, True, 'LOW_NET_RETURN', required_seconds=20, max_gap=30)
        self.store.prune(49 * 3600)
        self.assertEqual(self.store.series('BTCUSDT|a|b', 49 * 3600, 24), [])
        self.assertEqual(self.store.events('r'), [])

    def test_valid_cached_quote_counts_only_until_expiry(self):
        for now, timestamp in [(29, 0), (34, 0), (39, 39), (44, 39)]:
            self.store.record({'r': {'spread': 10, 'funding': 2, 'timestamp': timestamp}},
                              now, max_gap=30, max_age=30)
        self.assertEqual(self.store.summaries(60, 1)['r']['valid_seconds'], 6)

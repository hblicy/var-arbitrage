import unittest

from models import MarketDatum
from funding_strategy import settlement_schedule, project_funding, depth_scenarios


NOW = 1789871500.0


class FundingMathTests(unittest.TestCase):
    def markets(self):
        return (
            MarketDatum('BTCUSDT', 100, 0.0001, native_interval_hours=1,
                        best_bid=99.9, best_ask=100, next_funding_time=(NOW + 600) * 1000),
            MarketDatum('BTCUSDT', 100, 0.002, native_interval_hours=8,
                        best_bid=100, best_ask=100.1, next_funding_time=(NOW + 1800) * 1000),
        )

    def test_actual_settlement_times_and_open_boundary(self):
        market = self.markets()[1]
        self.assertEqual(settlement_schedule(market, NOW, 1)['count'], 1)
        self.assertEqual(settlement_schedule(market, NOW, 8)['count'], 1)
        market.next_funding_time = NOW * 1000
        self.assertEqual(settlement_schedule(market, NOW, 1)['count'], 0)
        self.assertEqual(settlement_schedule(market, NOW, 8)['count'], 1)

    def test_missing_schedule_is_explicitly_estimated(self):
        market = self.markets()[0]
        market.next_funding_time = None
        self.assertEqual(settlement_schedule(market, NOW, 4)['source'], 'utc_estimate')
        market.next_funding_time = (NOW + 600) * 1000
        market.next_funding_time_source = 'utc_estimate'
        self.assertEqual(settlement_schedule(market, NOW, 4)['source'], 'utc_estimate')

    def test_complete_round_trip_and_funding_cashflows(self):
        buy, sell = self.markets()
        row = project_funding(buy, sell, now=NOW, hours=1, notional=1000, buy_fee=2, sell_fee=3)
        self.assertAlmostEqual(row['funding_usd'], 1.9)
        self.assertAlmostEqual(row['entry_spread_usd'], 0)
        self.assertAlmostEqual(row['exit_spread_cost_usd'], 2)
        self.assertAlmostEqual(row['fees_usd'], 1.0001)
        self.assertAlmostEqual(row['net_usd'], -1.1001)
        self.assertEqual(row['exit_assumption'], 'unchanged_books')

    def test_missing_funding_is_not_zero(self):
        buy, sell = self.markets()
        buy.funding_rate = None
        row = project_funding(buy, sell, now=NOW, hours=1, notional=1000, buy_fee=0, sell_fee=0)
        self.assertEqual(row['reason'], 'FUNDING_UNAVAILABLE')
        buy.funding_rate = 0
        self.assertIsNone(project_funding(buy, sell, now=NOW, hours=1, notional=1000, buy_fee=0, sell_fee=0)['reason'])

    def test_positive_and_negative_funding_have_correct_signs(self):
        buy, sell = self.markets()
        buy.funding_rate, sell.funding_rate = -0.001, -0.0001
        row = project_funding(buy, sell, now=NOW, hours=1, notional=1000, buy_fee=0, sell_fee=0)
        self.assertAlmostEqual(row['funding_usd'], 0.9)

    def test_depth_and_capacity_use_all_four_book_sides(self):
        buy, sell = self.markets()
        buy.funding_rate, sell.funding_rate = 0, 0.05
        books = {'bids': [[99.9, 20]], 'asks': [[100, 20]]}, {'bids': [[100, 20]], 'asks': [[100.1, 5]]}
        result = depth_scenarios(buy, sell, *books, now=NOW, hours=1, budgets=(100, 1000), buy_fee=0, sell_fee=0, min_net_bps=20)
        self.assertIsNone(result['tiers'][0]['reason'])
        self.assertEqual(result['tiers'][1]['reason'], 'INSUFFICIENT_DEPTH')
        self.assertAlmostEqual(result['capacity']['notional_usd'], 500)
        self.assertTrue(result['capacity']['book_limited'])

    def test_capacity_stops_at_profit_threshold_inside_a_level(self):
        buy, sell = self.markets()
        buy.funding_rate, sell.funding_rate = 0, 0.01
        books = {'bids': [[100, 10]], 'asks': [[100, 1], [102, 9]]}, {'bids': [[100, 10]], 'asks': [[100.01, 10]]}
        result = depth_scenarios(buy, sell, *books, now=NOW, hours=1, budgets=(100, 1000), buy_fee=0, sell_fee=0, min_net_bps=20)
        self.assertGreater(result['capacity']['notional_usd'], 100)
        self.assertLess(result['capacity']['notional_usd'], 200)
        self.assertFalse(result['capacity']['book_limited'])

    def test_invalid_inputs_are_rejected(self):
        buy, sell = self.markets()
        with self.assertRaises(ValueError):
            project_funding(buy, sell, now=NOW, hours=3, notional=1000, buy_fee=0, sell_fee=0)
        buy.price = float('nan')
        self.assertEqual(project_funding(buy, sell, now=NOW, hours=1, notional=1000, buy_fee=0, sell_fee=0)['reason'], 'INVALID_MARKET')

    def test_depth_budget_is_respected_after_walking_levels(self):
        buy, sell = self.markets()
        books = {'bids': [[99.9, 100]], 'asks': [[100, 1], [110, 99]]}, {'bids': [[100, 100]], 'asks': [[100.1, 100]]}
        row = depth_scenarios(buy, sell, *books, now=NOW, hours=1, budgets=(1000,), buy_fee=0, sell_fee=0, min_net_bps=0)['tiers'][0]
        self.assertAlmostEqual(row['quantity'] * row['entry_buy_vwap'], 1000, places=5)

    def test_large_quantity_full_book_rounding(self):
        buy, sell = self.markets()
        buy.price = sell.price = 0.1
        books = [{'bids': [[bid-i*0.000001, 10000.1] for i in range(100)],
                  'asks': [[ask+i*0.000001, 10000.1] for i in range(100)]}
                 for bid, ask in [(0.0999, 0.1), (0.1, 0.1001)]]
        result = depth_scenarios(buy, sell, *books, now=NOW, hours=8,
                                 buy_fee=0, sell_fee=0, min_net_bps=0)
        self.assertIsNone(result['reason'])
        self.assertTrue(all(tier['reason'] is None for tier in result['tiers']))

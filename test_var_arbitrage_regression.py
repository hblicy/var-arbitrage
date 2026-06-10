"""
var-arbitrage 回归测试
覆盖审计发现的中危/低危 bug 修复验证
"""
import sys
import unittest
from unittest.mock import MagicMock, patch
from datetime import datetime

sys.path.insert(0, r'd:\code-web3\DEX\var-arbitrage_v1.6')

from position_tracker import check_exit_signals
from notifier import _format_exit_alerts, _format_text
from analyzer import analyse_markets
from models import ArbitrageOpportunity, MarketDatum


class MockConfig:
    """Mock config for testing."""
    class thresholds:
        funding_reversal_threshold_minutes = 0  # 立即触发
        exit_min_net_funding_bps = -10  # 宽松阈值便于测试

    class exchanges:
        @staticmethod
        def get(_name):
            m = MagicMock()
            m.taker_bps = 5.0
            return m


class TestCheckExitSignalsPriceInjection(unittest.TestCase):
    """测试 check_exit_signals 的价格注入逻辑（审计中危 bug）。"""

    def setUp(self):
        import position_tracker
        position_tracker._unfavorable_since = {}

    @patch('position_tracker.load_positions')
    def test_dict_style_price_injection(self, mock_load_positions):
        """测试 dict 类型行情数据的价格注入（之前只处理了对象类型）。"""
        mock_load_positions.return_value = {
            "BTCUSDT_binance_long_hyperliquid_short": {
                "symbol": "BTCUSDT",
                "direction": "binance_long_hyperliquid_short",
                "long_exchange": "binance",
                "short_exchange": "hyperliquid",
                "entry_qty": 0.1,
                "entry_price_long": 50000,
                "entry_price_short": 50100,
                "entry_funding": {"long_rate": 0.0001, "short_rate": -0.0005},
                "entry_time": datetime.now().isoformat(),
            }
        }

        # Dict-style market data (like some collectors return)
        # 设置不利条件：net funding < threshold (-10 bps)
        # net = short_scaled - long_scaled < -0.001
        # Long rate = -0.002 (scaled=-0.002), Short rate = -0.004 (scaled=-0.004)
        # Net = -0.004 - (-0.002) = -0.002 < -0.001 -> 触发
        # 价格设置：long_price=51000, short_price=51200（非对称，确保 mtm_pnl != 0）
        exchanges_data = {
            "binance": {
                "BTCUSDT": {
                    "funding_rate": -0.002,
                    "native_interval_hours": 8,
                    "price": 51000,
                }
            },
            "hyperliquid": {
                "BTCUSDT": {
                    "funding_rate": -0.004,
                    "native_interval_hours": 8,
                    "price": 51200,
                }
            }
        }

        cfg = MockConfig()
        alerts = check_exit_signals(exchanges_data, cfg)

        # 应该有 alert 生成
        self.assertEqual(len(alerts), 1)

        # 关键：验证价格注入成功 - 检查 current_price_long 和 current_price_short
        self.assertEqual(alerts[0].get("current_price_long"), 51000, "long 价格应正确注入")
        self.assertEqual(alerts[0].get("current_price_short"), 51200, "short 价格应正确注入")

        # 关键：pnl 信息应该包含 mtm_pnl 和 total_pnl（证明价格注入成功）
        pnl = alerts[0].get("pnl")
        self.assertIsNotNone(pnl, "应该有 PnL 信息")
        self.assertIn("mtm_pnl", pnl, "应该包含 mtm_pnl")
        self.assertIn("total_pnl", pnl, "应该包含 total_pnl")

        # 计算验证（非零 MTM）：
        # Long MTM = (51000 - 50000) * 0.1 = 100
        # Short MTM = (50100 - 51200) * 0.1 = -110
        # Total MTM = -10（非零，证明价格注入确实生效）
        expected_mtm = (51000 - 50000) * 0.1 + (50100 - 51200) * 0.1
        self.assertAlmostEqual(pnl["mtm_pnl"], expected_mtm, places=1)
        self.assertNotAlmostEqual(pnl["mtm_pnl"], 0, places=1, msg="mtm_pnl 不应为零，确保价格注入生效")

        # total_pnl 应该等于 net_pnl + mtm_pnl
        expected_total = round(pnl["net_pnl"] + pnl["mtm_pnl"], 2)
        self.assertAlmostEqual(pnl["total_pnl"], expected_total, places=1)

    @patch('position_tracker.load_positions')
    def test_object_style_price_injection(self, mock_load_positions):
        """测试对象类型行情数据的价格注入（原有逻辑）。"""
        mock_load_positions.return_value = {
            "BTCUSDT_binance_long_hyperliquid_short": {
                "symbol": "BTCUSDT",
                "direction": "binance_long_hyperliquid_short",
                "long_exchange": "binance",
                "short_exchange": "hyperliquid",
                "entry_qty": 0.1,
                "entry_price_long": 50000,
                "entry_price_short": 50100,
                "entry_funding": {"long_rate": 0.0001, "short_rate": -0.0005},
                "entry_time": datetime.now().isoformat(),
            }
        }

        # Object-style market data
        class MockMarketData:
            def __init__(self, funding_rate, price, interval=8):
                self.funding_rate = funding_rate
                self.price = price
                self.native_interval_hours = interval

        # 设置不利条件：net = short - long = -0.004 - (-0.002) = -0.002 < threshold
        # 价格设置：long_price=52000, short_price=52200（非对称，确保 mtm_pnl != 0）
        exchanges_data = {
            "binance": {
                "BTCUSDT": MockMarketData(-0.002, 52000)
            },
            "hyperliquid": {
                "BTCUSDT": MockMarketData(-0.004, 52200)
            }
        }

        cfg = MockConfig()
        alerts = check_exit_signals(exchanges_data, cfg)

        self.assertEqual(len(alerts), 1)

        # 关键：验证价格注入成功 - 检查 current_price_long 和 current_price_short
        self.assertEqual(alerts[0].get("current_price_long"), 52000, "long 价格应正确注入")
        self.assertEqual(alerts[0].get("current_price_short"), 52200, "short 价格应正确注入")

        pnl = alerts[0].get("pnl")
        self.assertIsNotNone(pnl)
        self.assertIn("mtm_pnl", pnl)
        # 计算验证（非零 MTM）：
        # Long MTM = (52000 - 50000) * 0.1 = 200
        # Short MTM = (50100 - 52200) * 0.1 = -210
        # Total MTM = -10（非零，证明价格注入确实生效）
        expected_mtm = (52000 - 50000) * 0.1 + (50100 - 52200) * 0.1
        self.assertAlmostEqual(pnl["mtm_pnl"], expected_mtm, places=1)
        self.assertNotAlmostEqual(pnl["mtm_pnl"], 0, places=1, msg="mtm_pnl 不应为零，确保价格注入生效")


class TestHyperliquidNotification(unittest.TestCase):
    """测试 Hyperliquid 提示显示（审计低危 bug）。"""

    def test_exit_alert_shows_hyperliquid_note(self):
        """测试平仓通知中 Hyperliquid 提示能正常显示（交易所名小写）。"""
        exit_signals = [{
            "symbol": "BTCUSDT",
            "direction": "binance_long_hyperliquid_short",
            "long_exchange": "binance",
            "short_exchange": "hyperliquid",
            "current_funding": {"long": 0.0001, "short": -0.0005},
            "native_intervals": {"buy": 8, "sell": 8},
            "base_interval": 8,
            "net_funding_bps": -50,
            "pnl": {
                "net_pnl": 10.0,
                "mtm_pnl": 5.0,
                "total_pnl": 15.0,
                "notional": 5000.0,
                "hours_held": 24,
            }
        }]

        content = _format_exit_alerts(exit_signals)

        # 应该包含 Hyperliquid 提示
        self.assertIn("Hyperliquid", content)
        self.assertIn("实时结算", content)

    def test_exit_alert_shows_hyperliquid_note_mixed_case(self):
        """测试混合大小写的 Hyperliquid 也能匹配。"""
        exit_signals = [{
            "symbol": "BTCUSDT",
            "direction": "Hyperliquid_long_binance_short",
            "long_exchange": "Hyperliquid",
            "short_exchange": "binance",
            "current_funding": {"long": 0.0001, "short": -0.0005},
            "native_intervals": {"buy": 8, "sell": 8},
            "base_interval": 8,
            "net_funding_bps": -50,
            "pnl": {
                "net_pnl": 10.0,
                "mtm_pnl": 5.0,
                "total_pnl": 15.0,
                "notional": 5000.0,
                "hours_held": 24,
            }
        }]

        content = _format_exit_alerts(exit_signals)
        self.assertIn("Hyperliquid", content)
        self.assertIn("实时结算", content)

    def test_arbitrage_alert_shows_hyperliquid_note(self):
        """测试套利通知中 Hyperliquid 提示能正常显示。"""
        opp = ArbitrageOpportunity(
            symbol="BTCUSDT",
            direction="hyperliquid_long_binance_short",
            entry_exchange="hyperliquid",
            exit_exchange="binance",
            gross_spread_bps=50.0,
            net_spread_bps=40.0,
            funding_diff=0.01,
            recommendation="OPEN",
            details={
                "buy_exchange": "hyperliquid",
                "sell_exchange": "binance",
                "native_intervals": {"buy": 1, "sell": 8},
            }
        )

        content = _format_text([opp])

        # 应该包含 Hyperliquid 提示
        self.assertIn("Hyperliquid", content)
        self.assertIn("1小时结算", content)


class TestPnLCalculationConsistency(unittest.TestCase):
    """测试 PnL 计算与持仓页面一致。"""

    def setUp(self):
        import position_tracker
        position_tracker._unfavorable_since = {}

    @patch('position_tracker.load_positions')
    def test_total_pnl_includes_mtm(self, mock_load_positions):
        """验证 total_pnl = net_pnl + mtm_pnl。"""
        mock_load_positions.return_value = {
            "BTCUSDT_test": {
                "symbol": "BTCUSDT",
                "direction": "binance_long_hyperliquid_short",
                "long_exchange": "binance",
                "short_exchange": "hyperliquid",
                "entry_qty": 0.1,
                "entry_price_long": 50000,
                "entry_price_short": 50100,
                "entry_funding": {"long_rate": 0.0001, "short_rate": -0.0005},
                "entry_time": datetime.now().isoformat(),
            }
        }

        # 设置不利条件：net = short - long = -0.004 - (-0.002) = -0.002 < threshold
        exchanges_data = {
            "binance": {
                "BTCUSDT": {
                    "funding_rate": -0.002,
                    "native_interval_hours": 8,
                    "price": 52000,
                }
            },
            "hyperliquid": {
                "BTCUSDT": {
                    "funding_rate": -0.004,
                    "native_interval_hours": 8,
                    "price": 52050,
                }
            }
        }

        cfg = MockConfig()
        alerts = check_exit_signals(exchanges_data, cfg)

        self.assertEqual(len(alerts), 1)
        pnl = alerts[0]["pnl"]

        # total_pnl 应该等于 net_pnl + mtm_pnl
        expected_total = round(pnl["net_pnl"] + pnl["mtm_pnl"], 2)
        self.assertAlmostEqual(pnl["total_pnl"], expected_total, places=1)


class TestAnalyzerExecutableSpread(unittest.TestCase):
    """Regression tests for executable spread calculation."""

    class AnalyzerConfig:
        class thresholds:
            dashboard_min_spread_bps = -9999
            dashboard_min_funding_bps = -9999
            dashboard_min_total_bps = -9999
            max_price_deviation_pct = 100
            min_volume_usd = 0

        class fees:
            slippage_bps = 10.0

        class Ex:
            taker_bps = 5.0

        exchanges = {"buyex": Ex(), "sellex": Ex()}

    def test_spread_uses_ask_to_buy_and_bid_to_sell(self):
        cfg = self.AnalyzerConfig()
        markets = {
            "buyex": {
                "BTCUSDT": MarketDatum(
                    symbol="BTCUSDT",
                    price=100.0,
                    funding_rate=0.0,
                    volume_24h=1_000_000,
                    exchange="buyex",
                    best_bid=99.0,
                    best_ask=101.0,
                )
            },
            "sellex": {
                "BTCUSDT": MarketDatum(
                    symbol="BTCUSDT",
                    price=104.0,
                    funding_rate=0.0,
                    volume_24h=1_000_000,
                    exchange="sellex",
                    best_bid=102.0,
                    best_ask=106.0,
                )
            },
        }

        opps, _, _ = analyse_markets(["BTCUSDT"], markets, cfg)
        target = next(o for o in opps if o.direction == "buyex_long_sellex_short")

        expected_executable = (102.0 / 101.0 - 1.0) * 10_000
        expected_reference = (104.0 / 100.0 - 1.0) * 10_000
        self.assertAlmostEqual(target.gross_spread_bps, expected_executable, places=6)
        self.assertAlmostEqual(target.details["reference_spread_bps"], expected_reference, places=6)
        self.assertEqual(target.details["buy_price_source"], "best_ask")
        self.assertEqual(target.details["sell_price_source"], "best_bid")
        self.assertAlmostEqual(target.net_spread_bps, expected_executable - 10.0, places=6)

    def test_missing_book_applies_fallback_slippage_to_prices(self):
        cfg = self.AnalyzerConfig()
        markets = {
            "buyex": {
                "BTCUSDT": MarketDatum(
                    symbol="BTCUSDT",
                    price=100.0,
                    funding_rate=0.0,
                    volume_24h=1_000_000,
                    exchange="buyex",
                )
            },
            "sellex": {
                "BTCUSDT": MarketDatum(
                    symbol="BTCUSDT",
                    price=102.0,
                    funding_rate=0.0,
                    volume_24h=1_000_000,
                    exchange="sellex",
                )
            },
        }

        opps, _, _ = analyse_markets(["BTCUSDT"], markets, cfg)
        target = next(o for o in opps if o.direction == "buyex_long_sellex_short")

        expected_buy = 100.0 * 1.001
        expected_sell = 102.0 * 0.999
        self.assertAlmostEqual(target.details["buy_execution_price"], expected_buy, places=6)
        self.assertAlmostEqual(target.details["sell_execution_price"], expected_sell, places=6)
        self.assertEqual(target.details["fallback_slippage_bps"], 20.0)

    def test_mixed_book_uses_available_book_and_fallback_for_missing_side(self):
        cfg = self.AnalyzerConfig()
        markets = {
            "buyex": {
                "BTCUSDT": MarketDatum(
                    symbol="BTCUSDT",
                    price=100.0,
                    funding_rate=0.0,
                    volume_24h=1_000_000,
                    exchange="buyex",
                    best_bid=99.0,
                    best_ask=101.0,
                )
            },
            "sellex": {
                "BTCUSDT": MarketDatum(
                    symbol="BTCUSDT",
                    price=104.0,
                    funding_rate=0.0,
                    volume_24h=1_000_000,
                    exchange="sellex",
                )
            },
        }

        opps, _, _ = analyse_markets(["BTCUSDT"], markets, cfg)
        target = next(o for o in opps if o.direction == "buyex_long_sellex_short")

        expected_sell = 104.0 * 0.999
        expected_executable = (expected_sell / 101.0 - 1.0) * 10_000
        self.assertEqual(target.details["buy_price_source"], "best_ask")
        self.assertEqual(target.details["sell_price_source"], "price_minus_fallback_slippage")
        self.assertEqual(target.details["fallback_slippage_bps"], 10.0)
        self.assertAlmostEqual(target.gross_spread_bps, expected_executable, places=6)

    def test_negative_spread_can_show_when_funding_covers_it(self):
        cfg = self.AnalyzerConfig()
        markets = {
            "buyex": {
                "HUSDT": MarketDatum(
                    symbol="HUSDT",
                    price=0.09,
                    funding_rate=-0.02,
                    volume_24h=1_000_000,
                    exchange="buyex",
                    native_interval_hours=1,
                    best_bid=0.089,
                    best_ask=0.09,
                )
            },
            "sellex": {
                "HUSDT": MarketDatum(
                    symbol="HUSDT",
                    price=0.07,
                    funding_rate=-0.0004,
                    volume_24h=1_000_000,
                    exchange="sellex",
                    native_interval_hours=1,
                    best_bid=0.07,
                    best_ask=0.071,
                )
            },
        }

        opps, _, _ = analyse_markets(["HUSDT"], markets, cfg)
        target = next(o for o in opps if o.direction == "buyex_long_sellex_short")

        self.assertLess(target.net_spread_bps, -100)
        self.assertEqual(target.details["opportunity_type"], "funding_cover")
        self.assertAlmostEqual(target.details["funding_hourly_bps"], 196.0, places=6)
        expected_cover = target.details["price_cost_bps"] / target.details["funding_hourly_bps"]
        self.assertAlmostEqual(target.details["cover_hours"], expected_cover, places=6)

    def test_dashboard_thresholds_do_not_hide_calculable_pairs(self):
        class StrictConfig:
            class thresholds:
                dashboard_min_spread_bps = 9999
                dashboard_min_funding_bps = 9999
                dashboard_min_total_bps = 9999
                dashboard_max_cover_hours = 0
                max_price_deviation_pct = 100
                min_volume_usd = 0

            class fees:
                slippage_bps = 0.0

            class Ex:
                taker_bps = 0.0

            exchanges = {"buyex": Ex(), "sellex": Ex()}

        markets = {
            "buyex": {
                "BTCUSDT": MarketDatum(
                    symbol="BTCUSDT",
                    price=100.0,
                    funding_rate=0.0,
                    volume_24h=1_000_000,
                    exchange="buyex",
                )
            },
            "sellex": {
                "BTCUSDT": MarketDatum(
                    symbol="BTCUSDT",
                    price=100.1,
                    funding_rate=0.0,
                    volume_24h=1_000_000,
                    exchange="sellex",
                )
            },
        }

        opps, reasons, _ = analyse_markets(["BTCUSDT"], markets, StrictConfig())

        self.assertEqual(len(opps), 2)
        self.assertNotEqual(reasons.get("BTCUSDT"), "NO_OPPORTUNITY")
        target = next(o for o in opps if o.direction == "buyex_long_sellex_short")
        self.assertFalse(target.details["meets_dashboard_threshold"])

    def test_notification_shows_cover_time(self):
        opp = ArbitrageOpportunity(
            symbol="HUSDT",
            direction="buyex_long_sellex_short",
            entry_exchange="buyex",
            exit_exchange="sellex",
            gross_spread_bps=-2222.22,
            net_spread_bps=-2232.22,
            funding_diff=0.0196,
            recommendation="OPEN",
            details={
                "buy_exchange": "buyex",
                "sell_exchange": "sellex",
                "buy_price": 0.09,
                "sell_price": 0.07,
                "funding_long_native": -0.02,
                "funding_short_native": -0.0004,
                "funding_daily_bps": 4704.0,
                "funding_hourly_bps": 196.0,
                "projected_24h_bps": 2471.78,
                "cover_hours": 11.4,
                "opportunity_type": "funding_cover",
                "native_intervals": {"buy": 1, "sell": 1},
            },
        )

        content = _format_text([opp])

        self.assertIn("资金费覆盖价差", content)
        self.assertIn("覆盖时间：约 11.4 小时", content)
        self.assertIn("24h估算：+24.7178%", content)


if __name__ == '__main__':
    unittest.main(verbosity=2)

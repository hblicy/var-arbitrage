"""
var-arbitrage 回归测试
覆盖审计发现的中危/低危 bug 修复验证
"""
import sys
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone
from types import SimpleNamespace

sys.path.insert(0, r'd:\code-web3\DEX\var-arbitrage_v1.6')

from position_tracker import check_exit_signals
from notifier import _format_exit_alerts, _format_text
from analyzer import analyse_markets
from collectors.ondoperps import OndoPerpsCollector
from collectors.aster import AsterCollector
from collectors.factory import create_collector
from config import Settings
from models import ArbitrageOpportunity, MarketDatum


class TestAuthBehavior(unittest.TestCase):
    """Regression tests for browser-friendly position auth."""

    def test_missing_credentials_do_not_trigger_browser_basic_auth_prompt(self):
        from fastapi import HTTPException
        from api import get_current_user

        with self.assertRaises(HTTPException) as ctx:
            get_current_user(None)

        self.assertEqual(ctx.exception.status_code, 401)
        self.assertNotIn("WWW-Authenticate", ctx.exception.headers or {})


class TestApplicationLifespan(unittest.IsolatedAsyncioTestCase):
    async def test_lifespan_runs_startup_and_shutdown(self):
        import api

        with (
            patch.object(api, "startup_event", new_callable=AsyncMock) as startup,
            patch.object(api, "shutdown_event", new_callable=AsyncMock) as shutdown,
        ):
            async with api.lifespan(api.app):
                startup.assert_awaited_once()
                shutdown.assert_not_awaited()

        shutdown.assert_awaited_once()


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


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class FakeOndoClient:
    async def request(self, **_kwargs):
        return FakeResponse({
            "success": True,
            "result": [
                {
                    "market": "CRCL-USD.P",
                    "disabled": False,
                    "lastPrice": "66.54",
                    "bid": "66.51",
                    "ask": "66.55",
                    "quoteVolume": "471688.752",
                    "fundingRate": "-0.0001634",
                    "nextFundingRate": "0.0000023",
                    "nextFundingRateTimestamp": "2026-07-04T14:00:00Z",
                },
                {
                    "market": "ZERO-USD.P",
                    "disabled": False,
                    "lastPrice": "10",
                    "bid": "9.9",
                    "ask": "10.1",
                    "quoteVolume": "1000000",
                    "fundingRate": "-0.001",
                    "nextFundingRate": "0",
                },
                {
                    "market": "BTC-USD.P",
                    "disabled": True,
                    "lastPrice": "60000",
                    "quoteVolume": "1000000",
                    "nextFundingRate": "0.0001",
                },
            ],
        })


class FakeAsterClient:
    async def get(self, path):
        if path == "/fapi/v1/ticker/24hr":
            return FakeResponse([
                {
                    "symbol": "CRCLUSDT",
                    "lastPrice": "66.54",
                    "quoteVolume": "471688.752",
                },
                {
                    "symbol": "BTCUSDT",
                    "lastPrice": "60000",
                    "quoteVolume": "1000000",
                },
                {
                    "symbol": "GNSUSD",
                    "lastPrice": "3.5",
                    "quoteVolume": "1000000",
                },
                {
                    "symbol": "SHIELDAMZNUSDT",
                    "lastPrice": "200",
                    "quoteVolume": "1000000",
                },
            ])
        if path == "/fapi/v1/premiumIndex":
            return FakeResponse([
                {
                    "symbol": "CRCLUSDT",
                    "markPrice": "66.55",
                    "lastFundingRate": "-0.00008481",
                    "nextFundingTime": 1783180800000,
                },
                {
                    "symbol": "BTCUSDT",
                    "markPrice": "60001",
                    "lastFundingRate": "0.00001",
                    "nextFundingTime": 1783180800000,
                },
            ])
        if path == "/fapi/v1/ticker/bookTicker":
            return FakeResponse([
                {"symbol": "CRCLUSDT", "bidPrice": "66.51", "askPrice": "66.55"},
                {"symbol": "BTCUSDT", "bidPrice": "59990", "askPrice": "60010"},
            ])
        if path == "/fapi/v1/fundingInfo":
            return FakeResponse([
                {"symbol": "CRCLUSDT", "fundingIntervalHours": 8},
                {"symbol": "BTCUSDT", "fundingIntervalHours": 1},
            ])
        raise AssertionError(f"unexpected path: {path}")


class TestOndoPerpsCollector(unittest.IsolatedAsyncioTestCase):
    async def test_contracts_are_normalized_with_next_funding_rate(self):
        collector = OndoPerpsCollector(Settings().ondoperps)
        collector._client = FakeOndoClient()

        markets = await collector.fetch_markets(["CRCLUSDT", "ZEROUSDT", "BTCUSDT"])

        self.assertIn("CRCLUSDT", markets)
        self.assertNotIn("BTCUSDT", markets)

        crcl = markets["CRCLUSDT"]
        self.assertEqual(crcl.exchange, "OndoPerps")
        self.assertEqual(crcl.price, 66.54)
        self.assertEqual(crcl.best_bid, 66.51)
        self.assertEqual(crcl.best_ask, 66.55)
        self.assertEqual(crcl.volume_24h, 471688.752)
        self.assertEqual(crcl.native_interval_hours, 1)
        self.assertEqual(crcl.funding_rate, 0.0000023)
        self.assertEqual(crcl.next_funding_time, 1783173600000.0)

        self.assertEqual(markets["ZEROUSDT"].funding_rate, 0.0)


class TestAsterCollector(unittest.IsolatedAsyncioTestCase):
    async def test_rest_markets_include_funding_book_and_interval(self):
        collector = AsterCollector(Settings().aster)
        collector._client = FakeAsterClient()

        markets = await collector.fetch_markets(["CRCLUSDT", "BTCUSDT", "GNSUSD", "SHIELDAMZNUSDT"])

        self.assertIn("CRCLUSDT", markets)
        self.assertIn("BTCUSDT", markets)
        self.assertNotIn("GNSUSD", markets)
        self.assertNotIn("SHIELDAMZNUSDT", markets)

        crcl = markets["CRCLUSDT"]
        self.assertEqual(crcl.exchange, "Aster")
        self.assertEqual(crcl.price, 66.54)
        self.assertEqual(crcl.best_bid, 66.51)
        self.assertEqual(crcl.best_ask, 66.55)
        self.assertEqual(crcl.volume_24h, 471688.752)
        self.assertEqual(crcl.native_interval_hours, 8)
        self.assertEqual(crcl.funding_rate, -0.00008481)
        self.assertEqual(crcl.next_funding_time, 1783180800000.0)

        self.assertEqual(markets["BTCUSDT"].native_interval_hours, 1)

    def test_aster_enabled_and_edgex_removed(self):
        settings = Settings()

        self.assertIn("aster", settings.exchanges)
        self.assertNotIn("edgex", settings.exchanges)
        self.assertEqual(settings.aster.taker_bps, 4.0)
        self.assertIsInstance(create_collector("aster", settings.aster), AsterCollector)


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
        self.assertIn("24h假设收敛估算：+24.7178%", content)

    def test_notification_requires_manual_depth_recheck(self):
        opp = ArbitrageOpportunity(
            symbol="BTCUSDT",
            direction="binance_long_aster_short",
            entry_exchange="binance",
            exit_exchange="aster",
            gross_spread_bps=50.0,
            net_spread_bps=40.0,
            funding_diff=0.0,
            recommendation="WATCH",
            details={
                "buy_exchange": "binance",
                "sell_exchange": "aster",
                "native_intervals": {"buy": 8, "sell": 8},
            },
        )

        content = _format_text([opp])

        self.assertIn("不是开仓指令", content)
        self.assertIn("1000 USDT/腿", content)


class TestMarketFreshness(unittest.TestCase):
    class FreshnessConfig:
        class thresholds:
            dashboard_min_spread_bps = -9999
            dashboard_min_funding_bps = -9999
            dashboard_min_total_bps = -9999
            dashboard_max_cover_hours = 24
            max_price_deviation_pct = 100
            min_volume_usd = 0

        class fees:
            slippage_bps = 0.0

        class schedule:
            market_stale_seconds = 2

        class Ex:
            taker_bps = 0.0

        exchanges = {"buyex": Ex(), "sellex": Ex()}

    def test_stale_market_data_is_excluded_from_opportunities(self):
        stale_at = time.time() - 3
        markets = {
            "buyex": {
                "BTCUSDT": MarketDatum(
                    symbol="BTCUSDT",
                    price=100.0,
                    funding_rate=0.0,
                    volume_24h=1_000_000,
                    timestamp=stale_at,
                    exchange="buyex",
                    best_bid=99.9,
                    best_ask=100.0,
                )
            },
            "sellex": {
                "BTCUSDT": MarketDatum(
                    symbol="BTCUSDT",
                    price=101.0,
                    funding_rate=0.0,
                    volume_24h=1_000_000,
                    timestamp=stale_at,
                    exchange="sellex",
                    best_bid=101.0,
                    best_ask=101.1,
                )
            },
        }

        opportunities, reasons, _ = analyse_markets(["BTCUSDT"], markets, self.FreshnessConfig())

        self.assertEqual(opportunities, [])
        self.assertEqual(reasons["BTCUSDT"], "STALE_DATA")


class TestBinanceFundingIntervalRefresh(unittest.IsolatedAsyncioTestCase):
    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self.payload

    class ChangingFundingInfoClient:
        def __init__(self):
            self.calls = 0

        async def get(self, path):
            self.calls += 1
            self.assertEqual(path, "/fapi/v1/fundingInfo")
            interval = 8 if self.calls == 1 else 4
            return TestBinanceFundingIntervalRefresh.FakeResponse([
                {"symbol": "BTCUSDT", "fundingIntervalHours": interval},
            ])

        def assertEqual(self, left, right):
            if left != right:
                raise AssertionError(f"{left!r} != {right!r}")

    async def test_refreshes_changed_interval_without_hour_long_delay(self):
        from collectors.binance import BinanceCollector

        collector = BinanceCollector(Settings().binance)
        collector.settings.metadata_refresh_seconds = 0
        collector._client = self.ChangingFundingInfoClient()

        await collector._refresh_intervals()
        await collector._refresh_intervals()

        self.assertEqual(collector._interval_map["BTCUSDT"], 4)


class TestEntryQuoteEvaluation(unittest.TestCase):
    def test_actionable_entry_requires_depth_and_keeps_safety_buffer(self):
        from entry_check import evaluate_entry_quote

        result = evaluate_entry_quote(
            buy_book={"asks": [[100.0, 20.0]], "bids": [[99.9, 20.0]]},
            sell_book={"asks": [[101.6, 20.0]], "bids": [[101.5, 20.0]]},
            notional_usd=1_000.0,
            buy_fee_bps=5.0,
            sell_fee_bps=5.0,
            safety_buffer_bps=20.0,
            buy_quoted_at=1_000.0,
            sell_quoted_at=1_000.2,
            checked_at=1_000.5,
            max_quote_age_ms=2_000,
            max_leg_skew_ms=1_000,
        )

        self.assertEqual(result["status"], "actionable")
        self.assertGreaterEqual(result["net_convergence_bps"], 20.0)
        self.assertEqual(result["hedge_quantity"], 1000.0 / 101.5)


class TestMarketSnapshotState(unittest.TestCase):
    def test_failed_fetch_replaces_old_snapshot_and_marks_exchange_error(self):
        from market_state import replace_exchange_snapshot

        raw_exchanges_data = {"binance": {"BTCUSDT": object()}}
        exchange_status = {}

        replace_exchange_snapshot(
            raw_exchanges_data,
            exchange_status,
            key="binance",
            data={},
            fetched_at=1_000.0,
            error="upstream timeout",
        )

        self.assertEqual(raw_exchanges_data["binance"], {})
        self.assertEqual(exchange_status["binance"]["state"], "error")
        self.assertEqual(exchange_status["binance"]["market_count"], 0)


class TestExchangeSettlementTimestamp(unittest.TestCase):
    def test_uses_exchange_next_funding_time_instead_of_utc_grid(self):
        from position_tracker import _next_settlement_utc

        now = datetime(2026, 7, 28, 1, 56)
        exchange_next = datetime(2026, 7, 28, 2, 0, tzinfo=timezone.utc).timestamp() * 1_000

        result = _next_settlement_utc(8, now, next_funding_time=exchange_next)

        self.assertEqual(result, datetime(2026, 7, 28, 2, 0))


class TestDepthQuoteCollectors(unittest.IsolatedAsyncioTestCase):
    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "bids": [["101.5", "20"]],
                "asks": [["100.0", "20"]],
            }

    class FakeClient:
        async def get(self, path, params=None):
            if path != "/fapi/v1/depth":
                raise AssertionError(f"unexpected path: {path}")
            if params != {"symbol": "BTCUSDT", "limit": 100}:
                raise AssertionError(f"unexpected params: {params}")
            return TestDepthQuoteCollectors.FakeResponse()

    async def test_binance_fetches_depth_for_pre_trade_check(self):
        from collectors.binance import BinanceCollector

        collector = BinanceCollector(Settings().binance)
        collector._client = self.FakeClient()

        book = await collector.fetch_order_book("BTCUSDT", 100)

        self.assertEqual(book["bids"], [[101.5, 20.0]])
        self.assertEqual(book["asks"], [[100.0, 20.0]])

    async def test_aster_fetches_depth_for_pre_trade_check(self):
        from collectors.aster import AsterCollector

        collector = AsterCollector(Settings().aster)
        collector._client = self.FakeClient()

        book = await collector.fetch_order_book("BTCUSDT", 100)

        self.assertEqual(book["bids"], [[101.5, 20.0]])
        self.assertEqual(book["asks"], [[100.0, 20.0]])


class TestManualEntryCheck(unittest.IsolatedAsyncioTestCase):
    class FakeDepthCollector:
        def __init__(self, book):
            self.book = book

        async def fetch_order_book(self, _symbol, _limit):
            return self.book

    async def test_manual_entry_check_uses_configured_one_leg_notional(self):
        from api import evaluate_manual_entry
        import api

        buy = self.FakeDepthCollector({"asks": [[100.0, 20.0]], "bids": [[99.9, 20.0]]})
        sell = self.FakeDepthCollector({"asks": [[101.6, 20.0]], "bids": [[101.5, 20.0]]})
        original_hub = api.collectors_hub
        api.collectors_hub = {"binance": buy, "aster": sell}
        try:
            result = await evaluate_manual_entry("BTCUSDT", "binance", "aster")
        finally:
            api.collectors_hub = original_hub

        self.assertEqual(result["status"], "actionable")
        self.assertEqual(result["notional_usd"], 1_000.0)
        self.assertEqual(result["long_exchange"], "binance")
        self.assertEqual(result["short_exchange"], "aster")


class TestCollectorFailures(unittest.IsolatedAsyncioTestCase):
    class FailingClient:
        async def get(self, *_args, **_kwargs):
            raise RuntimeError("upstream timeout")

    async def test_fetch_error_is_reported_to_the_api_loop(self):
        from collectors.aster import AsterCollector

        collector = AsterCollector(Settings().aster)
        collector._client = self.FailingClient()

        with self.assertRaises(RuntimeError):
            await collector.fetch_markets([])


class TestFundingMetadataFailures(unittest.IsolatedAsyncioTestCase):
    class FailingClient:
        async def get(self, *_args, **_kwargs):
            raise RuntimeError("funding metadata timeout")

    async def test_binance_interval_refresh_does_not_keep_an_old_interval_map(self):
        from collectors.binance import BinanceCollector

        collector = BinanceCollector(Settings().binance)
        collector._client = self.FailingClient()
        collector._interval_map = {"BTCUSDT": 8}

        with self.assertRaises(RuntimeError):
            await collector._refresh_intervals()

    async def test_aster_interval_refresh_does_not_keep_an_old_interval_map(self):
        from collectors.aster import AsterCollector

        collector = AsterCollector(Settings().aster)
        collector._client = self.FailingClient()
        collector._interval_map = {"BTCUSDT": 8}

        with self.assertRaises(RuntimeError):
            await collector._refresh_intervals()


class TestCliMarketTimestamps(unittest.IsolatedAsyncioTestCase):
    class FakeCollector:
        def __init__(self, exchange: str, price: float):
            self.exchange = exchange
            self.price = price

        async def fetch_markets(self, _symbols):
            return {
                "BTCUSDT": MarketDatum(
                    symbol="BTCUSDT",
                    price=self.price,
                    funding_rate=0.0,
                    volume_24h=1_000_000,
                    exchange=self.exchange,
                    best_bid=self.price - 0.1,
                    best_ask=self.price,
                )
            }

    async def test_run_cycle_stamps_markets_before_analysis(self):
        import monitor

        captured = {}

        def assert_fresh_timestamps(_symbols, exchanges_data, _cfg):
            captured["timestamps"] = [
                market.timestamp
                for markets in exchanges_data.values()
                for market in markets.values()
            ]
            return [], {}, {}

        cfg = SimpleNamespace(tracked_symbols=["BTCUSDT"])
        collectors = {
            "binance": self.FakeCollector("binance", 100.0),
            "aster": self.FakeCollector("aster", 101.0),
        }
        with patch("monitor.update_pre_settlement_rates"), patch(
            "monitor.analyse_markets", side_effect=assert_fresh_timestamps
        ):
            await monitor.run_cycle(cfg, collectors, MagicMock())

        self.assertTrue(all(timestamp is not None for timestamp in captured["timestamps"]))


class TestDashboardSnapshotFreshness(unittest.TestCase):
    def test_stale_exchange_is_hidden_from_dashboard_response(self):
        from market_state import dashboard_snapshot

        source = {
            "markets": {
                "BTCUSDT": {
                    "binance": {"price": 100.0},
                    "aster": {"price": 101.0},
                }
            },
            "opportunities": [
                {
                    "symbol": "BTCUSDT",
                    "details": {
                        "buy_exchange": "binance",
                        "sell_exchange": "aster",
                    },
                }
            ],
            "reasons": {},
            "symbol_max_intervals": {"BTCUSDT": 8},
            "last_update": "2026-07-28 00:00:00",
            "data_version": 1,
            "exchange_status": {
                "binance": {"state": "fresh", "last_success_at": 1_000.0},
                "aster": {"state": "fresh", "last_success_at": 1_080.0},
            },
        }

        snapshot = dashboard_snapshot(source, market_stale_seconds=30, now=1_100.0)

        self.assertEqual(snapshot["exchange_status"]["binance"]["state"], "stale")
        self.assertIsNone(snapshot["markets"]["BTCUSDT"]["binance"])
        self.assertEqual(snapshot["opportunities"], [])

    def test_zero_stale_threshold_keeps_fresh_snapshot_available(self):
        from market_state import dashboard_snapshot

        source = {
            "markets": {"BTCUSDT": {"binance": {"price": 100.0}}},
            "opportunities": [],
            "exchange_status": {
                "binance": {"state": "fresh", "last_success_at": 1_000.0},
            },
        }

        snapshot = dashboard_snapshot(source, market_stale_seconds=0, now=1_100.0)

        self.assertEqual(snapshot["exchange_status"]["binance"]["state"], "fresh")


class TestNotificationDepthFilter(unittest.IsolatedAsyncioTestCase):
    async def test_notifier_sends_only_depth_supported_routes(self):
        from notifier import WeChatNotifier

        settings = SimpleNamespace(
            wechat_webhook="https://example.com/webhook",
            notify_minute_offset=0,
            notification_exchanges=[],
            cooldown_seconds=0,
        )
        unsupported = ArbitrageOpportunity(
            symbol="UNSUPPORTEDUSDT",
            direction="lighter_long_hyperliquid_short",
            entry_exchange="lighter",
            exit_exchange="hyperliquid",
            gross_spread_bps=200.0,
            net_spread_bps=150.0,
            funding_diff=0.01,
            recommendation="WATCH",
            details={
                "buy_exchange": "lighter",
                "sell_exchange": "hyperliquid",
            },
        )
        supported = ArbitrageOpportunity(
            symbol="SUPPORTEDUSDT",
            direction="binance_long_aster_short",
            entry_exchange="binance",
            exit_exchange="aster",
            gross_spread_bps=200.0,
            net_spread_bps=150.0,
            funding_diff=0.01,
            recommendation="WATCH",
            details={
                "buy_exchange": "binance",
                "sell_exchange": "aster",
                "entry_check_supported": True,
            },
        )

        with (
            patch.object(WeChatNotifier, "_load_state", return_value={}),
            patch.object(WeChatNotifier, "_save_state"),
            patch.object(WeChatNotifier, "_post", new_callable=AsyncMock, return_value=True) as post,
        ):
            notifier = WeChatNotifier(settings)
            await notifier.send([unsupported, supported])

        post.assert_awaited_once()
        content = post.await_args.args[1]
        self.assertIn("SUPPORTEDUSDT", content)
        self.assertNotIn("UNSUPPORTEDUSDT", content)

    async def test_failed_delivery_does_not_start_cooldown(self):
        from notifier import WeChatNotifier

        settings = SimpleNamespace(
            wechat_webhook="https://example.com/webhook",
            notify_minute_offset=0,
            notification_exchanges=[],
            cooldown_seconds=1800,
        )
        opportunity = ArbitrageOpportunity(
            symbol="SUPPORTEDUSDT",
            direction="binance_long_aster_short",
            entry_exchange="binance",
            exit_exchange="aster",
            gross_spread_bps=200.0,
            net_spread_bps=150.0,
            funding_diff=0.01,
            recommendation="WATCH",
            details={
                "buy_exchange": "binance",
                "sell_exchange": "aster",
                "entry_check_supported": True,
            },
        )

        with (
            patch.object(WeChatNotifier, "_load_state", return_value={}),
            patch.object(WeChatNotifier, "_save_state") as save_state,
            patch.object(WeChatNotifier, "_post", new_callable=AsyncMock, side_effect=[False, True]) as post,
        ):
            notifier = WeChatNotifier(settings)
            await notifier.send([opportunity])
            await notifier.send([opportunity])

        self.assertEqual(post.await_count, 2)
        self.assertEqual(save_state.call_count, 1)


class TestEntryCheckRouteCapability(unittest.TestCase):
    class DepthCollector:
        async def fetch_order_book(self, _symbol, _limit):
            return {"bids": [], "asks": []}

    def test_route_requires_depth_on_both_legs(self):
        from api import entry_check_supported

        collectors = {
            "binance": self.DepthCollector(),
            "aster": self.DepthCollector(),
            "lighter": object(),
        }

        self.assertTrue(entry_check_supported("binance", "aster", collectors))
        self.assertFalse(entry_check_supported("binance", "lighter", collectors))

    def test_analysis_routes_are_annotated_before_notification(self):
        import entry_check

        opportunities = [
            ArbitrageOpportunity(
                symbol="SUPPORTEDUSDT",
                direction="binance_long_aster_short",
                entry_exchange="binance",
                exit_exchange="aster",
                gross_spread_bps=100.0,
                net_spread_bps=80.0,
                funding_diff=0.01,
                recommendation="WATCH",
                details={"buy_exchange": "binance", "sell_exchange": "aster"},
            ),
            ArbitrageOpportunity(
                symbol="UNSUPPORTEDUSDT",
                direction="binance_long_lighter_short",
                entry_exchange="binance",
                exit_exchange="lighter",
                gross_spread_bps=100.0,
                net_spread_bps=80.0,
                funding_diff=0.01,
                recommendation="WATCH",
                details={"buy_exchange": "binance", "sell_exchange": "lighter"},
            ),
        ]
        collectors = {
            "binance": self.DepthCollector(),
            "aster": self.DepthCollector(),
            "lighter": object(),
        }

        entry_check.annotate_entry_check_support(opportunities, collectors)

        self.assertTrue(opportunities[0].details["entry_check_supported"])
        self.assertFalse(opportunities[1].details["entry_check_supported"])


class TestGrvtCollectorFailures(unittest.IsolatedAsyncioTestCase):
    class FailingSession:
        async def post(self, *_args, **_kwargs):
            raise RuntimeError("GRVT instruments unavailable")

    async def test_instrument_fetch_error_reaches_api_health_tracking(self):
        from collectors.grvt import GrvtCollector

        collector = GrvtCollector(Settings().grvt)
        collector._session = self.FailingSession()

        with self.assertRaisesRegex(RuntimeError, "GRVT instruments unavailable"):
            await collector.fetch_markets([])


if __name__ == '__main__':
    unittest.main(verbosity=2)

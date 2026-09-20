"""Public exchange response contracts, based on mainnet samples from 2026-09-20."""
import copy
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from analyzer import analyse_markets
from collectors.factory import create_collector
from config import NotificationSettings, Settings
from entry_check import annotate_entry_check_support, entry_check_supported
from market_state import replace_exchange_snapshot, stamp_market_timestamps
from models import MarketDatum
from notifier import WeChatNotifier


NOW = 1789871500.0
EXCHANGES = {"binance", "variational", "hyperliquid", "aster", "lighter", "arcus", "bulk", "risex"}
ARCUS = {
    "marketDisplayName": "BTC-USD", "baseAsset": "BTC", "quoteAsset": "USD",
    "marketId": 1, "status": "ONLINE", "type": "PERPETUAL",
    "lastTradePrice": "100", "markPrice": "100.1",
    "fundingRate": "0.009", "nextFundingRate": "-0.0000125",
    "nextFundingAt": 1789873200, "volume24h": "10", "volume24hNotional": "1000000",
}
RISEX = {
    "market_id": "5", "config": {"name": "HYPE/USDC", "unlocked": True},
    "base_asset_symbol": "HYPE/USDC", "quote_asset_symbol": "USDC", "active": True,
    "last_price": "100", "mark_price": "100.1", "quote_volume_24h": "1000000",
    "funding_interval": "3600000000000", "next_funding_time": "1789873200000000000",
    "current_funding_rate": "-0.0000125", "funding_rate_8h": "-0.0001",
    "predicted_funding_rate": "0", "reduce_only": False,
}
BULK_MARKET = {"symbol": "SOL-USD", "baseAsset": "SOL", "quoteAsset": "USD", "status": "TRADING"}
BULK_TICKER = {
    "symbol": "SOL-USD", "lastPrice": 100, "markPrice": 100.1,
    "volume": 10, "quoteVolume": 1000000, "fundingRate": -0.0000125,
    "timestamp": int(NOW * 1e9),
}


class Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return copy.deepcopy(self.payload)


class ExchangeClient:
    def __init__(self, key):
        self.key = key
        self.calls = []
        self.market = copy.deepcopy({"arcus": ARCUS, "bulk": BULK_MARKET, "risex": RISEX}[key])
        self.ticker = copy.deepcopy(BULK_TICKER)
        self.book_time = NOW
        self.empty_book = False
        self.fail_book = False
        self.invalid_book = False

    async def get(self, path, params=None):
        self.calls.append((path, params))
        if self.key == "arcus" and path == "/v1/markets":
            return Response({"markets": [self.market]})
        if self.key == "bulk" and path == "/api/v1/exchangeInfo":
            return Response([self.market])
        if self.key == "bulk" and path == "/api/v1/ticker/SOL-USD":
            return Response(self.ticker)
        if self.key == "risex" and path == "/v1/markets":
            if params != {"force_refresh": "true"}:
                raise AssertionError("RISEx live data must bypass the five-minute cache")
            return Response({"data": {"markets": [self.market], "cached_at": str(int(NOW))}})
        if self.fail_book:
            raise RuntimeError(f"{self.key} order book unavailable")
        bids = [] if self.empty_book else [[99, 20], [98, 10]]
        asks = [] if self.empty_book else [[101, 20], [102, 10]]
        if self.invalid_book:
            bids = [[float("nan"), 20]]
        if self.key == "arcus" and path == "/v1/l2OrderBook/BTC-USD":
            return Response({"bids": bids, "asks": asks, "timestamp": int(self.book_time * 1e6)})
        if self.key == "bulk" and path == "/api/v1/l2book":
            if params.get("type") != "l2book" or params.get("coin") != "SOL-USD":
                raise AssertionError("Bulk requires lowercase l2book and native coin")
            return Response({"symbol": "SOL-USD", "updateType": "snapshot",
                             "levels": [[{"px": p, "sz": s, "n": 1} for p, s in side] for side in (bids, asks)],
                             "timestamp": int(self.book_time * 1e9)})
        if self.key == "risex" and path == "/v1/orderbook":
            if str(params.get("market_id")) != "5":
                raise AssertionError("RISEx must use its numeric market ID")
            return Response({"data": {"market_id": "5", "bids": [{"price": str(p), "quantity": str(s)} for p, s in bids],
                                      "asks": [{"price": str(p), "quantity": str(s)} for p, s in asks]}})
        raise AssertionError(f"Unexpected {self.key} request: {path} {params}")


class TestExchangeRegistry(unittest.TestCase):
    def test_stamping_preserves_existing_market_times(self):
        markets = {
            "known": MarketDatum("BTCUSDT", 100, 0, timestamp=NOW - 32),
            "zero": MarketDatum("ETHUSDT", 100, 0, timestamp=0),
            "missing": MarketDatum("SOLUSDT", 100, 0),
        }
        stamp_market_timestamps(markets, NOW)
        self.assertEqual(markets["known"].timestamp, NOW - 32)
        self.assertEqual(markets["zero"].timestamp, 0)
        self.assertEqual(markets["missing"].timestamp, NOW)

    def test_only_requested_eight_exchanges_remain(self):
        self.assertEqual(set(Settings().exchanges), EXCHANGES)

    def test_legacy_config_cannot_restore_removed_exchanges(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "exchanges.json"
            path.write_text(json.dumps({"nado": True, "grvt": True, "ondoperps": True, "backpack": True,
                                        "binance": False, "bulk": False}))
            cfg = Settings(runtime_config_file=str(path))
            cfg.load_runtime_config()
            self.assertEqual(set(cfg.exchanges), EXCHANGES)
            self.assertFalse(cfg.binance.enabled)
            self.assertFalse(cfg.bulk.enabled)
            self.assertTrue(cfg.arcus.enabled)
            self.assertTrue(cfg.risex.enabled)


class TestNewCollectors(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock = patch("time.time", return_value=NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.sleep = patch("collectors.decorators.asyncio.sleep", new_callable=AsyncMock)
        self.sleep.start()
        self.addCleanup(self.sleep.stop)

    def collector(self, key):
        cfg = Settings()
        self.assertIn(key, set(cfg.exchanges), f"{key} collector is not registered")
        with patch("collectors.session_pool.session_pool.get_session"):
            collector = create_collector(key, cfg.exchanges[key])
        collector._client = ExchangeClient(key)
        return collector

    async def test_live_fields_normalize_without_multiplying_hourly_funding(self):
        for key, symbol in [("arcus", "BTCUSDT"), ("bulk", "SOLUSDT"), ("risex", "HYPEUSDT")]:
            with self.subTest(exchange=key):
                collector = self.collector(key)
                markets = await collector.fetch_markets([symbol])
                self.assertEqual(set(markets), {symbol})
                item = markets[symbol]
                self.assertEqual(item.price, 100)
                self.assertEqual(item.volume_24h, 1000000)
                self.assertEqual(item.funding_rate, -0.0000125)
                self.assertEqual(item.native_interval_hours, 1)
                self.assertEqual(item.next_funding_time, 1789873200000)
                self.assertEqual((item.best_bid, item.best_ask), (99, 101))

    async def test_slow_batch_cannot_refresh_early_market_timestamps(self):
        for key, symbol in [("arcus", "BTCUSDT"), ("bulk", "SOLUSDT"), ("risex", "HYPEUSDT")]:
            with self.subTest(exchange=key):
                with patch("time.time", return_value=NOW) as clock:
                    collector = self.collector(key)
                    gather = collector._gather_with_semaphore

                    async def delayed_batch(coros):
                        results = await gather(coros)
                        clock.return_value = NOW + 32
                        return results

                    with patch.object(collector, "_gather_with_semaphore", side_effect=delayed_batch):
                        markets = await collector.fetch_markets([symbol])
                    data = {"binance": {symbol: MarketDatum(
                        symbol, 103, 0, volume_24h=1000000, timestamp=NOW + 32,
                        exchange="Binance", best_bid=102, best_ask=104,
                    )}}
                    replace_exchange_snapshot(data, {}, key=key, data=markets,
                                              fetched_at=NOW + 32, error=None)
                    opportunities, reasons, _ = analyse_markets([symbol], data, Settings())
                    self.assertEqual(opportunities, [])
                    self.assertEqual(reasons[symbol], "STALE_DATA")
                    self.assertEqual(markets[symbol].timestamp, NOW)

    async def test_market_metadata_age_includes_wait_for_books(self):
        for key, symbol in [("arcus", "BTCUSDT"), ("risex", "HYPEUSDT")]:
            with self.subTest(exchange=key), patch("time.time", return_value=NOW) as clock:
                collector = self.collector(key)
                get = collector._client.get

                async def delayed_metadata(path, params=None):
                    response = await get(path, params)
                    if path == "/v1/markets":
                        clock.return_value = NOW + 32
                        collector._client.book_time = NOW + 32
                    return response

                with patch.object(collector._client, "get", side_effect=delayed_metadata):
                    markets = await collector.fetch_markets([symbol])
                stamp_market_timestamps(markets, NOW + 32)
                self.assertEqual(markets[symbol].timestamp, NOW)

    async def test_bulk_preserves_ticker_source_time_after_book_request(self):
        collector = self.collector("bulk")
        collector._client.ticker["timestamp"] = int((NOW - 29) * 1e9)
        markets = await collector.fetch_markets(["SOLUSDT"])
        stamp_market_timestamps(markets, NOW + 2)
        self.assertEqual(markets["SOLUSDT"].timestamp, NOW - 29)

    async def test_zero_and_missing_funding_are_distinct(self):
        for key, symbol, field in [("arcus", "BTCUSDT", "nextFundingRate"), ("bulk", "SOLUSDT", "fundingRate"),
                                   ("risex", "HYPEUSDT", "current_funding_rate")]:
            with self.subTest(exchange=key):
                collector = self.collector(key)
                record = collector._client.ticker if key == "bulk" else collector._client.market
                record[field] = "0"
                self.assertEqual((await collector.fetch_markets([symbol]))[symbol].funding_rate, 0)
                record.pop(field)
                self.assertIsNone((await collector.fetch_markets([symbol]))[symbol].funding_rate)

    async def test_inactive_and_unrequested_markets_do_not_fetch_books(self):
        for key in ["arcus", "bulk", "risex"]:
            with self.subTest(exchange=key):
                collector = self.collector(key)
                self.assertEqual(await collector.fetch_markets(["NOTLISTEDUSDT"]), {})
                if key == "risex":
                    collector._client.market["active"] = False
                else:
                    collector._client.market["status"] = "HALTED"
                self.assertEqual(await collector.fetch_markets([]), {})
                self.assertFalse(any("book" in path.lower() for path, _ in collector._client.calls))

    async def test_symbol_overrides_and_exclusions_apply_to_market_and_depth(self):
        for key, native in [("arcus", "BTC-USD"), ("bulk", "SOL-USD"), ("risex", "HYPE/USDC")]:
            with self.subTest(exchange=key):
                collector = self.collector(key)
                collector.settings.symbol_overrides[native] = "CUSTOMUSDT"
                self.assertIn("CUSTOMUSDT", await collector.fetch_markets(["CUSTOMUSDT"]))
                self.assertEqual((await collector.fetch_order_book("CUSTOMUSDT", 100))["bids"][0], [99, 20])
                collector.settings.excluded_symbols.add(native)
                self.assertEqual(await collector.fetch_markets([]), {})

    async def test_books_support_manual_entry_including_before_first_scan(self):
        collectors = {}
        for key, symbol in [("arcus", "BTCUSDT"), ("bulk", "SOLUSDT"), ("risex", "HYPEUSDT")]:
            collector = self.collector(key)
            book = await collector.fetch_order_book(symbol, 100)
            self.assertEqual(book["bids"], [[99, 20], [98, 10]])
            self.assertEqual(book["asks"], [[101, 20], [102, 10]])
            collectors[key] = collector
        self.assertTrue(entry_check_supported("arcus", "bulk", collectors))
        self.assertTrue(entry_check_supported("bulk", "risex", collectors))

    async def test_empty_book_is_not_synthesized_from_mark_price(self):
        for key, symbol in [("arcus", "BTCUSDT"), ("bulk", "SOLUSDT"), ("risex", "HYPEUSDT")]:
            collector = self.collector(key)
            collector._client.empty_book = True
            book = await collector.fetch_order_book(symbol, 100)
            self.assertEqual(book["bids"], [])
            self.assertEqual(book["asks"], [])
            market = (await collector.fetch_markets([symbol]))[symbol]
            self.assertIsNone(market.best_bid)
            self.assertIsNone(market.best_ask)

    async def test_upstream_book_failure_propagates(self):
        for key in ["arcus", "bulk", "risex"]:
            collector = self.collector(key)
            collector._client.fail_book = True
            with self.assertRaisesRegex(RuntimeError, f"{key} order book unavailable"):
                await collector.fetch_markets([])

    async def test_invalid_book_numbers_fail_instead_of_becoming_opportunities(self):
        for key, symbol in [("arcus", "BTCUSDT"), ("bulk", "SOLUSDT"), ("risex", "HYPEUSDT")]:
            collector = self.collector(key)
            collector._client.invalid_book = True
            with self.assertRaises(ValueError):
                await collector.fetch_order_book(symbol, 100)

    async def test_source_book_timestamps_are_not_treated_as_fresh_receipt_times(self):
        for key, symbol in [("arcus", "BTCUSDT"), ("bulk", "SOLUSDT")]:
            collector = self.collector(key)
            collector._client.book_time = NOW - 60
            with self.assertRaisesRegex(ValueError, "[Ss]tale"):
                await collector.fetch_order_book(symbol, 100)

    async def test_manual_requote_rejects_source_quote_expiring_during_other_leg(self):
        import api

        for key, symbol in [("arcus", "BTCUSDT"), ("bulk", "SOLUSDT")]:
            with self.subTest(exchange=key), patch("time.time", return_value=NOW) as clock:
                collector = self.collector(key)
                collector._client.book_time = NOW - 1.9
                reference = AsyncMock()

                async def delayed_book(*_args):
                    clock.return_value = NOW + 0.9
                    return {"bids": [[104, 100]], "asks": [[106, 100]]}

                reference.fetch_order_book.side_effect = delayed_book
                with patch.object(api, "collectors_hub", {key: collector, "binance": reference}), \
                        patch.object(api, "settings", Settings()):
                    result = await api.evaluate_manual_entry(symbol, key, "binance")
                self.assertEqual(result["status"], "unavailable")
                self.assertEqual(result["reason"], "STALE_QUOTE")
                self.assertAlmostEqual(result["quote_age_ms"], 2800, delta=1)

    async def test_manual_requote_checks_skew_between_source_times(self):
        import api

        for key, symbol in [("arcus", "BTCUSDT"), ("bulk", "SOLUSDT")]:
            with self.subTest(exchange=key):
                collector = self.collector(key)
                collector._client.book_time = NOW - 1.5
                reference = AsyncMock()
                reference.fetch_order_book.return_value = {"bids": [[104, 100]], "asks": [[106, 100]]}
                with patch.object(api, "collectors_hub", {key: collector, "binance": reference}), \
                        patch.object(api, "settings", Settings()):
                    result = await api.evaluate_manual_entry(symbol, key, "binance")
                self.assertEqual(result["status"], "unavailable")
                self.assertEqual(result["reason"], "LEG_SKEW")
                self.assertAlmostEqual(result["leg_skew_ms"], 1500, delta=1)

    async def test_dashboard_expires_cached_routes_using_market_times(self):
        import api

        for key, symbol in [("arcus", "BTCUSDT"), ("bulk", "SOLUSDT"), ("risex", "HYPEUSDT")]:
            for partial in (False, True):
                with self.subTest(exchange=key, partial=partial), patch("time.time", return_value=NOW) as clock:
                    markets = await self.collector(key).fetch_markets([symbol])
                    if partial:
                        fresh = copy.copy(markets[symbol])
                        fresh.symbol, fresh.timestamp = "FRESHUSDT", NOW + 29
                        markets[fresh.symbol] = fresh
                    references = {sym: MarketDatum(
                        sym, 105, 0.001, volume_24h=1000000, timestamp=NOW + 29,
                        exchange="Binance", best_bid=104, best_ask=106,
                    ) for sym in markets}
                    source = {"raw_exchanges_data": {"binance": references}, "exchange_status": {}}
                    replace_exchange_snapshot(source["raw_exchanges_data"], source["exchange_status"],
                                              key=key, data=markets, fetched_at=NOW + 29, error=None)
                    cfg = Settings()
                    cfg.tracked_symbols = []
                    cfg.blacklist_symbols = []
                    cfg.schedule.market_stale_seconds = 30
                    with patch.object(api, "latest_data", source), patch.object(api, "settings", cfg):
                        clock.return_value = NOW + 29
                        await api.perform_analysis()
                        original = copy.deepcopy(source)
                        self.assertEqual(len(source["opportunities"]), 4 if partial else 2)
                        clock.return_value = NOW + 31
                        snapshot = await api.get_dashboard_data()
                        self.assertIsNone(snapshot["markets"][symbol][key])
                        self.assertEqual(snapshot["reasons"][symbol], "STALE_DATA")
                        self.assertEqual(len(snapshot["opportunities"]), 2 if partial else 0)
                        self.assertEqual(snapshot["exchange_status"][key]["state"], "fresh" if partial else "stale")
                        if partial:
                            self.assertIsNotNone(snapshot["markets"]["FRESHUSDT"][key])
                            self.assertTrue(all(o["symbol"] == "FRESHUSDT" for o in snapshot["opportunities"]))
                        self.assertEqual(source, original)
                        cfg.schedule.market_stale_seconds = 0
                        snapshot = await api.get_dashboard_data()
                        self.assertIsNotNone(snapshot["markets"][symbol][key])
                        self.assertEqual(len(snapshot["opportunities"]), 4 if partial else 2)

    async def test_stale_bulk_ticker_is_rejected(self):
        collector = self.collector("bulk")
        collector._client.ticker["timestamp"] = int((NOW - 300) * 1e9)
        with self.assertRaisesRegex(ValueError, "[Ss]tale"):
            await collector.fetch_markets([])

    async def test_risex_interval_comes_from_nanoseconds(self):
        collector = self.collector("risex")
        collector._client.market["funding_interval"] = "14400000000000"
        self.assertEqual((await collector.fetch_markets([]))["HYPEUSDT"].native_interval_hours, 4)
        collector._client.market["funding_interval"] = "0"
        with self.assertRaises(ValueError):
            await collector.fetch_markets([])

    async def test_risex_cannot_open_on_locked_reduce_only_or_post_only_markets(self):
        for field in ("unlocked", "reduce_only", "post_only"):
            collector = self.collector("risex")
            if field == "unlocked":
                collector._client.market["config"][field] = False
            else:
                collector._client.market[field] = True
            self.assertEqual(await collector.fetch_markets([]), {})

    async def test_collector_close_releases_its_own_pooled_session(self):
        for key in ("arcus", "bulk", "risex"):
            collector = self.collector(key)
            self.assertTrue(callable(getattr(collector, "aclose", None)))
            with patch("collectors.session_pool.session_pool.close_session", new_callable=AsyncMock) as close:
                await collector.aclose()
                close.assert_awaited_once_with(key)

    async def test_new_exchange_routes_reach_analysis_requote_and_mock_notification(self):
        import api

        cfg = Settings()
        for key, symbol in (("arcus", "BTCUSDT"), ("bulk", "SOLUSDT"), ("risex", "HYPEUSDT")):
            with self.subTest(exchange=key):
                collector = self.collector(key)
                markets = await collector.fetch_markets([symbol])
                stamp_market_timestamps(markets, NOW)
                reference = MarketDatum(symbol=symbol, price=105, funding_rate=0.001, volume_24h=1000000,
                                        timestamp=NOW, exchange="Binance", native_interval_hours=8,
                                        best_bid=104, best_ask=106)
                opportunities, _, _ = analyse_markets([symbol], {key: markets, "binance": {symbol: reference}}, cfg)
                self.assertEqual(len(opportunities), 2)
                reference_collector = AsyncMock()
                reference_collector.fetch_order_book.return_value = {"bids": [[104, 100]], "asks": [[106, 100]]}
                collectors = {key: collector, "binance": reference_collector}
                annotate_entry_check_support(opportunities, collectors)
                self.assertTrue(all(item.details["entry_check_supported"] for item in opportunities))
                with patch.object(api, "collectors_hub", collectors), patch.object(api, "settings", cfg):
                    result = await api.evaluate_manual_entry(symbol, key, "binance")
                self.assertEqual(result["status"], "actionable")
                self.assertEqual(result["buy_vwap"], 101)
                self.assertEqual(result["sell_vwap"], 104)
                with patch.object(WeChatNotifier, "_load_state", return_value={}), \
                        patch.object(WeChatNotifier, "_save_state"), \
                        patch.object(WeChatNotifier, "_post", new_callable=AsyncMock, return_value=True) as post:
                    notifier = WeChatNotifier(NotificationSettings(
                        wechat_webhook="https://example.invalid/test-only", notify_minute_offset=0,
                        notification_exchanges=[],
                    ))
                    await notifier.send(opportunities)
                    post.assert_awaited_once()
                    self.assertIn(key, post.await_args.args[1].lower())

    async def test_api_lists_only_the_eight_supported_exchanges(self):
        import httpx
        import api

        with patch.object(api, "settings", Settings()):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://test") as client:
                response = await client.get("/api/exchanges")
        self.assertEqual(response.status_code, 200)
        self.assertEqual({item["name"] for item in response.json()}, EXCHANGES)

    async def test_failed_batch_does_not_leave_other_requests_running(self):
        collector = self.collector("arcus")
        started = asyncio.Event()
        release = asyncio.Event()
        running = []

        async def fail():
            await started.wait()
            raise RuntimeError("upstream failed")

        async def slow():
            running.append(asyncio.current_task())
            started.set()
            await release.wait()

        try:
            with self.assertRaisesRegex(RuntimeError, "upstream failed"):
                await collector._gather_with_semaphore([fail(), slow()])
            self.assertTrue(all(task.done() for task in running), "failed batch leaked a running request")
        finally:
            release.set()
            await asyncio.gather(*running, return_exceptions=True)

    async def test_cancelled_batch_closes_queued_coroutines(self):
        import inspect

        collector = self.collector("arcus")
        started = asyncio.Event()

        async def slow():
            started.set()
            await asyncio.Event().wait()

        coroutines = [slow(), slow(), slow()]
        batch = asyncio.create_task(collector._gather_with_semaphore(coroutines, limit=1))
        await started.wait()
        batch.cancel()
        try:
            with self.assertRaises(asyncio.CancelledError):
                await batch
            self.assertTrue(all(inspect.getcoroutinestate(coro) == inspect.CORO_CLOSED for coro in coroutines))
        finally:
            for coro in coroutines:
                coro.close()


if __name__ == "__main__":
    unittest.main()

"""TxFlow quota handling across market scans and manual depth requests."""
import asyncio
import copy
import unittest
from unittest.mock import AsyncMock, patch

from curl_cffi.requests.exceptions import HTTPError

from collectors.factory import create_collector
from config import Settings


NOW = 1792832400.0


class Response:
    def __init__(self, payload=None, status=200, retry_after=None):
        self.status_code = status
        self.headers = {} if retry_after is None else {'Retry-After': retry_after}
        self.payload = payload or {'success': True, 'code': 0, 'data': {}}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise HTTPError(f'HTTP Error {self.status_code}', response=self)

    def json(self):
        return copy.deepcopy(self.payload)


class TxflowRateLimitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = 100.0
        with patch('collectors.session_pool.session_pool.get_session'):
            self.collector = create_collector('txflow', Settings().txflow)
        self.collector._client = AsyncMock()
        self.collector._symbols = {
            symbol: {'index': index, 'name': native, 'baseCurrency': base, 'quoteCurrency': 'USDC'}
            for symbol, index, native, base in [('BTCUSDT', 1, 'BTC-USDC', 'BTC'),
                                                ('ETHUSDT', 2, 'ETH-USDC', 'ETH')]
        }
        self.monotonic = patch('collectors.txflow.time.monotonic', side_effect=lambda: self.now)
        self.monotonic.start()
        self.addCleanup(self.monotonic.stop)
        self.wall = patch('collectors.txflow.time.time', return_value=NOW)
        self.wall.start()
        self.addCleanup(self.wall.stop)

        async def sleep(seconds):
            self.now += seconds
        self.sleep = patch('collectors.txflow.asyncio.sleep', new=AsyncMock(side_effect=sleep))
        self.sleeper = self.sleep.start()
        self.addCleanup(self.sleep.stop)

    async def test_retry_after_blocks_scan_and_manual_requests_until_the_deadline(self):
        client = self.collector._client
        client.post.side_effect = [Response(status=429, retry_after='7'), Response()]
        with self.assertRaisesRegex(ValueError, 'rate limited'):
            await self.collector._info('l2Book', coin='1')
        deadline = self.now + 7
        for request in (lambda: self.collector._info('webData2'),
                        lambda: self.collector.fetch_order_book('ETHUSDT', 1)):
            with self.assertRaisesRegex(ValueError, 'rate limited'):
                await request()
        self.assertEqual(client.post.await_count, 1)
        self.sleeper.assert_not_awaited()
        self.now = deadline - .001
        with self.assertRaisesRegex(ValueError, 'rate limited'):
            await self.collector._info('perpMeta')
        self.now = deadline
        self.assertEqual(await self.collector._info('webData2'), {})
        self.assertEqual(client.post.await_count, 2)

    async def test_invalid_retry_after_still_has_a_bounded_shared_cooldown(self):
        for header in (None, 'bad', 'nan', 'inf', '-5', '0'):
            with self.subTest(header=header):
                self.collector._client.post.return_value = Response(status=429, retry_after=header)
                self.now += 10
                with self.assertRaisesRegex(ValueError, 'rate limited'):
                    await self.collector._info('webData2')
                count = self.collector._client.post.await_count
                with self.assertRaisesRegex(ValueError, 'rate limited'):
                    await self.collector._info('perpMeta')
                self.assertEqual(self.collector._client.post.await_count, count)

    async def test_a_long_cooldown_does_not_sleep_or_retry_the_entire_scan(self):
        self.collector._client.post.return_value = Response(status=429, retry_after='3600')
        self.collector._symbols = {}
        with patch.object(self.collector, '_fetch_snapshot') as snapshot:
            with self.assertRaisesRegex(ValueError, 'rate limited'):
                await self.collector.fetch_markets([])
        self.assertEqual(self.collector._client.post.await_count, 1)
        self.sleeper.assert_not_awaited()
        snapshot.assert_not_called()

    async def test_requests_share_a_twenty_per_second_budget(self):
        starts = []
        async def post(*_args, **_kwargs):
            starts.append(self.now)
            return Response()
        self.collector._client.post.side_effect = post
        await asyncio.gather(*(self.collector._info(kind) for kind in ('perpMeta', 'webData2', 'l2Book')))
        self.assertTrue(all(later-earlier >= .05-1e-9 for earlier, later in zip(starts, starts[1:])))

    async def test_inflight_success_does_not_clear_a_later_rate_limit(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def post(_path, json):
            if json['type'] == 'perpMeta':
                entered.set()
                await release.wait()
                return Response()
            return Response(status=429, retry_after='4')
        self.collector._client.post.side_effect = post
        first = asyncio.create_task(self.collector._info('perpMeta'))
        try:
            await entered.wait()
            with self.assertRaisesRegex(ValueError, 'rate limited'):
                await self.collector._info('webData2')
            release.set()
            await first
            with self.assertRaisesRegex(ValueError, 'rate limited'):
                await self.collector._info('l2Book', coin='1')
            self.assertEqual(self.collector._client.post.await_count, 2)
        finally:
            release.set()
            await first

    async def test_a_queued_request_rechecks_cooldown_after_waiting_for_its_slot(self):
        entered, respond, sleeping, wake = (asyncio.Event() for _ in range(4))
        async def post(*_args, **_kwargs):
            entered.set()
            await respond.wait()
            return Response(status=429, retry_after='8')
        async def sleep(seconds):
            sleeping.set()
            await wake.wait()
            self.now += seconds
        self.collector._client.post.side_effect = post
        self.sleeper.side_effect = sleep
        first = asyncio.create_task(self.collector._info('l2Book', coin='1'))
        second = None
        try:
            await entered.wait()
            second = asyncio.create_task(self.collector._info('webData2'))
            await sleeping.wait()
            respond.set()
            with self.assertRaisesRegex(ValueError, 'rate limited'):
                await first
            wake.set()
            with self.assertRaisesRegex(ValueError, 'rate limited'):
                await second
            self.assertEqual(self.collector._client.post.await_count, 1)
        finally:
            respond.set()
            wake.set()
            await asyncio.gather(*(task for task in (first, second) if task), return_exceptions=True)

    async def test_a_rate_limited_book_preserves_other_books_and_current_funding(self):
        stats = {'assetCtxs': [{'instrumentId': index, 'lastPrice': '100', 'dayNtlVlm': '1000000'} for index in (1, 2)]}
        contexts = [{'coin': str(index), 'ctx': {'funding': '0.001', 'markPx': '100',
            'blockTime': NOW*1000, 'nextFundingTime': (NOW+3600)*1000, 'prevFundingTime': NOW*1000}} for index in (1, 2)]
        kinds = []
        async def post(_path, json):
            kinds.append(json['type'])
            if json['type'] == 'webData2':
                return Response({'success': True, 'code': 0, 'data': stats})
            if json['coin'] == '2':
                return Response(status=429, retry_after='12')
            return Response({'success': True, 'code': 0, 'data': {'coin': 'BTC-USDC', 'time': NOW*1000,
                'levels': [[{'px': '99', 'sz': '2'}], [{'px': '101', 'sz': '3'}]]}})
        self.collector._client.post.side_effect = post
        with patch.object(self.collector, '_load_markets', new_callable=AsyncMock), \
                patch.object(self.collector, '_fetch_snapshot', return_value=contexts):
            markets = await self.collector.fetch_markets([])
        self.assertEqual(set(markets), {'BTCUSDT', 'ETHUSDT'})
        self.assertEqual(markets['BTCUSDT'].best_bid, 99)
        self.assertIsNone(markets['ETHUSDT'].best_bid)
        self.assertIsNone(markets['ETHUSDT'].best_ask)
        self.assertEqual(markets['ETHUSDT'].timestamp, NOW)
        self.assertAlmostEqual(markets['ETHUSDT'].funding_rate, .00001)
        self.assertEqual(kinds.count('webData2'), 1)

    async def test_recovery_does_not_refresh_an_old_book_timestamp(self):
        self.collector._client.post.side_effect = [Response(status=429, retry_after='3'),
            Response({'success': True, 'code': 0, 'data': {'coin': 'BTC-USDC', 'time': (NOW-4)*1000,
                'levels': [[{'px': '99', 'sz': '2'}], [{'px': '101', 'sz': '3'}]]}})]
        with self.assertRaisesRegex(ValueError, 'rate limited'):
            await self.collector.fetch_order_book('BTCUSDT', 1)
        self.now += 3
        with self.assertRaisesRegex(ValueError, 'stale'):
            await self.collector.fetch_order_book('BTCUSDT', 1)

    async def test_non_rate_limit_errors_propagate_with_the_response(self):
        response = Response(status=500)
        self.collector._client.post.return_value = response
        with self.assertRaises(HTTPError) as caught:
            await self.collector._info('webData2')
        self.assertIs(caught.exception.response, response)

    async def test_transient_server_errors_retain_the_existing_scan_retry_limit(self):
        response = Response(status=500)
        self.collector._symbols = {}
        self.collector._client.post.return_value = response
        with self.assertRaises(HTTPError) as caught:
            await self.collector.fetch_markets([])
        self.assertIs(caught.exception.response, response)
        self.assertEqual(self.collector._client.post.await_count, 3)

    async def test_a_later_shorter_hint_cannot_shorten_the_shared_cooldown(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def post(_path, json):
            if json['type'] == 'perpMeta':
                entered.set()
                await release.wait()
                return Response(status=429, retry_after='1')
            return Response(status=429, retry_after='9')
        self.collector._client.post.side_effect = post
        first = asyncio.create_task(self.collector._info('perpMeta'))
        try:
            await entered.wait()
            with self.assertRaisesRegex(ValueError, 'rate limited'):
                await self.collector._info('webData2')
            release.set()
            with self.assertRaisesRegex(ValueError, 'rate limited'):
                await first
            self.now += 2
            with self.assertRaisesRegex(ValueError, 'rate limited'):
                await self.collector._info('l2Book', coin='1')
            self.assertEqual(self.collector._client.post.await_count, 2)
        finally:
            release.set()
            await asyncio.gather(first, return_exceptions=True)


if __name__ == '__main__':
    unittest.main()

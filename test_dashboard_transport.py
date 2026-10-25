"""Dashboard HTTP transport preserves data and status across compression."""
import unittest
from unittest.mock import patch

import httpx


class DashboardTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_large_snapshots_are_compressed_without_changing_the_json_contract(self):
        import api
        state = {'markets': {f'TEST{i}USDT': {'binance': {'price': 100, 'funding_rate': None,
                 'volume_24h': 1000000, 'native_interval': 8, 'quote_currency': 'USD'}} for i in range(1500)},
                 'opportunities': [], 'reasons': {}, 'symbol_max_intervals': {},
                 'last_update': 'test', 'data_version': 1, 'exchange_status': {}, 'raw_exchanges_data': {}}
        with patch.object(api, 'latest_data', state):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url='http://test') as client:
                plain = await client.get('/api/data', headers={'Accept-Encoding': 'identity'})
                compressed = await client.get('/api/data', headers={'Accept-Encoding': 'gzip'})
        self.assertEqual(plain.status_code, 200)
        self.assertEqual(compressed.status_code, 200)
        self.assertNotIn('content-encoding', plain.headers)
        self.assertEqual(compressed.headers.get('content-encoding'), 'gzip')
        self.assertIn('Accept-Encoding', compressed.headers.get('vary', ''))
        self.assertEqual(plain.json(), compressed.json())
        self.assertLess(int(compressed.headers['content-length']), len(plain.content) / 3)

    async def test_small_empty_snapshots_remain_valid(self):
        import api
        state = {'markets': {}, 'opportunities': [], 'last_update': 'Never'}
        with patch.object(api, 'latest_data', state):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url='http://test') as client:
                response = await client.get('/api/data', headers={'Accept-Encoding': 'gzip'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['markets'], {})
        self.assertNotIn('content-encoding', response.headers)

    async def test_server_errors_keep_the_http_failure_status(self):
        import api
        with patch.object(api, 'dashboard_snapshot', side_effect=RuntimeError('simulated dashboard failure')):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app,
                    raise_app_exceptions=False), base_url='http://test') as client:
                response = await client.get('/api/data', headers={'Accept-Encoding': 'gzip'})
        self.assertEqual(response.status_code, 500)


if __name__ == '__main__':
    unittest.main()

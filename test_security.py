"""Offline boundary and regression tests. No UCSD credentials or live login."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, MagicMock

import scheduler as app
from private_state import protect, unprotect
from ucsd_session import UCSDSession, SessionError


class LocalBoundaries(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        app.SAVED_DATA = str(Path(cls.tmp.name)/'saved.json')
        cls.server = app.ThreadingHTTPServer(('127.0.0.1', 0), app.SchedulerHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def call(self, method, path, data=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        hdr = dict(headers or {})
        if isinstance(data, dict):
            data = json.dumps(data)
            hdr.setdefault('Content-Type', 'application/json')
        conn.request(method, path, data, hdr)
        r = conn.getresponse()
        result = r.status, dict(r.getheaders()), r.read()
        conn.close()
        return result

    def test_01_signed_out(self):
        status, _, body = self.call('GET', '/api/terms')
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body)['code'], 'AUTH_REQUIRED')

    def test_02_cross_site_and_rebinding(self):
        for headers in ({'Origin': 'https://evil.example'}, {'Host': 'evil.example'}, {'Sec-Fetch-Site': 'cross-site'}):
            self.assertEqual(self.call('POST', '/api/auth/login', headers=headers)[0], 403)
        self.assertEqual(self.call('GET', '/api/auth/login')[0], 405)
        self.assertEqual(self.call('GET', '/api/schedules')[0], 405)

    def test_03_input_and_private_files(self):
        self.assertEqual(self.call('POST', '/api/state', '[]', {'Content-Type': 'application/json'})[0], 400)
        self.assertEqual(self.call('POST', '/api/state', '{}', {'Content-Type': 'text/plain'})[0], 400)
        self.assertEqual(self.call('POST', '/api/state', headers={'Content-Length': '99999999'})[0], 400)
        self.assertEqual(self.call('GET', '/saved_data.json')[0], 404)
        self.assertEqual(self.call('GET', '/../../auth-state.dpapi')[0], 404)

    def test_04_concurrent_save_preserves_conversations(self):
        def save(i):
            return self.call('POST', '/api/conversations', {'conversation': {'id': str(i), 'title': str(i)}})[0]
        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(list(pool.map(save, range(20))), [200]*20)
        saved = json.loads(Path(app.SAVED_DATA).read_text(encoding='utf-8'))
        self.assertEqual(len(saved['conversations']), 20)

    def test_05_term_drafts(self):
        for term in ('FA26', 'WI27'):
            self.assertEqual(self.call('POST', '/api/state', {'state': {'term': term, 'courses': [{'module_code': term}]}})[0], 200)
        d = json.loads(self.call('GET', '/api/state?term=FA26')[2])
        self.assertEqual(d['state']['courses'][0]['module_code'], 'FA26')
        d = json.loads(self.call('GET', '/api/state?term=WI27')[2])
        self.assertEqual(d['state']['courses'][0]['module_code'], 'WI27')

    def test_06_security_headers(self):
        status, headers, body = self.call('GET', '/')
        self.assertEqual(status, 200)
        self.assertIn("script-src 'sha256-", headers['Content-Security-Policy'])
        self.assertIn("connect-src 'self'", headers['Content-Security-Policy'])
        self.assertEqual(headers['X-Frame-Options'], 'DENY')

    def test_07_corrupt_save_not_overwritten(self):
        Path(app.SAVED_DATA).write_text('{broken', encoding='utf-8')
        try:
            self.assertEqual(self.call('POST', '/api/state', {'state': {'term': 'WI27'}})[0], 500)
            self.assertEqual(Path(app.SAVED_DATA).read_text(encoding='utf-8'), '{broken')
        finally:
            Path(app.SAVED_DATA).unlink()

    def test_08_computation_limit(self):
        models = [{'module_code': str(i), 'family_combos': {'1': [[{'type': 'lecture'}]]*150}} for i in range(3)]
        with self.assertRaises(ValueError):
            app.generate_schedules(models, {}, {})

    def test_09_dpapi(self):
        secret = b'fake-cookie-not-a-real-credential'
        encrypted = protect(secret)
        self.assertNotIn(secret, encrypted)
        self.assertEqual(unprotect(encrypted), secret)

    def test_10_actual_exam_dates(self):
        a = {'day': 'M', 'date': '2027-03-15', 'start': 600, 'end': 660}
        b = dict(a, date='2027-03-22')
        self.assertFalse(app._overlap(a, b))
        self.assertTrue(app._overlap(a, dict(a, day='')))
        self.assertTrue(app._overlap(a, dict(a, date='', day='M')))
        self.assertFalse(app._overlap(a, dict(a, date='', day='T')))
        self.assertFalse(app._overlap(a, dict(a, tba=True)))


class SessionBoundaries(unittest.IsolatedAsyncioTestCase):
    async def test_allowed_origins_and_response_disposal(self):
        session = UCSDSession()
        with self.assertRaises(SessionError):
            await session._fetch('GET', 'https://evil.example/api/v1/planner/terms', None, 1)
        with self.assertRaises(SessionError):
            await session._fetch('POST', '/api/v1/account/delete', None, 1)
        response = MagicMock(status=200, ok=True, headers={'content-type': 'application/json'})
        response.json = AsyncMock(return_value={'terms': []})
        response.dispose = AsyncMock()
        context = MagicMock()
        context.request.fetch = AsyncMock(return_value=response)
        session._context = context
        self.assertEqual(await session._fetch('GET', '/api/v1/planner/terms', None, 1), {'terms': []})
        self.assertEqual(context.request.fetch.call_args.kwargs['max_redirects'], 0)
        response.dispose.assert_awaited_once()
        for code in (302, 401, 403):
            response.status = code
            with self.assertRaises(SessionError):
                await session._fetch('GET', '/api/v1/planner/terms', None, 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)

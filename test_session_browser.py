"""Exercise the real session actor and cookie jar against a local fake UCSD."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import threading
import time
import ucsd_session as auth
from private_state import protect


class Site(BaseHTTPRequestHandler):
    issue_cookie = True
    expired = False
    starts = 0
    reject_catalog = False
    def log_message(self, *args): pass
    def do_GET(self):
        if self.path == '/start':
            Site.starts += 1
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            if self.issue_cookie:
                Site.expired = False
                self.send_header('Set-Cookie', 'fake_ucsd_session=test_only; Max-Age=3600; HttpOnly; Path=/; SameSite=Lax')
            self.end_headers()
            self.wfile.write(b'<title>Local test login</title>')
        elif self.path in ('/api/v1/planner/terms', '/auth/session'):
            ok = not self.expired and 'fake_ucsd_session=test_only' in self.headers.get('Cookie', '')
            self.send_response(200 if ok else 401)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            data = {'csrf_token': 'fake_csrf'} if self.path == '/auth/session' else {'terms': [{'term_code': 'WI27'}]}
            self.wfile.write(json.dumps(data if ok else {'detail': 'Sign in to continue'}).encode())
        else:
            self.send_error(404)

    def do_POST(self):
        expired = self.expired or self.reject_catalog
        ok = not expired and self.path == '/api/v1/catalog/courses/search' and self.headers.get('X-CSRF-Token') == 'fake_csrf' and 'fake_ucsd_session=test_only' in self.headers.get('Cookie', '')
        self.rfile.read(int(self.headers.get('Content-Length', '0')))
        self.send_response(200 if ok else 401 if expired else 403)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps({'courses': [{'module_code': 'CCE-001'}]} if ok else {'detail': 'Request verification failed'}).encode())


def wait_login(session):
    deadline = time.monotonic()+30
    while time.monotonic()<deadline:
        status=session.status()
        if status['state']=='authenticated': return
        if status['state']=='error': raise AssertionError(status)
        time.sleep(.1)
    raise AssertionError(session.status())


def run():
    server=ThreadingHTTPServer(('127.0.0.1',0),Site)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    auth.BASE=f'http://127.0.0.1:{server.server_port}'
    with tempfile.TemporaryDirectory() as td:
        state=Path(td)/'state.dpapi'
        state.write_bytes(protect(json.dumps({'cookies': [{
            'name': 'fake_ucsd_session', 'value': 'test_only', 'domain': '127.0.0.1',
            'path': '/', 'expires': time.time() + 3600, 'httpOnly': True,
            'secure': False, 'sameSite': 'Lax',
        }, {
            'name': 'session_only', 'value': 'test_only', 'domain': '127.0.0.1',
            'path': '/', 'expires': -1, 'httpOnly': True,
            'secure': False, 'sameSite': 'Lax',
        }], 'origins': []}).encode()))
        Site.issue_cookie = False
        session=auth.UCSDSession(state)
        session._headless=True
        try:
            session.login()
            wait_login(session)
            assert session._background, 'Authenticated browser must switch to background'
            assert not state.exists(), 'Retire the legacy snapshot after successful migration'
            assert session.profile_path.is_dir()
            async def remember_browser():
                assert any(c["name"] == "session_only" for c in await session._context.cookies())
                await session._context.pages[0].goto(auth.BASE + '/start')
                await session._context.pages[0].evaluate("localStorage.setItem('test-device', 'remembered')")
            session._submit(remember_browser).result(timeout=5)
            assert session.request('GET','/api/v1/planner/terms')['terms'][0]['term_code']=='WI27'
            assert session.request('POST','/api/v1/catalog/courses/search', {'term_code': 'WI27'})['courses'][0]['module_code']=='CCE-001'
            session._csrf_token='stale_token'
            assert session.request('POST','/api/v1/catalog/courses/search', {'term_code': 'WI27'})['courses']
            Site.issue_cookie = True
            Site.expired = True
            starts = Site.starts
            with ThreadPoolExecutor(max_workers=2) as pool:
                searches = [pool.submit(session.request, 'POST', '/api/v1/catalog/courses/search',
                                        {'term_code': 'WI27'}) for _ in range(2)]
                assert all(search.result()['courses'] for search in searches)
            assert Site.starts == starts + 1, 'Concurrent searches must share one recovery'
            Site.reject_catalog = True
            starts = Site.starts
            try:
                session.request('POST', '/api/v1/catalog/courses/search', {'term_code': 'WI27'})
                raise AssertionError('expected 401 after one replay')
            except auth.SessionError as exc:
                assert exc.code == 'AUTH_REQUIRED'
            assert Site.starts == starts + 1
            assert session.status()['state'] == 'expired'
            Site.reject_catalog = False
            session.close()
            Site.issue_cookie=False
            session=auth.UCSDSession(state)
            session._headless=True
            starts = Site.starts
            session.login()
            wait_login(session)
            assert Site.starts == starts, 'An existing session should connect without opening sign-in'
            async def remembered_browser():
                await session._context.pages[0].goto(auth.BASE + '/start')
                return await session._context.pages[0].evaluate("localStorage.getItem('test-device')")
            assert session._submit(remembered_browser).result(timeout=5) == 'remembered'
            Site.expired=True
            try:
                session.request('GET','/api/v1/planner/terms')
                raise AssertionError('expected manual login prompt')
            except auth.SessionError as exc:
                assert exc.code=='AUTH_PENDING'
            assert session.status()['state']=='signing_in'
            assert not session._background, 'Interactive sign-in must switch out of background mode'
            sibling = Path(td) / 'keep-me.txt'
            sibling.write_text('outside the browser profile', encoding='utf-8')
            session.logout()
            assert not state.exists()
            assert not session.profile_path.exists()
            assert sibling.is_file()
            assert session.status()['state']=='signed_out'
            print('PASS: legacy cookie migration, persistent cookies and browser storage across restarts, CSRF, bounded 401 recovery and scoped logout')
        finally:
            session.close()
            server.shutdown()
            server.server_close()


if __name__=='__main__': run()

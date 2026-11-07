"""Exercise the real session actor and cookie jar against a local fake UCSD."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import time
import ucsd_session as auth


class Site(BaseHTTPRequestHandler):
    issue_cookie = True
    expired = False
    def log_message(self, *args): pass
    def do_GET(self):
        if self.path == '/start':
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            if self.issue_cookie:
                self.send_header('Set-Cookie', 'fake_ucsd_session=test_only; HttpOnly; Path=/; SameSite=Lax')
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
        ok = self.path == '/api/v1/catalog/courses/search' and self.headers.get('X-CSRF-Token') == 'fake_csrf' and 'fake_ucsd_session=test_only' in self.headers.get('Cookie', '')
        self.rfile.read(int(self.headers.get('Content-Length', '0')))
        self.send_response(200 if ok else 403)
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
        session=auth.UCSDSession(state)
        session._headless=True
        try:
            session.login()
            wait_login(session)
            assert session.request('GET','/api/v1/planner/terms')['terms'][0]['term_code']=='WI27'
            assert session.request('POST','/api/v1/catalog/courses/search', {'term_code': 'WI27'})['courses'][0]['module_code']=='CCE-001'
            session._csrf_token='stale_token'
            assert session.request('POST','/api/v1/catalog/courses/search', {'term_code': 'WI27'})['courses']
            assert b'fake_ucsd_session' not in state.read_bytes()
            session.close()
            Site.issue_cookie=False
            session=auth.UCSDSession(state)
            session._headless=True
            session.login()
            wait_login(session)
            Site.expired=True
            try:
                session.request('GET','/api/v1/planner/terms')
                raise AssertionError('expected 401')
            except auth.SessionError as exc:
                assert exc.code=='AUTH_REQUIRED'
            assert session.status()['state']=='expired'
            session.logout()
            assert not state.exists()
            assert session.status()['state']=='signed_out'
            print('PASS: real browser cookie sharing, session actor, DPAPI restart restore, 401 expiry, local logout')
        finally:
            session.close()
            server.shutdown()
            server.server_close()


if __name__=='__main__': run()

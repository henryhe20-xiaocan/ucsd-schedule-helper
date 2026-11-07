"""Browser regression with synthetic terms; does not prove UCSD access."""
import json
from pathlib import Path
import tempfile
import threading
from playwright.sync_api import sync_playwright
import scheduler as app


class FakeUCSD:
    state = 'signed_out'
    def status(self):
        return {'state': self.state, 'message': {'signed_out': '请点击登录 UCSD', 'signing_in': '请在弹出的专用浏览器完成 UCSD / Duo 登录', 'authenticated': 'UCSD 已连接'}[self.state]}
    def login(self):
        self.state = 'signing_in'
        return self.status()
    def logout(self):
        self.state = 'signed_out'
        return self.status()
    def request(self, method, path, payload=None, timeout=30):
        if self.state != 'authenticated':
            raise app.SessionError('请先登录', 'AUTH_REQUIRED', 401)
        if path == '/api/v1/planner/terms':
            return {'terms': [{'term_code': t} for t in ('FA26', 'WI27')]}
        return {'candidates': [], 'courses': []}


def run():
    errors = []
    app.UCSD = FakeUCSD()
    app.fetch_rmp_ratings = lambda name: {}
    with tempfile.TemporaryDirectory() as td:
        app.SAVED_DATA = str(Path(td)/'saved.json')
        Path(app.SAVED_DATA).write_text(json.dumps({'state': {'term': 'FA26'}, 'conversations': [{'id': 'x', 'title': '<img src=x onerror="window.pwned=1">'}]}), encoding='utf-8')
        server = app.ThreadingHTTPServer(('127.0.0.1', 0), app.SchedulerHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch()
                page = browser.new_page(viewport={'width': 1500, 'height': 1000})
                page.on('pageerror', lambda e: errors.append(str(e)))
                page.goto(f'http://127.0.0.1:{server.server_port}/')
                page.wait_for_function("() => document.querySelector('#authStatus').textContent.includes('请点击')")
                assert page.locator('#termSel').input_value() == 'FA26'
                assert page.locator('#convList img').count() == 0
                assert page.evaluate('window.pwned || 0') == 0
                page.locator('#ucsdLogin').click()
                page.wait_for_function("() => document.querySelector('#ucsdLogin').disabled")
                app.UCSD.state = 'authenticated'
                page.locator('#ucsdCheck').click()
                page.wait_for_function("() => [...document.querySelector('#termSel').options].some(x=>x.value==='WI27')")
                page.locator('#termSel').select_option('WI27')
                page.wait_for_function("() => state.term==='WI27' && !document.querySelector('#termSel').disabled")
                assert page.locator('#courseList').inner_text().find('还没有添加') >= 0
                page.evaluate("state.locks={FAKE:'FAKE'}; scheduleSaveState()")
                page.locator('#termSel').select_option('FA26')
                page.wait_for_function("() => state.term==='FA26' && !document.querySelector('#termSel').disabled")
                assert page.evaluate('Object.keys(state.locks).length') == 0
                page.locator('#termSel').select_option('WI27')
                page.wait_for_function("() => state.term==='WI27' && !document.querySelector('#termSel').disabled")
                assert page.evaluate('state.locks.FAKE') == 'FAKE'
                page.reload()
                page.wait_for_function("() => state.term==='WI27'")
                assert page.evaluate('state.locks.FAKE') == 'FAKE'
                page.locator('#ucsdLogout').click()
                page.wait_for_function("() => document.querySelector('#ucsdLogout').hidden")
                assert page.evaluate('state.term') == 'WI27'
                # The exact inline script works; injected event handlers do not.
                page.evaluate("document.querySelector('#status').innerHTML='<img src=x onerror=window.pwned=2>'")
                page.wait_for_timeout(200)
                assert page.evaluate('window.pwned || 0') == 0
                assert not errors, errors
                browser.close()
            print('PASS: offline restore, escaped title, login states, WI27 term switch, independent drafts, reload, logout, CSP, no JS errors')
        finally:
            server.shutdown()
            server.server_close()


if __name__ == '__main__':
    run()

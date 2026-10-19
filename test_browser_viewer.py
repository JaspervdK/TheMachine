import datetime as dt
import http.server
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

import assistant
import browser_viewer
import preparation_state


def journal(*calls):
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.execute('CREATE TABLE preparations(job_id TEXT,suggestion_id TEXT,status TEXT,started_at TEXT,created_at TEXT)')
    preparation_state.initialize(db)
    for i, (tool, params, ago) in enumerate(calls):
        at = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=ago)).isoformat()
        db.execute('INSERT INTO preparation_calls(job_id,call_id,tool,params,started_at) VALUES(?,?,?,?,?)', ('job', str(i), tool, json.dumps(params), at))
    return db


class BrowserWaitTest(unittest.TestCase):
    def test_a_long_browser_step_offers_the_browser_with_its_page(self):
        db = journal(('read', {}, 300), ('browser', {'action': 'navigate', 'url': 'https://www.linkedin.com/login'}, 120), ('browser', {'action': 'snapshot'}, 50))
        waiting = assistant.browser_wait(db, 'job', 'running')
        self.assertEqual(waiting['page'], 'https://www.linkedin.com/login')
        self.assertLessEqual(dt.datetime.fromisoformat(waiting['since']), dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=119))

    def test_a_short_browser_step_or_other_work_offers_nothing(self):
        self.assertIsNone(assistant.browser_wait(journal(('browser', {'action': 'open'}, 10)), 'job', 'running'))
        self.assertIsNone(assistant.browser_wait(journal(('browser', {}, 300), ('read', {}, 200)), 'job', 'running'))
        self.assertIsNone(assistant.browser_wait(journal(('browser', {}, 300)), 'job', 'done'))

    def test_a_run_that_stopped_right_after_the_browser_offers_it_at_once(self):
        db = journal(('browser', {'action': 'open', 'targetUrl': 'https://x.com/login'}, 20), ('assistant_signals', {'action': 'preparation_blocked'}, 5))
        self.assertEqual(assistant.browser_wait(db, 'job', 'blocked')['page'], 'https://x.com/login')
        self.assertIsNone(assistant.browser_wait(journal(('read', {}, 20)), 'job', 'blocked'))

    def test_link_comes_from_browser_url_only(self):
        self.assertEqual(assistant.browser_link({'browser_url': 'https://cp.example/open?to=browser'}), 'https://cp.example/open?to=browser')
        self.assertEqual(assistant.browser_link({'browser_url': browser_viewer.VIEWER_URL}), browser_viewer.VIEWER_URL)
        self.assertIsNone(assistant.browser_link({'browser_url': 'javascript:alert(1)'}))
        self.assertIsNone(assistant.browser_link({}))

    def test_telegram_card_says_so_and_gets_a_button_only_for_a_https_viewer(self):
        row = {'id': 'abc', 'status': 'accepted', 'title': 'Reply', 'body': 'Body', 'preparation_status': 'running',
               'preparation_browser': {'since': assistant.now(), 'page': None, 'link': 'https://cp.example/open?to=browser'}}
        buttons = [b for block in assistant.controls(row)['blocks'] for b in block['buttons']]
        self.assertEqual(buttons[0], {'label': 'Open the browser', 'action': {'type': 'url', 'url': 'https://cp.example/open?to=browser'}})
        self.assertIn('browser', assistant.card(row))
        row['preparation_browser']['link'] = browser_viewer.VIEWER_URL  # Telegram can't open this computer's 127.0.0.1
        self.assertNotIn('Open the browser', [b['label'] for block in assistant.controls(row)['blocks'] for b in block['buttons']])


class Tabs(http.server.BaseHTTPRequestHandler):
    activated = []

    def do_GET(self):
        if self.path == '/json/list':
            body = json.dumps([{'id': 'w', 'type': 'service_worker', 'url': 'https://www.linkedin.com/sw.js'},
                               {'id': 'a', 'type': 'page', 'url': 'https://example.com/'},
                               {'id': 'b', 'type': 'page', 'url': 'https://www.linkedin.com/feed/'}]).encode()
        elif self.path.startswith('/json/activate/'):
            Tabs.activated.append(self.path.rsplit('/', 1)[1])
            body = b'Target activated'
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class FocusTest(unittest.TestCase):
    def test_brings_the_tab_of_the_same_site_to_the_front(self):
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Tabs)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            cdp = f'http://127.0.0.1:{server.server_port}'
            self.assertEqual(browser_viewer.focus('https://www.linkedin.com/login', cdp), 'https://www.linkedin.com/feed/')
            self.assertEqual(browser_viewer.focus(None, cdp), 'https://example.com/')
            self.assertEqual(Tabs.activated, ['b', 'a'])
        finally:
            server.shutdown()
        self.assertIsNone(browser_viewer.focus(None, cdp))  # no browser running
        with self.assertRaises(ValueError):
            browser_viewer.focus(None, 'http://192.0.2.1:9222')

    def test_viewer_files_add_the_clipboard_bridge_to_novnc(self):
        with tempfile.TemporaryDirectory() as tmp:
            novnc, web = Path(tmp, 'novnc'), Path(tmp, 'web')
            (novnc / 'app').mkdir(parents=True)
            (novnc / 'vnc.html').write_text('<html><head></head></html>')
            browser_viewer.build_web(web, novnc)
            browser_viewer.build_web(web, novnc)  # again is fine
            self.assertIn('src="machine-clipboard.js"', (web / 'vnc.html').read_text())
            self.assertIn("import UI from './app/ui.js'", (web / 'machine-clipboard.js').read_text())
            self.assertTrue((web / 'app').is_symlink())

    def test_units_listen_on_loopback_only(self):
        for unit in browser_viewer.UNITS.glob('machine-browser-*'):
            text = unit.read_text()
            for line in text.splitlines():
                if line.startswith('ListenStream='):
                    self.assertTrue(line.startswith('ListenStream=127.0.0.1:'), unit.name)
            if 'websockify' in text:
                self.assertIn(' 127.0.0.1:6081', text)


class InstallerTest(unittest.TestCase):
    def test_browser_viewer_option_points_the_dashboard_at_the_viewer(self):
        import installer
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            paths = installer.Paths(tmp)
            paths.config.parent.mkdir(parents=True)
            paths.config.write_text(json.dumps({'accounts': []}))
            with mock.patch.object(installer, 'run', return_value=mock.Mock(returncode=0)):
                self.assertEqual(installer.browser_viewer_setup(paths, install=lambda say: browser_viewer.VIEWER_URL), 0)
                self.assertEqual(installer.browser_viewer_setup(paths, install=lambda say: None), 1)
            config = json.loads(paths.config.read_text())
            self.assertEqual((config['browser_url'], config['browser_cdp_url']), (browser_viewer.VIEWER_URL, 'http://127.0.0.1:9222'))


if __name__ == '__main__':
    unittest.main()

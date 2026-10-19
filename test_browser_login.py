"""The sign-in dialog's path: fields found and filled in a real (headless) browser, the saved sign-ins encrypted, and
the actions for the owner on the dashboard only. Made-up credentials only."""
import http.server, json, os, shutil, socket, subprocess, sys, tempfile, threading, time, unittest, urllib.request
from pathlib import Path

import browser_login
import moving

USER, SECRET, CODE = 'test-user@example.invalid', 'not-a-real-password-123', '424242'

PAGES = {
    '/login': b'''<!doctype html><title>Log in</title><form action="/after" method="post">
        <input name="email" type="email"><input name="pw" type="password"><button>Log in</button></form>''',
    '/after': b'<!doctype html><title>Code</title><form action="/home" method="post"><input name="otp" autocomplete="one-time-code"><button>Verify</button></form>',
    '/home': b'<!doctype html><title>Home</title><p>Welcome</p>',
    '/stuck': b'''<!doctype html><title>Log in</title><form onsubmit="event.preventDefault()">
        <input name="username"><input type="password"><button>Sign in</button></form>''',
    '/news': b'<!doctype html><title>News</title><form><input name="q"><button>Search</button></form>',
}


class Site(http.server.BaseHTTPRequestHandler):
    posted = []

    def do_GET(self):
        self.reply()

    def do_POST(self):
        Site.posted.append((self.path, self.rfile.read(int(self.headers['Content-Length'])).decode()))
        self.reply()

    def reply(self):
        body = PAGES.get(self.path.split('?')[0], b'')
        self.send_response(200 if body else 404)
        self.send_header('Content-Type', 'text/html')
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


CHROME = shutil.which('chromium') or shutil.which('google-chrome-stable') or shutil.which('chromium-browser')


@unittest.skipUnless(CHROME, 'needs Chrome or Chromium')
class FillTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Site)
        threading.Thread(target=cls.site.serve_forever, daemon=True).start()
        cls.origin = 'http://127.0.0.1:%d' % cls.site.server_address[1]
        cls.profile = tempfile.mkdtemp()
        port = free_port()
        cls.chrome = subprocess.Popen([CHROME, '--headless=new', '--no-sandbox', '--disable-gpu', '--remote-debugging-port=%d' % port,
                                       '--user-data-dir=' + cls.profile, 'about:blank'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.config = {'browser_cdp_url': 'http://127.0.0.1:%d' % port, 'accounts': []}
        for _ in range(100):
            try:
                urllib.request.urlopen(cls.config['browser_cdp_url'] + '/json/version', timeout=1)
                break
            except OSError:
                time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.chrome.terminate()
        cls.chrome.wait()
        cls.site.shutdown()
        shutil.rmtree(cls.profile, ignore_errors=True)

    def open(self, path):
        base = self.config['browser_cdp_url']
        for tab in browser_login.browser_cdp._http(base, '/json/list'):
            if tab.get('type') == 'page':
                browser_login.browser_cdp.close_tab(base, tab['id'])
        browser_login.browser_cdp.open_tab(base, self.origin + path)
        time.sleep(1)

    def test_fills_submits_and_takes_the_code_next(self):
        Site.posted.clear()
        self.open('/news')  # not a sign-in page: not offered
        self.assertEqual(browser_login.pages(self.config), [])
        self.open('/login')
        [page] = browser_login.pages(self.config)
        self.assertEqual((page['username'], page['password'], page['code']), (True, True, False))
        self.assertNotIn(SECRET, json.dumps(page))
        done = browser_login.fill(self.config, page['id'], page['host'], USER, SECRET, settle=5)
        self.assertEqual(done['result'], 'next')
        self.assertTrue(done['page']['code'])
        self.assertIn(('/after', 'email=test-user%40example.invalid&pw=not-a-real-password-123'), Site.posted)
        done = browser_login.fill(self.config, page['id'], page['host'], code=CODE, settle=5)
        self.assertEqual(done, {'result': 'done', 'page': None})
        self.assertIn(('/home', 'otp=' + CODE), Site.posted)

    def test_a_page_that_stays_gets_its_password_emptied(self):
        self.open('/stuck')
        [page] = browser_login.pages(self.config)
        done = browser_login.fill(self.config, page['id'], page['host'], USER, SECRET, settle=2)
        self.assertEqual(done['result'], 'still_here')
        value = browser_login.browser_cdp.evaluate(self.config['browser_cdp_url'], page['id'].split(':', 1)[1],
                                                   "() => document.querySelector('input[type=password]').value")
        self.assertEqual(value, '')

    def test_another_site_is_refused_without_quoting_anything(self):
        self.open('/login')
        [page] = browser_login.pages(self.config)
        with self.assertRaises(ValueError) as caught:
            browser_login.fill(self.config, page['id'], 'evil.example', USER, SECRET)
        self.assertNotIn(SECRET, str(caught.exception))
        with self.assertRaises(ValueError):
            browser_login.fill(self.config, '7:nope', page['host'], USER, SECRET)


class EndpointTest(unittest.TestCase):
    def test_loopback_browsers_only(self):
        config = {'browser_cdp_url': 'http://127.0.0.1:9222', 'accounts': [{'browser_cdp': 'http://127.0.0.1:9333'},
                  {'browser_cdp': 'http://10.0.0.5:9222'}, {'browser_cdp': 'http://127.0.0.1:9222/'}, {}]}
        self.assertEqual(browser_login.endpoints(config), ['http://127.0.0.1:9222', 'http://127.0.0.1:9333'])


@unittest.skipUnless(shutil.which('openssl'), 'needs openssl')
class StoreTest(unittest.TestCase):
    def test_saved_encrypted_listed_without_password_and_forgotten(self):
        with tempfile.TemporaryDirectory() as folder:
            store = browser_login.LoginStore(folder)
            store.save('www.example.invalid', USER, None)  # a two-step sign-in: the username first
            store.save('example.invalid', None, SECRET)
            self.assertEqual(store.get('example.invalid')['password'], SECRET)
            self.assertNotIn(SECRET.encode(), (Path(folder) / browser_login.STORE).read_bytes())
            self.assertNotIn(USER.encode(), (Path(folder) / browser_login.STORE).read_bytes())
            for name in (browser_login.STORE, browser_login.KEY):
                self.assertEqual(os.stat(Path(folder) / name).st_mode & 0o777, 0o600)
            [entry] = store.list()
            self.assertEqual((entry['site'], entry['username']), ('example.invalid', USER))
            self.assertNotIn('password', entry)
            self.assertTrue(store.forget('example.invalid'))
            self.assertEqual(store.list(), [])

    def test_moves_only_with_a_passphrase(self):
        self.assertTrue({browser_login.STORE, browser_login.KEY} <= moving.SECRET_NAMES)


class OwnerOnlyTest(unittest.TestCase):
    def test_not_for_the_agent_or_the_cli(self):
        import assistant
        for name in ('browser_login_pages', 'browser_login_fill', 'browser_logins', 'browser_login_forget'):
            self.assertIn(name, assistant.OWNER_ACTIONS)
            app = assistant.App.__new__(assistant.App)
            app.config = {'accounts': [], 'database': '/nonexistent/x.db'}
            with self.assertRaisesRegex(ValueError, 'Only the owner'):
                app.action(name, {'page': '0:x', 'host': 'example.invalid', 'password': SECRET})


if __name__ == '__main__':
    unittest.main()

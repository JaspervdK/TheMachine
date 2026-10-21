"""The live look at a preparing card's browser tab, source emails kept in memory for the dashboard, and adding a
source from Connections, Add (onboarding.WebUI)."""
import datetime as dt
import json
import os
import sqlite3
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import assistant
import onboarding
import preparation_state

TAB = 'A0F20ED451EA2A1247613CAB889F3BCB'
OTHER = 'DF829ED2FFBB4631972AF70080134B94'


def opened(target, label, port=18800):
    answer = {'targetId': target, 'url': 'https://example.org/', 'wsUrl': f'ws://127.0.0.1:{port}/devtools/page/{target}',
              'type': 'page', 'suggestedTargetId': label, 'tabId': 't1', 'label': label}
    text = '\n<<<EXTERNAL_UNTRUSTED_CONTENT id="x">>>\nSource: Browser\n---\n' + json.dumps(answer, indent=2) + '\n<<<END_EXTERNAL_UNTRUSTED_CONTENT id="x">>>'
    return json.dumps({'content': [{'type': 'text', 'text': text}]})


def journal(*calls):
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.execute('CREATE TABLE preparations(job_id TEXT,suggestion_id TEXT,status TEXT,started_at TEXT,created_at TEXT)')
    preparation_state.initialize(db)
    for i, (tool, params, result) in enumerate(calls):
        at = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=100 - i)).isoformat()
        db.execute('INSERT INTO preparation_calls(job_id,call_id,tool,params,result,started_at) VALUES(?,?,?,?,?,?)',
                   ('job', str(i), tool, json.dumps(params), result, at))
    return db


class PeekTabTest(unittest.TestCase):
    def test_only_a_tab_the_run_opened_while_it_is_on_the_browser(self):
        mine = ('browser', {'action': 'open', 'url': 'https://example.org/'}, opened(TAB, 'bills'))
        self.assertEqual(assistant.peek_tab(journal(mine), 'job'), (TAB, 18800))
        self.assertEqual(assistant.peek_tab(journal(mine, ('browser', {'action': 'act', 'targetId': 'bills'}, None)), 'job'), (TAB, 18800))
        self.assertEqual(assistant.peek_tab(journal(mine, ('browser', {'action': 'snapshot'}, None)), 'job'), (TAB, 18800))
        # Another tab (one an earlier task opened, or the owner's): no look at all.
        self.assertIsNone(assistant.peek_tab(journal(mine, ('browser', {'action': 'act', 'targetId': 'someone-else'}, None)), 'job'))
        # Off the browser, or the tab closed.
        self.assertIsNone(assistant.peek_tab(journal(mine, ('exec', {'command': 'ls'}, None)), 'job'))
        self.assertIsNone(assistant.peek_tab(journal(mine, ('browser', {'action': 'close', 'targetId': 'bills'}, None)), 'job'))
        # A tab list names every tab: it never makes one this run's own.
        listing = ('browser', {'action': 'tabs'}, opened(OTHER, 'theirs'))
        self.assertIsNone(assistant.peek_tab(journal(listing, ('browser', {'action': 'act', 'targetId': 'theirs'}, None)), 'job'))
        # Only a browser on this computer.
        far = ('browser', {'action': 'open'}, opened(TAB, 'bills').replace('127.0.0.1', '10.0.0.5'))
        self.assertIsNone(assistant.peek_tab(journal(far), 'job'))

    def test_the_picture_comes_from_that_tab_only_when_it_is_in_front(self):
        store_dir = tempfile.TemporaryDirectory()
        self.addCleanup(store_dir.cleanup)
        store = assistant.Store(Path(store_dir.name) / 'events.sqlite')
        app = object.__new__(assistant.App)
        app.store, app.config = store, {'accounts': [], 'browser_cdp_url': 'http://127.0.0.1:18800'}
        with store.connect() as db:
            db.execute("INSERT INTO preparations(suggestion_id,decision_id,job_id,status,created_at,updated_at) VALUES('s1','d1','job','running',?,?)",
                       (assistant.now(), assistant.now()))
            db.execute('INSERT INTO preparation_calls(job_id,call_id,tool,params,result,started_at) VALUES(?,?,?,?,?,?)',
                       ('job', '1', 'browser', json.dumps({'action': 'open'}), opened(TAB, 'bills'), assistant.now()))
        shown = {'visible': 'hidden'}
        calls = []

        class Tab:
            def __init__(self, base, target, timeout=30):
                calls.append(target)

            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

            def call(self, method, params=None):
                calls.append(method)
                if method == 'Runtime.evaluate':
                    return {'result': {'value': shown['visible']}}
                if method == 'Page.getLayoutMetrics':
                    return {'cssVisualViewport': {'pageX': 0, 'pageY': 0, 'clientWidth': 1600, 'clientHeight': 900}}
                if method == 'Page.captureScreenshot':
                    self.params = params
                    calls.append(params['clip']['scale'])
                    return {'data': 'AAAA'}
                raise AssertionError(method)

        cdp = assistant.browser_login.browser_cdp
        with patch.object(cdp, 'Session', Tab), patch.object(cdp, '_http', return_value=[{'id': TAB, 'type': 'page'}, {'id': OTHER, 'type': 'page'}]):
            self.assertTrue(app.preparation_progress('s1')['peek'])
            self.assertEqual(app.preparation_peek('s1'), {'on': True, 'image': None})  # in the background: never brought forward
            self.assertNotIn('Page.captureScreenshot', calls)
            self.assertNotIn('Page.bringToFront', calls)
            shown['visible'] = 'visible'
            app.peeks.clear()
            self.assertEqual(app.preparation_peek('s1'), {'on': True, 'image': 'data:image/jpeg;base64,AAAA'})
            self.assertEqual(set(c for c in calls if isinstance(c, str) and len(c) == 32), {TAB})
            self.assertEqual(calls[-1], 0.5)  # 800 pixels across
            count = len(calls)
            app.preparation_peek('s1')  # a second dashboard right after: the same picture, no new look
            self.assertEqual(len(calls), count)
            # Another browser than this install's DevTools address: no look.
            app.config['browser_cdp_url'] = 'http://127.0.0.1:9222'
            self.assertEqual(app.preparation_peek('s1'), {'on': False})
            app.config['browser_cdp_url'] = 'http://127.0.0.1:18800'
            with store.connect() as db:
                db.execute("UPDATE preparations SET status='prepared'")
            self.assertEqual(app.preparation_peek('s1'), {'on': False})
            self.assertEqual(app.peeks, {})  # gone with the run


class MessageCacheTest(unittest.TestCase):
    def test_one_read_serves_the_card_its_html_and_its_images(self):
        app = object.__new__(assistant.App)
        app.config = {'accounts': [{'id': 'work', 'service': 'gmail'}]}
        reads = []

        def read(*args):
            reads.append(args[5]['id'])
            time.sleep(0.05)
            return {'id': args[5]['id'], 'payload': {}}
        with patch.object(assistant, 'gog_read', side_effect=read):
            threads = [threading.Thread(target=app.gmail_message, args=('work', 'm1')) for _ in range(3)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(app.gmail_message('work', 'm1')[1]['id'], 'm1')
            self.assertEqual(reads, ['m1'])
            for i in range(app.MESSAGE_CACHE + 5):
                app.gmail_message('work', f'x{i}')
            self.assertEqual(len(app._messages), app.MESSAGE_CACHE)
            app.gmail_message('work', 'm1')  # pushed out: read again
            self.assertEqual(reads.count('m1'), 2)


class WebConnectTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'config.json'
        self.path.write_text(json.dumps({'accounts': [], 'database': str(Path(self.temp.name) / 'events.sqlite')}))
        self.app = object.__new__(assistant.App)
        self.app.config, self.app.config_path = json.loads(self.path.read_text()), self.path

    def wait(self, view, until):
        for _ in range(200):
            if until(view):
                return view
            time.sleep(0.02)
            view = {**view, **self.app.action('connect_poll', {'session': view['session']}, owner_dashboard=True)}
        self.fail('no change: ' + json.dumps(view))

    def test_questions_are_answered_on_the_dashboard_and_the_source_is_saved(self):
        typed = {}

        def connect(path, name, ui, restart=True):
            self.assertFalse(restart)  # the service restarts itself after, not in the middle
            ui.say('Salesflare: create an API key')
            typed['key'] = ui.secret('Salesflare API key')
            boundary = ui.choose('Which part of your life is this account', [('work:Acme', 'work:Acme'), ('personal', 'personal')])
            typed['ok'] = ui.yes('Also read old mail?')
            typed['email'] = ui.ask('Address', 'owner@example.org')
            return {'id': 'acme-crm', 'boundary': boundary}
        with patch.object(onboarding, 'connect', connect), patch.object(assistant, 'restart_self', return_value=True) as restart:
            with self.assertRaises(ValueError):
                self.app.action('connect_start', {'source': 'crm'})  # not from the dashboard
            view = self.app.action('connect_start', {'source': 'crm'}, owner_dashboard=True)
            view = self.wait(view, lambda v: v['question'])
            self.assertEqual((view['question']['kind'], view['lines']), ('secret', ['Salesflare: create an API key']))
            with self.assertRaisesRegex(ValueError, 'Another'):
                self.app.action('connect_start', {'source': 'slack'}, owner_dashboard=True)
            q = view['question']['id']
            view.update(self.app.action('connect_answer', {'session': view['session'], 'question': q, 'value': 'fake-key-1'}, owner_dashboard=True))
            with self.assertRaisesRegex(ValueError, 'already answered'):
                self.app.action('connect_answer', {'session': view['session'], 'question': q, 'value': 'again'}, owner_dashboard=True)
            view = self.wait(view, lambda v: v['question'] and v['question']['kind'] == 'choose')
            self.assertEqual(view['question']['options'], ['work:Acme', 'personal'])
            view.update(self.app.action('connect_answer', {'session': view['session'], 'question': view['question']['id'], 'value': 1}, owner_dashboard=True))
            view = self.wait(view, lambda v: v['question'] and v['question']['kind'] == 'yes')
            view.update(self.app.action('connect_answer', {'session': view['session'], 'question': view['question']['id'], 'value': False}, owner_dashboard=True))
            view = self.wait(view, lambda v: v['question'] and v['question']['kind'] == 'ask')
            self.assertEqual(view['question']['default'], 'owner@example.org')
            view.update(self.app.action('connect_answer', {'session': view['session'], 'question': view['question']['id'], 'value': ''}, owner_dashboard=True))
            view = self.wait(view, lambda v: v['state'] != 'running')
        self.assertEqual((view['state'], view['account'], view['restarting']), ('done', 'acme-crm', True))
        self.assertEqual(typed, {'key': 'fake-key-1', 'ok': False, 'email': 'owner@example.org'})
        self.assertNotIn('fake-key-1', json.dumps(view))
        restart.assert_called_once()

    def test_stop_and_setup_errors_show_with_their_fix(self):
        from onboarding import SetupError

        def connect(path, name, ui, restart=True):
            if name == 'whatsapp':
                raise SetupError('WhatsApp is not linked yet', 'run this in a terminal on this computer and scan the code: wacli --store /x auth')
            ui.ask('Anything')
        with patch.object(onboarding, 'connect', connect):
            view = self.app.action('connect_start', {'source': 'whatsapp'}, owner_dashboard=True)
            view = self.wait(view, lambda v: v['state'] != 'running')
            self.assertEqual(view['state'], 'failed')
            self.assertIn('wacli --store /x auth', view['fix'])
            view = self.app.action('connect_start', {'source': 'gmail'}, owner_dashboard=True)
            view = self.wait(view, lambda v: v['question'])
            view.update(self.app.action('connect_cancel', {'session': view['session']}, owner_dashboard=True))
            view = self.wait(view, lambda v: v['state'] != 'running')
            self.assertEqual(view['state'], 'cancelled')

    def test_hosted_google_and_microsoft_stay_on_the_account_page(self):
        self.app.config.update(google_broker={'url': 'https://cp.example.org/broker'}, manage_url='https://cp.example.org/account')
        listed = self.app.action('connect_sources', {}, owner_dashboard=True)
        by = {s['name']: s for s in listed['sources']}
        self.assertTrue(by['gmail']['broker'] and by['calendar']['broker'])
        self.assertFalse(by['outlook']['broker'] or by['slack']['broker'])
        self.assertEqual(listed['manage_url'], 'https://cp.example.org/account')
        with self.assertRaises(ValueError):
            self.app.action('connect_start', {'source': 'gmail'}, owner_dashboard=True)
        with self.assertRaises(ValueError):
            self.app.action('connect_start', {'source': 'nonsense'}, owner_dashboard=True)


class WebSignInTest(unittest.TestCase):
    """Google from the dashboard: gog's remote flow, read-only, and never for an address gog already holds."""
    def gog(self, folder, known):
        log = Path(folder) / 'calls'
        script = Path(folder) / 'gog'
        script.write_text('#!/bin/sh\necho "$@" >> ' + str(log) + '\n'
                          'case "$*" in\n'
                          '  "auth list"*) echo \'{"accounts":[' + ','.join('{"email":"%s"}' % k for k in known) + ']}\' ;;\n'
                          '  *"--step 1"*) echo "Open: https://accounts.google.com/o/oauth2/auth?client_id=x&scope=readonly" ;;\n'
                          'esac\n')
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        return str(script), log

    def test_remote_flow_for_a_new_address(self):
        with tempfile.TemporaryDirectory() as folder:
            gog, log = self.gog(folder, ['old@example.org'])
            ui = onboarding.WebUI(wait=5)
            account = {'email': 'new@example.org', 'client': 'default', 'service': 'gmail'}
            done = threading.Thread(target=onboarding.sign_in, args=({}, account, gog, ui))
            done.start()
            for _ in range(200):
                if ui.view()['question']:
                    break
                time.sleep(0.02)
            self.assertIn('https://accounts.google.com/o/oauth2/auth?client_id=x&scope=readonly', ui.view()['lines'])
            ui.answer(ui.view()['question']['id'], 'http://127.0.0.1:1/oauth2/callback?code=fake-code')
            done.join(5)
            calls = log.read_text().splitlines()
            self.assertTrue(all('--readonly' in c and '--gmail-scope readonly' in c for c in calls if c.startswith('auth add')))
            self.assertIn('--step 2 --auth-url http://127.0.0.1:1/oauth2/callback?code=fake-code', calls[-1])

    def test_an_address_gog_already_holds_is_never_signed_in_again_from_here(self):
        with tempfile.TemporaryDirectory() as folder:
            gog, log = self.gog(folder, ['owner@example.org'])
            with self.assertRaisesRegex(onboarding.SetupError, 'already'):
                onboarding.sign_in({}, {'email': 'Owner@example.org', 'client': 'default'}, gog, onboarding.WebUI(wait=1))
            self.assertFalse([c for c in log.read_text().splitlines() if c.startswith('auth add')])


if __name__ == '__main__':
    unittest.main()

"""onboarding.py: Telegram detection, assistant connect, first sync, doctor and the starter USER.md.
Stub gog/openclaw/wacli/tailscale binaries and patched source reads; nothing contacts a real account."""
import datetime as dt
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import onboarding

ROOT = Path(__file__).resolve().parent
EXAMPLE = {a['id']: a for a in json.loads((ROOT / 'config.example.json').read_text())['accounts']}


class FakeUI(onboarding.UI):
    def __init__(self, answers=(), secrets=(), yes=()):
        super().__init__(True)
        self.answers, self.secrets, self.yeses, self.out = list(answers), list(secrets), list(yes), []

    def say(self, text=''):
        self.out.append(str(text))

    def ask(self, prompt, default=''):
        self.out.append(prompt)
        answer = self.answers.pop(0) if self.answers else ''
        return answer or default

    def secret(self, prompt):
        self.out.append(prompt)
        return self.secrets.pop(0) if self.secrets else ''

    def yes(self, prompt, default=True):
        self.out.append(prompt)
        return self.yeses.pop(0) if self.yeses else default

    def text(self):
        return '\n'.join(self.out)


class OnboardingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='machine-onboarding-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home, self.bin, self.log = self.tmp / 'home', self.tmp / 'bin', self.tmp / 'calls.log'
        self.home.mkdir()
        self.bin.mkdir()
        self.data = self.home / '.local/share/personal-assistant'
        self.data.mkdir(parents=True)
        self.path = self.data / 'config.json'
        env = mock.patch.dict(os.environ, {'HOME': str(self.home), 'PATH': f'{self.bin}{os.pathsep}{os.environ["PATH"]}',
                                           'OPENCLAW_WORKSPACE': str(self.home / 'workspace')})
        env.start()
        self.addCleanup(env.stop)
        self.stub('systemctl', 'print("inactive")')

    def stub(self, name, body):
        path = self.bin / name
        path.write_text(f'#!{sys.executable}\nimport json, os, sys\nargs = sys.argv[1:]\n'
                        f'open({str(self.log)!r}, "a").write({name!r} + " " + " ".join(args) + "\\n")\n' + body + '\n')
        path.chmod(0o755)
        return str(path)

    def calls(self):
        return self.log.read_text() if self.log.exists() else ''

    def config(self, *accounts, **extra):
        config = {'database': str(self.data / 'events.sqlite'), 'port': 1, 'telegram_owner': '1000001',
                  'tailscale_owner': 'alex@example.com', 'dashboard_host': 'box.example.ts.net',
                  'credentials': {'api-token': str(self.data / 'api-token'), 'owner-token': str(self.data / 'owner-token')},
                  'accounts': [json.loads(json.dumps(a)) for a in accounts], **extra}
        for account in config['accounts']:
            account.pop('exclude', None)
            if account.get('credential'):
                config['credentials'][account['credential']] = str(self.data / f'{account["credential"]}.env')
        onboarding.write_private(self.path, json.dumps(config))
        return config

    def saved(self):
        return json.loads(self.path.read_text())

    def mode(self, path):
        return stat.S_IMODE(Path(path).stat().st_mode)

    # ------------------------------------------------------------ Telegram

    def test_telegram_ids_come_from_openclaw_owner_first_without_touching_the_bot_token(self):
        openclaw = self.home / '.openclaw'
        (openclaw / 'state').mkdir(parents=True)
        (openclaw / 'openclaw.json').write_text(json.dumps({
            'commands': {'ownerAllowFrom': ['telegram:111111', 'discord:999999']},
            'channels': {'telegram': {'botToken': '123:SECRET', 'allowFrom': ['222222', '@someone'],
                                      'accounts': {'work': {'allowFrom': ['tg:333333']}}}}}))
        with sqlite3.connect(openclaw / 'state/openclaw.sqlite') as db:
            db.execute('CREATE TABLE channel_pairing_allow_entries(channel_key TEXT, account_id TEXT, entry TEXT, sort_order INTEGER, updated_at INTEGER)')
            db.executemany('INSERT INTO channel_pairing_allow_entries VALUES(?,?,?,?,0)',
                           [('telegram', 'default', '444444', 1), ('telegram', 'default', '111111', 0), ('whatsapp', 'default', '555555', 0)])
        self.assertEqual(onboarding.telegram_owners(), ['111111', '222222', '333333', '444444'])
        self.assertEqual(onboarding.telegram_owners(root=self.tmp / 'nowhere'), [])

    def test_pairing_fallback_picks_up_the_new_sender_and_approves_only_on_yes(self):
        counter = self.tmp / 'count'
        openclaw = self.stub('openclaw', f'''
n = int(open({str(counter)!r}).read()) if os.path.exists({str(counter)!r}) else 0
open({str(counter)!r}, "w").write(str(n + 1))
old = [{{"id": "777777", "code": "OLDCODE2"}}]
new = [{{"id": "555555", "code": "ABCD2345", "meta": {{"username": "alex"}}}}]
if args[:2] == ["pairing", "list"]:
    print(json.dumps({{"channel": "telegram", "requests": old + (new if n >= 2 else [])}}))''')
        ui = FakeUI(yes=[True])
        with mock.patch.object(onboarding.time, 'sleep'):
            self.assertEqual(onboarding.telegram_from_pairing(openclaw, ui), '555555')
        self.assertIn('openclaw pairing approve telegram ABCD2345 --notify', self.calls())
        self.assertNotIn('OLDCODE2 --notify', self.calls())
        self.assertIn('alex', ui.text())
        counter.unlink()
        ui = FakeUI(yes=[False])
        with mock.patch.object(onboarding.time, 'sleep'):
            self.assertEqual(onboarding.telegram_from_pairing(openclaw, ui), '555555')
        self.assertEqual(self.calls().count('pairing approve'), 1)

    # ------------------------------------------------------------ connect

    def test_slack_finds_its_ids_and_the_key_never_shows(self):
        self.config(EXAMPLE['work-slack'])
        answers = {'auth.test': {'ok': True, 'team_id': 'T123', 'user_id': 'U456', 'url': 'https://acme.slack.com/'},
                   'users.info': {'ok': True, 'user': {'profile': {'email': 'alex@acme.test'}}}}
        ui = FakeUI(answers=['conn-1'], secrets=['MATON_API_KEY=mk_SECRET'])
        with mock.patch.object(onboarding.assistant().Slack, 'get', lambda self, method, params=None: answers[method]):
            onboarding.connect(self.path, 'slack', ui, restart=False)
        account = self.saved()['accounts'][0]
        self.assertEqual((account['team_id'], account['user_id'], account['email'], account['slack_host'], account['connection_id']),
                         ('T123', 'U456', 'alex@acme.test', 'acme.slack.com', 'conn-1'))
        secret = self.data / 'maton-slack.env'
        self.assertEqual(secret.read_text(), 'MATON_API_KEY=mk_SECRET\n')
        self.assertEqual((self.mode(secret), self.mode(self.path)), (0o600, 0o600))
        self.assertNotIn('mk_SECRET', ui.text())
        self.assertTrue((self.data / 'config.json.bak').exists())
        self.assertEqual(onboarding.missing(self.saved(), account), '')

    def test_salesflare_ids_come_from_me(self):
        self.config(EXAMPLE['work-crm'])
        me = {'id': 42, 'email': 'alex@acme.test', 'team': {'id': 7}}
        with mock.patch.object(onboarding.assistant().Salesflare, 'get', lambda self, path, params=None: me):
            onboarding.connect(self.path, 'crm', FakeUI(secrets=['sf_KEY']), restart=False)
        account = self.saved()['accounts'][0]
        self.assertEqual((account['user_id'], account['team_id'], account['email'], account['owner_emails']), (42, 7, 'alex@acme.test', ['alex@acme.test']))
        self.assertEqual((self.data / 'salesflare.env').read_text(), 'SALESFLARE_API_KEY=sf_KEY\n')

    def test_a_failing_test_read_saves_nothing_and_says_how_to_fix_it(self):
        self.config(EXAMPLE['work-crm'])
        before = self.path.read_text()
        error = onboarding.assistant().SourceError('salesflare', 401)
        with mock.patch.object(onboarding.assistant().Salesflare, 'get', side_effect=error):
            with self.assertRaises(onboarding.SetupError) as raised:
                onboarding.connect(self.path, 'crm', FakeUI(secrets=['wrong']), restart=False)
        self.assertIn('rejected the API key', raised.exception.fix)
        self.assertEqual(self.path.read_text(), before)

    def test_whatsapp_identity_is_the_hash_of_the_linked_account(self):
        jid = '32470000000@s.whatsapp.net'
        wacli = self.stub('wacli', f'print(json.dumps({{"success": True, "error": None, "data": {{"authenticated": True, "linked_jid": {jid!r}}}}}))')
        store = self.home / 'wa'
        store.mkdir()
        self.config(EXAMPLE['personal-whatsapp'], wacli=wacli)
        onboarding.connect(self.path, 'whatsapp', FakeUI(answers=[str(store)]), restart=False)
        account = self.saved()['accounts'][0]
        self.assertEqual(account['identity_sha256'], hashlib.sha256(jid.encode()).hexdigest())
        self.assertIn('--read-only', self.calls())
        self.assertNotIn('wacli --store ' + str(store) + ' auth\n', self.calls())

    def test_android_takes_the_only_paired_phone(self):
        nodes = {'pending': [], 'paired': [{'nodeId': 'linux-1', 'platform': 'linux'},
                                           {'nodeId': 'phone-1', 'platform': 'android', 'displayName': 'Pixel', 'connected': True}]}
        openclaw = self.stub('openclaw', f'print(json.dumps({nodes!r}))')
        self.config(EXAMPLE['personal-android'], openclaw=openclaw)
        ui = FakeUI()
        with mock.patch.object(onboarding.assistant().Android, 'snapshot', return_value={}):
            onboarding.connect(self.path, 'android', ui, restart=False)
        self.assertEqual(self.saved()['accounts'][0]['node_id'], 'phone-1')
        self.assertIn('Pixel', ui.text())

    def test_gmail_signs_in_read_only_and_calendar_reuses_the_sign_in(self):
        flag = self.tmp / 'authorized'
        gog = self.stub('gog', f'''
if args[:3] == ["auth", "credentials", "list"]:
    print(json.dumps({{"clients": [{{"client": "work", "default": True, "path": "x"}}]}}))
elif args[:2] == ["auth", "list"]:
    print(json.dumps({{"accounts": []}}))
elif args[:2] == ["auth", "add"]:
    open({str(flag)!r}, "w").write(os.environ.get("GOG_KEYRING_PASSWORD", ""))
elif args[:2] == ["api", "call"] and os.path.exists({str(flag)!r}) and os.environ.get("GOG_KEYRING_PASSWORD") == open({str(flag)!r}).read():
    print(json.dumps({{"emailAddress": "alex@acme.test", "historyId": "9", "id": "alex@acme.test"}}))
else:
    sys.stderr.write("HTTP 401"); sys.exit(1)''')
        gmail = dict(EXAMPLE['work-gmail'], client='default')
        self.config(gmail, gog=gog)
        ui = FakeUI(answers=['alex@acme.test'], secrets=[''])
        with mock.patch.object(onboarding, 'headless', return_value=False):
            onboarding.connect(self.path, 'gmail', ui, restart=False)
            config = self.saved()
            password = Path(config['credentials']['gog-password'])
            self.assertEqual(self.mode(password), 0o600)
            self.assertGreater(len(password.read_text().strip()), 20)
            self.assertNotIn(password.read_text().strip(), ui.text())
            self.assertIn('gog auth add alex@acme.test --client work --services gmail,calendar --readonly --gmail-scope readonly', self.calls())
            self.assertEqual(config['accounts'][0]['client'], 'work')
            onboarding.connect(self.path, 'calendar', FakeUI(answers=['']), restart=False)
        config = self.saved()
        self.assertEqual([a['id'] for a in config['accounts']], ['work-gmail', 'work-calendar'])
        self.assertEqual((config['accounts'][1]['email'], config['accounts'][1]['client']), ('alex@acme.test', 'work'))
        self.assertEqual(self.calls().count('auth add'), 1)

    def test_a_plugin_source_asks_its_own_setup_fields(self):
        self.config()
        onboarding.connect(self.path, 'rss', FakeUI(answers=['https://example.com/feed.xml']), restart=False)
        account = self.saved()['accounts'][0]
        self.assertEqual((account['id'], account['service'], account['url']), ('feed', 'rss', 'https://example.com/feed.xml'))
        with self.assertRaises(onboarding.SetupError) as raised:
            onboarding.connect(self.path, 'fax', FakeUI(), restart=False)
        self.assertIn('rss', raised.exception.fix)

    def test_every_plugin_setup_is_complete(self):
        for service in onboarding.assistant().connector_services():
            setup = getattr(onboarding.assistant().load_connector(service), 'SETUP', None)
            self.assertIsNotNone(setup, service)
            self.assertEqual(setup['account']['service'], service)
            for key, prompt, *_ in setup.get('ask', []):
                self.assertTrue(key and prompt)

    # ------------------------------------------------------------ first sync, doctor

    def database(self, rows=(), events=(), checkpoints=()):
        with sqlite3.connect(self.data / 'events.sqlite') as db:
            db.execute('CREATE TABLE sync_activity(id TEXT, account_id TEXT, at TEXT, status TEXT, signals INTEGER, error TEXT, retry_at TEXT)')
            db.execute('CREATE TABLE events(id INTEGER PRIMARY KEY, account_id TEXT, occurred_at TEXT, deleted INTEGER DEFAULT 0)')
            db.execute('CREATE TABLE checkpoints(account_id TEXT, state TEXT, last_success TEXT, error TEXT, error_at TEXT, failures INTEGER, retry_at TEXT)')
            db.execute('CREATE TABLE analyses(id TEXT, started_at TEXT)')
            db.executemany('INSERT INTO sync_activity VALUES(?,?,?,?,?,?,NULL)', rows)
            db.executemany('INSERT INTO events(account_id, occurred_at) VALUES(?,?)', events)
            db.executemany('INSERT INTO checkpoints VALUES(?,?,?,?,?,0,NULL)', checkpoints)

    def test_first_sync_shows_counts_and_failures(self):
        stamp = lambda days: (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat()
        since = stamp(0.01)
        self.database(rows=[('1', 'work-gmail', stamp(0), 'success', 3, None), ('2', 'work-slack', stamp(0), 'failed', None, 'slack_invalid_auth'),
                            ('3', 'home', stamp(1), 'success', 1, None)],
                      events=[('work-gmail', stamp(1)), ('work-gmail', stamp(2)), ('work-gmail', stamp(30))])
        ui = FakeUI()
        ok = onboarding.first_sync({'database': str(self.data / 'events.sqlite')}, ['work-gmail', 'work-slack', 'home'], since, ui, wait=0)
        self.assertFalse(ok)
        self.assertIn('✓ work-gmail: 3 signals so far (2 from the last 7 days)', ui.text())
        self.assertIn('✗ work-slack: slack_invalid_auth: Maton rejected the API key', ui.text())
        self.assertIn('… home: no sync finished yet', ui.text())

    def test_doctor_points_every_problem_at_its_fix(self):
        recent = dt.datetime.now(dt.timezone.utc).isoformat()
        gmail = dict(EXAMPLE['work-gmail'], email='alex@acme.test')
        config = self.config(gmail, EXAMPLE['personal-whatsapp'], openclaw=str(self.bin / 'missing'))
        config['credentials']['gog-password'] = str(self.data / 'gog-password')
        onboarding.write_private(self.path, json.dumps(config))
        onboarding.write_private(self.data / 'gog-password', 'x')
        onboarding.write_private(self.data / 'api-token', 'x')
        (self.data / 'owner-token').write_text('x')
        os.chmod(self.data / 'owner-token', 0o644)
        self.database(events=[('work-gmail', recent)], checkpoints=[('work-gmail', '{}', recent, None, None)])
        results = onboarding.doctor(self.path)
        text = '\n'.join(f'{m} {t} | {f}' for m, t, f in results)
        self.assertIn('✗ service not running', text)
        self.assertIn('✓ work-gmail (Gmail): synced just now, 1 signals', text)
        self.assertIn('✗ personal-whatsapp (WhatsApp): not set up (identity_sha256 is still the example value) | assistant connect whatsapp', text)
        self.assertIn('! credential owner-token is readable by others | chmod 600', text)
        self.assertIn('! reviews and Telegram cards are off | try one: assistant preview', text)
        self.assertIn('USER.md missing', text)
        ui = FakeUI()
        self.assertEqual(onboarding.print_doctor(results, ui), 1)
        self.assertIn('fix: assistant connect whatsapp', ui.text())

    def test_only_the_service_that_reads_this_config_is_restarted(self):
        self.stub('systemctl', f'''
if "is-active" in args: print("active")
elif "show" in args: print("HOME=/home/someone-else PATH=/usr/bin")''')
        self.assertTrue(onboarding.service_active())
        self.assertFalse(onboarding.service_active(self.path))  # same default path, but under this test's HOME
        self.assertTrue(onboarding.service_active('/home/someone-else/.local/share/personal-assistant/config.json'))
        ui = FakeUI()
        self.assertFalse(onboarding.restart_and_sync({'port': 1}, ['a'], ui, config_path=self.path))
        self.assertNotIn('restart', ' '.join(line for line in self.calls().splitlines() if line.startswith('systemctl --user restart')))
        self.assertIn('reads another config', ui.text())
        self.stub('systemctl', f'''
if "is-active" in args: print("active")
elif "show" in args: print('"ASSISTANT_CONFIG={self.path}" HOME=/elsewhere')''')
        self.assertTrue(onboarding.service_active(self.path))

    def test_dashboard_is_found_in_tailscale_serve(self):
        status = {'TCP': {'443': {'HTTPS': True}}, 'Web': {'box.example.ts.net:443': {'Handlers': {'/assistant': {'Proxy': 'http://127.0.0.1:8787'}}}}}
        self.stub('tailscale', f'print(json.dumps({status!r}))')
        self.assertEqual(onboarding.dashboard_served({'port': 8787}), 'https://box.example.ts.net/assistant/')
        self.assertIsNone(onboarding.dashboard_served({'port': 9999}))
        self.assertEqual(onboarding.serve_command({'dashboard_path': '/assistant/', 'port': 8787})[-3:],
                         ['--set-path', '/assistant', 'http://127.0.0.1:8787'])

    def test_starter_user_md_is_written_once_from_the_answers(self):
        config = {'owner_name': 'Alex Owner', 'timezone': 'Europe/Brussels',
                  'accounts': [dict(EXAMPLE['work-gmail'], email='alex@acme.test'), dict(EXAMPLE['personal-whatsapp'])]}
        workspace = self.home / 'workspace'
        self.assertTrue(onboarding.offer_user_md(config, FakeUI(), workspace))
        text = (workspace / 'USER.md').read_text()
        for expected in ('Name: Alex Owner', 'Time zone: Europe/Brussels', 'Work: Acme', 'Email: alex@acme.test', 'Sign-off: Alex'):
            self.assertIn(expected, text)
        (workspace / 'USER.md').write_text('mine')
        self.assertFalse(onboarding.offer_user_md(config, FakeUI(), workspace))
        self.assertEqual((workspace / 'USER.md').read_text(), 'mine')
        self.assertFalse(onboarding.offer_user_md(config, onboarding.UI(False), self.home / 'other'))


if __name__ == '__main__':
    unittest.main()

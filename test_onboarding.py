"""onboarding.py: Telegram detection, assistant connect, first sync, doctor and the starter USER.md.
Stub gog/openclaw/wacli/tailscale binaries and patched source reads; nothing contacts a real account."""
import datetime as dt
import hashlib
import json
import os
import re
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


PLUGINS = hasattr(onboarding.assistant(), 'load_connector')  # connector plugins (OSS); else built-in SETUPS


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
        self.assertEqual((onboarding.telegram_account('333333'), onboarding.telegram_account('111111')), ('work', 'default'))
        # openclaw.json is JSON5: comments, trailing commas and unquoted keys are fine; a named-only bot is found too.
        (openclaw / 'openclaw.json').write_text('{\n  // mine\n  channels: {telegram: {accounts: {bot: {botToken: "1:x", allowFrom: ["555555",],},},},},\n}')
        self.assertEqual(onboarding.telegram_owners()[:1], ['555555'])
        self.assertEqual(onboarding.telegram_account('999'), 'bot')

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

    def test_an_existing_gog_keyring_keeps_its_password(self):
        gog = self.stub('gog', '''
if args[:3] == ["auth", "credentials", "list"]: print(json.dumps({"clients": [{"client": "default"}]}))
elif args[:2] == ["auth", "list"]: print(json.dumps({"accounts": [{"email": "alex@acme.test", "client": "default"}]}))
elif os.environ.get("GOG_KEYRING_PASSWORD") == "kept": print(json.dumps({"emailAddress": "alex@acme.test", "historyId": "9"}))
else: sys.stderr.write("HTTP 401"); sys.exit(1)''')
        self.config(dict(EXAMPLE['work-gmail'], client='default'), gog=gog)
        ui = FakeUI(answers=[''], secrets=[''])
        with self.assertRaises(onboarding.SetupError) as raised:
            onboarding.connect(self.path, 'gmail', ui, restart=False)
        self.assertIn('already holds alex@acme.test', ui.text())
        self.assertIn('GOG_KEYRING_PASSWORD', raised.exception.fix)
        self.assertFalse((self.data / 'gog-password').exists())
        with mock.patch.dict(os.environ, {'GOG_KEYRING_PASSWORD': 'kept'}):
            onboarding.connect(self.path, 'gmail', FakeUI(answers=['']), restart=False)
        self.assertEqual((self.data / 'gog-password').read_text().strip(), 'kept')
        self.assertNotIn('auth add', self.calls())

    def test_boundaries_from_what_people_type(self):
        cases = {'Acme': 'work:Acme', 'personal': 'personal:me', 'Me': 'personal:me', 'family': 'personal:family',
                 'work:Beta': 'work:Beta', 'personal:Sam': 'personal:Sam', '': 'work:Main', 'work:': 'work:Main'}
        self.assertEqual({k: onboarding.boundary_label(k) for k in cases}, cases)
        self.assertEqual([onboarding.company_of(e) for e in ('alex@acme.io', 'sam@mail.globex.co.uk', 'me@gmail.com', 'alex@example.com')],
                         ['Acme', 'Globex', '', ''])
        self.assertEqual(onboarding.slug('Café Ünïon'), 'cafe-union')

    # ------------------------------------------------------------ setups that are not the example

    def test_a_shared_key_file_keeps_its_other_keys_and_saved_keys_are_offered_by_name(self):
        crm = dict(EXAMPLE['work-crm'], credential_variable='SALESFLARE_STAGING_API_KEY', api_base='https://stagingapi.example.test')
        self.config(crm)
        shared = self.data / 'salesflare.env'
        onboarding.write_private(shared, '# mine\nSALESFLARE_PROD_API_KEY=prod1\nSALESFLARE_BASE_URL=https://x\nSALESFLARE_STAGING_API_KEY=old\n')
        me = {'id': 1, 'email': 'alex@acme.test', 'team': {'id': 1}}
        ui = FakeUI(answers=['3'], secrets=['new-staging'])  # 3 = a new key
        with mock.patch.object(onboarding.assistant().Salesflare, 'get', lambda self, path, params=None: me):
            onboarding.connect(self.path, 'crm', ui, restart=False)
        self.assertEqual(shared.read_text(), '# mine\nSALESFLARE_PROD_API_KEY=prod1\nSALESFLARE_BASE_URL=https://x\nSALESFLARE_STAGING_API_KEY=new-staging\n')
        self.assertIn('the saved SALESFLARE_STAGING_API_KEY (salesflare.env)', ui.text())
        self.assertNotIn('prod1', ui.text())
        account = self.saved()['accounts'][0]
        self.assertEqual((account['api_base'], account['credential_variable']), ('https://stagingapi.example.test', 'SALESFLARE_STAGING_API_KEY'))
        ui = FakeUI(answers=['1', '2'])  # redo it, with the saved production key
        with mock.patch.object(onboarding.assistant().Salesflare, 'get', lambda self, path, params=None: me):
            onboarding.connect(self.path, 'crm', ui, restart=False)
        self.assertEqual(self.saved()['accounts'][0]['credential_variable'], 'SALESFLARE_PROD_API_KEY')
        self.assertEqual(ui.secrets, [])
        config = self.saved()
        config['accounts'][0]['credential_variable'] = 'SALESFLARE_GONE_KEY'
        self.assertEqual(onboarding.missing(config, config['accounts'][0]), 'SALESFLARE_GONE_KEY is not in salesflare.env')

    @unittest.skipUnless(PLUGINS, 'Stripe with its own key needs the connector plugin')
    def test_stripe_can_reuse_the_maton_key_slack_already_uses(self):
        slack = dict(EXAMPLE['work-slack'], email='alex@acme.test', credential_variable='MATON_WORK_KEY', connection_id='conn-slack', team_id='T1', user_id='U1')
        self.config(slack)
        onboarding.write_private(self.data / 'maton-slack.env', 'MATON_WORK_KEY=mk\n')
        ui = FakeUI(answers=['acct_123', '1', 'conn-stripe', '1'])  # account, through Maton, connection, the saved key
        onboarding.connect(self.path, 'stripe', ui, restart=False)
        stripe = self.saved()['accounts'][1]
        self.assertEqual({k: stripe[k] for k in ('transport', 'credential', 'credential_variable', 'connection_id', 'stripe_account', 'boundary')},
                         {'transport': 'maton', 'credential': 'maton-slack', 'credential_variable': 'MATON_WORK_KEY',
                          'connection_id': 'conn-stripe', 'stripe_account': 'acct_123', 'boundary': 'work:Acme'})
        self.assertEqual((self.data / 'maton-slack.env').read_text(), 'MATON_WORK_KEY=mk\n')
        self.assertEqual(ui.secrets, [])
        # A new key for one of them goes to its own file; the other keeps the shared one.
        answers = {'auth.test': {'ok': True, 'team_id': 'T1', 'user_id': 'U1'}, 'users.info': {'ok': True, 'user': {'profile': {'email': 'alex@acme.test'}}}}
        ui = FakeUI(answers=['1', '2', ''], secrets=['mk2'])  # redo the Slack account, a new key, same connection
        with mock.patch.object(onboarding.assistant().Slack, 'get', lambda self, method, params=None: answers[method]):
            onboarding.connect(self.path, 'slack', ui, restart=False)
        config = self.saved()
        self.assertEqual(config['accounts'][0]['credential'], 'work-slack')
        self.assertEqual(Path(config['credentials']['work-slack']).read_text(), 'MATON_WORK_KEY=mk2\n')
        self.assertEqual((self.data / 'maton-slack.env').read_text(), 'MATON_WORK_KEY=mk\n')

    def test_a_second_company_gets_its_own_account_named_your_way(self):
        gog = self.stub('gog', '''
if args[:2] == ["auth", "list"]:
    print(json.dumps({"accounts": [{"email": "me@one.test", "client": "mine"}, {"email": "me@two.test", "client": "mine"}]}))
elif args[:3] == ["auth", "credentials", "list"]:
    print(json.dumps({"clients": [{"client": "mine", "default": True}]}))
elif args[:2] == ["calendar", "calendars"]:
    print(json.dumps([{"id": "me@two.test", "summary": "Me", "primary": True}, {"id": "team@group.test", "summary": "Team", "selected": True}]))
elif args[:2] == ["api", "call"]:
    me = args[args.index("--account") + 1]
    me = json.loads(args[args.index("--params") + 1]).get("calendarId", "primary").replace("primary", me)
    print(json.dumps({"emailAddress": me, "historyId": "1", "id": me}))''')
        one = dict(EXAMPLE['work-gmail'], id='one-gmail', email='me@one.test', boundary='work:One', client='mine', excluded_terms=['secret-1'])
        self.config(one, EXAMPLE['personal-whatsapp'], gog=gog)
        onboarding.write_private(self.data / 'gog-password', 'pw')
        config = self.saved()
        config['credentials']['gog-password'] = str(self.data / 'gog-password')
        onboarding.write_private(self.path, json.dumps(config))
        ui = FakeUI(answers=['2', '2', 'work:Two', ''])  # add another, something else, its name, the suggested address
        onboarding.connect(self.path, 'gmail', ui, restart=False)
        two = self.saved()['accounts'][-1]
        self.assertEqual((two['id'], two['boundary'], two['email'], two['client']), ('two-gmail', 'work:Two', 'me@two.test', 'mine'))
        self.assertNotIn('excluded_terms', two)
        self.assertNotIn('personal:whatsapp', ui.text())
        self.assertNotIn('auth add', self.calls())
        ui = FakeUI(answers=['2', '', '2'])  # Two, its own Google account, then the shared team calendar
        onboarding.connect(self.path, 'calendar', ui, restart=False)
        calendar = self.saved()['accounts'][-1]
        self.assertIn('Which part of your life', ui.text())
        self.assertEqual((calendar['id'], calendar['email'], calendar['calendar_id'], calendar['display_name']),
                         ('two-calendar', 'me@two.test', 'team@group.test', 'Team'))

    def test_salesflare_on_another_server(self):
        self.config(EXAMPLE['work-crm'])
        def get(source, path, params=None):
            if source.account['api_base'] != 'https://stagingapi.example.test':
                raise onboarding.assistant().SourceError('salesflare', 401)
            return {'id': 5, 'email': 'alex@acme.test', 'team': {'id': 6}}
        with mock.patch.object(onboarding.assistant().Salesflare, 'get', get):
            onboarding.connect(self.path, 'crm', FakeUI(answers=['stagingapi.example.test'], secrets=['k']), restart=False)
        account = self.saved()['accounts'][0]
        self.assertEqual((account['api_base'], account['app_base']), ('https://stagingapi.example.test', 'https://staging.example.test'))

    def test_dashboard_on_your_own_domain_counts(self):
        self.stub('tailscale', 'print("{}")')
        config = {'port': 8787, 'dashboard_host': 'my.example.test', 'dashboard_path': '/'}
        with mock.patch.object(onboarding, 'reachable', return_value=True) as probe:
            self.assertEqual(onboarding.dashboard_url(config), 'https://my.example.test/')
        probe.assert_called_once_with('https://my.example.test/')
        with mock.patch.object(onboarding, 'reachable', return_value=False):
            self.assertIsNone(onboarding.dashboard_url(config))

    def test_connect_lists_what_this_machine_has_but_no_source_reads(self):
        gog = self.stub('gog', 'print(json.dumps({"accounts": [{"email": "me@one.test"}, {"email": "new@two.test"}]}))')
        nodes = {'paired': [{'nodeId': 'p1', 'platform': 'android', 'displayName': 'Pixel'}, {'nodeId': 'p2', 'platform': 'android'}]}
        openclaw = self.stub('openclaw', f'print(json.dumps({nodes!r}))')
        self.stub('gh', 'print("octo")')
        config = self.config(dict(EXAMPLE['work-gmail'], email='me@one.test'), dict(EXAMPLE['personal-android'], node_id='p1'), gog=gog, openclaw=openclaw)
        found = dict(onboarding.discover(config))
        self.assertEqual(found, {'gog is signed in as new@two.test, but no source reads it': 'assistant connect gmail',
                                 'gh is signed in as octo': 'assistant connect github',
                                 'an Android phone is paired with OpenClaw': 'assistant connect android'})

    @unittest.skipUnless(PLUGINS, 'the RSS connector is a plugin')
    def test_a_plugin_source_asks_its_own_setup_fields(self):
        self.config()
        onboarding.connect(self.path, 'rss', FakeUI(answers=['personal', 'https://example.com/feed.xml']), restart=False)
        account = self.saved()['accounts'][0]
        self.assertEqual((account['id'], account['service'], account['url'], account['boundary']),
                         ('feed', 'rss', 'https://example.com/feed.xml', 'personal:me'))
        with self.assertRaises(onboarding.SetupError) as raised:
            onboarding.connect(self.path, 'fax', FakeUI(), restart=False)
        self.assertIn('rss', raised.exception.fix)

    def test_every_plugin_setup_is_complete(self):
        extra = [s for s in onboarding.sources() if s not in onboarding.SERVICES]
        self.assertIn('github', extra)
        for service in extra:
            setup = onboarding.setup_of(service)
            self.assertTrue(setup, service)
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

    def test_doctor_checks_the_telegram_bot_and_the_gateway_unit(self):
        (self.home / '.openclaw').mkdir()
        (self.home / '.openclaw/openclaw.json').write_text(json.dumps({'channels': {'telegram': {'accounts': {
            'family': {'botToken': 'x', 'allowFrom': ['5']}, 'work': {'botToken': 'y', 'allowFrom': ['1000001']}}}}}))
        self.config(dict(EXAMPLE['openclaw-gateway'], unit='openclaw-gateway.service'), openclaw=str(self.bin / 'missing'))
        text = '\n'.join(f'{m} {t} | {f}' for m, t, f in onboarding.doctor(self.path))
        self.assertIn('✗ telegram_account default is not a Telegram account in OpenClaw (there: family, work) | set "telegram_account": "work"', text)
        self.assertIn('✗ openclaw-gateway (OpenClaw Gateway log): no systemd unit openclaw-gateway.service here', text)
        self.stub('systemctl', 'print("openclaw-gateway.service enabled enabled" if "list-unit-files" in args else "inactive")')
        self.config(dict(EXAMPLE['openclaw-gateway'], unit='openclaw-gateway.service'), telegram_account='family', openclaw=str(self.bin / 'missing'))
        text = '\n'.join(f'{m} {t} | {f}' for m, t, f in onboarding.doctor(self.path))
        self.assertIn('! you are approved on the Telegram account work, but cards go out through family', text)
        self.assertNotIn('no systemd unit', text)

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
        # New installs: the dashboard on its own port, so its phone app is its own origin.
        status = {'Web': {'box.example.ts.net:8443': {'Handlers': {'/': {'Proxy': 'http://127.0.0.1:8787'}}}}}
        self.stub('tailscale', f'print(json.dumps({status!r}))')
        self.assertEqual(onboarding.dashboard_served({'port': 8787}), 'https://box.example.ts.net:8443/')
        self.assertEqual(onboarding.dashboard_url({'port': 8787, 'dashboard_host': 'box.example.ts.net:8443', 'dashboard_path': '/'}),
                         'https://box.example.ts.net:8443/')
        self.assertEqual(onboarding.serve_command({'dashboard_host': 'box.example.ts.net:8443', 'dashboard_path': '/', 'port': 8787}),
                         ['tailscale', 'serve', '--bg', '--https=8443', '--set-path', '/', 'http://127.0.0.1:8787'])

    def test_tailscale_serve_says_what_is_in_the_way(self):
        self.stub('tailscale', '''
if args[:2] == ["serve", "status"]: print("{}")
else: print("Serve is not enabled on your tailnet. To enable, visit: https://login.tailscale.com/f/serve?node=abc"); sys.exit(1)''')
        ui = FakeUI()
        self.assertIsNone(onboarding.serve_dashboard({'port': 8787, 'dashboard_path': '/assistant/'}, ui))
        self.assertIn('Enable it at https://login.tailscale.com/f/serve?node=abc', ui.text())
        self.stub('tailscale', '''
if args[:2] == ["serve", "status"]: print("{}")
else: sys.stderr.write("Access denied: serve config denied"); sys.exit(1)''')
        ui = FakeUI()
        onboarding.serve_dashboard({'port': 8787}, ui)
        self.assertIn('sudo tailscale set --operator=$USER', ui.text())

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

    def test_the_openclaw_template_user_md_still_gets_an_about_me(self):
        workspace = self.home / 'seeded'
        workspace.mkdir()
        template = '# USER.md - User Model\n\n## Directives\n\n<!-- observed: YYYY-MM-DD | status: active -->\n\n- Prefer ...\n'
        (workspace / 'USER.md').write_text(template)
        self.assertTrue(onboarding.offer_user_md({'owner_name': 'Alex Owner', 'accounts': []}, FakeUI(), workspace))
        text = (workspace / 'USER.md').read_text()
        self.assertTrue(text.startswith('# USER.md - User Model'))
        self.assertIn('Name: Alex Owner', text)
        self.assertFalse(onboarding.offer_user_md({'accounts': []}, FakeUI(), workspace))

    def test_a_profile_draft_fills_the_starter_blanks_and_never_overwrites_the_owner(self):
        config = {'owner_name': 'Alex Owner', 'accounts': [dict(EXAMPLE['work-gmail'], email='alex@acme.test')]}
        starter = onboarding.starter_user_md(config)
        path = self.home / 'profile' / 'USER.md'
        path.parent.mkdir()
        path.write_text(starter)
        self.assertFalse(onboarding.says_how_you_write(path))
        draft = {'role': 'Founder at Acme', 'tone': 'Short and friendly', 'languages': ['Dutch: team', 'English: customers'],
                 'examples': ['Thanks, sounds good'], 'matters': ['Sam (Globex): weekly call'], 'based_on': '12 sent emails'}
        merged = onboarding.merge_profile(starter, draft, '2026-10-17')
        for gone in ('- Role: \n', '- Languages: \n', '(for example: short and friendly', '(customers, projects, people'):
            self.assertNotIn(gone, merged)
        for kept in ('Name: Alex Owner', 'Sign-off: Alex', '## Leave alone', '- Role: Founder at Acme', '- Languages: Dutch: team; English: customers',
                     '"Thanks, sounds good"', '### What matters\n- Sam (Globex): weekly call', '2026-10-17 · from 12 sent emails'):
            self.assertIn(kept, merged)
        path.write_text(merged)
        self.assertTrue(onboarding.says_how_you_write(path))
        # A later draft replaces only its own section, while the marker line is still there.
        mine = merged.replace('## Leave alone', '## Leave alone\n\n- Recruiters')
        again = onboarding.merge_profile(mine, {'tone': 'Warm'}, '2026-11-01')
        self.assertEqual(again.count(onboarding.PROFILE_HEADING), 1)
        self.assertIn('- Recruiters', again)
        self.assertIn('- Tone: Warm', again)
        self.assertNotIn('Founder at Acme', again)
        # Once the owner deletes the marker, the section is theirs.
        adopted = re.sub(r'^<!-- assistant profile.*\n', '', merged, flags=re.M)
        self.assertIsNone(onboarding.merge_profile(adopted, {'tone': 'Warm'}, '2026-11-01'))
        # Text the owner wrote under their own headings stays word for word.
        own = '# About me\n\n- Tone: blunt, always in English\n\n## Food\n\n- vegetarian\n'
        self.assertTrue(onboarding.merge_profile(own, draft, '2026-10-17').startswith(own.rstrip()))
        self.assertTrue(onboarding.says_how_you_write(self.home / 'missing.md') is False)

    def test_a_profile_draft_fits_in_what_openclaw_loads(self):
        own = '# About me\n\n' + '- A rule I wrote myself and want kept.\n' * 95  # about 3,700 characters
        draft = {'tone': 'Short and friendly', 'habits': 'Shares a booking link instead of trading times.',
                 'examples': ['Thanks, sounds good', 'Works for me'], 'matters': [f'Customer {n}: weekly call about the rollout' for n in range(8)],
                 'based_on': '30 email, 20 Slack'}
        self.assertEqual(onboarding.profile_room(own), 4000 - len(own.rstrip()) - 2)
        merged, left_out = onboarding.fit_profile(own, draft, '2026-10-17')
        self.assertLessEqual(len(merged), 4000)
        self.assertTrue(merged.startswith(own.rstrip()))
        self.assertIn('- Tone: Short and friendly', merged)
        self.assertEqual(left_out[:2], ['examples: Works for me', 'examples: Thanks, sounds good'])  # examples go first, last ones first
        self.assertIn('matters: Customer 7: weekly call about the rollout', left_out)
        self.assertTrue(all(line.startswith(('examples', 'matters')) for line in left_out))
        # An earlier draft section doesn't count against the room; the owner's own text always stays.
        self.assertEqual(onboarding.profile_room(merged), onboarding.profile_room(own))
        tiny, gone = onboarding.fit_profile(own * 2, draft, '2026-10-17')
        self.assertIn('- Tone: Short and friendly', tiny)  # never less than one line
        self.assertTrue(tiny.startswith((own * 2).rstrip()))
        self.assertEqual(onboarding.fit_profile('## Observed from my messages\n- mine\n', draft, '2026-10-17'), (None, []))
        # Per-channel voices: their own block, the tone blank goes, and the least used channel is cut first.
        voices = {'tone': 'Warm', 'voices': {'Email': 'Hey [name]; Regards, Alex', 'Slack': 'terse, lowercase', 'WhatsApp': 'emoji, Dutch'}}
        starter = onboarding.starter_user_md({'owner_name': 'Alex', 'accounts': []})
        merged = onboarding.merge_profile(starter, {'voices': voices['voices']}, '2026-10-17')
        self.assertIn('### Per channel\n- Email: Hey [name]; Regards, Alex\n- Slack: terse, lowercase\n- WhatsApp: emoji, Dutch', merged)
        self.assertNotIn('(for example: short and friendly', merged)
        fitted, cut = onboarding.fit_profile(own + '- one more of mine\n' * 4, voices, '2026-10-17')
        self.assertLessEqual(len(fitted), 4000)
        self.assertEqual(cut[0], 'WhatsApp voice: emoji, Dutch')
        self.assertIn('- Email: Hey [name]; Regards, Alex', fitted)


if __name__ == '__main__':
    unittest.main()

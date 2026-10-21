"""Message contents sealed at rest (message_seal.py): every source's text is unreadable without the owner's key, the
app reads, searches and exports as before, and support's learnings export never holds it."""
import base64
import importlib.util
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('assistant', ROOT / 'assistant.py')
assistant = importlib.util.module_from_spec(spec)
spec.loader.exec_module(assistant)
import learnings
import message_seal
import moving

CANARY = 'Zanzibarquill'  # in every message's text; never in a sealed database's files
SOURCES = ['gmail.message', 'outlook.message', 'imap.message', 'slack.message', 'whatsapp.message', 'telegram.message',
           'signal.message', 'linkedin.message', 'x.dm', 'teams.message', 'sms.message', 'android.notification',
           'intercom.conversation', 'github.comment', 'zoom.transcript', 'calendar.event', 'brand_new_connector.thing']


def event(i, kind, extra=None):
    return {'account_id': 'acct', 'source_id': f'{kind}:{i}', 'kind': kind, 'occurred_at': f'2026-10-{10 + i % 9:02d}T09:00:00+00:00',
            'title': f'{CANARY} subject {i}', 'body': f'Dear owner, the {CANARY} contract is ready. Óscar will sign on Friday {i}.',
            'deleted': False, 'metadata': {'thread_id': f't{i}', 'from': 'Pat <pat@example.com>', 'labels': ['INBOX'],
                                            'snippet': f'{CANARY} preview', 'attachments': [f'{CANARY}.pdf'],
                                            'a_key_no_one_reviewed_yet': f'{CANARY} quoted text', **(extra or {})}}


def files(path):
    return b''.join(p.read_bytes() for p in Path(path).parent.glob(Path(path).name + '*'))


class Sealer(unittest.TestCase):
    def setUp(self):
        self.sealer = message_seal.Sealer(os.urandom(32))

    def test_round_trip_and_fresh_nonce(self):
        for text in ['a', 'Grüße 👋\n' * 500, CANARY]:
            sealed = self.sealer.seal(text)
            self.assertTrue(message_seal.is_sealed(sealed))
            self.assertNotIn(text.encode(), sealed)
            self.assertEqual(self.sealer.open(sealed), text)
        self.assertNotEqual(self.sealer.seal('same'), self.sealer.seal('same'))
        for value in [None, '', 3, b'\x00plain blob']:
            self.assertEqual(self.sealer.seal(value), value)
            self.assertEqual(self.sealer.open(value), value)

    def test_tampering_and_another_key_fail_loudly(self):
        sealed = bytearray(self.sealer.seal('pay the invoice'))
        sealed[-20] ^= 1
        with self.assertRaises(message_seal.SealError):
            self.sealer.open(bytes(sealed))
        with self.assertRaises(message_seal.SealError):
            message_seal.Sealer(os.urandom(32)).open(self.sealer.seal('pay the invoice'))

    def test_metadata_keeps_only_lookup_keys_in_the_clear_new_keys_sealed(self):
        text = assistant.encoded(event(1, 'gmail.message')['metadata'])
        sealed = self.sealer.seal_json(text)
        self.assertNotIn(CANARY, sealed)
        self.assertEqual(set(json.loads(sealed)), {'thread_id', 'from', 'labels', message_seal.SEALED_KEY})
        self.assertEqual(self.sealer.open_json(sealed), text)  # byte for byte what the app wrote (sorted keys)
        self.assertEqual(self.sealer.seal_json(sealed), sealed)
        only_plain = assistant.encoded({'thread_id': 'x', 'from_me': True})
        self.assertEqual(self.sealer.seal_json(only_plain), only_plain)
        self.assertTrue(message_seal.is_sealed(self.sealer.seal_json('"not an object"', frozenset())))

    def test_index_words_fold_like_sqlite_and_hide_the_word(self):
        self.assertEqual(self.sealer.words('Óscar, OSCAR oscar'), ' '.join([self.sealer.words('oscar')] * 3))
        self.assertNotIn('oscar', self.sealer.words('Óscar'))
        self.assertEqual(self.sealer.words('e-mail'), self.sealer.words('e mail'))


class Protected(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'events.sqlite'

    def fill(self, store):
        store.commit_page('acct', {'cursor': 1}, [event(i, kind) for i, kind in enumerate(SOURCES)])
        with store.connect() as db:
            db.execute("INSERT INTO suggestions(id,dedupe_key,title,body,event_ids,account_ids,created_at,next_action,outcome) "
                       "VALUES('s1','k1',?,?,'[1]','[\"acct\"]','2026-10-10',?,?)",
                       (f'Reply about {CANARY}', f'Draft: {CANARY} looks good', json.dumps({'kind': 'email', 'body': CANARY}),
                        json.dumps({'state': 'needs_you', 'needs': CANARY})))
            db.execute("INSERT INTO draft_edits VALUES('d1','s1','a','b','edit',?,?,?,?,'owner','2026-10-10')", (CANARY,) * 4)
            db.execute("INSERT INTO facts(text,decision_id,created_at) VALUES('Prefers short replies','x','2026-10-10')")

    def test_every_source_is_sealed_at_rest_and_the_app_reads_and_searches_as_before(self):
        store = assistant.Store(self.path, protect=True)
        self.fill(store)
        self.assertNotIn(CANARY.encode(), files(self.path))
        self.assertNotIn(CANARY.lower().encode(), files(self.path))
        rows = store.events(['acct'], limit=100)
        self.assertEqual({r['kind'] for r in rows}, set(SOURCES))
        self.assertTrue(all(CANARY in r['title'] and CANARY in r['body'] and r['metadata']['snippet'] for r in rows))
        self.assertEqual(len(store.events(['acct'], query=CANARY.lower())), len(SOURCES))
        self.assertEqual(len(store.events(['acct'], query='oscar friday')), len(SOURCES))  # folded like before
        self.assertEqual(store.events(['acct'], query='absent'), [])
        with store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM events WHERE json_extract(metadata,'$.thread_id')='t3'").fetchone()[0], 1)
            card = db.execute('SELECT * FROM suggestions').fetchone()
            self.assertIn(CANARY, card['title'])
            self.assertEqual(json.loads(card['next_action'])['body'], CANARY)
            db.execute("INSERT INTO event_search(event_search,rank) VALUES('integrity-check',1)")
        raw = sqlite3.connect(self.path)
        self.assertEqual(raw.execute("SELECT json_extract(next_action,'$.kind'), json_extract(outcome,'$.state') FROM suggestions").fetchone(),
                         ('email', 'needs_you'))
        self.assertEqual(raw.execute("SELECT text FROM facts").fetchone()[0], 'Prefers short replies')  # a learning stays readable

    def test_without_the_key_nothing_reads_or_writes_message_text(self):
        store = assistant.Store(self.path, protect=True)
        self.fill(store)
        raw = sqlite3.connect(self.path)
        self.assertEqual(raw.execute('SELECT typeof(title), typeof(body) FROM events LIMIT 1').fetchone(), ('blob', 'blob'))
        with self.assertRaises(sqlite3.OperationalError):  # a tool without the key can't slip plain text in
            raw.execute("INSERT INTO events(account_id,source_id,kind,occurred_at,title,body,metadata,content_hash,ingested_at) "
                        "VALUES('a','b','k','t','plain','plain','{}','h','t')")
        message_seal.key_path(self.path).rename(self.path.with_name('elsewhere'))
        with self.assertRaises(message_seal.SealError):
            assistant.Store(self.path)

    def test_a_path_that_forgets_to_seal_is_sealed_by_the_database(self):
        store = assistant.Store(self.path, protect=True)
        with store.connect() as db:  # plain SQL, as a future code path might write
            db.execute("INSERT INTO events(account_id,source_id,kind,occurred_at,title,body,metadata,content_hash,ingested_at) "
                       "VALUES('acct','x','new.kind','2026-10-10',?,?,?,'h','t')", (CANARY, CANARY, json.dumps({'note': CANARY})))
            db.execute("UPDATE events SET body=? WHERE source_id='x'", (CANARY + ' again',))
        self.assertNotIn(CANARY.encode(), files(self.path))
        self.assertEqual(store.events(['acct'], query=CANARY.lower())[0]['body'], CANARY + ' again')

    def test_a_table_made_later_is_guarded_and_sealed(self):
        store = assistant.Store(self.path, protect=True)
        with store.connect() as db:
            db.execute('DROP TABLE IF EXISTS network_replies')
        with sqlite3.connect(self.path) as raw:  # made, and written to, by code that knew nothing of sealing
            raw.execute('CREATE TABLE network_replies(ask_id TEXT PRIMARY KEY,body TEXT NOT NULL,at TEXT NOT NULL)')
            raw.execute("INSERT INTO network_replies VALUES('a',?,'t')", (CANARY,))
        with store.connect() as db:
            self.assertEqual(db.execute('SELECT body FROM network_replies').fetchone()[0], CANARY)
            db.execute("INSERT INTO network_replies VALUES('b',?,'t')", (CANARY,))
        with store.connect():
            pass
        self.assertEqual(sqlite3.connect(self.path).execute("SELECT count(*) FROM network_replies WHERE typeof(body)='blob'").fetchone()[0], 2)

    def test_migration_seals_existing_messages_and_rolls_back(self):
        plain = assistant.Store(self.path)
        self.fill(plain)
        self.assertIn(CANARY.encode(), files(self.path))
        self.assertEqual(len(plain.events(['acct'], query=CANARY.lower())), len(SOURCES))
        sealed = assistant.Store(self.path, protect=True)
        self.assertNotIn(CANARY.encode(), files(self.path))  # VACUUM: no free page keeps the old text
        self.assertFalse(self.path.with_name('events.sqlite.before-protection').exists())  # the copy goes once checked
        self.assertEqual(len(sealed.events(['acct'], query=CANARY.lower())), len(SOURCES))
        self.assertTrue(message_seal.is_protected(sqlite3.connect(self.path)))
        assistant.Store(self.path)  # no setting: stays as it is
        self.assertTrue(message_seal.is_protected(sqlite3.connect(self.path)))
        opened = assistant.Store(self.path, protect=False)
        self.assertFalse(message_seal.is_protected(sqlite3.connect(self.path)))
        self.assertIn(CANARY.encode(), files(self.path))
        self.assertEqual(len(opened.events(['acct'], query=CANARY.lower())), len(SOURCES))
        opened.commit_page('acct', {'cursor': 2}, [event(99, 'slack.message')])
        self.assertEqual(sqlite3.connect(self.path).execute("SELECT count(*) FROM events WHERE typeof(body)='blob'").fetchone()[0], 0)
        with opened.connect() as db:
            db.execute("INSERT INTO event_search(event_search,rank) VALUES('integrity-check',1)")

    def test_a_failed_migration_changes_nothing(self):
        plain = assistant.Store(self.path)
        self.fill(plain)
        original = message_seal.protect
        message_seal.protect = lambda db: (original(db), (_ for _ in ()).throw(RuntimeError('power cut')))
        try:
            with self.assertLogs(assistant.LOG, 'ERROR'):  # logged; the app starts as it was
                self.assertFalse(assistant.Store(self.path, protect=True).protected)
        finally:
            message_seal.protect = original
        self.assertFalse(message_seal.is_protected(sqlite3.connect(self.path)))
        self.assertTrue(self.path.with_name('events.sqlite.before-protection').exists())  # kept for a look
        self.assertEqual(len(assistant.Store(self.path).events(['acct'], query=CANARY.lower())), len(SOURCES))

    def test_an_export_opens_the_messages_and_leaves_the_key_behind(self):
        config = Path(self.temp.name) / 'config.json'
        config.write_text(json.dumps({'database': str(self.path), 'accounts': []}))
        self.fill(assistant.Store(self.path, protect=True))
        archive, _ = moving.export(config, Path(self.temp.name) / 'out', workspace=Path(self.temp.name) / 'ws',
                                   openclaw_home=Path(self.temp.name) / 'oc')
        import zipfile
        with zipfile.ZipFile(archive) as z:
            self.assertNotIn(f'data/{message_seal.KEY_NAME}', z.namelist())
            db = Path(self.temp.name) / 'exported.sqlite'
            db.write_bytes(z.read('data/events.sqlite'))
        self.assertFalse(message_seal.is_protected(sqlite3.connect(db)))
        self.assertIn(CANARY.encode(), db.read_bytes())

    def test_learnings_hold_what_the_machine_learned_never_message_text(self):
        store = assistant.Store(self.path, protect=True)
        self.fill(store)
        with store.connect() as db:
            db.execute("INSERT INTO decisions VALUES('d','s1','dismiss','Too pushy','dashboard','2026-10-11')")
        workspace = Path(self.temp.name) / 'ws'
        (workspace / 'memory').mkdir(parents=True)
        (workspace / 'USER.md').write_text('# About me\nShort answers.')
        (workspace / 'memory' / 'notes.md').write_text('Likes Fridays off.')
        out = learnings.export(self.path, workspace, {'accounts': [{'id': 'gmail:owner@example.com', 'service': 'gmail'}],
                                                      'credentials': {'api-token': '/secret'}})
        text = json.dumps(out)
        self.assertNotIn(CANARY.lower(), text.lower())
        self.assertNotIn('owner@example.com', text)
        self.assertNotIn('/secret', text)
        self.assertIn('Too pushy', text)
        self.assertIn('Prefers short replies', text)
        self.assertEqual(out['notes']['USER.md'], '# About me\nShort answers.')
        self.assertEqual(out['tables']['suggestions'][0]['status'], 'pending')
        self.assertEqual(out['tables']['action_jobs'], [])
        self.assertEqual(sum(r['n'] for r in out['counts']['signals']), len(SOURCES))
        self.assertEqual(learnings.export(self.path)['tables']['suggestions'][0]['kind'], 'gmail.message')


class NothingOutsideTheSealedFields(unittest.TestCase):
    def test_learnings_never_select_a_sealed_column(self):
        sealed = {('events', c) for c in ('title', 'body', 'metadata')}
        sealed |= {(t, c) for t, cs in message_seal.DERIVED.items() for c in cs}
        sealed |= {(t, c) for t, c, _ in message_seal.DERIVED_JSON}
        for table, columns in learnings.TABLES.items():
            for column in columns:
                self.assertNotIn((table, column), sealed)
                if ' AS ' in column:  # computed: only a JSON column's plain keys, counts, or another table's kind
                    for t, c, plain in message_seal.DERIVED_JSON:
                        if t == table and c in column:
                            self.assertTrue(re.search(r"'\$\.(\w+)'", column)[1] in plain)

    def test_every_text_column_that_could_hold_a_message_is_classified(self):
        """A new table or column whose name says it may hold message text has to be sealed (message_seal.DERIVED) or
        listed here with the reason it is not a message."""
        not_messages = {
            'events.content_hash': 'a hash', 'facts.text': 'a learned fact', 'rule_changes.text': 'a learned rule',
            'decisions.feedback': "the owner's feedback", 'decision_annotations.reason': 'a status reason',
            'profile_runs.draft': 'profile notes (USER.md)', 'skills.body': 'a skill', 'skills.description': 'a skill',
            'skills.note': 'a skill', 'skill_hints.summary': 'a skill hint', 'tool_approval_proposals.title': 'a proposal',
            'tool_approval_proposals.detail': 'a proposal', 'checkpoints.state': 'a sync cursor (MESSAGE_SEAL_AUDIT checks)',
            'analyses.items': 'ids and versions', 'preparations.details': 'why a preparation stopped',
            'preparations.reason': 'a reason code', 'promises.what': 'matched in SQL (unique per message); a known gap',
            'promises.note': 'a known gap, with promises.what',
            'android_inbox.payload': "written by the Gateway's plugin without the key; deleted once read (a known gap)",
            'network_outbox.body': 'matched in SQL until sent; sealed end to end in transit (network_crypto)',
            'fleet_log.what': "a log of what was shared, by kind", 'fleet_out.context': 'matched in SQL: an id',
            'fleet_rules.rule': 'a sharing rule', 'fleet_briefs.view': 'counts and dates (fleet brief)',
            'fleet_notices.view': 'a Fleet notice', 'fleet_notices.reason': 'a reason code', 'fleet_splits.title': 'a meeting title',
            'graph_nodes.info': 'people and organisations', 'network_people.reasons': 'why someone is suggested',
        }
        sealed = {f'{t}.{c}' for t, cs in message_seal.DERIVED.items() for c in cs} | {f'{t}.{c}' for t, c, _ in message_seal.DERIVED_JSON}
        sealed |= {'events.title', 'events.body', 'events.metadata'}
        words = re.compile(r'title|body|subject|text|snippet|question|answer|summary|draft|what|payload|params|result|preview|'
                           r'description|note|content|feedback|reason|detail|view|info|items|context|rule|state|transcript|message$')
        with tempfile.TemporaryDirectory() as temp:
            store = assistant.Store(Path(temp) / 'events.sqlite')
            import discovery, fleet, fleet_requests, network, skills
            with store.connect() as db:
                for module in (network, fleet, skills, fleet_requests, discovery):
                    db.executescript(module.SCHEMA)
                fleet.Fleet.ensure_schema(db) if hasattr(fleet.Fleet, 'ensure_schema') else None
                for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'event_search%'"):
                    for _, column, kind, *_ in db.execute(f'PRAGMA table_info({table})'):
                        name = f'{table}.{column}'
                        if kind.upper() in ('TEXT', '') and words.search(column) and not column.endswith(('_id', '_at', 'status', '_kind', '_until', '_state', '_hash')):
                            with self.subTest(column=name):
                                self.assertTrue(name in sealed or name in not_messages, f'{name}: seal it or say why it is no message')

    def test_connectors_store_only_through_the_sealed_path(self):
        for path in sorted((ROOT / 'connectors').glob('*.py')) + sorted((ROOT / 'integrations').glob('*.py')):
            text = path.read_text()
            with self.subTest(path=path.name):
                self.assertNotRegex(text, r'\bsqlite3\b|INSERT\s+INTO|UPDATE\s+\w+\s+SET')
        writes = [m for p in ROOT.glob('*.py') if not p.name.startswith('test_')
                  for m in re.finditer(r'(INSERT(?: OR \w+)? INTO|UPDATE) events\b', p.read_text()) if p.name != 'assistant.py']
        self.assertEqual(writes, [])

    def test_a_connector_putting_message_text_in_a_plain_field_is_caught(self):
        leaky = event(1, 'new.connector', {'thread_id': 'Dear owner the zanzibarquill contract is ready'})
        self.assertEqual(message_seal.plain_leaks(leaky), ['metadata.thread_id'])
        self.assertEqual(message_seal.plain_leaks(event(1, 'new.connector'), {'cursor': 'abc'}), [])
        self.assertEqual(message_seal.plain_leaks(event(1, 'x'), {'last': 'the zanzibarquill contract is ready'}), ['cursor'])

    def test_every_connector_keeps_message_text_in_the_sealed_fields(self):
        """The connector suites again, with commit_page refusing any event whose text shows in a plain field."""
        suites = ['test_connectors', 'test_new_connectors', 'test_x', 'test_linkedin', 'test_whatsapp', 'test_ms_graph',
                  'test_salesflare', 'test_connector_trello', 'test_github_stripe', 'test_connector_shortcut', 'test_email_bodies',
                  'test_gmail_exclusions', 'test_android_coalescing', 'test_gateway', 'test_ingestion_recovery']
        result = subprocess.run([sys.executable, '-m', 'unittest', *suites], cwd=ROOT, capture_output=True, text=True, timeout=900,
                                env={**os.environ, 'MESSAGE_SEAL_AUDIT': '1'})
        self.assertNotIn('keeps message text in the clear', result.stderr)
        self.assertEqual(result.returncode, 0, result.stderr[-3000:])


if __name__ == '__main__':
    unittest.main()

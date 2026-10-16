"""X connector plugin: mentions, replies and DMs from the x-inbox page shape, events and sync. No live calls."""
import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('assistant_x', Path(__file__).with_name('assistant.py'))
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)
x = a.load_connector('x')
H = a.CONNECTOR_HELPERS

ACCOUNT = {'id': 'x-sf', 'service': 'x', 'transport': 'x-inbox', 'handle': 'acme', 'primary': 'owner',
           'email': '@acme', 'boundary': 'work:Example'}
NOW = '2026-10-16T10:00:00+00:00'


def mention(**extra):
    return {'id': '1978000000000000001', 'at': 1792130000000, 'text': '@acme does the Gmail sync work with shared inboxes?', 'lang': 'en',
            'conversation_id': '1977000000000000000',
            'author': {'id': '42', 'name': 'Ada', 'screen_name': 'ada', 'followers': 1200, 'verified': False},
            'reply_to': {'id': '1977000000000000000', 'screen_name': 'acme', 'to_account': True, 'text': 'New: shared inboxes are here.'},
            'quoted': None, **extra}


def dm(**extra):
    return {'id': '1978000000000000009', 'at': 1792131000000, 'conversation_id': '42-777', 'from_me': False,
            'from': {'id': '42', 'name': 'Ada', 'screen_name': 'ada'}, 'with': 'Ada', 'group': False, 'request': True,
            'text': 'Hi! Could someone help me with an invoice?', **extra}


def page(mentions=(), dms=(), state=None):
    return {'account': {'id': '777', 'screen_name': 'acme'}, 'mentions': list(mentions), 'dms': list(dms),
            'state': state or {'mention_since_id': '1978000000000000001', 'dm_cursor': 'C1'}}


class Source:
    def __init__(self, *pages):
        self.pages, self.calls = list(pages), []

    def page(self, since, state):
        self.calls.append((since, state))
        return self.pages.pop(0)


class XSource(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = a.Store(Path(self.tmp.name) / 'a.sqlite')
        a.set_timezone('Europe/Brussels')

    def test_events_carry_who_what_it_answers_and_a_link(self):
        reply, message = x.events(ACCOUNT, page([mention()], [dm()]), H)
        self.assertEqual(reply['kind'], 'x.mention')
        self.assertEqual(reply['title'], 'Ada (@ada) replied to @acme')
        self.assertIn('[In reply to @acme: “New: shared inboxes are here.”]', reply['body'])
        self.assertEqual(reply['metadata']['source_url'], 'https://x.com/ada/status/1978000000000000001')
        self.assertEqual(message['kind'], 'x.dm')
        self.assertTrue(message['metadata']['request'])
        self.assertEqual(message['metadata']['source_url'], 'https://x.com/messages/42-777')
        self.assertEqual(x.events(ACCOUNT, page(dms=[dm(from_me=True)]), H)[0]['metadata']['from'], 'You')
        plain = x.events(ACCOUNT, page([mention(reply_to=None)]), H)[0]
        self.assertEqual(plain['title'], 'Ada (@ada) mentioned @acme')
        for bad in (page([mention(id='x')]), page([mention(at='soon')]), page(dms=[dm(from_me=None)])):
            with self.assertRaises(a.SourceError):
                x.events(ACCOUNT, bad, H)

    def test_sync_keeps_the_cursor_dedupes_and_sleeps_at_night(self):
        src = Source(page([mention(), mention()], [dm()]), page([mention()], [], {'mention_since_id': '1978000000000000001', 'dm_cursor': 'C2'}))
        with patch.object(a, 'now', return_value=NOW):
            self.assertEqual(x.sync(self.store, {}, ACCOUNT, H, src)['events'], 2)
            x.sync(self.store, {}, ACCOUNT, H, src)
        self.assertEqual(src.calls[0][1], {})
        self.assertEqual(src.calls[1][1], {'mention_since_id': '1978000000000000001', 'dm_cursor': 'C1'})
        self.assertEqual(self.store.checkpoint('x-sf')['page']['dm_cursor'], 'C2')
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM events').fetchone()[0], 2)
        with patch.object(a, 'now', return_value='2026-10-16T22:30:00+00:00'):  # 00:30 local time
            x.sync(self.store, {}, ACCOUNT, H, src)
        self.assertEqual(len(src.calls), 2)

    def test_reader_maps_errors_and_reads_only(self):
        source = x.Source({'x_inbox': '/bin/x-inbox-test'}, ACCOUNT, H)
        ok = subprocess.CompletedProcess([], 0, json.dumps(page([mention()])), '')
        with patch.object(a.subprocess, 'run', return_value=ok) as run:
            source.page(5, {'dm_cursor': 'C1'})
        command = run.call_args.args[0]
        self.assertEqual(command[:6], ['/bin/x-inbox-test', 'sync', '--handle', 'acme', '--primary', 'owner'])
        self.assertEqual(json.loads(command[command.index('--state') + 1]), {'dm_cursor': 'C1'})
        for stderr, code in (('the OpenClaw browser is busy', 'x_browser_busy'), ('X is signed out in the OpenClaw browser', 'x_signed_out'),
                             ('@acme is not signed in in the OpenClaw browser', 'x_signed_out'), ('X rate limit reached', 'x_rate_limited'),
                             ('X read failed: boom', 'x_read_failed')):
            with patch.object(a.subprocess, 'run', return_value=subprocess.CompletedProcess([], 2, '', 'x-inbox: ' + stderr)):
                with self.assertRaises(a.SourceError) as e:
                    source.page(5, {})
                self.assertEqual(str(e.exception), code)
        with patch.object(a.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '{"mentions": []}', '')):
            with self.assertRaises(a.SourceError):
                source.page(5, {})

    def test_review_context_source_line_and_promises(self):
        with patch.object(a, 'now', return_value=NOW):
            self.store.commit_page('x-sf', {}, x.events(ACCOUNT, page([], [dm(id='1', at=1792130000000, text='Hello?'), dm()]), H))
        with self.store.connect() as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM events ORDER BY occurred_at")]
        context = self.store.conversation_context([rows[1]])
        self.assertEqual(len(context[rows[1]['id']]), 1)
        self.assertTrue(context[rows[1]['id']][0].endswith('Ada (@ada): Hello?'))
        self.assertTrue(a.source_line([rows[1]]).startswith('Source: X message from Ada (@ada) · '))
        watch = a.awaiting_key(rows[1])
        self.assertEqual(watch['conversation'], '42-777')
        self.assertTrue(a.awaiting_match(watch, {**rows[1], 'metadata': {**json.loads(rows[1]['metadata']), 'message_id': '2'}}))


if __name__ == '__main__':
    unittest.main()

"""Card quality (hit rate), follow-ups on other people's promises, and checks before a result is saved."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('quality_assistant', Path(__file__).with_name('assistant.py'))
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)


def mail(source_id, at, sender='Sam <sam@example.com>', thread='t1', labels=('INBOX',), title='Contract'):
    return {'account_id': 'work', 'source_id': source_id, 'kind': 'gmail.message', 'occurred_at': at, 'title': title,
            'body': 'I will send the contract by Friday.', 'deleted': False,
            'metadata': {'from': sender, 'thread_id': thread, 'labels': list(labels)}}


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = a.Store(Path(self.temp.name) / 'events.sqlite')
        self.app = object.__new__(a.App)
        self.app.store = self.store
        self.app.config = {'accounts': [{'id': 'work', 'service': 'gmail', 'transport': 'gog', 'email': 'owner@example.com', 'boundary': 'work:Example'}]}

    def q(self, sql, *args):
        with self.store.connect() as db:
            return dict(db.execute(sql, args).fetchone())

    def deliver(self, sent):
        class Noon(a.dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return a.dt.datetime(2026, 10, 14, 12, tzinfo=a.ZoneInfo('Europe/Brussels')).astimezone(tz or a.dt.timezone.utc)
        with patch.object(a.dt, 'datetime', Noon), patch.object(a, 'telegram', side_effect=lambda *x, **k: (sent.append(x), {'messageId': '7'})[1]):
            a.deliver_one(self.store, self.app.config, edit_cards=False)


class OthersPromises(Base):
    def setUp(self):
        super().setUp()
        self.store.commit_page('work', {}, [mail('promise', '2026-10-10T09:00:00+00:00'),
                                            mail('mine', '2026-10-10T10:00:00+00:00', 'Owner <owner@example.com>', labels=('SENT',))])

    def follow_up(self, **extra):
        return self.app.action('suggest', {'dedupe_key': 'contract', 'title': "Contract from Sam hasn't arrived", 'body': 'Sam promised it by Friday.',
                                           'event_ids': [1], 'awaiting_event_id': 1, 'due': '2026-10-20', **extra})

    def test_follow_up_waits_for_the_deadline_and_needs_their_message(self):
        made = self.follow_up()
        self.assertIn('closes itself', made['awaiting'])
        self.assertTrue(made['deliver_after'].startswith('2026-10-21T06:30'))  # the morning after, 08:30 Brussels
        with self.assertRaises(ValueError):  # The owner's own sent email is not someone else's promise
            self.app.action('suggest', {'dedupe_key': 'x', 'title': 'T', 'body': 'B', 'event_ids': [2], 'awaiting_event_id': 2, 'due': '2026-10-16'})
        with self.assertRaises(ValueError):  # without a deadline it would go out right away
            self.app.action('suggest', {'dedupe_key': 'y', 'title': 'T', 'body': 'B', 'event_ids': [1], 'awaiting_event_id': 1})
        with self.assertRaises(ValueError):
            self.app.action('suggest', {'dedupe_key': 'z', 'title': 'T', 'body': 'B', 'event_ids': [1], 'awaiting_event_id': 2, 'due': '2026-10-16'})

    def test_closes_itself_when_they_write_first(self):
        sid = self.follow_up()['id']
        # The owner's own message in the thread doesn't count; theirs (a new thread, same address) does.
        self.store.commit_page('work', {}, [mail('nudge', '2026-10-11T09:00:00+00:00', 'Owner <owner@example.com>', labels=('SENT',)),
                                            mail('sent-it', '2026-10-12T09:00:00+00:00', thread='t2', title='The contract')])
        sent = []
        self.deliver(sent)
        self.assertEqual(sent, [])
        self.assertEqual(self.q('SELECT status FROM suggestions WHERE id=?', sid)['status'], 'completed')
        self.assertEqual(self.q('SELECT status FROM deliveries WHERE suggestion_id=?', sid)['status'], 'cancelled')
        self.assertIn('They followed up: The contract', self.q("SELECT feedback FROM decisions WHERE suggestion_id=?", sid)['feedback'])
        self.assertIsNone(self.store.quality(30, '2026-10-30T00:00:00+00:00')['hit_rate'])  # never reached the owner: not counted

    def test_arrives_when_nothing_came(self):
        sid = self.follow_up()['id']
        self.store.commit_page('work', {}, [mail('other', '2026-10-12T09:00:00+00:00', 'Kim <kim@example.com>', thread='t9')])
        with self.store.connect() as db:
            db.execute("UPDATE suggestions SET deliver_after='2026-10-14T06:00:00+00:00' WHERE id=?", (sid,))
        sent = []
        self.deliver(sent)
        self.assertEqual(len(sent), 1)
        self.assertEqual(self.q('SELECT status FROM suggestions WHERE id=?', sid)['status'], 'pending')


class HitRate(Base):
    def card(self, key, kind='gmail.message', *choices, status=None):
        n = len(self.store.events(['work'], limit=1000)) + 1
        self.store.commit_page('work', {}, [{**mail(key, a.now()), 'kind': kind}])
        sid = self.store.suggest(key, 'Card ' + key, 'Body', [n])['id']
        for i, choice in enumerate(choices):
            self.store.decide(sid, choice, 'Shorter' if choice == 'other' else '', f'{key}-{i}', 'dashboard')
        return sid

    def test_hit_rate_first_time_and_sources_reach_dashboard_reviews_and_recap(self):
        self.card('a', 'gmail.message', 'accept')
        self.card('b', 'gmail.message', 'accept', 'other')
        self.card('c', 'gmail.message', 'complete')
        self.card('d', 'whatsapp.message', 'reject')
        self.card('e', 'whatsapp.message', 'reject')
        self.card('f', 'whatsapp.message', 'reject')
        self.card('g', 'gmail.message')
        q = self.store.quality()
        self.assertEqual((q['cards'], q['worth_it'], q['dismissed'], q['open'], q['hit_rate']), (7, 3, 3, 1, 50))
        self.assertEqual((q['do_it'], q['first_time'], q['first_time_rate']), (2, 1, 50))
        self.assertEqual(q['by_source']['WhatsApp'], {'worth_it': 0, 'dismissed': 3, 'open': 0})
        summary = self.store.preference_summary()
        self.assertTrue(summary.startswith('Cards in the last 30 days by source'))
        self.assertIn('- WhatsApp: 0 of 3 worth it', summary)
        self.assertIn('- email: 3 of 3 worth it', summary)
        self.assertEqual(self.store.snapshot(self.app.config['accounts'])['quality']['previous']['hit_rate'], None)
        recap = a.weekly_recap(self.store, self.app.config)
        self.assertIn('**Hit rate:** 3 of 6 cards you decided on were worth it, 50%.', recap)
        self.assertIn('Do it got it right the first time 1 of 2 times.', recap)


class CheckBeforeSaving(Base):
    def setUp(self):
        super().setUp()
        self.store.commit_page('work', {}, [mail('one', a.now())])
        self.sid = self.store.suggest('task', 'Reply to Sam', 'I can draft it.', [1])['id']
        self.store.decide(self.sid, 'accept', '', 'go', 'telegram')

    def save(self, body, **extra):
        email = {'kind': 'email', 'account_id': 'work', 'to': ['sam@example.com'], 'subject': 'Contract', 'body': body}
        return self.app.action('prepare', {'suggestion_id': self.sid, 'body': 'Drafted.', 'next_action': email, **extra})

    def test_placeholders_and_missing_attachments_go_back_to_the_run(self):
        with self.assertRaisesRegex(ValueError, r'Check before saving: .*placeholder \(\[Name\]\).*mentions an attachment'):
            self.save('Hi [Name], please find attached the contract.\n\nCheers,\n\nAlex')
        self.assertEqual(self.q('SELECT status FROM suggestions WHERE id=?', self.sid)['status'], 'accepted')
        with self.assertRaisesRegex(ValueError, 'no email next action'):
            self.app.action('prepare', {'suggestion_id': self.sid, 'body': 'Draft:\nSubject: Contract\nHi Sam'})
        self.assertEqual(self.save('Hi Sam, thanks for the attached invoice.\n\nCheers,\n\nAlex')['status'], 'prepared')

    def test_checked_saves_a_deliberate_one(self):
        self.assertEqual(self.save('Hi Sam, see attached [TBD].\n\nCheers,\n\nAlex', checked=True)['status'], 'prepared')


if __name__ == '__main__':
    unittest.main()

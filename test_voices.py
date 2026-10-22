"""How the owner writes, per channel: voices learned from their notes, edits and own messages, and what a draft gets.

Voices are a short block per channel (and All). A voice study reads the owner's own messages per channel and writes
each block; a style note on a card rewrites that channel's block; the owner edits them in Settings; a profile run's
channel lines fill empty ones. A preparation gets every voice (its card's channel
marked), the owner's own recent messages on that channel (same conversation first) and earlier edits on that channel
only. A draft the owner answered themselves instead becomes an edit to learn from. All local; nothing is sent."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('voices_assistant', Path(__file__).with_name('assistant.py'))
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)


def mail(source_id, at, body, thread='t1', sender='Sam <sam@example.com>', to='Owner <owner@example.com>', labels=('INBOX',)):
    return {'account_id': 'work', 'source_id': source_id, 'kind': 'gmail.message', 'occurred_at': at, 'title': 'Re: Contract',
            'body': body, 'deleted': False, 'metadata': {'from': sender, 'to': to, 'thread_id': thread, 'labels': list(labels)}}


def chat(source_id, at, body, chat_id='c1', mine=False):
    return {'account_id': 'phone', 'source_id': source_id, 'kind': 'whatsapp.message', 'occurred_at': at, 'title': 'Kim',
            'body': body, 'deleted': False, 'metadata': {'chat_id': chat_id, 'from_me': mine, 'chat_name': 'Kim'}}


SENT = dict(sender='Owner <owner@example.com>', to='Sam <sam@example.com>', labels=('SENT',))


class Voices(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = a.Store(Path(self.temp.name) / 'events.sqlite')
        self.store.accounts = [{'id': 'work', 'service': 'gmail', 'email': 'owner@example.com'}, {'id': 'phone', 'service': 'whatsapp'}]
        self.app = object.__new__(a.App)
        self.app.store = self.store
        self.app.config = {'accounts': self.store.accounts}
        self.store.commit_page('work', {}, [
            mail('in1', '2026-10-01T09:00:00+00:00', 'Can you send the contract?'),
            mail('out1', '2026-10-01T10:00:00+00:00', 'Hey Sam,\n\nHere it is.\n\nRegards,\n\nOwner', **SENT),
            mail('out2', '2026-10-02T10:00:00+00:00', 'Hi Lee,\n\nAll good, thanks!\n\nRegards,\n\nOwner', thread='t2',
                 sender='Owner <owner@example.com>', to='Lee <lee@example.org>', labels=('SENT',)),
        ])
        self.store.commit_page('phone', {}, [
            chat('w1', '2026-10-01T08:00:00+00:00', 'Are you coming tonight?'),
            chat('w2', '2026-10-01T08:05:00+00:00', 'ja tuurlijk, tot straks', mine=True),
            chat('w3', '2026-10-01T08:06:00+00:00', 'Sent a photo', mine=True),
        ])
        self.sid = self.store.suggest('contract', 'Send Sam the contract', 'Sam asked for it.', [1])['id']
        self.chat_sid = self.store.suggest('tonight', 'Answer Kim about tonight', 'Kim asks.', [4])['id']
        self.note = self.store.decide(self.chat_sid, 'other', 'no capitals on whatsapp please', 'n1', 'dashboard')

    def note_id(self):
        with self.store.connect() as db:
            return db.execute("SELECT id FROM decisions WHERE choice='other' AND feedback!=''").fetchone()['id']

    def test_a_style_note_rewrites_that_channels_voice_and_only_owner_input_teaches_it(self):
        with self.assertRaisesRegex(ValueError, 'own written notes'):  # no note: a message can't set how the owner writes
            self.app.action('remember_fact', {'kind': 'voice', 'channel': 'WhatsApp', 'text': 'Formal, full sentences.'})
        with self.assertRaisesRegex(ValueError, 'own written notes'):
            self.app.action('remember_fact', {'kind': 'voice', 'channel': 'WhatsApp', 'text': 'Formal.', 'decision_id': 'nope'})
        saved = self.app.action('remember_fact', {'kind': 'voice', 'channel': 'whatsapp', 'text': 'Short lowercase Flemish, no capitals.',
                                                  'decision_id': self.note_id()})
        self.assertEqual(saved, {'channel': 'WhatsApp', 'text': 'Short lowercase Flemish, no capitals.'})
        # The next note rewrites the whole line, it doesn't pile up beside it.
        self.app.action('remember_fact', {'kind': 'voice', 'channel': 'WhatsApp', 'text': 'Short lowercase Flemish, no capitals, no full stop at the end.',
                                          'decision_id': self.note_id()})
        self.assertEqual([(v['channel'], v['text'], v['source']) for v in self.store.voices()],
                         [('WhatsApp', 'Short lowercase Flemish, no capitals, no full stop at the end.', 'assistant')])
        with self.assertRaisesRegex(ValueError, 'short block'):
            self.app.action('remember_fact', {'kind': 'voice', 'channel': 'Email', 'text': 'x' * 451, 'decision_id': self.note_id()})
        with self.assertRaisesRegex(ValueError, 'short block'):
            self.app.action('remember_fact', {'kind': 'voice', 'channel': 'Email', 'text': '\n'.join(['Short.'] * 6), 'decision_id': self.note_id()})
        # Each change is marked on the quality chart, like a draft rule.
        self.assertEqual([c['kind'] for c in self.store.rule_changes('2000-01-01')], ['voice', 'voice'])

    def test_the_owner_edits_and_removes_voices_in_settings_only(self):
        with self.assertRaisesRegex(ValueError, 'Settings'):
            self.app.action('set_voice', {'channel': 'Email', 'text': 'Warm and short.'})
        out = self.app.action('set_voice', {'channel': 'All', 'text': 'Natural dates, never 2026-10-04.'}, owner_dashboard=True)
        out = self.app.action('set_voice', {'channel': 'Email', 'text': 'Warm and short. Regards, a blank line, then my name.'}, owner_dashboard=True)
        self.assertEqual([(v['channel'], v['source']) for v in out['voices']], [('All', 'owner'), ('Email', 'owner')])
        out = self.app.action('set_voice', {'channel': 'Email', 'text': ''}, owner_dashboard=True)
        self.assertEqual([v['channel'] for v in out['voices']], ['All'])

    def test_a_voice_is_a_few_lines_and_a_learned_update_keeps_the_rest_of_it(self):
        block = '- English: Hi or Hey + first name\n- Regards, a blank line, then the name\n\n• Short paragraphs, no bullets'
        out = self.app.action('set_voice', {'channel': 'Email', 'text': block}, owner_dashboard=True)
        self.assertEqual(out['voices'][0]['text'], 'English: Hi or Hey + first name\nRegards, a blank line, then the name\nShort paragraphs, no bullets')
        # A note works one thing in; it doesn't shrink the block back to that one thing.
        with self.assertRaisesRegex(ValueError, 'Keep the whole Email block'):
            self.app.action('remember_fact', {'kind': 'voice', 'channel': 'Email', 'text': 'Less formal.', 'decision_id': self.note_id()})
        grown = out['voices'][0]['text'] + '\nLess formal with customers than before'
        self.assertEqual(self.app.action('remember_fact', {'kind': 'voice', 'channel': 'Email', 'text': grown, 'decision_id': self.note_id()})['text'], grown)
        self.assertIn('- Email:\n  - English: Hi or Hey + first name\n  - Regards', self.store.voices_text())

    def study_messages(self):
        sent = [mail(f's{i}', f'2026-10-0{i}T10:00:00+00:00', f'Hi {n},\n\nQuick one about item {i}: {w}.\n\nRegards,\n\nOwner', thread=f'ts{i}',
                     sender='Owner <owner@example.com>', to=f'{n} <{n.lower()}@example.org>', labels=('SENT',))
                for i, (n, w) in enumerate([('Ann', 'all set'), ('Bo', 'see the doc'), ('Cy', 'call tomorrow?'), ('Di', 'thanks a lot'),
                                            ('Ed', 'invoice is in'), ('Fay', 'works for me')], 1)]
        self.store.commit_page('work', {}, sent)

    def test_a_voice_study_reads_own_messages_per_channel_and_keeps_the_owners_own_blocks(self):
        self.study_messages()
        with self.store.connect() as db:
            db.execute("INSERT INTO note_kinds VALUES(?,?,'style','assistant',?)", (self.note_id(), self.chat_sid, a.now()))
        self.app.action('set_voice', {'channel': 'All', 'text': 'Natural dates.'}, owner_dashboard=True)
        sample = self.store.voice_sample()
        self.assertEqual({m['channel'] for m in sample['messages']}, {'Email'})  # one WhatsApp message says too little
        self.assertTrue(all(m['text'].startswith(('Hi ', 'Hey ')) for m in sample['messages']))
        self.assertEqual(sample['notes'][0]['note'], 'no capitals on whatsapp please')
        started = self.store.voice_study_start()
        session = a.PROFILE_SESSION_PREFIX + started['run_id']
        page = self.store.profile_evidence(1, session)
        self.assertEqual((page['counts'], page['current_voices'][0]['written_by_owner']), ({'Email': 8}, True))
        prompt = a.voices_prompt(started['run_id'], page['counts'], page['pages'])
        self.assertIn('greeting per language', prompt)
        with self.assertRaisesRegex(ValueError, 'at most 5 lines'):
            self.store.save_profile({'voices': {'Email': '\n'.join(['Short line.'] * 6)}}, session)
        email_block = 'English: Hi + first name, a blank line\nRegards, a blank line, then the name\nShort paragraphs; questions to close'
        self.store.save_profile({'voices': {'Email': email_block, 'All': 'Overwrites nothing of theirs.'}}, session)
        with self.store.connect() as db:
            db.execute("UPDATE profile_runs SET status='ok',finished_at=? WHERE run_id=?", (a.now(), started['run_id']))
        self.assertEqual(self.store.apply_voice_study(started['run_id']), 1)
        self.assertEqual(self.store.apply_voice_study(started['run_id']), 0)  # once
        self.assertEqual({v['channel']: (v['text'], v['source']) for v in self.store.voices()},
                         {'All': ('Natural dates.', 'owner'), 'Email': (email_block, 'study')})
        self.assertIsNone(self.store.voice_study_due())  # read again only after a while
        with patch.object(a, 'now', return_value=(a.dt.datetime.fromisoformat(a.now()) + a.dt.timedelta(days=31)).isoformat()):
            self.assertIn('run_id', self.store.voice_study_due())

    def test_a_voice_study_runs_as_a_turn_and_applies_when_it_finishes(self):
        self.study_messages()
        started = self.store.voice_study_start()
        self.app.preparation_gateway = None
        sent = []
        def runner(request):
            sent.append(request)
            if request['action'] == 'start':
                self.store.save_profile({'voices': {'Email': 'Hi + first name\nRegards, a blank line, then the name'}},
                                        a.PROFILE_SESSION_PREFIX + started['run_id'])
                return {'status': 'admitted'}
            return {'status': 'ok'}
        self.app.profile_step(runner)
        self.assertIn('voice study', sent[0]['message'])
        self.assertEqual(self.store.voices(), [])  # not before the run is done
        self.app.profile_step(runner)
        self.assertEqual([(v['channel'], v['source']) for v in self.store.voices()], [('Email', 'study')])

    def test_a_profile_fills_empty_channels_once(self):
        draft = {'voices': {'Email': 'Short paragraphs, Regards and name.', 'WhatsApp': 'Tiny lowercase bursts.'}}
        with self.store.connect() as db:
            db.execute("INSERT INTO profile_runs(run_id,status,started_at,updated_at,draft,kind) VALUES('p1','ok','2026-10-01','2026-10-01',?,'profile')", (json.dumps(draft),))
        self.app.action('set_voice', {'channel': 'Email', 'text': 'My own email line.'}, owner_dashboard=True)
        self.assertEqual(self.store.seed_voices(), 1)
        self.assertEqual({v['channel']: v['text'] for v in self.store.voices()}, {'Email': 'My own email line.', 'WhatsApp': 'Tiny lowercase bursts.'})
        self.app.action('set_voice', {'channel': 'WhatsApp', 'text': ''}, owner_dashboard=True)
        self.assertEqual(self.store.seed_voices(), 0)  # removed stays removed until a new profile
        self.assertEqual([v['channel'] for v in self.store.voices()], ['Email'])

    def test_a_draft_gets_its_channels_voice_own_messages_and_only_that_channels_edits(self):
        self.app.action('set_voice', {'channel': 'Email', 'text': 'Hey name, short, Regards then a blank line then the name.'}, owner_dashboard=True)
        self.app.action('set_voice', {'channel': 'WhatsApp', 'text': 'lowercase, no full stop.'}, owner_dashboard=True)
        with self.store.connect() as db:
            db.execute("INSERT INTO draft_edits(id,suggestion_id,old_revision,new_revision,kind,before_body,after_body,source,created_at) "
                       "VALUES('e1',?,'a','b','whatsapp','Ah ok. See you.','ah ok, tot straks','dashboard',?)", (self.chat_sid, a.now()))
            db.execute("INSERT INTO draft_edits(id,suggestion_id,old_revision,new_revision,kind,before_body,after_body,source,created_at) "
                       "VALUES('e2',?,'a','b','email','Dear Sam, kindly find','Hey Sam, here it is','dashboard',?)", (self.sid, a.now()))
        text = self.store.style_text(self.sid)
        self.assertIn('- Email (this card):\n  - Hey name', text)
        self.assertIn('- WhatsApp:\n  - lowercase', text)  # every voice, so a draft on another channel still has one
        examples = text.split("own recent Email messages")[1]
        # Same conversation first, then others; the owner's own words without the quoted history.
        self.assertLess(examples.index('(this conversation) Hey Sam'), examples.index('(to Lee) Hi Lee'))
        self.assertIn('Hey Sam, here it is', text)
        self.assertNotIn('tot straks', text)  # a WhatsApp edit says little about email
        chat_text = self.store.style_text(self.chat_sid)
        self.assertIn('- WhatsApp (this card):\n  - lowercase', chat_text)
        self.assertIn('(this conversation) ja tuurlijk, tot straks', chat_text)
        self.assertNotIn('Sent a photo', chat_text)  # media placeholders say nothing about how they write
        self.assertNotIn('Hey Sam', chat_text)
        prompt = a.preparation_prompt({'job_id': 'j' * 16, 'suggestion_id': self.sid}, '', None, '', '- Offer the booking link.', text)
        self.assertLess(prompt.index('Draft rules'), prompt.index('How the owner writes, per channel'))
        self.assertIn('kind=voice, channel=', prompt)

    def test_a_draft_answered_elsewhere_becomes_an_edit_once(self):
        draft = {'kind': 'email', 'label': 'Send', 'account_id': 'work', 'to': ['sam@example.com'], 'subject': 'Re: Contract',
                 'body': 'Dear Sam,\n\nPlease find the contract attached.\n\nKind regards', 'revision': 'r1'}
        with self.store.connect() as db:
            db.execute("UPDATE suggestions SET status='prepared',next_action=? WHERE id=?", (json.dumps(draft), self.sid))
            db.execute("INSERT INTO preparations(suggestion_id,decision_id,job_id,status,created_at,updated_at) VALUES(?,'d','j1','done',?,?)",
                       (self.sid, '2026-10-03T09:00:00+00:00', '2026-10-03T09:00:00+00:00'))
        self.store.commit_page('work', {}, [mail('out3', '2026-10-03T11:00:00+00:00', 'Hey Sam,\n\nhere you go\n\nRegards,\n\nOwner\n\n[Quoted earlier messages]\nold', **SENT),
                                            mail('out4', '2026-10-03T12:00:00+00:00', 'Other thread entirely', thread='t9', **{k: v for k, v in SENT.items()})])
        with patch.object(a, 'now', return_value='2026-10-03T13:00:00+00:00'):
            self.assertEqual(self.store.learn_sent_elsewhere(), 1)
            self.assertEqual(self.store.learn_sent_elsewhere(), 0)
        with self.store.connect() as db:
            row = dict(db.execute("SELECT * FROM draft_edits WHERE source='elsewhere'").fetchone())
        self.assertEqual((row['kind'], row['old_revision']), ('email', 'r1'))
        self.assertEqual(row['after_body'], 'Hey Sam,\n\nhere you go\n\nRegards,\n\nOwner')
        with patch.object(a, 'now', return_value='2026-10-03T13:00:00+00:00'):
            self.assertIn('They wrote this themselves instead: Hey Sam', self.store.style_text(self.sid))
            # A review gets it once to fold a lasting habit into the channel's voice.
            lines = self.store.edits_to_learn('run1')
            self.assertIn(f"- Email, \"Send Sam the contract\" (edit_id {row['id']})", lines)
            self.assertIn('They wrote themselves instead: Hey Sam', lines)
            self.assertEqual(self.store.edits_to_learn('run2'), '')
        saved = self.app.action('remember_fact', {'kind': 'voice', 'channel': 'Email', 'text': 'Hey name, plain, short.', 'edit_id': row['id']})
        self.assertEqual(saved['channel'], 'Email')

    def test_a_draft_sent_from_the_card_or_without_a_reply_teaches_nothing(self):
        draft = {'kind': 'whatsapp', 'label': 'Send reply', 'account_id': 'phone', 'chat_id': 'c1', 'body': 'Yes, see you.', 'revision': 'r1'}
        with self.store.connect() as db:
            db.execute("UPDATE suggestions SET status='prepared',next_action=? WHERE id=?", (json.dumps(draft), self.chat_sid))
            db.execute("UPDATE preparations SET status='done',updated_at=? WHERE suggestion_id=?", ('2026-10-01T09:00:00+00:00', self.chat_sid))
        with patch.object(a, 'now', return_value='2026-10-01T12:00:00+00:00'):
            self.assertEqual(self.store.learn_sent_elsewhere(), 0)  # their earlier message came before the draft
        self.store.commit_page('phone', {}, [chat('w4', '2026-10-01T10:00:00+00:00', 'ja ik kom', mine=True)])
        with self.store.connect() as db:
            db.execute("INSERT INTO action_jobs(id,suggestion_id,revision,payload,status,created_at,updated_at,approval_id,source,rfc_message_id) "
                       "VALUES('j',?,'r1','{}','sent',?,?,'x','dashboard','')", (self.chat_sid, a.now(), a.now()))
        with patch.object(a, 'now', return_value='2026-10-01T12:00:00+00:00'):
            self.assertEqual(self.store.learn_sent_elsewhere(), 0)  # sent from the card: that message is the draft itself
        with self.store.connect() as db:
            db.execute("UPDATE action_jobs SET status='cancelled'")
        with patch.object(a, 'now', return_value='2026-10-01T12:00:00+00:00'):
            self.assertEqual(self.store.learn_sent_elsewhere(), 1)  # not sent from the card: their own reply teaches

class Learned(unittest.TestCase):
    """A preparation gets what was learned that bears on its card, not all of it; a review condenses the list now and
    then (merging what says the same, the newer winning), and the owner sees and can undo each merge."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = a.Store(Path(self.temp.name) / 'events.sqlite')
        self.app = object.__new__(a.App)
        self.app.store = self.store
        self.store.accounts = [{'id': 'work', 'service': 'gmail', 'email': 'owner@example.com'}]
        self.app.config = {'accounts': self.store.accounts}
        self.store.commit_page('work', {}, [mail('in1', '2026-10-01T09:00:00+00:00', 'Deal on the listing swap.'),
                                            mail('in2', '2026-10-01T09:00:00+00:00', 'Quote attached.', thread='t2'),
                                            mail('in3', '2026-10-01T09:00:00+00:00', 'Quick question.', thread='t3')])
        self.sid = self.store.suggest('listing', 'Answer the listing exchange', 'They agreed to swap listings.', [1])['id']
        self.other = self.store.suggest('paint', 'Pay the painter', 'The quote excludes VAT.', [2])['id']
        self.note = self.store.decide(self.other, 'other', 'remember these', 'n1', 'dashboard')
        with self.store.connect() as db:
            self.decision = db.execute("SELECT id FROM decisions WHERE feedback!=''").fetchone()['id']

    def fact(self, text, kind='fact'):
        return self.store.remember_fact(text, self.decision, kind)['id']

    def test_a_preparation_gets_the_facts_that_bear_on_its_card(self):
        few = self.fact('The office is closed on Fridays.')
        self.assertEqual(self.store.facts_for(self.sid), '- The office is closed on Fridays.')  # few: all of them
        topics = ['listing', 'paint', 'tax', 'travel', 'hiring', 'billing', 'support', 'garden', 'school', 'bank', 'car', 'dentist', 'insurance', 'lunch']
        ids = {t: self.fact(f'A long enough fact about {t}, so that all of them together are more than a preparation should carry around. ' * 1)
               for t in topics}
        own = self.store.remember_fact('This card’s own note: they write listing copy themselves.', self.decision)['id']
        with self.store.connect() as db:  # a fact learned on this card
            db.execute('UPDATE facts SET suggestion_id=? WHERE id=?', (self.sid, own))
        # No embeddings: the newest first, up to the limit.
        newest = self.store.facts_for(self.sid, limit=400).splitlines()
        self.assertEqual(newest[0], '- This card’s own note: they write listing copy themselves.')
        self.assertIn('lunch', newest[1])
        # With embeddings: this card's own facts, then the closest ones above the floor.
        def embed(settings, texts, timeout=120):
            return [[1.0, 0.0] if 'listing' in t or 'Answer the listing' in t else [0.6, 0.8] if 'paint' in t or 'tax' in t else [0.0, 1.0] for t in texts]
        settings = {'url': 'http://embed.invalid', 'model': 'm', 'dims': 2}
        with patch.object(a.embeddings, 'embed', side_effect=embed) as called:
            picked = self.store.facts_for(self.sid, settings=settings, top=2, floor=0.5).splitlines()
            self.assertEqual(len(called.call_args.args[1]), len(topics) + 3)  # every fact once, plus the card
            self.store.facts_for(self.sid, settings=settings, top=2, floor=0.5)
            self.assertEqual(len(called.call_args.args[1]), 1)  # facts' meaning is kept: only the card again
        self.assertEqual(picked[0], '- This card’s own note: they write listing copy themselves.')
        self.assertEqual([('listing' in p, 'paint' in p or 'tax' in p) for p in picked[1:]], [(True, False), (False, True)])
        self.assertFalse(any('garden' in p or 'Fridays' in p for p in picked))  # below the floor
        with patch.object(a.embeddings, 'embed', side_effect=a.embeddings.EmbeddingError('down')):
            self.assertIn('lunch', self.store.facts_for(self.sid, settings=settings, limit=400).splitlines()[1])  # down: the newest
        del few, ids

    def test_condensing_merges_keeps_the_originals_and_the_owner_can_undo_it(self):
        old = self.fact('In emails, put links behind text.')
        new = self.fact('In emails, show the URL itself, hyperlinked.')
        rule = self.fact('For listing exchanges, prepare both inclusions.', 'draft')
        with self.assertRaisesRegex(ValueError, 'not current'):
            self.app.action('remember_fact', {'text': 'Merged.', 'replaces': [old, 999]})
        out = self.app.action('remember_fact', {'text': 'In emails, show the URL itself as the link text, hyperlinked.', 'replaces': [old, new]})
        self.assertEqual(out['merged'], 2)
        self.assertEqual([f['text'] for f in self.store.facts() if f['kind'] == 'fact'], ['In emails, show the URL itself as the link text, hyperlinked.'])
        with self.store.connect() as db:  # the originals stay, behind the merged one
            self.assertEqual({r['id']: r['merged_into'] for r in db.execute('SELECT id,merged_into FROM facts WHERE merged_into IS NOT NULL')},
                             {old: f"fact:{out['id']}", new: f"fact:{out['id']}"})
        snap = self.store.snapshot([])
        self.assertEqual([(f['text'][:9], f['merged']) for f in snap['facts']], [('In emails', 2), ('For listi', 0)])  # newest first
        self.assertEqual(snap['condensed']['notes'], 2)
        # A style fact folds into its channel's voice.
        voice = self.app.action('remember_fact', {'kind': 'voice', 'channel': 'Email', 'text': 'Short, warm, URLs shown and hyperlinked.', 'replaces': [out['id']]})
        self.assertEqual(voice['merged'], 1)
        self.assertEqual([v['merged'] for v in self.store.snapshot([])['voices']], [1])
        with self.assertRaisesRegex(ValueError, 'dashboard'):
            self.app.action('unmerge_fact', {'channel': 'Email'})
        self.assertEqual(self.app.action('unmerge_fact', {'channel': 'Email'}, owner_dashboard=True), {'restored': 1})
        self.assertEqual(self.app.action('unmerge_fact', {'id': out['id']}, owner_dashboard=True), {'restored': 2})
        self.assertEqual(sorted(f['id'] for f in self.store.facts()), sorted([old, new, rule]))
        with self.assertRaisesRegex(ValueError, 'Nothing was merged'):
            self.app.action('unmerge_fact', {'id': rule}, owner_dashboard=True)
        # Owner notes stay the only source: a merge can't name facts that aren't there, and the owner's Settings line can't merge.
        self.app.action('set_voice', {'channel': 'Email', 'text': 'Mine.', 'replaces': [old]}, owner_dashboard=True)
        self.assertIn(old, [f['id'] for f in self.store.facts()])

    def test_a_review_is_offered_the_list_to_condense_now_and_then_and_unlearned_style_notes_once(self):
        for i in range(6):
            self.fact(f'Fact number {i} about something.')
        self.assertIn('id 1 (fact): Fact number 0', self.store.condense_text())
        self.assertTrue(self.store.condense_text('r1'))
        self.assertEqual(self.store.condense_text(), '')  # offered: not again until the list changes and a day passed
        self.fact('Another fact.')
        self.assertEqual(self.store.condense_text(), '')
        with patch.object(a, 'now', return_value='2099-01-01T00:00:00+00:00'):
            self.fact('A much later fact.')
            self.assertTrue(self.store.condense_text())
        # A style note nothing was learned from is offered once.
        with self.store.connect() as db:
            db.execute("INSERT INTO note_kinds VALUES(?,?,?,?,?)", (self.decision, self.other, 'style', 'assistant', a.now()))
        self.assertEqual(self.store.style_notes_to_learn(), '')  # facts came from it already
        sid = self.store.suggest('mail', 'Answer Ann', 'Ann asks.', [3])['id']
        self.store.decide(sid, 'other', 'less formal please, always', 'n2', 'dashboard')
        with self.store.connect() as db:
            note = db.execute("SELECT id FROM decisions WHERE suggestion_id=? AND feedback!=''", (sid,)).fetchone()['id']
            db.execute("INSERT INTO note_kinds VALUES(?,?,?,?,?)", (note, sid, 'style', 'assistant', a.now()))
        self.assertIn('less formal please', self.store.style_notes_to_learn('r1'))
        self.assertEqual(self.store.style_notes_to_learn(), '')


if __name__ == '__main__':
    unittest.main()

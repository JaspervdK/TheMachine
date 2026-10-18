import datetime as dt
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location('graph_assistant', Path(__file__).with_name('assistant.py'))
a = importlib.util.module_from_spec(spec); spec.loader.exec_module(a)
graph = a.graph
NOW = dt.datetime(2026, 10, 16, 12, tzinfo=dt.timezone.utc)
CONFIG = {'accounts': [{'id': 'work', 'email': 'sam@acme.com'}, {'id': 'chat', 'service': 'whatsapp', 'email': 'Personal WhatsApp'},
                       {'id': 'slack', 'service': 'slack', 'email': 'sam@acme.com', 'user_id': 'U0OWNER'}]}
counter = iter(range(1, 10**6))


def at(days_ago, hour=0):
    return (NOW - dt.timedelta(days=days_ago, hours=hour)).isoformat()


def mail(days_ago, sender, to, labels=('INBOX',), cc='', thread=None, hour=0, title=None, **extra):
    n = next(counter)
    return {'account_id': 'work', 'source_id': f'm{n}', 'kind': 'gmail.message', 'occurred_at': at(days_ago, hour), 'title': title or f'Mail {n}', 'body': 'Hello',
            'deleted': False, 'metadata': {'from': sender, 'to': to, 'cc': cc, 'labels': list(labels), 'message-id': f'<{n}@x>',
                                           'thread_id': thread or f't{n}', **extra}}


def chat(days_ago, phone, name, mine=False, hour=0):
    n = next(counter)
    return {'account_id': 'chat', 'source_id': f'w{n}', 'kind': 'whatsapp.message', 'occurred_at': at(days_ago, hour), 'title': name, 'body': 'Hi',
            'deleted': False, 'metadata': {'chat_id': phone + '@s.whatsapp.net', 'chat_name': name, 'from_me': mine, 'from': 'You' if mine else name,
                                           'sender_id': None if mine else phone + '@s.whatsapp.net'}}


class Graph(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.store = a.Store(Path(self.temp.name) / 'events.sqlite')
        self.app = object.__new__(a.App); self.app.store = self.store; self.app.config = CONFIG
        self.now = a.now
        a.now = lambda: NOW.isoformat(); self.addCleanup(setattr, a, 'now', self.now)

    def add(self, *events):
        for e in events:
            self.store.commit_page(e['account_id'], {}, [e])

    def build(self):
        with self.store.connect() as db:
            db.isolation_level = None
            return graph.build(db, CONFIG, NOW.isoformat())

    def node(self, node_id):
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM graph_nodes WHERE id=?', (node_id,)).fetchone()
            return graph.node_view(row) if row else None

    def ids(self, source_ids):
        with self.store.connect() as db:
            return [db.execute('SELECT id FROM events WHERE source_id=?', (s,)).fetchone()[0] for s in source_ids]

    def test_owner_bulk_and_aliases_never_become_people(self):
        self.add(mail(1, 'Sam <sam@acme.com>', 'Ann <ann@client.com>', ('SENT',)),
                 mail(1, 'Sam Owner <sam@home.org>', 'ann@client.com', ('SENT',)),  # an alias the owner sends from
                 mail(2, 'News <newsletter@shop.com>', 'sam@acme.com', ('CATEGORY_PROMOTIONS', 'INBOX')),
                 mail(2, 'Robot <noreply@service.com>', 'sam@acme.com'),
                 mail(2, 'Acme info <info@acme.com>', 'sam@acme.com'),  # a shared role address at the owner's own domain
                 mail(3, 'Ann <ann@client.com>', 'Sam <sam@acme.com>, Sam Owner <sam@home.org>'),
                 chat(1, '32470000001', 'Sam Owner'),  # the owner's own name on another channel
                 {'account_id': 'work', 'source_id': 'crm1', 'kind': 'salesflare.email', 'occurred_at': at(2), 'title': 'Your workspace',
                  'body': 'Hi', 'deleted': False, 'metadata': {'contacts': [{'email': 'no-reply@email.slackhq.com', 'name': 'Slack'}]}})
        self.build()
        with self.store.connect() as db:
            people = {r['id'] for r in db.execute("SELECT id FROM graph_nodes WHERE kind='person'")}
        self.assertEqual(people, {'email:ann@client.com'})
        self.assertEqual(self.node('org:client.com')['name'], 'client.com')

    def test_owner_messages_weigh_more_a_burst_is_one_day_and_it_fades(self):
        self.add(*[chat(1, '32470000001', 'Bea Burst', hour=h) for h in range(10)],
                 chat(1, '32470000002', 'Cas Owner', mine=True),
                 chat(120, '32470000003', 'Dee Old'), chat(120, '32470000003', 'Dee Old', mine=True))
        self.build()
        bea, cas, dee = (self.node('wa:3247000000' + n)['strength'] for n in '123')
        self.assertAlmostEqual(bea, 1.0 * 0.5 ** (1 / 60), places=2)  # ten messages, one day, capped
        self.assertGreater(cas, 0.55)
        self.assertLess(dee, 0.25)  # two months of fading, twice over

    def test_same_full_name_joins_channels_unless_said_to_differ(self):
        self.add(mail(1, 'Ann Example <ann@client.com>', 'sam@acme.com'), chat(2, '32470000001', 'Ann Example'),
                 chat(2, '32470000009', 'Bo Ambiguous'), mail(2, 'Bo Ambiguous <bo@one.com>', 'sam@acme.com'), mail(2, 'Bo Ambiguous <bo@two.com>', 'sam@acme.com'))
        self.build()
        self.assertEqual(self.node('email:ann@client.com')['ids'], ['+32470000001', 'ann@client.com'])
        self.assertIsNone(self.node('wa:32470000001'))
        self.assertIsNotNone(self.node('wa:32470000009'))  # two addresses carry that name: not joined
        with self.store.connect() as db:
            graph.merge(db, 'wa:32470000001', 'email:ann@client.com', False, NOW.isoformat())
        self.build()
        self.assertIsNotNone(self.node('wa:32470000001'))
        with self.store.connect() as db:
            graph.merge(db, 'wa:32470000009', 'email:bo@one.com', True, NOW.isoformat())
        self.build()
        self.assertIn('+32470000009', self.node('email:bo@one.com')['ids'])

    def test_slack_email_joins_the_mail_identity(self):
        n = next(counter)
        self.add({'account_id': 'slack', 'source_id': f's{n}', 'kind': 'slack.message', 'occurred_at': at(1), 'title': '#DM U0ANN: hi', 'body': 'hi',
                  'deleted': False, 'metadata': {'conversation_type': 'dm', 'channel_name': 'DM U0ANN', 'user_id': 'U0ANN', 'channel_id': 'D1'}},
                 mail(2, 'Ann <ann@client.com>', 'sam@acme.com'))
        self.store.remember_name('slack:U0ANN', 'Ann Example', 'ann@client.com')
        self.assertEqual(self.store.names_to_check(['slack:U0ANN', 'slack:U0NEW']), ['slack:U0NEW'])
        self.build()
        ann = self.node('email:ann@client.com')
        self.assertEqual((ann['name'], ann['ids']), ('Ann Example', ['Slack', 'ann@client.com']))

    def test_slack_names_are_looked_up_a_few_at_a_time_once_a_month(self):
        calls = []
        slack = object.__new__(a.Slack); slack.account = {'user_id': 'U0OWNER'}
        slack.get = lambda method, params: calls.append(params['user']) or {'user': {'profile': {'real_name': 'Name ' + params['user'], 'email': params['user'].lower() + '@acme.com'}}}
        channels = [{'name': 'DM U0A'}, {'name': 'DM U0B'}, {'name': 'general'}, {'name': 'My notes'}]
        events = [{'metadata': {'user_id': 'U0C', 'channel_name': 'general'}}, {'metadata': {'user_id': 'U0OWNER', 'channel_name': 'general'}}]
        a.remember_slack_names(self.store, slack, events, channels, per_page=2)
        a.remember_slack_names(self.store, slack, events, channels, per_page=2)
        a.remember_slack_names(self.store, slack, events, channels, per_page=2)
        self.assertEqual(calls, ['U0A', 'U0B', 'U0C'])
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT name,email FROM identity_names WHERE identifier='slack:U0A'").fetchone()[:], ('Name U0A', 'u0a@acme.com'))

    def test_projects_from_review_and_cards_spread_to_threads_and_people(self):
        self.add(mail(3, 'Ann <ann@client.com>', 'sam@acme.com', thread='deal'), mail(2, 'Sam <sam@acme.com>', 'ann@client.com, Bob <bob@client.com>', ('SENT',), thread='deal'),
                 mail(1, 'Zed <zed@else.com>', 'sam@acme.com'))
        first, _, other = self.thread_ids()
        claim = self.store.begin_analysis(['work'], 10)
        with self.assertRaises(ValueError):
            self.store.finish_analysis(claim['analysis_id'], 'x', projects={'Acme deal': [999999]})
        self.store.finish_analysis(claim['analysis_id'], 'Reviewed', projects={'Acme deal': [first]})
        self.build()
        with self.store.connect() as db:
            project = db.execute("SELECT * FROM graph_nodes WHERE kind='project'").fetchone()
            self.assertEqual((project['name'], project['signals']), ('Acme deal', 2))  # the reply in the same thread joined it
            people = {n['id'] for n in graph.neighbours(db, project['id'])}
            self.assertEqual(people, {'email:ann@client.com', 'email:bob@client.com'})
            self.assertEqual(graph.active_projects(db), ['Acme deal'])
        card = self.store.suggest('zed-1', 'Answer Zed', 'Zed asked a question.', [other], project='acme  DEAL')
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT project FROM suggestions WHERE id=?', (card['id'],)).fetchone()[0], 'Acme deal')
        self.build()
        with self.store.connect() as db:
            ids = {n['id'] for n in graph.neighbours(db, project['id'])}
        self.assertIn('email:zed@else.com', ids)

    def thread_ids(self):
        with self.store.connect() as db:
            rows = db.execute("SELECT id,json_extract(metadata,'$.thread_id') t FROM events ORDER BY id").fetchall()
        deal = [r['id'] for r in rows if r['t'] == 'deal']
        return deal[0], deal[1], next(r['id'] for r in rows if r['t'] != 'deal')

    def test_card_decisions_strengthen_and_not_relevant_weakens(self):
        self.add(mail(5, 'Ann <ann@client.com>', 'sam@acme.com'), mail(5, 'Bob <bob@client.com>', 'sam@acme.com'), mail(5, 'Cy <cy@client.com>', 'sam@acme.com'))
        ann, bob, cy = self.ids(self.sources())
        self.build()
        base = self.node('email:ann@client.com')['strength']
        for key, event_id, choice, feedback in (('a', ann, 'accept', ''), ('a2', ann, 'complete', ''), ('b', bob, 'reject', ''), ('c', cy, 'reject', 'Already handled')):
            card = self.store.suggest(key, 'T', 'B', [event_id]) if key != 'a2' else card
            with self.store.connect() as db:
                db.execute('INSERT INTO decisions(id,suggestion_id,choice,feedback,source,created_at) VALUES(?,?,?,?,?,?)', (key, card['id'], choice, feedback, 'dashboard', at(1)))
        self.build()
        self.assertAlmostEqual(self.node('email:ann@client.com')['strength'] - base, 0.5 ** (1 / 60), places=2)  # one boost per card
        self.assertIsNone(self.node('email:bob@client.com'))  # faded out entirely
        self.assertAlmostEqual(self.node('email:cy@client.com')['strength'], base, places=3)

    def sources(self):
        with self.store.connect() as db:
            return [r[0] for r in db.execute('SELECT source_id FROM events ORDER BY id')]

    def test_slack_root_and_reply_share_project_not_neighbour_or_other_account(self):
        channel = {'id': 'C1', 'name': 'projects', 'type': 'channel'}
        account = {'id': 'slack', 'slack_host': 'example.invalid'}
        stamp = str(int((NOW - dt.timedelta(days=1)).timestamp())) + '.000001'
        root = a.slack_event(account, channel, {'ts': stamp, 'text': 'Project root', 'user': 'U1', 'reply_count': 1})
        reply = a.slack_event(account, channel, {'ts': str(float(stamp) + 60), 'thread_ts': stamp, 'text': 'Project reply', 'user': 'U2'})
        other = a.slack_event(account, channel, {'ts': str(float(stamp) + 120), 'text': 'Unrelated root', 'user': 'U3'})
        foreign = {**root, 'account_id': 'other-slack'}
        self.add(root, reply, other, foreign)
        with self.store.connect() as db:
            root_id, reply_id, other_id, foreign_id = [r[0] for r in db.execute('SELECT id FROM events ORDER BY id')]
            pid = graph.label(db, [root_id], 'Exact thread', 'agent', NOW.isoformat())
        self.build()
        with self.store.connect() as db:
            members = {r[0] for r in db.execute('SELECT event_id FROM graph_events WHERE node_id=?', ('project:' + str(pid),))}
        self.assertEqual(members, {root_id, reply_id})
        with self.store.connect() as db:
            db.execute('DELETE FROM event_projects')
            graph.label(db, [reply_id], 'Exact thread', 'agent', NOW.isoformat())
        self.build()
        with self.store.connect() as db:
            members = {r[0] for r in db.execute('SELECT event_id FROM graph_events WHERE node_id=?', ('project:' + str(pid),))}
        self.assertEqual(members, {root_id, reply_id})

    def test_old_task_membership_is_complete_but_display_is_bounded(self):
        old = mail(3, 'Ann Example <ann@client.com>', 'sam@acme.com')
        self.add(old, *[mail(1, 'Ann Example <ann@client.com>', 'sam@acme.com', hour=i / 100) for i in range(45)])
        first = self.ids([old['source_id']])[0]
        card = self.store.suggest('older', 'Answer Ann', 'Question', [first])
        self.build()
        with self.store.connect() as db:
            self.assertTrue(graph.for_events(db, [first]))
            self.assertEqual(len(graph.detail(db, 'email:ann@client.com', graph.EVENTS_PER_NODE)['event_ids']), 40)
        self.assertIn('Ann Example', self.app.graph_lines(card['id']))

    def test_annotated_decisions_do_not_teach_graph(self):
        self.add(mail(3, 'Ann Example <ann@client.com>', 'sam@acme.com'))
        first = self.ids(self.sources())[0]
        self.build()
        baseline = self.node('email:ann@client.com')['strength']
        card = self.store.suggest('annotation', 'Answer Ann', 'Question', [first])
        with self.store.connect() as db:
            db.execute('INSERT INTO decisions(id,suggestion_id,choice,feedback,source,created_at) VALUES(?,?,?,?,?,?)', ('bad', card['id'], 'accept', '', 'telegram', at(1)))
            db.execute('INSERT INTO decision_annotations(decision_id,status,reason) VALUES(?,?,?)', ('bad', 'misassociated', 'Wrong prompt'))
        self.build()
        self.assertEqual(self.node('email:ann@client.com')['strength'], baseline)

    def test_quiet_project_actually_cools(self):
        self.add(*[mail(15, 'Ann Example <ann@client.com>', 'sam@acme.com', hour=i / 100) for i in range(10)])
        with self.store.connect() as db:
            pid = graph.label(db, self.ids(self.sources()), 'Quiet project', 'agent', NOW.isoformat())
        self.build()
        self.assertEqual(self.node('project:' + str(pid))['state'], 'cooling')
        for ratio, expected in ((1.0, 'active'), (1.5, 'heating up')):
            self.assertEqual(graph.project_state({'strength': 1.0, 'trend': ratio}), expected)

    def test_going_quiet_is_measured_against_their_own_rhythm(self):
        weekly = [chat(d, '32470000001', 'Wes Weekly', mine=d % 2 == 0) for d in range(30, 120, 7)]
        self.add(*weekly, chat(30, '32470000001', 'Wes Weekly', mine=True),
                 *[chat(d, '32470000002', 'Rae Recent', mine=True) for d in (2, 9, 16, 23)])
        self.build()
        wes, rae = self.node('wa:32470000001'), self.node('wa:32470000002')
        self.assertEqual((wes['usual_gap_days'], wes['quiet']), (7, True))
        self.assertFalse(rae['quiet'])
        with self.store.connect() as db:
            self.assertEqual([p['id'] for p in graph.overview(db)['quiet']], ['wa:32470000001'])
        self.assertIn('• Gone quiet: Wes Weekly (usually every 7 days)', a.recap_graph_lines(self.store))

    def test_preparation_context_and_agent_lookup(self):
        self.add(mail(3, 'Ann Example <ann@client.com>', 'sam@acme.com', thread='deal'), mail(2, 'Sam <sam@acme.com>', 'Ann Example <ann@client.com>, Bob <bob@client.com>', ('SENT',), thread='deal'))
        first = self.ids(self.sources())[0]
        card = self.store.suggest('ann-1', 'Answer Ann', 'Ann asked about pricing.', [first], project='Acme deal')
        self.build()
        lines = self.app.graph_lines(card['id'])
        self.assertIn('People and projects', lines)
        self.assertIn('- Ann Example (ann@client.com): ', lines)
        self.assertIn('projects: Acme deal', lines)
        self.assertIn('often with Bob', lines)
        self.assertIn('- Project Acme deal: ', lines)
        found = self.app.graph_action({'query': 'ann example'})['found']
        self.assertEqual([f['id'] for f in found], ['email:ann@client.com'])
        detail = self.app.graph_action({'node_id': 'email:ann@client.com'})
        self.assertEqual(len(detail['recent']), 2)
        self.assertIn('email:bob@client.com', [n['id'] for n in detail['around']])
        self.app.graph_build = self.build
        self.app.graph_action({'close_project': 'acme deal'})
        with self.store.connect() as db:
            self.assertEqual(graph.overview(db)['projects'], [])
        with self.assertRaises(ValueError):
            self.app.graph_action({'merge': ['email:ann@client.com']})

    def vectors(self, pairs):
        import struct
        with self.store.connect() as db:
            for source_id, values in pairs:
                size = sum(v * v for v in values) ** 0.5
                event_id, version = db.execute('SELECT id,version FROM events WHERE source_id=?', (source_id,)).fetchone()
                db.execute('INSERT OR REPLACE INTO event_vectors(event_id,version,model,dims,vector) VALUES(?,?,?,?,?)',
                           (event_id, version, 'test', len(values), struct.pack('<%df' % len(values), *[v / size for v in values])))

    def test_a_signal_that_reads_like_a_project_and_shares_a_person_is_probably_part_of_it(self):
        labelled = mail(5, 'Ann <ann@client.com>', 'sam@acme.com', title='Pricing')
        alike = mail(3, 'Ann <ann@client.com>', 'sam@acme.com')
        stranger = mail(3, 'Zed <zed@else.com>', 'sam@acme.com')
        other = mail(2, 'Ann <ann@client.com>', 'sam@acme.com')
        meeting = {'account_id': 'work', 'source_id': 'meet1', 'kind': 'calendar.event', 'occurred_at': at(2), 'title': 'Call', 'body': '', 'deleted': False,
                   'metadata': {'attendees': [{'email': 'sam@acme.com', 'self': True}, {'email': 'ann@client.com'}]}}
        self.add(labelled, alike, stranger, other, meeting)
        self.vectors([(labelled['source_id'], [1, 0.1, 0, 0]), (alike['source_id'], [1, 0.2, 0, 0]), (stranger['source_id'], [1, 0.1, 0, 0]),
                      (other['source_id'], [0, 0, 1, 0]), ('meet1', [1, 0.1, 0, 0])])
        first, guess, zed, unrelated, call = self.ids([labelled['source_id'], alike['source_id'], stranger['source_id'], other['source_id'], 'meet1'])
        with self.store.connect() as db:
            graph.label(db, [first], 'Acme deal', 'review', NOW.isoformat())
        self.build()
        with self.store.connect() as db:
            rows = dict(db.execute("SELECT event_id,probable FROM graph_events WHERE node_id LIKE 'project:%'").fetchall())
        self.assertEqual(rows, {first: 0, guess: 1})  # not the stranger, not a different topic, not a bare meeting
        self.assertEqual(self.store.project_hints([first, guess, zed]), {first: {'projects': ['Acme deal']}, guess: {'probably_project': 'Acme deal'}})
        detail = self.app.graph_action({'node_id': 'project:1'})
        self.assertEqual(detail['probable_signals'], 1)
        self.assertTrue(any(line.startswith(f'{guess} ') and line.endswith('(probably part of it)') for line in detail['recent']))
        claim = self.store.begin_analysis(['work'], 10)
        page = self.store.analysis_page(claim['analysis_id'], 1) if claim.get('pages', 1) > 1 else claim
        shown = {e['id']: e for e in page['events']}
        self.assertEqual(shown[guess].get('probably_project'), 'Acme deal')
        with self.store.connect() as db:  # a review confirms it
            graph.label(db, [guess], 'Acme deal', 'review', NOW.isoformat())
        self.build()
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT probable FROM graph_events WHERE event_id=? AND node_id=?', (guess, 'project:1')).fetchone()[0], 0)

    def test_threads_that_read_alike_without_a_project_are_offered_once(self):
        threads = [mail(9 - n, 'Ann <ann@client.com>', 'sam@acme.com', thread=f'x{n}') for n in range(3)]
        threads.append(mail(5, 'Sam Stone <sam@acme.com>', 'Ann <ann@client.com>', ('SENT',), thread='x0'))
        chats = [chat(4 - n, '32470000001', 'Bo Chat', mine=n == 0) for n in range(3)]
        self.add(*threads, *chats)
        self.vectors([(e['source_id'], [1, 0.05 * i, 0, 0]) for i, e in enumerate(threads + chats)])
        self.build()
        first = self.store.unnamed_text('run1')
        self.assertIn('4 signals with Ann: ', first)
        self.assertIn('"Mail ', first)
        self.assertNotIn('Bo Chat', first)  # chats read alike per person, whatever the topic
        self.assertEqual(self.store.unnamed_text('run2'), '')
        self.assertIn('Possibly one piece of work nobody named', a.review_prompt('run', ['work'], unnamed=first))
        self.assertNotIn('Possibly one piece of work', a.review_prompt('run', ['work']))

    def test_roles_only_as_said_by_the_owner_or_a_review(self):
        self.add(mail(3, 'Sam Stone <sam@acme.com>', 'Ann Stone <ann@home.example>, Pat <pat@client.com>, Lee <lee@globex.com>', ('SENT',)),
                 {'account_id': 'work', 'source_id': 'st1', 'kind': 'stripe.event', 'occurred_at': at(2), 'title': 'Payment failed', 'body': '', 'deleted': False,
                  'metadata': {'from': 'pat@client.com', 'customer_name': 'Client Ltd', 'type': 'invoice.payment_failed'}},
                 {'account_id': 'work', 'source_id': 'cr1', 'kind': 'salesflare.email', 'occurred_at': at(2), 'title': 'Hi', 'body': '', 'deleted': False,
                  'metadata': {'contacts': [{'email': 'lee@globex.com', 'name': 'Lee'}], 'crm_account': {'domain': 'globex.com', 'name': 'Globex'}}},
                 {'account_id': 'slack', 'source_id': 's1', 'kind': 'slack.message', 'occurred_at': at(1), 'title': 'DM', 'body': 'hey', 'deleted': False,
                  'metadata': {'channel_id': 'D1', 'channel_name': 'DM U0TEAM1', 'conversation_type': 'dm', 'user_id': 'U0TEAM1'}})
        self.build()
        role = lambda node: self.node(node)['role']
        for node in ('slack:U0TEAM1', 'email:pat@client.com', 'email:ann@home.example', 'email:lee@globex.com'):
            self.assertIsNone(role(node))  # a source doesn't say what someone is to the owner
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT name FROM graph_nodes WHERE id='org:client.com'").fetchone()[0], 'Client Ltd')
        self.app.graph_build = self.build
        self.app.graph_action({'node_id': 'email:pat@client.com', 'role': 'CFO at Client Ltd'})
        self.assertEqual(role('email:pat@client.com'), 'CFO at Client Ltd')
        with self.assertRaisesRegex(ValueError, 'Only the owner'):
            self.app.action('graph_role', {'node_id': 'email:pat@client.com', 'role': 'friend'})
        self.app.action('graph_role', {'node_id': 'email:ann@home.example', 'role': 'sister'}, owner_dashboard=True)
        self.app.graph_action({'node_id': 'email:ann@home.example', 'role': 'cousin'})  # a review can't overrule the owner
        self.assertEqual(role('email:ann@home.example'), 'sister')
        self.app.action('graph_role', {'node_id': 'email:ann@home.example', 'role': ''}, owner_dashboard=True)
        self.assertIsNone(role('email:ann@home.example'))
        self.assertIn('graph_role', a.OWNER_ACTIONS)
        with self.store.connect() as db:
            lines = graph.context_lines(db, self.ids(['st1']))
        self.assertIn('- Pat (pat@client.com): CFO at Client Ltd; ', lines)

    def test_what_is_open_between_you_shows_on_the_person(self):
        ask = mail(3, 'Ann Example <ann@client.com>', 'sam@acme.com', thread='deal')
        reply = mail(2, 'Sam <sam@acme.com>', 'Ann Example <ann@client.com>', ('SENT',), thread='deal')
        self.add(ask, reply)
        ask_id, reply_id = self.ids([ask['source_id'], reply['source_id']])
        self.store.owner_user_ids = set()
        self.app.action('promise', {'direction': 'mine', 'event_id': reply_id, 'who': 'Ann', 'what': 'send the deck', 'due': '2026-10-20'})
        self.app.action('promise', {'direction': 'theirs', 'event_id': ask_id, 'who': 'Ann', 'what': 'sign the contract'})
        task = self.store.suggest('ann-1', 'Answer Ann', 'Ann asked about pricing.', [ask_id])
        other = self.store.suggest('ann-2', 'Book the call with Ann', 'Ann wants a call.', [reply_id])
        with self.store.connect() as db:
            db.execute("INSERT INTO facts(text,decision_id,suggestion_id,created_at) VALUES('Ann prefers calls before 10','d1',?,?)", (other['id'], NOW.isoformat()))
        self.build()
        detail = self.app.graph_action({'node_id': 'email:ann@client.com'})
        self.assertEqual([p['what'] for p in detail['open']['you_promised']], ['send the deck'])
        self.assertEqual([p['what'] for p in detail['open']['they_promised']], ['sign the contract'])
        self.assertEqual({c['title'] for c in detail['open']['cards']}, {'Answer Ann', 'Book the call with Ann'})
        self.assertEqual(detail['open']['facts'], ['Ann prefers calls before 10'])
        lines = self.app.graph_lines(task['id'])
        self.assertIn('open: you promised to send the deck by Tue 20 Oct; they promised to sign the contract; card "Book the call with Ann"', lines)
        self.assertNotIn('card "Answer Ann"', lines)  # the task itself


if __name__ == '__main__':
    unittest.main()

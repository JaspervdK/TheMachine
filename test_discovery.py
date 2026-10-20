"""Finding people you know on the Network (discovery.py): off until turned on, contacts go out blinded, only matches are
looked up, a card only with a concrete reason, colleague read from structure, connect is a friend request, Not now,
Never, findable, and leaving. A fake Network stands in for the control plane; no network, no sends."""
import datetime as dt
import json
import secrets
import unittest

import test_fleet as tf
import test_friends as tfr
from test_fleet import CONFIG, NOW

discovery = tf.a.fleet.discovery
fleet = tf.a.fleet

ON_NETWORK = {'nia@nia.example': ('nia', 'Nia North', False), 'lou@lou.example': ('lou', 'Lou', True),
              'sam@acme.example': ('sam', 'Sam', False)}


class NetworkRelay(tfr.FriendsRelay):
    def __init__(self):
        super().__init__()
        self.b = secrets.randbits(256) | 1
        self.found = {e: discovery.hexed(pow(discovery.to_group(e), self.b, discovery.P)) for e in ON_NETWORK}

    def call(self, method, path, body=None, timeout=40):
        if path != '/discover':
            return super().call(method, path, body, timeout)
        self.calls.append((method, path, body))
        if body['action'] == 'match':
            return {'items': [discovery.hexed(pow(int(x, 16), self.b, discovery.P)) for x in body['items']],
                    'found': sorted(y for e, y in self.found.items() if discovery.bucket(e) in body['buckets'])}
        if body['action'] == 'resolve':
            return {'people': [{'email': e, 'username': ON_NETWORK[e][0], 'name': ON_NETWORK[e][1], 'friends': ON_NETWORK[e][2]}
                               for e in body['emails'] if e in ON_NETWORK]}
        if body['action'] == 'discoverable':
            return {'discoverable': body['on']}
        return {'left': True, 'friends_removed': 1}


class Discovery(unittest.TestCase):
    def setUp(self):
        tf.Fleet.setUp(self)
        self.relay = NetworkRelay()
        self.fleet = fleet.Fleet(self.store.connect, CONFIG, lambda: 'token', relay=self.relay)
        self.fleet.directory(refresh=True)
        meeting = NOW + dt.timedelta(days=3)
        self.store.commit_page('home', {}, [{'account_id': 'home', 'source_id': 'cal-nia', 'kind': 'calendar.event', 'occurred_at': meeting.isoformat(),
                                            'title': 'Picnic', 'body': '', 'deleted': False, 'metadata': {}}])
        with self.store.connect() as db:
            event = db.execute("SELECT id FROM events WHERE source_id='cal-nia'").fetchone()[0]
            for node, name, emails, strength in [('p-nia', 'Nia North', ['nia@nia.example'], 5), ('p-lou', 'Lou', ['lou@lou.example'], 4),
                                                 ('p-sam', 'Sam', ['sam@acme.example'], 3), ('p-zed', 'Zed', ['zed@zed.example'], 2),
                                                 ('p-me', 'Owner', ['owner@gmail.com'], 9)]:
                db.execute('INSERT INTO graph_nodes(id,kind,name,strength,signals,info) VALUES(?,?,?,?,?,?)',
                           (node, 'person', name, strength, 3, json.dumps({'identifiers': ['email:' + e for e in emails]})))
            db.execute('INSERT INTO graph_events(node_id,event_id,at) VALUES(?,?,?)', ('p-nia', event, meeting.isoformat()))

    def people(self):
        return {p['username']: p for p in self.fleet.network_view()['people']}

    def test_off_until_turned_on(self):
        self.assertEqual(self.fleet.discovery_round(force=True), 0)
        self.assertEqual(self.relay.sent('/discover'), [])
        self.assertEqual(self.fleet.view()['network'], {'find': None, 'discoverable': None, 'asked': False, 'last': None, 'people': []})

    def test_lookup_is_blinded_and_only_matches_are_resolved(self):
        self.fleet.network_action('find_on')
        match, resolve = self.relay.sent('/discover')
        self.assertEqual(match['action'], 'match')
        with self.store.connect() as db:
            contacts = [c[0] for c in self.fleet.network_contacts(db)]
        self.assertEqual(len(match['items']), len(contacts))
        self.assertNotIn('owner@gmail.com', contacts)  # the owner's own addresses stay out
        self.assertIn('zed@zed.example', contacts)
        self.assertNotIn('@', json.dumps(match))
        self.assertEqual(sorted(resolve['emails']), ['lou@lou.example', 'nia@nia.example', 'sam@acme.example'])  # not Zed
        people = self.people()
        self.assertEqual(people['lou']['status'], 'connected')
        self.assertEqual((people['nia']['status'], people['nia']['hint']), ('new', 'personal'))
        self.assertEqual([r['kind'] for r in people['nia']['reasons']], ['meeting'])
        self.assertEqual((people['sam']['hint'], people['sam']['reasons']), ('colleague', []))  # same work domain; no reason, no card
        # A week has not passed: no second lookup
        self.fleet.discovery_round()
        self.assertEqual(len(self.relay.sent('/discover')), 2)
        with self.store.connect() as db:
            self.assertIn(f'Checked {len(contacts)} contacts privately; 3 on the Network', [r['what'] for r in db.execute('SELECT what FROM fleet_log')])

    def test_connect_later_never(self):
        self.fleet.network_action('find_on')
        self.assertEqual(self.fleet.network_action('connect', 'nia@nia.example', 'family'), {'status': 'requested', 'relation': 'family'})
        self.assertEqual(self.relay.sent('/friends')[-1], {'action': 'add', 'username': 'nia'})
        self.assertEqual(self.people()['nia']['relation'], 'family')
        self.fleet.network_action('never', 'sam@acme.example')
        self.assertNotIn('sam', self.people())
        with self.assertRaises(ValueError):
            self.fleet.network_action('connect', 'zed@zed.example')

    def test_later_comes_back_only_with_something_new(self):
        self.fleet.network_action('find_on')
        self.fleet.network_action('later', 'nia@nia.example')
        self.fleet.discovery_round()
        self.assertEqual(self.people()['nia']['status'], 'later')  # the same meeting: stays hidden
        # An open card with them comes up: back, with that reason
        with self.store.connect() as db:
            event = db.execute("SELECT id FROM events WHERE source_id='cal-nia'").fetchone()[0]
            db.execute("INSERT INTO suggestions(id,dedupe_key,title,body,event_ids,account_ids,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
                       ('s-nia', 'k-nia', 'Bring the tent', '', json.dumps([event]), '["home"]', 'pending', NOW.isoformat()))
        self.fleet.discovery_round()
        nia = self.people()['nia']
        self.assertEqual((nia['status'], nia['reasons'][0]), ('new', {'kind': 'card', 'card': 's-nia', 'title': 'Bring the tent'}))

    def test_findable_and_leave(self):
        self.assertEqual(self.fleet.network_action('discoverable_on'), {'discoverable': True})
        self.fleet.network_action('find_on')
        self.assertEqual(self.fleet.network_action('leave'), {'left': True})
        self.assertEqual(self.relay.sent('/discover')[-1], {'action': 'leave'})
        view = self.fleet.network_view()
        self.assertEqual((view['find'], view['discoverable'], view['people']), (False, False, []))

    def test_group_matches_the_network(self):
        a, items, buckets = discovery.blind(['x@example.com'], secret=7)
        self.assertEqual(items, [discovery.hexed(pow(discovery.to_group('X@Example.com '), 7, discovery.P))])
        self.assertEqual(buckets, [discovery.bucket('x@example.com')])


if __name__ == '__main__':
    unittest.main()

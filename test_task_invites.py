"""Handing a card to someone not on the Network, by email (task_invites.py): an email draft the owner sends themselves,
with a task link sealed by a key only the link has; and the other side adding that link as a card. A fake Network
stands in for the control plane; nothing is sent anywhere (no email, Telegram or push)."""
import json
import unittest
from unittest.mock import patch

import task_invites
import test_fleet_requests as tr

LINK_ID = 'A' * 22


class Sealing(unittest.TestCase):
    def test_a_task_seals_and_opens_only_with_its_key(self):
        task = {'kind': 'handoff', 'title': 'Send Sam the pricing', 'summary': 'For 40 seats, café included', 'due': '2026-11-03'}
        sealed, key = task_invites.seal(task)
        self.assertNotIn('Sam', sealed)
        self.assertEqual(task_invites.unseal(sealed, key), task)
        raw = bytearray(task_invites._unb64(sealed))
        raw[20] ^= 1
        with self.assertRaisesRegex(ValueError, 'not one this Machine can open'):
            task_invites.unseal(task_invites._b64(bytes(raw)), key)
        with self.assertRaisesRegex(ValueError, 'not one this Machine can open'):
            task_invites.unseal(sealed, task_invites.seal(task)[1])

    def test_only_the_fixed_form_comes_through(self):
        sealed, key = task_invites.seal({'title': '  Call   back ', 'run': 'send_email', 'due': 'tomorrow', 'kind': 'evil'})
        self.assertEqual(task_invites.unseal(sealed, key), {'title': 'Call back', 'kind': 'handoff'})
        with self.assertRaisesRegex(ValueError, 'holds no task'):
            task_invites.clean({'summary': 'no title'})

    def test_links_parse_in_their_three_forms(self):
        key = 'k' * 43
        for text in (f'see https://app.example.test/t/{LINK_ID}#k={key} thanks', f'#task={LINK_ID}.{key}', f'{LINK_ID}.{key}'):
            self.assertEqual(task_invites.parse_link(text), (LINK_ID, key))
        with self.assertRaisesRegex(ValueError, 'whole task link'):
            task_invites.parse_link(f'https://app.example.test/t/{LINK_ID}')


class ByEmail(unittest.TestCase):
    handed_card, card_row = tr.Requests.handed_card, tr.Requests.card_row

    def setUp(self):
        tr.Requests.setUp(self)
        self.made = {}
        plain = self.relay.call

        def call(method, path, body=None, timeout=40):
            if path != '/task-invite':
                return plain(method, path, body, timeout)
            self.relay.calls.append((method, path, body))
            if body['action'] == 'create':
                self.made = {'id': LINK_ID, 'url': 'https://app.example.test/t/' + LINK_ID, 'sealed': body['sealed']}
                return {k: self.made[k] for k in ('id', 'url')}
            return {'id': LINK_ID, 'kind': 'handoff', 'sealed': self.made['sealed'], 'from': {'name': 'Owner Example', 'username': 'owner'}}
        self.relay.call = call

    def test_a_handoff_by_email_is_a_draft_the_owner_sends_and_its_link_brings_the_task_along(self):
        card = self.handed_card()
        with self.assertRaisesRegex(ValueError, 'email address'):
            self.app.action('fleet_handoff_email', {'suggestion_id': card, 'email': 'not an address'}, owner_dashboard=True)
        with self.assertRaisesRegex(ValueError, 'dashboard'):
            self.app.action('fleet_handoff_email', {'suggestion_id': card, 'email': 'kim@other.example'}, owner_dashboard=False)
        done = self.app.action('fleet_handoff_email', {'suggestion_id': card, 'email': 'kim@other.example', 'title': 'Send Sam the pricing',
                                                       'summary': 'Sam wants pricing for 40 seats.'}, owner_dashboard=True)
        self.assertEqual(done, {'card_id': card, 'invite_id': LINK_ID})
        row = self.card_row(card)
        self.assertEqual(row['status'], 'prepared')
        action = json.loads(row['next_action'])
        self.assertEqual((action['kind'], action['account_id'], action['to']), ('email', 'work', ['kim@other.example']))
        self.assertEqual(action['subject'], 'Could you take this on: Send Sam the pricing')
        self.assertIn('Sam wants pricing for 40 seats.', action['body'])
        self.assertIn('by 2026-11-03', action['body'])
        self.assertTrue(action['body'].rstrip().endswith(f'https://app.example.test/t/{LINK_ID}#k=' + task_invites.LINK.search(action['body']).group(2)))
        self.assertEqual(action['body'].count('The Machine'), 1)  # one line of invitation, after the task
        # Nothing went out: the Network got the sealed task only, never the words or the address
        sent = self.relay.sent('/task-invite')[0]
        self.assertNotIn('Sam', json.dumps(sent))
        self.assertNotIn('kim@', json.dumps(sent))
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM action_jobs WHERE status!='local_preparation'").fetchone()[0], 0)
        self.assertEqual(self.told, [])
        # Their Machine adds it from the link: a card with what the owner wrote, and who to connect with
        link = task_invites.LINK.search(action['body']).group(0)
        with self.store.connect() as db:
            before = db.execute('SELECT COUNT(*) FROM suggestions').fetchone()[0]
        with self.assertRaisesRegex(ValueError, 'dashboard'):
            self.app.action('task_import', {'link': link}, owner_dashboard=False)
        added = self.app.action('task_import', {'link': link}, owner_dashboard=True)
        self.assertEqual((added['from'], added['can_connect']), ({'name': 'Owner Example', 'username': 'owner'}, True))
        new = self.card_row(added['card_id'])
        self.assertEqual((new['title'], new['due'], new['status']), ('Send Sam the pricing', '2026-11-03', 'pending'))
        self.assertIn('sent this by email', new['body'])
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM suggestions').fetchone()[0], before + 1)
            meta = json.loads(db.execute("SELECT metadata FROM events WHERE source_id=?", ('fleet:task:' + LINK_ID,)).fetchone()[0])
        self.assertEqual(meta['sender_display'], 'Owner Example (by email)')
        self.assertEqual(self.app.action('task_import', {'link': link}, owner_dashboard=True)['card_id'], added['card_id'])  # once

    def test_failed_local_save_reuses_the_same_remote_link_and_key(self):
        card=self.handed_card()
        args={'suggestion_id':card,'email':'kim@other.example','title':'Help','summary':'Please help'}
        with patch.object(self.app.store,'prepare',side_effect=RuntimeError('restart')):
            with self.assertRaises(RuntimeError):self.app.action('fleet_handoff_email',args,owner_dashboard=True)
        self.app.action('fleet_handoff_email',args,owner_dashboard=True)
        sent=[r for r in self.relay.sent('/task-invite') if r['action']=='create']
        self.assertEqual(sent[0]['request_id'],sent[1]['request_id'])
        self.assertEqual(sent[0]['sealed'],sent[1]['sealed'])

    def test_bad_opening_key_does_not_acknowledge_an_added_card(self):
        sealed,key=task_invites.seal({'title':'Help'})
        self.made={'sealed':sealed}
        wrong=task_invites.seal({'title':'Wrong'})[1]
        with self.assertRaises(ValueError):self.app.task_import(LINK_ID+'.'+wrong)
        self.assertFalse(any(r['action']=='added' for r in self.relay.sent('/task-invite')))

    def test_a_machine_not_on_the_network_adds_the_link_too(self):
        sealed, key = task_invites.seal({'title': 'Plan the offsite', 'kind': 'schedule'})
        self.app.fleet = None
        with patch.object(task_invites, 'claim', return_value={'sealed': sealed, 'from': {'name': 'Ann', 'username': 'ann'}}) as claim:
            added = self.app.action('task_import', {'link': f'{LINK_ID}.{key}'}, owner_dashboard=True)
        self.assertEqual(claim.call_args.args[1], LINK_ID)
        self.assertFalse(added['can_connect'])  # connecting needs the Network
        self.assertEqual(self.card_row(added['card_id'])['title'], 'Plan the offsite')


if __name__ == '__main__':
    unittest.main()

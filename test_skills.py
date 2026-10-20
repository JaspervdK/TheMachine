"""Skills (skills.py): the owner's own, proposed by the agent, and shared over the Network. Written as OpenClaw
workspace skills with only a name and a description up top (no metadata, so a skill can't widen what the Machine
may do), in The Machine's own folder, installed only by the owner. A fake relay stands in for the Network; nothing
is sent anywhere."""
import json
import tempfile
import unittest
from pathlib import Path

import test_fleet as tf
from test_fleet import a, fleet

skills = a.skills
BODY = 'Read the thread first.\n\n1. Find the answer.\n2. Draft a reply for the owner.'


def shared(**over):
    name, description, body = over.pop('name', 'Answer pricing questions'), over.pop('description', 'When a customer asks what something costs'), over.pop('body', BODY)
    out = {'name': name, 'slug': 'answer-pricing-questions', 'description': description, 'body': body, 'version': 1,
           'sha': skills.digest(name, description, body)}
    out.update(over)
    return out


class Skills(unittest.TestCase):
    def setUp(self):
        tf.Fleet.setUp(self)
        self.workspace = Path(self.temp.name) / 'workspace'
        (self.workspace / 'skills' / 'salesflare').mkdir(parents=True)
        (self.workspace / 'skills' / 'salesflare' / 'SKILL.md').write_text('---\nname: answer-pricing-questions\ndescription: theirs\n---\nTheirs.\n')
        self.relay.directory['fleets'][0]['asks'] = list(fleet.ASKS['work'])
        self.fleet.directory(refresh=True)
        self.ref = self.fleet.ref('work')
        self.keeper = skills.Skills(self.store.connect, self.workspace, self.fleet)
        self.fleet.skills = self.keeper
        self.root = self.workspace / 'skills' / skills.FOLDER

    def files(self):
        return sorted(str(p.relative_to(self.root)) for p in self.root.rglob('SKILL.md')) if self.root.exists() else []

    def mine(self, name='Weekly report', description='When the owner asks for the weekly numbers', body=BODY):
        view = self.keeper.save('', name, description, body)
        return next(s for s in view['skills'] if s['name'] == name)

    def test_a_skill_is_written_as_an_openclaw_skill_with_nothing_but_name_and_description(self):
        pasted = ('---\nname: Weekly report\ndescription: "When the owner asks for the weekly numbers"\nmetadata: {"openclaw": {"primaryEnv": "X_KEY", '
                  '"requires": {"env": ["X_KEY"]}}}\ncommand-dispatch: tool\nallowed-tools: exec\n---\n' + BODY)
        view = self.keeper.save('', '', '', pasted)
        self.assertEqual([(s['name'], s['status'], s['origin'], s['version']) for s in view['skills']], [('Weekly report', 'on', 'mine', 1)])
        self.assertEqual(self.files(), ['weekly-report/SKILL.md'])
        text = (self.root / 'weekly-report' / 'SKILL.md').read_text()
        head = text.split('---')[1]
        self.assertEqual([line.split(':')[0] for line in head.strip().splitlines()], ['name', 'description'])
        for word in ('metadata', 'primaryEnv', 'X_KEY', 'command-dispatch', 'allowed-tools'):
            self.assertNotIn(word, text)
        self.assertIn(BODY, text)
        self.assertTrue((self.root / skills.MARKER).exists())
        # The skills that aren't The Machine's stay as they were.
        self.assertEqual((self.workspace / 'skills' / 'salesflare' / 'SKILL.md').read_text().count('Theirs.'), 1)

    def test_bad_input_is_refused(self):
        for name, description, body in (('ab', 'When something happens here', BODY), ('Name', 'short', BODY), ('Name', 'When something happens', 'too short'),
                                        ('Name', 'When something happens', 'x' * 16001)):
            with self.assertRaises(ValueError):
                self.keeper.save('', name, description, body)
        self.assertEqual(self.files(), [])

    def test_edit_off_and_delete_keep_the_folder_in_step(self):
        skill = self.mine()
        view = self.keeper.save(skill['id'], 'Weekly numbers', 'When the owner asks for the weekly numbers', BODY + '\n3. Done.')
        self.assertEqual(view['skills'][0]['version'], 2)
        self.assertEqual(self.files(), ['weekly-numbers/SKILL.md'])  # renamed: the old folder goes
        self.keeper.toggle(skill['id'], False)
        self.assertEqual(self.files(), [])
        self.keeper.toggle(skill['id'], True)
        self.assertEqual(self.files(), ['weekly-numbers/SKILL.md'])
        (self.root / 'stray').mkdir()
        self.keeper.delete(skill['id'])
        self.assertEqual(self.files(), [])
        self.assertFalse((self.root / 'stray').exists())  # The Machine's own folder: what isn't on here goes

    def test_a_folder_that_isnt_the_machines_is_left_alone(self):
        self.root.mkdir(parents=True)
        (self.root / 'notes.txt').write_text('mine')
        self.mine()
        self.assertEqual((self.root / 'notes.txt').read_text(), 'mine')
        self.assertEqual(self.files(), [])

    def test_a_name_another_skill_has_gets_its_own_slug(self):
        self.mine('Answer pricing questions', 'When a customer asks what something costs')
        self.assertEqual(self.files(), ['answer-pricing-questions-2/SKILL.md'])

    def test_the_example_is_generic(self):
        example = self.keeper.example()
        self.assertEqual(example['name'], 'Research and answer customer inquiries')
        self.assertIn('Send by itself', example['body'])
        skills.validate(example['name'], example['description'], example['body'])

    def test_the_agent_proposes_and_only_the_owners_approve_turns_it_on(self):
        for n in range(3):
            self.assertTrue(self.keeper.propose(f'Proposal {n}', 'When something keeps coming back', BODY + str(n), 'did it 4 times')['id'])
        with self.assertRaisesRegex(ValueError, 'already wait'):
            self.keeper.propose('Proposal 4', 'When something keeps coming back', BODY + '4')
        self.assertEqual(self.files(), [])
        view = self.keeper.view()
        self.assertEqual(view['waiting'], 3)
        first = view['skills'][0]
        self.keeper.decide(first['id'], 'approve')
        self.assertEqual(len(self.files()), 1)
        self.keeper.decide(view['skills'][1]['id'], 'dismiss')
        self.assertEqual(self.keeper.view()['waiting'], 1)
        self.assertEqual(self.relay.sent('/answer'), [])  # nobody to tell
        self.assertTrue(self.keeper.propose(first['name'], 'When something keeps coming back', BODY + 'x')['duplicate'])

    def test_a_shared_skill_waits_for_the_owner_then_installs_and_tells_the_sender(self):
        question = {**shared(note='Saves us  time'), 'metadata': {'env': 'X'}}
        self.assertEqual(self.fleet.take_ask({'id': 'q1', 'fleet': 'work', 'from': 'pat1', 'kind': 'skill', 'question': question}), 'waiting')
        self.assertEqual(self.relay.sent('/answer')[-1], {'id': 'q1', 'status': 'waiting'})
        self.assertEqual(self.files(), [])  # never by itself
        view = self.keeper.view()
        self.assertEqual(view['waiting'], 1)
        incoming = view['skills'][0]
        self.assertEqual((incoming['status'], incoming['author'], incoming['note']), ('incoming', 'Pat', 'Saves us time'))
        self.assertEqual(self.keeper.get(incoming['id'])['body'], BODY)
        self.keeper.decide(incoming['id'], 'install')
        self.assertEqual(self.relay.sent('/answer')[-1], {'id': 'q1', 'status': 'answered', 'answer': {'stage': 'installed'}})
        self.assertEqual(self.files(), ['answer-pricing-questions-2/SKILL.md'])  # their slug is taken by a skill here
        text = (self.root / 'answer-pricing-questions-2' / 'SKILL.md').read_text()
        self.assertIn('> Shared by Pat, version 1. Guidance only', text)
        self.assertNotIn('env', text.split('---')[1])
        with self.assertRaisesRegex(ValueError, 'Already decided'):
            self.keeper.decide(incoming['id'], 'decline')
        # The same again: already there. A new version: an update to read and install again.
        self.assertEqual(self.fleet.take_ask({'id': 'q2', 'fleet': 'work', 'from': 'pat1', 'kind': 'skill', 'question': shared()}), 'answered')
        newer = shared(body=BODY + '\n3. Check the price list.', version=2)
        self.assertEqual(self.fleet.take_ask({'id': 'q3', 'fleet': 'work', 'from': 'pat1', 'kind': 'skill', 'question': newer}), 'waiting')
        update = next(s for s in self.keeper.view()['skills'] if s['status'] == 'incoming')
        self.assertTrue(update['update'])
        self.assertEqual(self.keeper.get(update['id'])['previous_body'], BODY)
        self.keeper.decide(update['id'], 'install')
        rows = [s for s in self.keeper.view()['skills'] if s['origin'] == 'shared']
        self.assertEqual([(s['status'], s['version']) for s in rows], [('on', 2)])
        self.assertIn('Check the price list', (self.root / 'answer-pricing-questions-2' / 'SKILL.md').read_text())

    def test_a_declined_skill_is_gone_and_the_sender_hears_declined(self):
        self.fleet.take_ask({'id': 'q1', 'fleet': 'work', 'from': 'pat1', 'kind': 'skill', 'question': shared()})
        self.keeper.decide(self.keeper.view()['skills'][0]['id'], 'decline')
        self.assertEqual(self.relay.sent('/answer')[-1], {'id': 'q1', 'status': 'declined'})
        self.assertEqual(self.keeper.view()['skills'], [])

    def test_a_tampered_or_unknown_skill_is_declined(self):
        for n, question in enumerate((shared(sha='0' * 64), shared(body='short'), shared(slug='../etc'), shared(version=True), 'text')):
            self.assertEqual(self.fleet.take_ask({'id': f'q{n}', 'fleet': 'work', 'from': 'pat1', 'kind': 'skill', 'question': question}), 'declined')
        self.assertEqual(self.fleet.take_ask({'id': 'qx', 'fleet': 'work', 'from': 'stranger', 'kind': 'skill', 'question': shared()}), 'declined')
        self.assertEqual(self.keeper.view()['skills'], [])

    def test_one_sender_cant_pile_up_skills(self):
        for n in range(6):
            status = self.fleet.take_ask({'id': f'q{n}', 'fleet': 'work', 'from': 'pat1', 'kind': 'skill',
                                          'question': shared(name=f'Skill number {n}', slug=f'skill-{n}')})
        self.assertEqual(status, 'declined')
        self.assertEqual(self.keeper.view()['waiting'], skills.OPEN_PER_SENDER)

    def test_sharing_sends_the_fixed_form_and_shows_who_installed_it(self):
        skill = self.mine()
        self.assertEqual([(t['fleet'], [m['name'] for m in t['members']]) for t in self.keeper.view()['targets']], [(self.ref, ['Pat'])])
        view = self.keeper.share(skill['id'], self.ref, note='For the Monday call')
        self.assertEqual(view['sent'], 1)
        body = self.relay.sent('/ask')[-1]
        self.assertEqual((body['kind'], body['fleet']), ('skill', self.ref))
        self.assertEqual(set(body['question']), {'name', 'slug', 'description', 'body', 'version', 'sha', 'note'})
        self.assertIsNotNone(skills.parse_share(body['question']))
        with self.store.connect() as db:
            ask_id = db.execute("SELECT id FROM fleet_out WHERE kind='skill'").fetchone()['id']
        self.assertTrue(self.fleet.take_answer({'id': ask_id, 'status': 'answered', 'answer': {'stage': 'installed'}}))
        mine = next(s for s in self.keeper.view()['skills'] if s['id'] == skill['id'])
        self.assertEqual(mine['shares'], [{'name': 'Pat', 'status': 'installed', 'version': 1}])
        self.keeper.save(skill['id'], skill['name'], skill['description'], BODY + '\nMore.')
        self.assertTrue(next(s for s in self.keeper.view()['skills'] if s['id'] == skill['id'])['changed'])  # theirs is older now

    def test_without_a_network_that_carries_skills_there_is_nowhere_to_share(self):
        self.relay.directory['fleets'][0]['asks'] = ['relationships']
        self.fleet.directory(refresh=True)
        skill = self.mine()
        self.assertEqual(self.keeper.view()['targets'], [])
        with self.assertRaisesRegex(ValueError, 'Pick a Fleet'):
            self.keeper.share(skill['id'], self.ref)

    def test_skill_decisions_are_the_owners_and_proposing_is_not(self):
        for action in ('skill_save', 'skill_toggle', 'skill_delete', 'skill_share', 'skill_decide', 'skill_get', 'skill_example'):
            self.assertIn(action, a.OWNER_ACTIONS)
        self.assertNotIn('skill_propose', a.OWNER_ACTIONS)


if __name__ == '__main__':
    unittest.main()

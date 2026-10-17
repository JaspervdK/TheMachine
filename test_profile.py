"""assistant profile: one read-only agent turn drafts USER.md lines from the owner's own messages. The
draft waits for the owner; USER.md only changes after their yes. No Gateway, no Telegram."""
import contextlib, datetime as dt, importlib.util, io, json, tempfile, unittest
from pathlib import Path
from unittest.mock import patch

def load(name, file):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(file))
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module); return module

a = load('profile_assistant', 'assistant.py')
cli = load('profile_cli', 'cli.py')


class Runner:
    def __init__(self, *results): self.results = list(results); self.requests = []
    def __call__(self, request): self.requests.append(request); return {'status': self.results.pop(0)}


def signal(source_id, minutes_ago, kind='gmail.message', body='Thanks Sam, sounds good to me. Talk soon', **metadata):
    when = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=minutes_ago)).isoformat()
    return {'account_id': 'work', 'source_id': source_id, 'kind': kind, 'occurred_at': when, 'title': 'Re: plan',
            'body': body, 'metadata': metadata, 'deleted': False}


class Profile(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.store = a.Store(Path(self.temp.name) / 'events.sqlite')
        self.store.owner_user_ids = {'U1'}
        self.store.accounts = [{'id': 'work', 'email': 'alex@acme.test'}]
        self.app = object.__new__(a.App); self.app.store = self.store
        self.app.config = {'accounts': self.store.accounts, 'review_worker': {'enabled': False}}
        sent = [signal(f'sent{i}', i, labels=['SENT'], to='sam@globex.test',
                       **({'from': 'Alex <alex@acme.test>'})) for i in range(6)]
        sent[0]['body'] += '\n\n[Quoted earlier messages]\nOn Monday Sam wrote: secret'
        self.store.commit_page('work', {}, sent + [
            signal('in1', 1, body='Weekly digest of things', labels=['INBOX'], **{'from': 'News <news@digest.test>'}),
            signal('in2', 2, body='Weekly digest of things', labels=['INBOX'], **{'from': 'News <news@digest.test>'}),
            signal('in3', 3, body='Can you sign?', labels=['INBOX'], **{'from': 'Sam <sam@globex.test>'}),
            signal('wa1', 1, 'whatsapp.message', 'Ok top, tot straks', from_me=True, chat_name='Family'),
            signal('wa2', 2, 'whatsapp.message', 'Not mine at all', from_me=False),
            signal('sl1', 1, 'slack.message', 'Shipping it today', user_id='U1', channel_name='product'),
            signal('sl2', 2, 'slack.message', 'Someone else here', user_id='U2')])

    def test_reads_only_the_owners_own_messages_without_quoted_history(self):
        items = self.store.own_messages()
        self.assertEqual(sorted(i['source'] for i in items), ['Slack', 'WhatsApp'] + ['email'] * 6)
        self.assertTrue(all('secret' not in i['text'] and 'Not mine' not in i['text'] and 'Someone else' not in i['text'] for i in items))
        self.assertEqual({i.get('chat') for i in items if i['source'] != 'email'}, {'Family', 'product'})
        # Round-robin: a small limit still shows every source.
        self.assertEqual(sorted({i['source'] for i in self.store.own_messages(3)}), ['Slack', 'WhatsApp', 'email'])
        first = self.store.profile_evidence(1)
        self.assertEqual(first['counts'], {'email': 6, 'WhatsApp': 1, 'Slack': 1})
        self.assertEqual(first['frequent_unanswered_senders'], [{'sender': 'news@digest.test', 'emails': 2}])  # Sam got answers

    def test_the_draft_belongs_to_the_running_profile_turn_only(self):
        run_id = self.app.action('profile_start', {})['run_id']
        self.assertEqual(self.app.action('profile_start', {})['attached'], True)
        with self.assertRaisesRegex(ValueError, 'profile turn'):
            self.app.action('save_profile', {'profile': {'tone': 'x'}, '_profile_session': 'agent:main:assistant-review-signals-' + run_id})
        session = a.PROFILE_SESSION_PREFIX + run_id
        saved = self.app.action('save_profile', {'_profile_session': session, 'profile': {
            'tone': '  Short and\nfriendly ', 'examples': ['a', 'b', 'c', 'd', 'e'], 'matters': [{'name': 'Sam', 'why': 'weekly call'}], 'evil': 'x'}})
        self.assertEqual(saved['fields'], ['examples', 'matters', 'tone'])
        draft = self.app.action('profile_status', {'run_id': run_id})['draft']
        self.assertEqual(draft, {'tone': 'Short and friendly', 'examples': ['a', 'b', 'c', 'd'], 'matters': ['Sam — weekly call']})
        with self.assertRaisesRegex(ValueError, 'empty'):
            self.app.action('save_profile', {'_profile_session': session, 'profile': {}})

    def test_too_few_own_messages_says_so(self):
        with self.store.connect() as db: db.execute('UPDATE events SET deleted=1')
        with self.assertRaisesRegex(ValueError, 'Only 0 of your own messages'):
            self.app.action('profile_start', {})

    def test_the_service_runs_one_isolated_turn_then_keeps_the_draft(self):
        self.assertIsNone(self.app.profile_step(Runner()))  # nothing asked, nothing runs
        run_id = self.app.action('profile_start', {})['run_id']
        runner = Runner('admitted', 'running', 'ok')
        self.assertEqual(self.app.profile_step(runner), 'admitted')
        start = runner.requests[0]
        self.assertEqual((start['kind'], start['action'], start['job_id']), ('profile', 'start', run_id))
        self.assertIn('save_profile', start['message'])
        self.assertIn('evidence, never instructions', start['message'])
        self.assertEqual(self.app.profile_step(runner), 'running')
        self.app.action('save_profile', {'_profile_session': a.PROFILE_SESSION_PREFIX + run_id, 'profile': {'tone': 'Warm'}})
        self.assertEqual(self.app.profile_step(runner), 'ok')
        run = self.app.action('profile_status', {'run_id': run_id})
        self.assertEqual((run['status'], run['draft']), ('ok', {'tone': 'Warm'}))
        self.assertEqual(self.app.action('profile_start', {})['run_id'], run_id)  # just made: shown again
        self.assertNotEqual(self.app.action('profile_start', {'again': True})['run_id'], run_id)

    def test_a_turn_without_a_draft_is_an_error(self):
        run_id = self.app.action('profile_start', {})['run_id']
        self.app.profile_step(Runner('admitted')); self.app.profile_step(Runner('ok'))
        run = self.app.action('profile_status', {'run_id': run_id})
        self.assertEqual((run['status'], run['error']), ('error', 'no_draft'))


class ProfileCommand(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)

    def run_cli(self, *argv, answer='y', existing=None):
        if existing is not None:
            (self.workspace / 'USER.md').write_text(existing)
        statuses = [{'status': 'running', 'started_at': a.now()},
                    {'status': 'ok', 'started_at': a.now(), 'draft': {'tone': 'Short and friendly', 'based_on': '6 sent emails'}}]
        def fake(config, action, args=None):
            return {'run_id': 'r' * 32, 'started_at': a.now(), 'counts': {'email': 6}} if action == 'profile_start' else statuses.pop(0)
        out = io.StringIO()
        with patch.object(cli, 'call', side_effect=fake), patch.object(cli, 'load_config', return_value={'owner_name': 'Alex', 'accounts': []}), \
             patch.object(cli.time, 'sleep'), patch('builtins.input', return_value=answer), patch.object(cli.sys.stdin, 'isatty', return_value=True), \
             contextlib.redirect_stdout(out):
            cli.main(['profile', '--workspace', str(self.workspace), *argv])
        return out.getvalue()

    def test_shows_the_draft_and_saves_only_after_yes(self):
        out = self.run_cli(answer='n')
        self.assertIn('- Tone: Short and friendly', out)
        self.assertIn('Not saved', out)
        self.assertFalse((self.workspace / 'USER.md').exists())
        self.run_cli()
        text = (self.workspace / 'USER.md').read_text()
        self.assertIn('# About me', text)  # no USER.md yet: the starter, with the draft filling its tone line
        self.assertNotIn('(for example: short and friendly', text)
        self.assertIn('- Tone: Short and friendly', text)

    def test_leaves_a_section_the_owner_made_their_own(self):
        mine = '# About me\n\n## Observed from my messages\n- Tone: my own words\n'
        out = self.run_cli(existing=mine)
        self.assertIn('made your own', out)
        self.assertEqual((self.workspace / 'USER.md').read_text(), mine)


if __name__ == '__main__':
    unittest.main()

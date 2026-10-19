"""Export and import: what moves, what stays behind, and that keys only travel encrypted."""
import io
import json
import shutil
import sqlite3
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import moving

HAS_OPENSSL = shutil.which('openssl') is not None


class Moving(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        patcher = patch.object(moving, 'own_connectors', return_value=[])
        patcher.start()
        self.addCleanup(patcher.stop)
        self.data = self.tmp / 'hosted/data'
        self.data.mkdir(parents=True)
        db = sqlite3.connect(self.data / 'events.sqlite')
        db.execute('CREATE TABLE events(id TEXT, title TEXT)')
        db.execute("INSERT INTO events VALUES ('e1', 'Example subject')")
        db.commit()
        db.close()
        for name, text in (('slack.env', 'SLACK_TOKEN=xoxb-example\n'), ('owner-token', 'owner-example'),
                           ('machine-token', 'hosted-example'), ('events-old.sqlite.bak', 'old copy'),
                           ('notes.txt', 'kept as data')):
            (self.data / name).write_text(text)
        self.config = self.data / 'config.json'
        self.config.write_text(json.dumps({
            'database': str(self.data / 'events.sqlite'),
            'credentials': {'slack': str(self.data / 'slack.env'), 'google-broker': str(self.data / 'machine-token')},
            'google_broker': {'url': 'https://broker.example.test'}, 'manage_url': 'https://manage.example.test',
            'needs_you_file': str(self.data / 'needs-you.json'), 'enrichment': {'enabled': False, 'by': 'hosting'},
            'accounts': [{'id': 'work-gmail', 'service': 'gmail', 'transport': 'broker'},
                         {'id': 'slack', 'service': 'slack', 'transport': 'api', 'credential': 'slack'}]}))
        self.space = self.tmp / 'hosted/workspace'
        (self.space / 'memory').mkdir(parents=True)
        (self.space / 'tools/personal-assistant').mkdir(parents=True)
        (self.space / 'USER.md').write_text('About the owner\n')
        (self.space / 'memory/today.md').write_text('A note\n')
        (self.space / 'tools/personal-assistant/assistant.py').write_text('app code')
        self.claw = self.tmp / 'hosted/openclaw'
        (self.claw / 'credentials').mkdir(parents=True)
        (self.claw / 'openclaw.json').write_text(json.dumps({'channels': {'telegram': {'enabled': True, 'botToken': '123:example'}},
                                                             'commands': {'ownerAllowFrom': ['tg:1000']}}))
        (self.claw / 'credentials/relay.secret').write_text('relay-example')
        self.out = self.tmp / 'out'

    def export(self, passphrase=None):
        return moving.export(self.config, self.out, passphrase=passphrase, workspace=self.space, openclaw_home=self.claw,
                             source='hosted')

    def names(self, path):
        with tarfile.open(path, 'r:gz') as archive:
            return set(archive.getnames()), archive

    def read(self, path, name):
        with tarfile.open(path, 'r:gz') as archive:
            return archive.extractfile(name).read().decode()

    def target(self):
        local = self.tmp / 'local'
        return local / 'data/config.json', local / 'workspace', local / 'openclaw'

    def test_without_a_passphrase_no_key_leaves(self):
        path, manifest = self.export()
        self.assertTrue(path.name.endswith('.tar.gz'))
        names, _ = self.names(path)
        self.assertIn('data/events.sqlite', names)
        self.assertIn('data/notes.txt', names)
        self.assertIn('workspace/USER.md', names)
        self.assertIn('workspace/memory/today.md', names)
        self.assertFalse([n for n in names if n.startswith(('secrets/', 'workspace/tools'))])
        self.assertNotIn('data/events-old.sqlite.bak', names)
        with tarfile.open(path, 'r:gz') as archive:
            blob = b''.join(archive.extractfile(m).read() for m in archive.getmembers() if m.isfile())
        for secret in (b'xoxb-example', b'owner-example', b'hosted-example', b'123:example', b'relay-example'):
            self.assertNotIn(secret, blob)
        self.assertEqual(manifest['telegram'], {'enabled': True, 'owner_allow_from': ['tg:1000']})
        self.assertFalse(manifest['includes_keys'])
        self.assertTrue(any('sign in to Google again' in line for line in manifest['reconnect']))
        self.assertTrue(any(line.startswith('slack:') for line in manifest['reconnect']))
        self.assertIn('assistant import', self.read(path, 'README.txt'))

    def test_the_database_copy_is_whole(self):
        path, _ = self.export()
        with tarfile.open(path, 'r:gz') as archive:
            archive.extract('data/events.sqlite', self.tmp / 'check', filter='data')
        db = sqlite3.connect(self.tmp / 'check/data/events.sqlite')
        self.addCleanup(db.close)
        rows = db.execute('SELECT title FROM events').fetchall()
        self.assertEqual(rows, [('Example subject',)])

    def test_import_into_a_fresh_install(self):
        path, _ = self.export()
        config_path, space, claw = self.target()
        result = moving.import_(path, config_path, workspace=space, openclaw_home=claw)
        config = json.loads(config_path.read_text())
        data = str(config_path.parent)
        self.assertEqual(config['database'], data + '/events.sqlite')
        self.assertEqual(config['credentials'], {'slack': data + '/slack.env'})
        for key in ('google_broker', 'manage_url', 'needs_you_file'):
            self.assertNotIn(key, config)
        self.assertEqual(config['enrichment'], {'enabled': False})
        self.assertEqual(config['accounts'][0]['transport'], 'gog')
        self.assertEqual((space / 'USER.md').read_text(), 'About the owner\n')
        self.assertFalse((config_path.parent / 'slack.env').exists())
        self.assertFalse((claw / 'openclaw.json').exists())
        self.assertTrue(result['reconnect'])
        self.assertEqual(oct((config_path.parent / 'events.sqlite').stat().st_mode & 0o777), '0o600')

    def test_an_existing_setup_is_not_overwritten_without_force(self):
        path, _ = self.export()
        config_path, space, claw = self.target()
        config_path.parent.mkdir(parents=True)
        config_path.write_text(json.dumps({'accounts': [{'id': 'mine'}]}))
        (space).mkdir(parents=True)
        (space / 'USER.md').write_text('Local version\n')
        with self.assertRaisesRegex(moving.MoveError, 'already has connected sources'):
            moving.import_(path, config_path, workspace=space, openclaw_home=claw)
        result = moving.import_(path, config_path, workspace=space, openclaw_home=claw, force=True)
        self.assertEqual((space / 'USER.md').read_text(), 'Local version\n')
        self.assertEqual((space / 'USER.md.from-export').read_text(), 'About the owner\n')
        self.assertTrue(any('USER.md' in note for note in result['notes']))
        self.assertTrue(list(config_path.parent.parent.glob('data.before-import-*')))

    def test_an_archive_reaching_outside_is_refused(self):
        bad = self.tmp / 'bad.tar.gz'
        with tarfile.open(bad, 'w:gz') as archive:
            info = tarfile.TarInfo('../escape.txt')
            info.size = 2
            archive.addfile(info, io.BytesIO(b'hi'))
        config_path, space, claw = self.target()
        with self.assertRaisesRegex(moving.MoveError, 'outside'):
            moving.import_(bad, config_path, workspace=space, openclaw_home=claw)
        self.assertFalse((self.tmp / 'escape.txt').exists())

    def test_a_short_passphrase_is_refused(self):
        with self.assertRaisesRegex(moving.MoveError, 'at least'):
            self.export('short')

    @unittest.skipUnless(HAS_OPENSSL, 'openssl not installed')
    def test_with_a_passphrase_keys_move_encrypted(self):
        path, manifest = self.export('a long example passphrase')
        self.assertTrue(path.name.endswith('.tar.gz.enc'))
        self.assertFalse(list(self.out.glob('*.tar.gz')))
        self.assertTrue(manifest['includes_keys'])
        raw = path.read_bytes()
        self.assertNotIn(b'xoxb-example', raw)
        with self.assertRaises(tarfile.TarError):
            tarfile.open(path, 'r:gz').getnames()
        config_path, space, claw = self.target()
        with self.assertRaisesRegex(moving.MoveError, 'Wrong passphrase'):
            moving.import_(path, config_path, passphrase='not the right one', workspace=space, openclaw_home=claw)
        moving.import_(path, config_path, passphrase='a long example passphrase', workspace=space, openclaw_home=claw)
        self.assertEqual((config_path.parent / 'slack.env').read_text(), 'SLACK_TOKEN=xoxb-example\n')
        self.assertEqual(oct((config_path.parent / 'slack.env').stat().st_mode & 0o777), '0o600')
        self.assertIn('123:example', (claw / 'openclaw.json').read_text())
        self.assertFalse((config_path.parent / 'machine-token').exists())

    def test_estimate_counts_without_writing(self):
        plain = moving.estimate(self.config, workspace=self.space, openclaw_home=self.claw)
        keys = moving.estimate(self.config, secrets=True, workspace=self.space, openclaw_home=self.claw)
        self.assertGreater(keys['files'], plain['files'])
        self.assertFalse(self.out.exists())


if __name__ == '__main__':
    unittest.main()

"""install.sh / installer.py against a temporary HOME with stub openclaw and systemctl.
Nothing here touches the real user's systemd, OpenClaw or data directory."""
import builtins, json, os, shutil, socket, stat, subprocess, sys, tempfile, unittest
from pathlib import Path
from unittest import mock

import installer

ROOT = Path(__file__).resolve().parent
STUB = '''#!/bin/sh
echo "{name} $*" >> "{log}"
if [ "$*" = "$ASSISTANT_TEST_FAIL" ]; then echo "Synthetic failure" >&2; exit 1; fi
case "$*" in --version) echo "OpenClaw 2026.10.2 (test)";; esac
exit 0
'''
ANSWERS = {'ASSISTANT_OWNER_NAME': 'Alex Owner', 'ASSISTANT_TELEGRAM_OWNER': '1000001',
           'ASSISTANT_TAILSCALE_OWNER': 'alex@example.com', 'ASSISTANT_DASHBOARD_HOST': 'machine.example-tailnet.ts.net',
           'ASSISTANT_TIMEZONE': 'Europe/Amsterdam', 'ASSISTANT_CONNECTORS': 'gmail,calendar,gateway',
           'ASSISTANT_EMAIL': 'alex@example.com', 'ASSISTANT_GOG_CLIENT': 'work', 'ASSISTANT_BOUNDARY': 'work:Acme'}


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class InstallerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='machine-install-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home, self.bin, self.log = self.tmp / 'home', self.tmp / 'bin', self.tmp / 'calls.log'
        self.home.mkdir()
        self.bin.mkdir()
        for name in ('openclaw', 'systemctl'):
            path = self.bin / name
            path.write_text(STUB.format(log=self.log, name=name))
            path.chmod(0o755)
        package = self.tmp / 'lib/node_modules/openclaw'
        package.mkdir(parents=True)
        (package / 'package.json').write_text(json.dumps({'name':'openclaw','version':'2026.10.2'}))
        (self.bin / 'node').symlink_to(shutil.which('node'))
        self.data = self.home / '.local/share/personal-assistant'
        self.config = self.data / 'config.json'

    def env(self, **extra):
        env = {k: v for k, v in os.environ.items() if not k.startswith(('ASSISTANT_', 'OPENCLAW_'))}
        env.update(HOME=str(self.home), PATH=str(self.bin))
        env.update(ANSWERS)
        env.update(extra)
        return env

    def install(self, *args, **extra):
        return subprocess.run([sys.executable, str(ROOT / 'installer.py'), '--non-interactive', '--health-timeout', '0', *args],
                              env=self.env(**extra), capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def mode(self, path):
        return stat.S_IMODE(path.stat().st_mode)

    def test_non_interactive_install_writes_config_secrets_unit_plugin_and_cli(self):
        result = self.install('--no-start')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        config = json.loads(self.config.read_text())
        self.assertEqual((config['owner_name'], config['telegram_owner'], config['tailscale_owner']),
                         ('Alex Owner', '1000001', 'alex@example.com'))
        self.assertEqual(config['dashboard_host'], 'machine.example-tailnet.ts.net')
        self.assertEqual([a['id'] for a in config['accounts']], ['work-gmail', 'work-calendar', 'openclaw-gateway'])
        gmail = config['accounts'][0]
        self.assertEqual((gmail['email'], gmail['client'], gmail['boundary']), ('alex@example.com', 'work', 'work:Acme'))
        self.assertNotIn('exclude', gmail)
        self.assertEqual(config['app_dir'], str(ROOT))
        self.assertEqual(config['database'], str(self.data / 'events.sqlite'))
        self.assertFalse(config['delivery_enabled'])
        self.assertFalse(config['review_worker']['enabled'])
        self.assertTrue(Path(config['openclaw_package_dir'], 'package.json').exists())
        self.assertNotIn('~', json.dumps(config))
        self.assertEqual(self.mode(self.data), 0o700)
        for name in ('config.json', 'api-token', 'owner-token', 'vapid.json'):
            self.assertEqual(self.mode(self.data / name), 0o600, name)
        self.assertEqual(len((self.data / 'api-token').read_text()), 64)
        unit = (self.home / '.config/systemd/user/personal-assistant.service').read_text()
        self.assertIn(f'ExecStart={sys.executable} {ROOT}/assistant.py serve', unit)
        self.assertIn(f'WorkingDirectory={ROOT}', unit)
        self.assertEqual((self.home / '.local/bin/assistant').resolve(), ROOT / 'cli.py')
        cli = subprocess.run([sys.executable, str(self.home / '.local/bin/assistant'), '--help'], capture_output=True, text=True)
        self.assertIn('usage: assistant', cli.stdout, cli.stderr)
        calls = self.calls()
        self.assertIn(f'openclaw plugins install --link --force --accept-capabilities {ROOT}/openclaw-plugin', calls)
        self.assertIn('openclaw plugins enable personal-assistant-bridge', calls)
        self.assertIn('systemctl --user daemon-reload', calls)
        self.assertFalse(any('enable personal-assistant.service' in c for c in calls), calls)  # --no-start
        self.assertIn('Next steps', result.stdout)
        self.assertIn('gog auth add alex@example.com --client work', result.stdout)

    def test_rerun_upgrades_without_clobbering_config_or_secrets(self):
        self.assertEqual(self.install('--no-start').returncode, 0)
        token = (self.data / 'api-token').read_text()
        vapid = (self.data / 'vapid.json').read_text()
        config = json.loads(self.config.read_text())
        config['owner_name'] = 'Changed By Hand'
        config['accounts'][0]['exclude'] = [{'senders': ['noise@example.com']}]
        config['port'] = free_port()
        del config['weekly_recap']
        self.config.write_text(json.dumps(config))
        result = self.install(ASSISTANT_OWNER_NAME='Someone Else', ASSISTANT_CONNECTORS='gateway')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        again = json.loads(self.config.read_text())
        self.assertEqual(again['owner_name'], 'Changed By Hand')
        self.assertEqual(again['accounts'][0]['exclude'], [{'senders': ['noise@example.com']}])
        self.assertEqual(len(again['accounts']), 3)
        self.assertEqual(again['weekly_recap'], {'enabled': False, 'weekday': 5, 'at': '16:00'})  # missing default restored
        self.assertEqual(self.mode(self.config), 0o600)
        self.assertEqual((self.data / 'api-token').read_text(), token)
        self.assertEqual((self.data / 'vapid.json').read_text(), vapid)
        self.assertIn('Keeping existing', result.stdout)
        self.assertIn('already present, unchanged', result.stdout)
        calls = self.calls()
        self.assertIn('systemctl --user enable personal-assistant.service', calls)
        self.assertIn('systemctl --user restart personal-assistant.service', calls)
        self.assertIn('not answering yet', result.stdout)  # nothing listens on the free port
        before = self.config.read_text()
        self.assertEqual(self.install().returncode, 1)
        self.assertEqual(self.config.read_text(), before)

    def test_missing_answers_fail_clearly_without_writing(self):
        result = self.install('--no-start', ASSISTANT_TELEGRAM_OWNER='')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('ASSISTANT_TELEGRAM_OWNER', result.stdout + result.stderr)
        self.assertFalse(self.config.exists())
        result = self.install('--no-start', ASSISTANT_TELEGRAM_OWNER='@alex')
        self.assertIn('numeric chat ID', result.stdout + result.stderr)
        result = self.install('--no-start', ASSISTANT_CONNECTORS='gmail,fax')
        self.assertIn('Unknown sources: fax', result.stdout + result.stderr)

    def test_missing_openclaw_is_reported_with_a_fix(self):
        (self.bin / 'openclaw').unlink()
        result = self.install('--no-start')
        self.assertEqual(result.returncode, 1)
        self.assertIn('FAIL  openclaw CLI not found', result.stdout)
        self.assertIn('npm install -g openclaw@2026.10.2', result.stdout)
        self.assertFalse(self.config.exists())

    def test_failed_install_steps_exit_nonzero(self):
        for command in ('plugins install --link --force --accept-capabilities ' + str(ROOT / 'openclaw-plugin'),
                        'plugins enable personal-assistant-bridge',
                        'plugins reload personal-assistant-bridge --wait',
                        '--user daemon-reload', '--user enable personal-assistant.service',
                        '--user restart personal-assistant.service'):
            with self.subTest(command=command):
                result = self.install(ASSISTANT_TEST_FAIL=command)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        result = self.install('--no-start', ASSISTANT_TEST_FAIL='--user daemon-reload')
        self.assertEqual(result.returncode, 1, result.stdout)

    def test_activation_is_explicit_and_preserves_existing_choices(self):
        self.assertEqual(self.install('--no-start').returncode, 0)
        with mock.patch.dict(os.environ, self.env(), clear=True), mock.patch.object(installer, 'health', return_value=True):
            self.assertEqual(installer.main(['--non-interactive', '--activate']), 0)
        activated = json.loads(self.config.read_text())
        self.assertTrue(activated['delivery_enabled'])
        self.assertTrue(activated['review_worker']['enabled'])
        self.assertEqual(self.install('--no-start').returncode, 0)
        self.assertTrue(json.loads(self.config.read_text())['delivery_enabled'])

    def test_failed_uninstall_never_purges_running_service_data(self):
        self.assertEqual(self.install('--no-start').returncode, 0)
        result = self.install('--uninstall', '--purge', ASSISTANT_TEST_FAIL='--user disable --now personal-assistant.service')
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertTrue(self.config.exists())
        self.assertTrue((self.home / '.config/systemd/user/personal-assistant.service').exists())

    def test_removed_source_is_rejected_without_rewriting_private_config(self):
        self.assertEqual(self.install('--no-start').returncode, 0)
        config = json.loads(self.config.read_text())
        config['accounts'].append({'id':'work-feed','service':'rss','transport':'http','boundary':'work','url':'https://example.com/feed.xml'})
        self.config.write_text(json.dumps(config))
        self.assertEqual(self.install('--no-start').returncode, 0, 'connector plugins are supported sources')
        config = json.loads(self.config.read_text())
        config['accounts'].append({'id':'old-source','service':'unsupported'})
        self.config.write_text(json.dumps(config))
        before = self.config.read_text()
        result = self.install('--no-start')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Unsupported sources', result.stderr)
        self.assertEqual(self.config.read_text(), before)

    def test_uninstall_keeps_data_and_purge_removes_it(self):
        self.assertEqual(self.install('--no-start').returncode, 0)
        result = self.install('--uninstall')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.home / '.config/systemd/user/personal-assistant.service').exists())
        self.assertFalse((self.home / '.local/bin/assistant').exists())
        self.assertTrue(self.config.exists())
        calls = self.calls()
        self.assertIn('systemctl --user disable --now personal-assistant.service', calls)
        self.assertIn('openclaw plugins uninstall personal-assistant-bridge --force --keep-files', calls)
        self.assertTrue((ROOT / 'openclaw-plugin/index.ts').exists())
        self.assertEqual(self.install('--uninstall', '--purge').returncode, 0)
        self.assertFalse(self.data.exists())
        self.assertNotEqual(self.install('--purge').returncode, 0)


class WizardTest(unittest.TestCase):
    def test_node_versions_match_the_pinned_openclaw_engine_range(self):
        for text, supported in [('22.13.0',False),('24.15.9',False),('24.16.0',True),
                                ('25.8.0',False),('26.0.9',False),('26.1.0',True),('27.0.0',True)]:
            with self.subTest(version=text):
                self.assertEqual(installer.supported_node(installer.version_tuple(text)), supported)

    def test_mise_shim_uses_the_installed_executable_and_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shim = root / 'shims/openclaw'
            binary = root / 'node/26.1/bin/openclaw'
            package = binary.parent.parent / 'lib/node_modules/openclaw'
            package.mkdir(parents=True)
            binary.parent.mkdir(parents=True)
            binary.write_text('stub')
            (package / 'package.json').write_text('{"name":"openclaw"}')
            with mock.patch.object(installer.shutil, 'which', return_value='/bin/mise'), \
                 mock.patch.object(installer, 'run', return_value=subprocess.CompletedProcess([],0,str(binary)+'\n','')):
                resolved = installer.resolve_tool(str(shim))
            self.assertEqual(resolved, str(binary))
            self.assertEqual(installer.openclaw_package_dir(resolved), package)

    def test_interactive_answers_build_the_config(self):
        home = Path(tempfile.mkdtemp(prefix='machine-wizard-'))
        self.addCleanup(shutil.rmtree, home, True)
        replies = iter(['Alex Owner', '1000001', 'alex@example.com', 'box.example-tailnet.ts.net', 'Europe/Amsterdam',
                        'gmail,android,whatsapp', 'alex@example.com', 'work:Acme', 'personal', '0123456789abcdef', ''])
        with mock.patch.dict(os.environ, {'HOME': str(home)}, clear=False), \
             mock.patch.dict(os.environ, {k: '' for k in installer.ENV.values()}), \
             mock.patch.object(builtins, 'input', lambda prompt='': next(replies)), \
             mock.patch.object(installer, 'say'):
            paths = installer.Paths(home)
            values = installer.answers(True, {})
            config = installer.new_config(values, {}, paths)
        self.assertEqual(next(replies, 'done'), 'done')
        self.assertEqual([a['id'] for a in config['accounts']], ['work-gmail', 'personal-android', 'personal-whatsapp'])
        self.assertEqual(config['accounts'][0]['client'], 'personal')
        self.assertEqual(config['accounts'][1]['node_id'], '0123456789abcdef')
        self.assertEqual(config['accounts'][1]['boundary'], 'personal:phone')
        self.assertEqual(config['accounts'][2]['store'], str(home / '.local/share/wacli'))
        self.assertEqual(config['credentials']['gog-password'], str(home / '.local/share/personal-assistant/gog-password'))
        self.assertEqual(config['dashboard_host'], 'box.example-tailnet.ts.net')

    def test_example_config_covers_every_connector(self):
        example = json.loads((ROOT / 'config.example.json').read_text())
        self.assertEqual(sorted(a['id'] for a in example['accounts']), sorted(installer.CONNECTORS.values()))


if __name__ == '__main__':
    unittest.main()

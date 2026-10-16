#!/usr/bin/env python3
"""Install, upgrade or remove The Machine for the current user.

    ./install.sh                     interactive install, or upgrade of an existing install
    ./install.sh --non-interactive   read answers from ASSISTANT_* environment variables
    ./install.sh --uninstall         remove the service, plugin and CLI link; keep data
    ./install.sh --uninstall --purge also delete the data directory (config, database, secrets)

Re-running is safe: an existing config.json is kept, missing keys are added, and secrets
are only created when absent. Nothing here reads or changes OpenClaw's own configuration
beyond `openclaw plugins install/enable/uninstall`.
"""
import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PLUGIN_ID = 'personal-assistant-bridge'
UNIT_NAME = 'personal-assistant.service'
MIN_PYTHON = (3, 12)
NODE_REQUIREMENT = '>=24.16.0 <25 or >=26.1.0 (OpenClaw 2026.10.2)'


def supported_node(version):
    return bool(version and ((version >= (24, 16, 0) and version < (25, 0, 0))
                             or version >= (26, 1, 0)))

# Answers for --non-interactive (and defaults for the wizard).
ENV = {
    'owner_name': 'ASSISTANT_OWNER_NAME',
    'telegram_owner': 'ASSISTANT_TELEGRAM_OWNER',
    'tailscale_owner': 'ASSISTANT_TAILSCALE_OWNER',
    'dashboard_host': 'ASSISTANT_DASHBOARD_HOST',
    'timezone': 'ASSISTANT_TIMEZONE',
    'connectors': 'ASSISTANT_CONNECTORS',       # comma list: gmail,calendar,gateway,android,whatsapp,slack,crm
    'email': 'ASSISTANT_EMAIL',                 # Google account for gmail/calendar
    'gog_client': 'ASSISTANT_GOG_CLIENT',
    'boundary': 'ASSISTANT_BOUNDARY',           # e.g. work:Acme
    'android_node_id': 'ASSISTANT_ANDROID_NODE_ID',
    'whatsapp_store': 'ASSISTANT_WHATSAPP_STORE',
}
# Connector name -> account id in config.example.json.
CONNECTORS = {'gmail': 'work-gmail', 'calendar': 'work-calendar', 'slack': 'work-slack',
              'crm': 'work-crm', 'android': 'personal-android', 'whatsapp': 'personal-whatsapp', 'gateway': 'openclaw-gateway'}
NEEDS_MANUAL = {'slack': 'Slack via Maton: fill connection_id, team_id, user_id and put MATON_API_KEY=… in the maton-slack credential file',
                'crm': 'Salesflare CRM: fill user_id/team_id and put SALESFLARE_API_KEY=… in the salesflare credential file',
                'whatsapp': 'WhatsApp: set identity_sha256 to the sha256 of your linked JID (`wacli auth status --json`)'}


class Paths:
    def __init__(self, home=None):
        self.home = Path(home or os.environ.get('HOME') or Path.home())
        self.data = self.home / '.local/share/personal-assistant'
        self.config = Path(os.environ.get('ASSISTANT_CONFIG') or self.data / 'config.json')
        self.unit = self.home / '.config/systemd/user' / UNIT_NAME
        self.cli = self.home / '.local/bin/assistant'
        self.workspace = Path(os.environ.get('OPENCLAW_WORKSPACE') or self.home / '.openclaw/workspace')


def say(*parts):
    print(*parts, flush=True)


def version_tuple(text):
    found = re.search(r'(\d+)\.(\d+)(?:\.(\d+))?', text or '')
    return tuple(int(x or 0) for x in found.groups()) if found else None


def run(args, check=False, **kwargs):
    kwargs.setdefault('capture_output', True)
    kwargs.setdefault('text', True)
    try:
        return subprocess.run(args, check=check, timeout=kwargs.pop('timeout', 120), **kwargs)
    except (OSError, subprocess.TimeoutExpired) as error:
        return subprocess.CompletedProcess(args, 127, '', str(error))


# ---------------------------------------------------------------- prerequisites

def required_openclaw():
    package = json.loads((ROOT / 'openclaw-plugin/package.json').read_text())
    return package.get('peerDependencies', {}).get('openclaw', '')


def resolve_tool(binary):
    """Resolve symlinks and mise/asdf shims without depending on a shell startup file."""
    if not binary:
        return None
    path = Path(binary)
    if 'shims' in path.parts:
        for manager in ('mise', 'asdf'):
            command = shutil.which(manager)
            if command:
                result = run([command, 'which', path.name])
                candidate = Path(result.stdout.strip())
                if result.returncode == 0 and candidate.is_file() and candidate != path:
                    return str(candidate.absolute())
    return str(path.absolute())


def openclaw_package_dir(binary, node=None):
    """The installed openclaw npm package (for the Node helpers), found from the CLI's real path."""
    if not binary:
        return None
    path = Path(binary).resolve()
    for parent in [path.parent, *path.parents]:
        manifest = parent / 'package.json'
        if manifest.exists():
            try:
                if json.loads(manifest.read_text()).get('name') == 'openclaw':
                    return parent
            except ValueError:
                pass
    guesses = [Path(binary).parent.parent / 'lib/node_modules/openclaw']
    if node:
        guesses.append(Path(node).resolve().parent.parent / 'lib/node_modules/openclaw')
    npm = shutil.which('npm')
    if npm:
        result = run([npm, 'root', '-g'])
        if result.returncode == 0 and result.stdout.strip():
            guesses.append(Path(result.stdout.strip()) / 'openclaw')
    for guess in guesses:
        manifest = guess / 'package.json'
        try:
            if json.loads(manifest.read_text()).get('name') == 'openclaw':
                return guess.resolve()
        except (OSError, ValueError):
            pass
    return None


def check_prerequisites():
    """Report what is there; return (ok, facts). Python, Node and OpenClaw are required."""
    facts, ok = {}, True
    say('Checking prerequisites')
    py = sys.version_info[:2]
    if py >= MIN_PYTHON:
        say(f'  ok    python {py[0]}.{py[1]} ({sys.executable})')
    else:
        ok = False
        say(f'  FAIL  python {py[0]}.{py[1]}: need {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+; install a newer python3 and rerun with it')
    facts['python'] = sys.executable

    node = resolve_tool(shutil.which('node'))
    node_version = version_tuple(run([node, '--version']).stdout) if node else None
    if node and supported_node(node_version):
        say(f'  ok    node {".".join(map(str, node_version))} ({node})')
    else:
        ok = False
        say(f'  FAIL  need Node.js {NODE_REQUIREMENT}; install a supported version and rerun')
    facts['node'] = node

    openclaw = resolve_tool(shutil.which('openclaw'))
    want = required_openclaw()
    if openclaw:
        got = run([openclaw, '--version']).stdout
        have = '.'.join(map(str, version_tuple(got) or ())) or 'unknown'
        note = '' if have == want else f' (tested with {want}; the Telegram Change prompt falls back to a plain prompt on other versions)'
        say(f'  ok    openclaw {have}{note}')
        facts['openclaw_version'] = have
    else:
        ok = False
        say(f'  FAIL  openclaw CLI not found: install OpenClaw {want} (npm install -g openclaw@{want}) and set up its Telegram channel')
    facts['openclaw'] = openclaw
    facts['openclaw_package_dir'] = openclaw_package_dir(openclaw, node)
    if openclaw and not facts['openclaw_package_dir']:
        ok = False
        say('  FAIL  cannot locate the OpenClaw npm package; expose its installed CLI on PATH and rerun')

    for name, hint in (('gog', 'Gmail and Calendar sources: install gog and run `gog auth add <email>`'),
                       ('tailscale', 'dashboard access: install Tailscale and join your tailnet'),
                       ('caddy', 'only needed to serve the dashboard on a custom domain (see deploy/Caddyfile)'),
                       ('wacli', 'only needed for the WhatsApp source'),
                       ('systemctl', 'needed to run the service; without systemd start `python3 assistant.py serve` yourself')):
        found = shutil.which(name)
        facts[name] = found
        say(f'  {"ok   " if found else "skip "} {name}{"" if found else ": " + hint}')
    return ok, facts


# ---------------------------------------------------------------- config

def tailscale_identity(binary):
    if not binary:
        return {}
    result = run([binary, 'status', '--json'], timeout=10)
    try:
        status = json.loads(result.stdout)
        user = status['User'][str(status['Self']['UserID'])]['LoginName']
        return {'tailscale_owner': user, 'dashboard_host': status['Self']['DNSName'].rstrip('.')}
    except (ValueError, KeyError, TypeError):
        return {}


def ask(prompt, default=''):
    answer = input(f'{prompt}{f" [{default}]" if default else ""}: ').strip()
    return answer or default


def answers(interactive, facts):
    detected = tailscale_identity(facts.get('tailscale'))
    defaults = {'owner_name': '', 'telegram_owner': '', 'tailscale_owner': detected.get('tailscale_owner', ''),
                'dashboard_host': detected.get('dashboard_host', ''), 'timezone': local_timezone(),
                'connectors': 'gmail,calendar,gateway', 'email': '', 'gog_client': 'default', 'boundary': 'work:Main',
                'android_node_id': '', 'whatsapp_store': str(Path.home() / '.local/share/wacli')}
    values = {key: os.environ.get(var) or defaults[key] for key, var in ENV.items()}
    if interactive:
        say('\nA few questions (Enter keeps the default):')
        values['owner_name'] = ask('Your name (used to sign emails)', values['owner_name'])
        values['telegram_owner'] = ask('Your Telegram user/chat ID (the private chat with your OpenClaw bot)', values['telegram_owner'])
        values['tailscale_owner'] = ask('Your Tailscale login (only this identity may open the dashboard)', values['tailscale_owner'])
        values['dashboard_host'] = ask('Dashboard host name (your machine on the tailnet, e.g. machine.tailnet.ts.net)', values['dashboard_host'])
        values['timezone'] = ask('Time zone', values['timezone'])
        values['connectors'] = ask('Sources to enable (' + ','.join(CONNECTORS) + ')', values['connectors'])
        chosen = set(split(values['connectors']))
        if chosen & {'gmail', 'calendar', 'slack', 'crm'}:
            values['email'] = ask('Your work email address', values['email'])
            values['boundary'] = ask('Boundary label for these work sources (work:<Company>)', values['boundary'])
        if chosen & {'gmail', 'calendar'}:
            values['gog_client'] = ask('gog OAuth client name for that account', values['gog_client'])
        if 'android' in chosen:
            values['android_node_id'] = ask('OpenClaw node ID of your Android phone (`openclaw nodes list`)', values['android_node_id'])
        if 'whatsapp' in chosen:
            values['whatsapp_store'] = ask('wacli store directory', values['whatsapp_store'])
    missing = [key for key in ('telegram_owner', 'tailscale_owner', 'dashboard_host') if not values[key]]
    if missing:
        raise SystemExit('Missing answers: ' + ', '.join(missing) + ' (set ' + ', '.join(ENV[k] for k in missing) + ')')
    if not re.fullmatch(r'-?\d{3,20}', values['telegram_owner']):
        raise SystemExit('The Telegram owner must be a numeric chat ID, got ' + repr(values['telegram_owner']))
    unknown = set(split(values['connectors'])) - set(CONNECTORS)
    if unknown:
        raise SystemExit('Unknown sources: ' + ', '.join(sorted(unknown)))
    return values


def split(text):
    return [part.strip() for part in (text or '').split(',') if part.strip()]


def local_timezone():
    link = Path('/etc/localtime')
    try:
        target = str(link.resolve())
        return target.split('zoneinfo/', 1)[1] if 'zoneinfo/' in target else 'UTC'
    except OSError:
        return 'UTC'


def expand(value, home):
    if isinstance(value, str) and value.startswith('~/'):
        return str(home / value[2:])
    if isinstance(value, list):
        return [expand(v, home) for v in value]
    if isinstance(value, dict):
        return {k: expand(v, home) for k, v in value.items()}
    return value


def new_config(values, facts, paths):
    """config.json from config.example.json plus the wizard's answers."""
    example = json.loads((ROOT / 'config.example.json').read_text())
    templates = {a['id']: a for a in example.pop('accounts')}
    config = expand(example, paths.home)
    config.update(owner_name=values['owner_name'], timezone=values['timezone'], telegram_owner=values['telegram_owner'],
                  tailscale_owner=values['tailscale_owner'], dashboard_host=values['dashboard_host'], heartbeat_job_id='',
                  booking_links={})
    config['delivery_enabled'] = False
    config['review_worker']['enabled'] = False
    data = str(paths.data)
    config['database'] = data + '/events.sqlite'
    config['prompt_receipts'] = data + '/prompt-receipts'
    config['attachment_dir'] = data + '/attachments'
    config['credentials'] = {'api-token': data + '/api-token', 'owner-token': data + '/owner-token'}
    config['attachment_sources'] = [str(paths.workspace / 'data/assistant-attachments'), str(paths.workspace / 'memory/assistant')]
    accounts = []
    for name in split(values['connectors']):
        account = expand(dict(templates[CONNECTORS[name]]), paths.home)
        if 'email' in account:
            account['email'] = values['email'] or account['email']
            if 'owner_emails' in account:
                account['owner_emails'] = [account['email']]
        if account.get('transport') == 'gog':
            account['client'] = values['gog_client']
            config['credentials']['gog-password'] = data + '/gog-password'
        if account.get('boundary', '').startswith('work:'):
            account['boundary'] = values['boundary']
        account.pop('exclude', None)  # example noise rules are not yours
        if name == 'android':
            account['node_id'] = values['android_node_id'] or account['node_id']
        if name == 'whatsapp':
            account['store'] = values['whatsapp_store']
        if account.get('credential'):
            config['credentials'][account['credential']] = f"{data}/{account['credential']}.env"
        accounts.append(account)
    config['accounts'] = accounts
    return config


def refresh_install_paths(config, facts, paths):
    """Keys that describe this checkout and toolchain; safe to update on every run."""
    config['app_dir'] = str(ROOT)
    for key in ('openclaw', 'node'):
        if facts.get(key):
            config[key] = facts[key]
    if facts.get('openclaw_package_dir'):
        config['openclaw_package_dir'] = str(facts['openclaw_package_dir'])
    if facts.get('gog') and config.get('gog') in (None, 'gog'):
        config['gog'] = facts['gog']
    if facts.get('wacli') and config.get('wacli') in (None, 'wacli'):
        config['wacli'] = facts['wacli']
    return config


def write_private(path, text):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as handle:
        handle.write(text)
    os.chmod(temp, 0o600)
    os.replace(temp, path)


def setup_config(interactive, facts, paths):
    if paths.config.exists():
        config = json.loads(paths.config.read_text())
        example = expand(json.loads((ROOT / 'config.example.json').read_text()), paths.home)
        example.pop('accounts')
        added = [key for key in example if key not in config and key not in ('booking_links', 'heartbeat_job_id')]
        for key in added:
            config[key] = example[key]
        say(f'Keeping existing {paths.config}' + (f' (added defaults: {", ".join(added)})' if added else ''))
    else:
        config = new_config(answers(interactive, facts), facts, paths)
        say(f'Writing {paths.config}')
    refresh_install_paths(config, facts, paths)
    # Built-in sources plus connector plugins (connectors/<service>.py, see docs/adding-a-source.md).
    supported = {'gmail', 'calendar', 'slack', 'salesflare', 'android', 'whatsapp', 'gateway'} | {
        p.stem for p in (ROOT / 'connectors').glob('*.py') if re.fullmatch(r'[a-z][a-z0-9_]{0,30}', p.stem)}
    unsupported = [a['id'] for a in config.get('accounts', []) if a.get('service') not in supported]
    if unsupported:
        raise SystemExit('Unsupported sources in existing config: ' + ', '.join(unsupported)
                         + '. Remove those accounts and unused credentials from config.json; database history is kept.')
    text = json.dumps(config, indent=2) + '\n'
    if not paths.config.exists() or paths.config.read_text() != text:
        write_private(paths.config, text)
    os.chmod(paths.config, 0o600)
    return config


# ---------------------------------------------------------------- secrets

def setup_secrets(config, facts, paths):
    paths.data.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(paths.data, 0o700)
    created = []
    for name in ('api-token', 'owner-token'):
        path = Path(config['credentials'].get(name) or paths.data / name)
        if not path.exists():
            write_private(path, secrets.token_hex(32))
            created.append(name)
        os.chmod(path, 0o600)
    vapid = Path(config['database']).parent / 'vapid.json'
    if not vapid.exists() and facts.get('node'):
        env = dict(os.environ, PUSH_VAPID_FILE=str(vapid))
        result = run([facts['node'], str(ROOT / 'push-send.mjs')], input='{"action":"key"}', env=env, timeout=30)
        if result.returncode == 0 and vapid.exists():
            created.append('vapid.json')
        else:
            say('  note: could not create the Web Push key now; the service creates it on first use')
    if vapid.exists():
        os.chmod(vapid, 0o600)
    say('Secrets: ' + (('created ' + ', '.join(created)) if created else 'already present, unchanged') + f' in {paths.data} (mode 600)')


# ---------------------------------------------------------------- systemd, CLI, plugin

def unit_text(config, facts, config_path=None):
    bins = [str(Path(p).parent) for p in (facts.get('node'), facts.get('openclaw'), facts.get('gog'), facts.get('wacli')) if p]
    path = ':'.join(dict.fromkeys(bins + ['%h/.local/bin', '/usr/local/bin', '/usr/bin', '/bin']))
    return f"""# Written by install.sh from {ROOT}; rerun ./install.sh to update it.
[Unit]
Description=The Machine: signal collector and suggestions inbox
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=300
StartLimitBurst=10

[Service]
Type=simple
WorkingDirectory={ROOT}
ExecStart={facts.get('python') or sys.executable} {ROOT / 'assistant.py'} serve
Restart=always
RestartSec=10
TimeoutStopSec=20
KillMode=control-group
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
Environment=PATH={path}
Environment=PYTHONUNBUFFERED=1
Environment="ASSISTANT_CONFIG={str(config_path or Paths().config)}"
Environment="OPENCLAW_WORKSPACE={Paths().workspace}"

[Install]
WantedBy=default.target
"""


def install_unit(config, facts, paths, start):
    text = unit_text(config, facts, paths.config)
    changed = not paths.unit.exists() or paths.unit.read_text() != text
    if changed:
        paths.unit.parent.mkdir(parents=True, exist_ok=True)
        paths.unit.write_text(text)
    say(f'systemd unit: {"written" if changed else "unchanged"} {paths.unit}')
    if not facts.get('systemctl'):
        say('  systemctl not found: start the service with `python3 assistant.py serve`')
        return None if not start else False
    reload = run(['systemctl', '--user', 'daemon-reload'])
    if reload.returncode != 0:
        say('  daemon-reload failed: ' + (reload.stderr or reload.stdout).strip()[:300])
        return False
    if not start:
        say('  not started (--no-start)')
        return None
    result = run(['systemctl', '--user', 'enable', UNIT_NAME])
    result = run(['systemctl', '--user', 'restart', UNIT_NAME]) if result.returncode == 0 else result
    if result.returncode != 0:
        say('  could not start it: ' + (result.stderr or result.stdout).strip()[:300])
        return False
    say('  enabled and (re)started')
    return True


def install_cli(paths):
    target = ROOT / 'cli.py'
    paths.cli.parent.mkdir(parents=True, exist_ok=True)
    if paths.cli.is_symlink() and paths.cli.resolve() == target:
        return
    if paths.cli.exists() and not paths.cli.is_symlink():
        raise SystemExit(f'CLI install failed: {paths.cli} is an existing file; move it aside and rerun')
    if paths.cli.is_symlink():
        paths.cli.unlink()
    paths.cli.symlink_to(target)
    os.chmod(target, 0o755)
    say(f'CLI: {paths.cli} -> cli.py')


def install_plugin(facts, reload=True):
    openclaw = facts.get('openclaw')
    if not openclaw:
        return False
    plugin = str(ROOT / 'openclaw-plugin')
    say('Installing the local bridge and accepting its declared OpenClaw capabilities (see README Security model).')
    result = run([openclaw, 'plugins', 'install', '--link', '--force', '--accept-capabilities', plugin], timeout=300, capture_output=False)
    if result.returncode != 0:
        say(f'  plugin install failed; run it yourself: openclaw plugins install --link --force --accept-capabilities {plugin}')
        return False
    result = run([openclaw, 'plugins', 'enable', PLUGIN_ID], timeout=120)
    if result.returncode != 0:
        say('  plugin enable failed: ' + (result.stderr or result.stdout).strip()[:300])
        return False
    if reload:
        result = run([openclaw, 'plugins', 'reload', PLUGIN_ID, '--wait'], timeout=300)
        if result.returncode != 0:
            say('  plugin reload failed: ' + (result.stderr or result.stdout).strip()[:300])
            return False
    say(f'OpenClaw plugin {PLUGIN_ID}: linked and enabled'
        + ('' if reload else ' (not reloaded: --no-start)'))
    return True


def health(config, timeout):
    deadline = time.time() + timeout
    url = f"http://127.0.0.1:{config.get('port', 8787)}/health"
    while True:
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                if json.loads(response.read()).get('ok'):
                    return True
        except (OSError, ValueError):
            pass
        if time.time() >= deadline:
            return False
        time.sleep(1)


def next_steps(config, healthy, started):
    host, path = config.get('dashboard_host', ''), config.get('dashboard_path', '/assistant/')
    gmail = [a for a in config.get('accounts', []) if a.get('transport') == 'gog']
    lines = ['', 'Next steps:']
    if started:
        lines.append(f'  - Health: {"OK" if healthy else "not answering yet; check journalctl --user -u " + UNIT_NAME + " -n 50"}')
    if gmail:
        lines += [f'  - Gmail/Calendar: gog auth add {gmail[0]["email"]} --client {gmail[0]["client"]} (read-only scopes are enough to start)',
                  f'    and put the gog file-keyring password in {config["credentials"].get("gog-password")} (chmod 600)']
    for account in config.get('accounts', []):
        name = next((k for k, v in CONNECTORS.items() if v == account['id']), None)
        if name in NEEDS_MANUAL:
            lines.append('  - ' + NEEDS_MANUAL[name])
    lines += [f'  - Dashboard on your tailnet: tailscale serve --bg --https=443 --set-path {path.rstrip("/")} http://127.0.0.1:{config.get("port", 8787)}',
              f'    then open https://{host}{path} (custom domain instead: see deploy/Caddyfile)',
              '  - Tell the agent about you: create USER.md in your OpenClaw workspace (name, companies, tone, preferences)',
              '  - Keep `Do it` in mind: it lets the agent act with your accounts. Read README "Security model".',
              '  - After editing config.json: systemctl --user restart ' + UNIT_NAME]
    if not config.get('delivery_enabled') or not config.get('review_worker', {}).get('enabled'):
        lines.append('  - Once sources are connected and Security model is understood: ./install.sh --activate')
    say('\n'.join(lines))


# ---------------------------------------------------------------- uninstall

def uninstall(paths, purge, interactive):
    if shutil.which('systemctl'):
        stopped = run(['systemctl', '--user', 'disable', '--now', UNIT_NAME])
        if stopped.returncode != 0 and paths.unit.exists():
            say('Could not stop the service; kept files and data. Check systemctl --user status ' + UNIT_NAME)
            return 1
    elif paths.unit.exists():
        say('Cannot stop the installed service without systemctl; kept files and data.')
        return 1
    if paths.unit.exists():
        paths.unit.unlink()
        say(f'Removed {paths.unit}')
    if shutil.which('systemctl'):
        if run(['systemctl', '--user', 'daemon-reload']).returncode != 0:
            say('systemd daemon-reload failed; rerun uninstall after fixing the user service manager.')
            return 1
    openclaw = shutil.which('openclaw')
    if openclaw:
        result = run([openclaw, 'plugins', 'uninstall', PLUGIN_ID, '--force', '--keep-files'], timeout=120)
        say(f'OpenClaw plugin {PLUGIN_ID}: ' + ('uninstalled' if result.returncode == 0 else 'not removed (' + (result.stderr or '').strip()[:200] + ')'))
        if result.returncode != 0:
            say('Kept data because plugin removal failed.')
            return 1
    if paths.cli.is_symlink() and paths.cli.resolve() == ROOT / 'cli.py':
        paths.cli.unlink()
        say(f'Removed {paths.cli}')
    if purge:
        if interactive and ask(f'Delete {paths.data} with your config, database and secrets? Type "delete"') != 'delete':
            say('Kept the data directory.')
            return 0
        if paths.data.exists():
            shutil.rmtree(paths.data)
            say(f'Deleted {paths.data}')
    else:
        say(f'Kept your data in {paths.data} (use --purge to delete it)')
    return 0


# ---------------------------------------------------------------- main

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--non-interactive', action='store_true', help='take answers from ASSISTANT_* environment variables')
    parser.add_argument('--uninstall', action='store_true', help='remove the service, plugin and CLI link (keeps data)')
    parser.add_argument('--purge', action='store_true', help='with --uninstall: also delete config, database and secrets')
    parser.add_argument('--no-start', action='store_true', help='install files but do not enable or start the service')
    parser.add_argument('--skip-plugin', action='store_true', help='do not install the OpenClaw plugin')
    parser.add_argument('--activate', action='store_true', help='enable reviews and Telegram delivery on an existing install')
    parser.add_argument('--health-timeout', type=float, default=30)
    args = parser.parse_args(argv)
    interactive = not args.non_interactive and sys.stdin.isatty()
    paths = Paths()
    if args.purge and not args.uninstall:
        parser.error('--purge only works with --uninstall')
    if args.activate and (args.no_start or args.uninstall or not paths.config.exists()):
        parser.error('--activate needs an existing installation and a running service (no --no-start/--uninstall)')
    if args.uninstall:
        return uninstall(paths, args.purge, interactive)
    ok, facts = check_prerequisites()
    if not ok:
        say('\nFix the FAIL items above and rerun ./install.sh')
        return 1
    config = setup_config(interactive, facts, paths)
    setup_secrets(config, facts, paths)
    install_cli(paths)
    plugin_ok = args.skip_plugin or install_plugin(facts, reload=not args.no_start)
    if not plugin_ok:
        return 1
    if args.activate:
        config['delivery_enabled'] = True
        config.setdefault('review_worker', {})['enabled'] = True
        write_private(paths.config, json.dumps(config, indent=2) + '\n')
    started = install_unit(config, facts, paths, start=not args.no_start)
    healthy = started and health(config, args.health_timeout)
    next_steps(config, healthy, started)
    return 0 if (args.no_start and started is None) or (started and healthy) else 1


if __name__ == '__main__':
    sys.exit(main())

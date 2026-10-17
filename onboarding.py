"""Guided setup shared by install.sh and the `assistant` CLI.

    assistant connect gmail     ask, write config and credential files (mode 600), find IDs, test, first sync
    assistant doctor            check the whole setup; every problem comes with its fix

Reads OpenClaw's state read-only (approved Telegram senders, paired nodes, pairing requests).
The only OpenClaw change it can make is approving your own Telegram pairing request, and only
after you say yes. Secrets are read without echo, written only to files with mode 600 in the
data directory, and never printed.
"""
import datetime as dt
import getpass
import hashlib
import importlib.util
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import time
import urllib.parse
import urllib.request
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parent
UNIT_NAME = 'personal-assistant.service'
# Connector name -> account id in config.example.json.
CONNECTORS = {'gmail': 'work-gmail', 'calendar': 'work-calendar', 'slack': 'work-slack',
              'crm': 'work-crm', 'android': 'personal-android', 'whatsapp': 'personal-whatsapp', 'gateway': 'openclaw-gateway'}
SERVICES = {'gmail': 'gmail', 'calendar': 'calendar', 'slack': 'slack', 'crm': 'salesflare', 'salesflare': 'salesflare',
            'android': 'android', 'whatsapp': 'whatsapp', 'gateway': 'gateway'}
LABELS = {'gmail': 'Gmail', 'calendar': 'Google Calendar', 'slack': 'Slack', 'salesflare': 'Salesflare CRM',
          'android': 'Android notifications', 'whatsapp': 'WhatsApp', 'gateway': 'OpenClaw Gateway log'}
# Values in config.example.json that mean "not filled in yet".
PLACEHOLDERS = {'email': 'alex@example.com', 'connection_id': '00000000-0000-0000-0000-000000000000',
                'team_id': 'T00000000', 'user_id': 'U00000000', 'node_id': 'replace-with-your-openclaw-node-id',
                'identity_sha256': 'sha256-of-your-linked-whatsapp-jid'}
# Redacted sync error labels -> what to do about them (first matching prefix wins).
HINTS = (
    ('account_identity_mismatch', 'the sign-in belongs to a different account than config.json says'),
    ('connection_identity_mismatch', 'Maton answered for a different connection; check connection_id'),
    ('gmail:HTTP_403', 'enable the Gmail API in your Google Cloud project, or authorize again'),
    ('calendar:HTTP_403', 'enable the Google Calendar API in your Google Cloud project, or authorize again'),
    ('gmail', 'gog could not read Gmail: authorize again, and check the keyring password file'),
    ('calendar', 'gog could not read the calendar: authorize again, and check the keyring password file'),
    ('slack_invalid_auth', 'Maton rejected the API key'),
    ('slack_token_revoked', 'the Slack sign-in in Maton was revoked; reconnect Slack in Maton'),
    ('slack_credential_unavailable', 'the Maton credential file needs exactly one MATON_API_KEY=… line'),
    ('slack', 'Slack through Maton failed; check the key and connection in Maton'),
    ('missing_salesflare_credential', 'the Salesflare credential file needs a SALESFLARE_API_KEY=… line'),
    ('salesflare:HTTP_401', 'Salesflare rejected the API key'),
    ('whatsapp_source_unavailable', 'wacli could not read its store; is it linked (wacli auth status)?'),
    ('whatsapp', 'wacli answered unexpectedly; try wacli doctor'),
    ('android_offline', 'the phone is not connected to OpenClaw right now; open the OpenClaw app on it'),
    ('android_notifications_unavailable', 'allow notification access for the OpenClaw app on the phone'),
    ('android_identity_mismatch', 'node_id is not a paired Android node; pick it again'),
    ('android', 'the OpenClaw Gateway could not reach the phone'),
    ('x_browser_busy', 'the OpenClaw browser was busy; it retries by itself'),
    ('linkedin_browser_busy', 'the OpenClaw browser was busy; it retries by itself'),
    ('network:', 'a network error; it retries by itself'),
    ('timeout', 'the source was slow to answer; it retries by itself'),
)


class SetupError(Exception):
    def __init__(self, message, fix=''):
        self.message, self.fix = message, fix
        super().__init__(message + (f' (fix: {fix})' if fix else ''))


class UI:
    """Terminal questions. Without a terminal every question takes its default and nothing is approved."""
    def __init__(self, interactive=True):
        self.interactive = interactive

    def say(self, text=''):
        print(text, flush=True)

    def ask(self, prompt, default=''):
        if not self.interactive:
            return default
        answer = input(f'{prompt}{f" [{default}]" if default else ""}: ').strip()
        return answer or default

    def secret(self, prompt):
        return getpass.getpass(prompt + ': ').strip() if self.interactive else ''

    def yes(self, prompt, default=True):
        if not self.interactive:
            return False
        answer = input(f'{prompt} [{"Y/n" if default else "y/N"}]: ').strip().lower()
        return default if not answer else answer.startswith('y')

    def choose(self, prompt, options):
        """options: [(value, label)]; one option is taken without asking."""
        if len(options) == 1:
            return options[0][0]
        for number, (_, label) in enumerate(options, 1):
            self.say(f'  {number}. {label}')
        while True:
            answer = self.ask(prompt, '1')
            if answer.isdigit() and 1 <= int(answer) <= len(options):
                return options[int(answer) - 1][0]
            if not self.interactive:
                return options[0][0]


def run(args, **kwargs):
    kwargs.setdefault('capture_output', True)
    kwargs.setdefault('text', True)
    try:
        return subprocess.run(args, timeout=kwargs.pop('timeout', 60), **kwargs)
    except (OSError, subprocess.TimeoutExpired) as error:
        return subprocess.CompletedProcess(args, 127, '', str(error))


def write_private(path, text):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as handle:
        handle.write(text)
    os.chmod(temp, 0o600)
    os.replace(temp, path)


def expand(value, home):
    if isinstance(value, str) and value.startswith('~/'):
        return str(Path(home) / value[2:])
    if isinstance(value, list):
        return [expand(v, home) for v in value]
    if isinstance(value, dict):
        return {k: expand(v, home) for k, v in value.items()}
    return value


_assistant = None


def assistant():
    """assistant.py, loaded on first use (sources, credentials, connector plugins)."""
    global _assistant
    if _assistant is None:
        spec = importlib.util.spec_from_file_location('assistant', ROOT / 'assistant.py')
        _assistant = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_assistant)
    return _assistant


def home():
    return Path(os.environ.get('HOME') or Path.home())


def tool(config, name):
    path = (config or {}).get(name) or name
    return shutil.which(path) or (path if Path(path).is_file() else None)


# ---------------------------------------------------------------- Telegram

def telegram_id(value):
    text = re.sub(r'^(telegram|tg):', '', str(value).strip(), flags=re.I)
    return text if re.fullmatch(r'-?\d{3,20}', text) else None


def telegram_owners(config=None, root=None):
    """Telegram IDs OpenClaw already trusts, owner first: commands.ownerAllowFrom, then the
    channel's allowFrom, then approved pairing senders. Read-only; never prints the bot token."""
    root = Path(root or home())
    found = []
    try:
        settings = json.loads((root / '.openclaw/openclaw.json').read_text())
    except (OSError, ValueError):
        settings = {}
    if isinstance(settings, dict):
        owners = (settings.get('commands') or {}).get('ownerAllowFrom') or []
        found += [o for o in (owners if isinstance(owners, list) else [owners]) if str(o).lower().startswith(('telegram:', 'tg:'))]
        channel = (settings.get('channels') or {}).get('telegram') or {}
        scopes = [channel] + [a for a in (channel.get('accounts') or {}).values() if isinstance(a, dict)]
        for scope in scopes:
            allow = scope.get('allowFrom') or []
            found += allow if isinstance(allow, list) else [allow]
    state = Path((config or {}).get('openclaw_state') or root / '.openclaw/state/openclaw.sqlite').expanduser()
    if state.exists():
        try:
            with closing(sqlite3.connect(f'file:{state}?mode=ro', uri=True, timeout=2)) as db:
                found += [r[0] for r in db.execute("SELECT entry FROM channel_pairing_allow_entries "
                                                   "WHERE channel_key='telegram' ORDER BY sort_order")]
        except sqlite3.Error:
            pass
    return list(dict.fromkeys(i for i in map(telegram_id, found) if i))


def pairing_requests(openclaw):
    result = run([openclaw, 'pairing', 'list', 'telegram', '--json'], timeout=30)
    try:
        return json.loads(result.stdout).get('requests') or [] if result.returncode == 0 else None
    except (ValueError, AttributeError):
        return None


def telegram_from_pairing(openclaw, ui, wait=120):
    """Ask the owner to message the bot, then read the sender ID from OpenClaw's pairing request
    and offer to approve it. Returns the numeric ID or None."""
    before = pairing_requests(openclaw)
    if before is None:
        return None
    known = {(r.get('id'), r.get('code')) for r in before}
    ui.say('Send any message to your OpenClaw bot in Telegram now; I will pick up your ID from it.\n'
           f'Waiting up to {wait // 60} minutes (Ctrl+C to type the ID yourself)...')
    deadline, new = time.monotonic() + wait, []
    try:
        while not new and time.monotonic() < deadline:
            time.sleep(3)
            new = [r for r in pairing_requests(openclaw) or [] if (r.get('id'), r.get('code')) not in known and telegram_id(r.get('id', ''))]
    except KeyboardInterrupt:
        ui.say('')
        return None
    if not new:
        ui.say('No new message reached the bot. If you were approved before, nothing new shows up here.')
        return None
    request = new[-1]
    sender = telegram_id(request['id'])
    meta = request.get('meta') or {}
    who = ', '.join(str(v) for v in meta.values() if isinstance(v, (str, int)) and str(v).strip())[:80] if isinstance(meta, dict) else ''
    ui.say(f'Got a message from Telegram user {sender}' + (f' ({who})' if who else '') + '.')
    if request.get('code') and ui.yes('Approve this account in OpenClaw so your bot answers it?', True):
        result = run([openclaw, 'pairing', 'approve', 'telegram', str(request['code']), '--notify'], timeout=60)
        ui.say('  approved' if result.returncode == 0 else
               f'  could not approve it; run: openclaw pairing approve telegram {request["code"]}')
    return sender


# ---------------------------------------------------------------- detection for the wizard

def owner_name():
    try:
        import pwd
        return pwd.getpwuid(os.getuid()).pw_gecos.split(',')[0].strip()
    except (ImportError, KeyError):
        return ''


def gog_env(config):
    env = os.environ.copy()
    env['GOG_KEYRING_BACKEND'] = 'file'
    password = Path(config.get('credentials', {}).get('gog-password') or '/nonexistent')
    if password.exists():
        env['GOG_KEYRING_PASSWORD'] = password.read_text().strip()
    env.pop('DBUS_SESSION_BUS_ADDRESS', None)
    return env


def gog_accounts(gog):
    """Google accounts gog already knows (email, client); no tokens are read."""
    if not gog:
        return []
    result = run([gog, 'auth', 'list', '--json', '--no-input'], timeout=20,
                 env={**os.environ, 'GOG_KEYRING_BACKEND': 'file', 'GOG_KEYRING_PASSWORD': ''})
    try:
        return [(a['email'], a.get('client') or 'default') for a in json.loads(result.stdout).get('accounts', []) if a.get('email')]
    except (ValueError, AttributeError, TypeError):
        return []


def gog_clients(gog):
    result = run([gog, 'auth', 'credentials', 'list', '--json'], timeout=20)
    try:
        clients = json.loads(result.stdout).get('clients') or []
        return sorted((c['client'] for c in clients), key=lambda name: not next(c.get('default') for c in clients if c['client'] == name))
    except (ValueError, AttributeError, TypeError, KeyError, StopIteration):
        return []


def android_nodes(openclaw):
    if not openclaw:
        return []
    result = run([openclaw, 'nodes', 'list', '--json'], timeout=30)
    try:
        return [n for n in json.loads(result.stdout).get('paired', []) if n.get('platform') == 'android' and n.get('nodeId')]
    except (ValueError, AttributeError):
        return []


def whatsapp_linked(wacli, store):
    if not wacli or not Path(store).is_dir():
        return None
    result = run([wacli, '--store', str(store), '--read-only', '--json', '--timeout', '15s', 'auth', 'status'],
                 timeout=20, env={**os.environ, 'WACLI_READONLY': '1'})
    try:
        data = json.loads(result.stdout).get('data') or {}
        return data.get('linked_jid') if data.get('authenticated') is True else None
    except (ValueError, AttributeError):
        return None


def suggested_sources(facts):
    """Default source list for the wizard from what is installed and linked on this machine."""
    chosen = ['gmail', 'calendar'] if facts.get('gog') else []
    if whatsapp_linked(facts.get('wacli'), home() / '.local/share/wacli'):
        chosen.append('whatsapp')
    if android_nodes(facts.get('openclaw')):
        chosen.append('android')
    return ','.join(chosen + ['gateway'])


# ---------------------------------------------------------------- config helpers

def load(path):
    try:
        return json.loads(Path(path).read_text())
    except OSError as error:
        raise SetupError(f'Cannot read {path}: {error.strerror}', 'run ./install.sh first') from None
    except ValueError as error:
        raise SetupError(f'{path} is not valid JSON: {error}', 'fix the file, or restore config.json.bak') from None


def save(path, config):
    path = Path(path)
    if path.exists():
        write_private(path.with_name(path.name + '.bak'), path.read_text())
    write_private(path, json.dumps(config, indent=2) + '\n')


def data_dir(config):
    return Path(config['database']).expanduser().parent


def plugin(service):
    return assistant().load_connector(service) if service not in LABELS else None


def label(service):
    if service in LABELS:
        return LABELS[service]
    module = plugin(service)
    kinds = list((getattr(module, 'KINDS', None) or {}).values())
    return (getattr(module, 'SETUP', None) or {}).get('label') or (kinds[0] if kinds else service.title())


def connect_name(account, config=None):
    name = 'crm' if account.get('service', 'gmail') == 'salesflare' else account.get('service', 'gmail')
    same = [a for a in (config or {}).get('accounts', []) if a.get('service', 'gmail') == account.get('service', 'gmail')]
    return name + (f' --account {account["id"]}' if len(same) > 1 else '')


def missing(config, account):
    """Why this account cannot sync yet ('' when it is filled in). Nothing is contacted."""
    if account.get('service', 'gmail') == 'gateway':
        return ''
    for key, value in PLACEHOLDERS.items():
        if account.get(key) == value:
            return f'{key} is still the example value'
    names = [account['credential']] if account.get('credential') else []
    if account.get('transport') == 'gog':
        names.append('gog-password')
    for name in names:
        path = config.get('credentials', {}).get(name)
        if not path or not Path(path).expanduser().exists():
            return f'no {name} credential yet'
    module = plugin(account.get('service', 'gmail')) if account.get('service', 'gmail') not in LABELS else None
    for key, *_ in (getattr(module, 'SETUP', None) or {}).get('ask', []):
        if not account.get(key):
            return f'{key} is not set'
    return ''


def hint(error):
    return next((text for prefix, text in HINTS if str(error or '').startswith(prefix)), '')


def new_account(config, service, account_id=None):
    if service in LABELS:
        name = next(k for k, v in SERVICES.items() if v == service)
        example = json.loads((ROOT / 'config.example.json').read_text())
        account = next(a for a in example['accounts'] if a['id'] == CONNECTORS[name])
    else:
        account = dict((getattr(plugin(service), 'SETUP', None) or {}).get('account') or {'id': service, 'service': service, 'boundary': 'work'})
    account = expand(json.loads(json.dumps(account)), home())
    account.pop('exclude', None)  # example noise rules are not yours
    taken = {a['id'] for a in config.get('accounts', [])}
    account['id'] = account_id or account['id']
    base, number = account['id'], 2
    while account['id'] in taken:
        account['id'], number = f'{base}-{number}', number + 1
    work = next((a['boundary'] for a in config.get('accounts', []) if str(a.get('boundary', '')).startswith('work:')), None)
    if work and str(account.get('boundary', '')).startswith('work:'):
        account['boundary'] = work
    email = next((a['email'] for a in config.get('accounts', []) if a.get('email') and a['email'] != PLACEHOLDERS['email']), None)
    if email and account.get('email') == PLACEHOLDERS['email']:
        account['email'] = email
        if 'owner_emails' in account:
            account['owner_emails'] = [email]
    if account.get('transport') == 'gog':
        account['client'] = next((a['client'] for a in config.get('accounts', []) if a.get('transport') == 'gog'), account.get('client', 'default'))
        config.setdefault('credentials', {}).setdefault('gog-password', str(data_dir(config) / 'gog-password'))
    if account.get('credential'):
        config.setdefault('credentials', {}).setdefault(account['credential'], str(data_dir(config) / f'{account["credential"]}.env'))
    return account


def store_secret(config, name, ui, prompt, variable=None):
    """Ask for a key without echo and write it to its credential file (mode 600). Enter keeps a saved one."""
    path = Path(config.setdefault('credentials', {}).setdefault(name, str(data_dir(config) / f'{name}.env'))).expanduser()
    if path.exists():
        value = ui.secret(f'{prompt} (Enter keeps the saved one)')
        if not value:
            return
    else:
        value = ui.secret(prompt)
        if not value:
            raise SetupError(f'No {prompt.split(" (")[0]} given', 'rerun this when you have it')
    value = value.split('=', 1)[1].strip() if variable and value.startswith(variable + '=') else value
    write_private(path, f'{variable}={value}\n' if variable else value + '\n')


def check(config, account):
    """Read the source once as the service would. Returns None when it works, else a redacted error label."""
    a = assistant()
    service = account.get('service', 'gmail')
    try:
        if service == 'gmail':
            a.Gmail(config, account).profile()
        elif service == 'calendar':
            a.Calendar(config, account).profile()
        elif service == 'slack':
            a.Slack(config, account).profile()
        elif service == 'salesflare':
            a.Salesflare(config, account).profile()
        elif service == 'whatsapp':
            a.WhatsApp(config, account).profile()
        elif service == 'android':
            a.Android(config, account).snapshot()
        elif service == 'gateway':
            return None if shutil.which('journalctl') else 'journalctl_missing'
        return None
    except a.SourceError as error:
        return str(error)
    except (OSError, KeyError, TypeError, ValueError) as error:
        return 'setup_incomplete:' + type(error).__name__


# ---------------------------------------------------------------- per-source steps

def headless():
    return not (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')) or bool(os.environ.get('SSH_CONNECTION'))


def step_google(config, account, ui):
    gog = tool(config, 'gog')
    if not gog:
        raise SetupError('gog is not installed', 'install gog (gogcli), then run: assistant connect ' + account.get('service', 'gmail'))
    config['gog'] = config.get('gog') if config.get('gog') not in (None, 'gog') else gog
    known = gog_accounts(gog)
    default = account['email'] if account.get('email') != PLACEHOLDERS['email'] else (known[0][0] if known else '')
    email = ui.ask('Google account to read', default)
    if not email or '@' not in email:
        raise SetupError('No Google account given', 'rerun with the address you use for Gmail and Calendar')
    account['email'] = email
    if 'owner_emails' in account:
        account['owner_emails'] = sorted({email, *account['owner_emails']} - {PLACEHOLDERS['email']})
    clients = gog_clients(gog)
    client = next((c for e, c in known if e.lower() == email.lower() and c in clients), None)
    if not clients:
        ui.say('gog needs a Google OAuth client first. In the Google Cloud console: create a project, enable the Gmail\n'
               'and Google Calendar APIs, create an OAuth client of type "Desktop app" and download its JSON.\n'
               '(`gog auth setup` walks you through the same steps.)')
        path = ui.ask('Path to the downloaded client JSON')
        if not path or not Path(path).expanduser().is_file():
            raise SetupError('No OAuth client JSON', 'download it, then run: assistant connect ' + account.get('service', 'gmail'))
        client = account.get('client') or 'default'
        result = run([gog, 'auth', 'credentials', 'set', str(Path(path).expanduser()), '--client', client], timeout=30)
        if result.returncode:
            raise SetupError('gog could not store the OAuth client', 'gog auth credentials set <file> --client ' + client)
    elif not client:
        client = account.get('client') if account.get('client') in clients else clients[0]
        if len(clients) > 1:
            client = ui.choose('gog OAuth client', [(c, c) for c in clients])
    account['client'] = client
    password = Path(config.setdefault('credentials', {}).setdefault('gog-password', str(data_dir(config) / 'gog-password'))).expanduser()
    if not password.exists():
        typed = ui.secret('Password for the gog file keyring that keeps the Google token (Enter generates one)')
        write_private(password, (typed or secrets.token_urlsafe(24)) + '\n')
        ui.say('  keyring password saved to ' + str(password) + (' (generated)' if not typed else ''))
    if check(config, account) is None:
        ui.say(f'  {email} is already authorized')
        return
    ui.say('Signing in to Google with read-only access to Gmail and Calendar'
           + (' (open the link it prints, then paste the address you land on)' if headless() else ' (a browser opens)') + '...')
    args = [gog, 'auth', 'add', email, '--client', client, '--services', 'gmail,calendar', '--readonly', '--gmail-scope', 'readonly']
    if headless():
        args.append('--manual')
    try:
        subprocess.run(args, env=gog_env(config), timeout=900)
    except (OSError, subprocess.TimeoutExpired):
        pass


def step_slack(config, account, ui):
    ui.say('Slack is read through Maton (maton.ai), which holds the Slack sign-in.\n'
           'In Maton: connect Slack, then copy your API key and the Slack connection ID.')
    store_secret(config, account['credential'], ui, 'Maton API key', account.get('credential_variable', 'MATON_API_KEY'))
    current = '' if account.get('connection_id') == PLACEHOLDERS['connection_id'] else account.get('connection_id', '')
    account['connection_id'] = ui.ask('Maton Slack connection ID', current)
    if not account['connection_id']:
        raise SetupError('No Maton connection ID given', 'copy it from Maton, then run: assistant connect slack')
    slack = assistant().Slack(config, account)
    try:
        me = slack.get('auth.test')
        user = slack.get('users.info', {'user': me['user_id']}).get('user') or {}
    except assistant().SourceError as error:
        raise SetupError(f'Slack did not answer: {error}', hint(error)) from None
    account.update(team_id=me['team_id'], user_id=me['user_id'], email=(user.get('profile') or {}).get('email') or account['email'])
    if me.get('url'):
        account['slack_host'] = urllib.parse.urlparse(me['url']).netloc or account.get('slack_host')
    ui.say(f'  Slack: {account["email"]} in {account.get("slack_host") or me["team_id"]}')


def step_salesflare(config, account, ui):
    ui.say('Salesflare: create an API key in Salesflare\'s settings (API keys).')
    account.setdefault('credential_variable', 'SALESFLARE_API_KEY')
    store_secret(config, account['credential'], ui, 'Salesflare API key', account['credential_variable'])
    try:
        me = assistant().Salesflare(config, account).get('/me')
    except assistant().SourceError as error:
        raise SetupError(f'Salesflare did not answer: {error}', hint(error)) from None
    account.update(user_id=me['id'], team_id=(me.get('team') or {}).get('id'), email=me['email'])
    account['owner_emails'] = sorted({me['email'], *account.get('owner_emails', [])} - {PLACEHOLDERS['email']})
    ui.say(f'  Salesflare: {me["email"]}')


def step_whatsapp(config, account, ui):
    wacli = tool(config, 'wacli')
    if not wacli:
        raise SetupError('wacli is not installed', 'install wacli (https://wacli.sh), then run: assistant connect whatsapp')
    config['wacli'] = config.get('wacli') if config.get('wacli') not in (None, 'wacli') else wacli
    account['store'] = ui.ask('wacli store directory', account.get('store') or str(home() / '.local/share/wacli'))
    linked = whatsapp_linked(wacli, account['store'])
    if not linked and ui.yes('WhatsApp is not linked yet. Link it now? (scan the QR code in WhatsApp > Linked devices)', True):
        Path(account['store']).mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run([wacli, '--store', account['store'], 'auth'], timeout=900)
        except (OSError, subprocess.TimeoutExpired):
            pass
        linked = whatsapp_linked(wacli, account['store'])
    if not linked:
        raise SetupError('WhatsApp is not linked', f'wacli --store {account["store"]} auth, then: assistant connect whatsapp')
    account['identity_sha256'] = hashlib.sha256(linked.encode()).hexdigest()
    ui.say('  WhatsApp: linked')


def step_android(config, account, ui):
    nodes = android_nodes(tool(config, 'openclaw'))
    if not nodes:
        raise SetupError('No Android phone is paired with OpenClaw',
                         'install the OpenClaw app on the phone, pair it with this Gateway (openclaw nodes pending / approve), '
                         'allow notification access, then: assistant connect android')
    account['node_id'] = ui.choose('Which phone', [(n['nodeId'], f'{n.get("displayName") or n["nodeId"]}'
                                                   + ('' if n.get('connected') else ' (offline now)')) for n in nodes])
    name = next(n.get('displayName') for n in nodes if n['nodeId'] == account['node_id'])
    ui.say(f'  Android: {name or account["node_id"]}')


def step_gateway(config, account, ui):
    account.setdefault('unit', 'openclaw-gateway.service')


def step_plugin(config, account, ui):
    module = plugin(account.get('service', 'gmail'))
    setup = getattr(module, 'SETUP', None) or {}
    if setup.get('note'):
        ui.say(setup['note'])
    for key, prompt, *rest in setup.get('ask', []):
        command = (setup.get('detect') or {}).get(key)
        detected = run(command, timeout=20).stdout.strip() if command and shutil.which(command[0]) else ''
        default = account.get(key) or detected or (rest[0].format(**account) if rest else '')
        account[key] = ui.ask(prompt, default)
        if not account[key]:
            raise SetupError(f'No {key} given', f'rerun: assistant connect {account.get("service", "gmail")}')
    if setup.get('secret'):
        account.setdefault('credential', account.get('service', 'gmail'))
        store_secret(config, account['credential'], ui, setup['secret'], account.get('credential_variable'))


STEPS = {'gmail': step_google, 'calendar': step_google, 'slack': step_slack, 'salesflare': step_salesflare,
         'whatsapp': step_whatsapp, 'android': step_android, 'gateway': step_gateway}


def sources():
    return sorted({*SERVICES} - {'salesflare'}) + assistant().connector_services()


def connect(config_path, name, ui, account_id=None, restart=True):
    """Set up one source: ask, write config and credentials, find IDs, read it once; then restart
    the service and show the first sync. Returns the account. Raises SetupError with a fix."""
    config = load(config_path)
    service = SERVICES.get(name, name)
    if service not in LABELS and not plugin(service):
        raise SetupError(f'Unknown source {name}', 'choose one of: ' + ', '.join(sources()))
    same = [a for a in config.get('accounts', []) if a.get('service', 'gmail') == service]
    account = next((a for a in same if a['id'] == account_id), None) if account_id else (
        next((a for a in same if missing(config, a)), None) or (same[0] if same else None))
    existing = account is not None
    account = json.loads(json.dumps(account)) if existing else new_account(config, service, account_id)
    ui.say(f'\n{label(service)} ({account["id"]})')
    inherited = any(a.get('boundary') == account.get('boundary') for a in config.get('accounts', []))
    if not existing and not inherited and str(account.get('boundary', '')).startswith('work:'):
        account['boundary'] = ui.ask('Boundary label (work:<Company>, or personal:<name>)', account['boundary'])
    STEPS.get(service, step_plugin)(config, account, ui)
    error = check(config, account)
    if error:
        raise SetupError(f'{account["id"]} does not work yet: {error}', hint(error) or f'rerun: assistant connect {name}')
    config['accounts'] = [account if a['id'] == account['id'] else a for a in config.get('accounts', [])] + ([] if existing else [account])
    save(config_path, config)
    ui.say(f'  ✓ {label(service)} connected')
    if restart:
        restart_and_sync(config, [account['id']], ui, config_path=config_path)
    return account


# ---------------------------------------------------------------- service, first sync

def service_active(config_path=None):
    """Is the service running (and, given a config path, running with that config)?"""
    if not shutil.which('systemctl') or run(['systemctl', '--user', 'is-active', UNIT_NAME], timeout=15).stdout.strip() != 'active':
        return False
    if config_path is None:
        return True
    env = run(['systemctl', '--user', 'show', UNIT_NAME, '-p', 'Environment', '--value'], timeout=15).stdout
    found = re.search(r'(?:^|\s)"?ASSISTANT_CONFIG=([^"\s]+)', env)
    # Without ASSISTANT_CONFIG the service reads the default under its own HOME (not this process's).
    unit_home = re.search(r'(?:^|\s)"?HOME=([^"\s]+)', env)
    try:
        import pwd
        user_home = pwd.getpwuid(os.getuid()).pw_dir
    except (ImportError, KeyError):
        user_home = str(Path.home())
    used = Path(found.group(1)) if found else Path(unit_home.group(1) if unit_home else user_home) / '.local/share/personal-assistant/config.json'
    return used.expanduser().resolve() == Path(config_path).expanduser().resolve()


def healthy(config, timeout=0):
    deadline = time.time() + timeout
    while True:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{config.get('port', 8787)}/health", timeout=3) as response:
                if json.loads(response.read()).get('ok'):
                    return True
        except (OSError, ValueError):
            pass
        if time.time() >= deadline:
            return False
        time.sleep(1)


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def restart_and_sync(config, account_ids, ui, wait=90, config_path=None):
    if not service_active(config_path):
        ui.say(f'  The running service reads another config; restart the one that reads {config_path} to start syncing.'
               if config_path and service_active() else f'  Start syncing with: systemctl --user restart {UNIT_NAME}')
        return False
    since = now()
    result = run(['systemctl', '--user', 'restart', UNIT_NAME], timeout=60)
    if result.returncode or not healthy(config, 30):
        ui.say(f'  The service did not come back; see: journalctl --user -u {UNIT_NAME} -n 50')
        return False
    return first_sync(config, account_ids, since, ui, wait)


def database(config):
    path = Path(config.get('database', '')).expanduser()
    if not path.is_file():
        return None
    db = sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    return db


def first_sync(config, account_ids, since, ui, wait=90):
    """Wait for each account's first sync after `since` and show what came in. Read-only."""
    ui.say('First sync (Ctrl+C stops waiting; syncing goes on in the background):')
    deadline, seen = time.monotonic() + wait, {}
    try:
        while True:
            db = database(config)
            if db:
                with closing(db):
                    for account_id in account_ids:
                        row = db.execute('SELECT status,signals,error FROM sync_activity WHERE account_id=? AND at>=? '
                                         'ORDER BY at DESC LIMIT 1', (account_id, since)).fetchone()
                        if row:
                            seen[account_id] = dict(row)
            if len(seen) == len(account_ids) or time.monotonic() >= deadline:
                break
            time.sleep(2)
    except KeyboardInterrupt:
        ui.say('')
    week = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7)).isoformat()
    counts = {}
    db = database(config)
    if db:
        with closing(db):
            for account_id in account_ids:
                counts[account_id] = db.execute('SELECT count(*), sum(occurred_at>=?) FROM events WHERE account_id=? AND deleted=0',
                                                (week, account_id)).fetchone()
    ok = True
    for account_id in account_ids:
        row, (total, recent) = seen.get(account_id), counts.get(account_id) or (0, 0)
        if not row:
            ui.say(f'  … {account_id}: no sync finished yet; it may still be working. Check later: assistant sources')
        elif row['status'] == 'failed':
            ok = False
            ui.say(f'  ✗ {account_id}: {row["error"]}' + (f': {hint(row["error"])}' if hint(row['error']) else ''))
        else:
            ui.say(f'  ✓ {account_id}: {total} signals so far ({recent or 0} from the last 7 days)')
    return ok


# ---------------------------------------------------------------- dashboard, USER.md

def dashboard_served(config):
    """The tailnet URL that proxies to this service, from `tailscale serve status`, or None."""
    tailscale = shutil.which('tailscale')
    if not tailscale:
        return None
    try:
        status = json.loads(run([tailscale, 'serve', 'status', '--json'], timeout=15).stdout or '{}')
    except ValueError:
        return None
    port = str(config.get('port', 8787))
    for host, web in (status.get('Web') or {}).items():
        for path, handler in ((web or {}).get('Handlers') or {}).items():
            target = str((handler or {}).get('Proxy', '')).rstrip('/')
            if target.endswith(':' + port) and ('127.0.0.1' in target or 'localhost' in target):
                return f'https://{host.rsplit(":", 1)[0]}{path.rstrip("/")}/'
    return None


def serve_command(config):
    path = config.get('dashboard_path', '/assistant/').rstrip('/') or '/'
    return ['tailscale', 'serve', '--bg', '--https=443', '--set-path', path, f'http://127.0.0.1:{config.get("port", 8787)}']


def serve_dashboard(config, ui):
    """Offer to publish the dashboard on the tailnet (only your tailnet can reach it)."""
    served = dashboard_served(config)
    if served:
        ui.say(f'Dashboard: {served}')
        return served
    if not shutil.which('tailscale'):
        return None
    command = serve_command(config)
    if not ui.yes(f'Make the dashboard reachable on your tailnet? ({" ".join(command)})', True):
        return None
    result = run(command, timeout=60)
    served = dashboard_served(config)
    if result.returncode or not served:
        ui.say('  tailscale refused. Allow your user once with: sudo tailscale set --operator=$USER, then run:\n    ' + ' '.join(command))
        return None
    ui.say(f'  Dashboard: {served} (add it to your phone\'s home screen)')
    return served


def user_md_path(workspace=None):
    return Path(workspace or os.environ.get('OPENCLAW_WORKSPACE') or home() / '.openclaw/workspace') / 'USER.md'


def starter_user_md(config):
    companies = sorted({a['boundary'].split(':', 1)[1] for a in config.get('accounts', [])
                        if str(a.get('boundary', '')).startswith('work:') and ':' in a['boundary']})
    emails = sorted({a['email'] for a in config.get('accounts', []) if a.get('email') and a['email'] != PLACEHOLDERS['email']})
    first = (config.get('owner_name') or '').split(' ')[0]
    return '\n'.join([
        '# About me', '',
        f'- Name: {config.get("owner_name") or ""}',
        f'- Time zone: {config.get("timezone") or ""}',
        f'- Work: {", ".join(companies)}' if companies else '- Work: ',
        f'- Email: {", ".join(emails)}' if emails else '- Email: ',
        '- Role: ', '',
        '## How I write', '',
        '- Languages: ',
        '- Tone: (for example: short and friendly, first names, no exclamation marks)',
        f'- Sign-off: {first}', '',
        '## What matters to me', '',
        '- (customers, projects, people you never want to keep waiting)', '',
        '## Leave alone', '',
        '- (newsletters, tools or people the assistant should ignore)', ''])


def offer_user_md(config, ui, workspace=None):
    path = user_md_path(workspace)
    if path.exists():
        return False
    if not ui.yes(f'Write a starter {path} so your agent knows who you are? (edit it afterwards)', True):
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    write_private(path, starter_user_md(config))
    ui.say(f'  wrote {path}; fill in the empty lines when you have a minute')
    return True


# ---------------------------------------------------------------- doctor

def ago(stamp):
    try:
        seconds = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(str(stamp).replace('Z', '+00:00'))).total_seconds()
    except ValueError:
        return str(stamp)
    for size, unit in ((86400, 'day'), (3600, 'hour'), (60, 'minute')):
        if seconds >= size:
            count = int(seconds // size)
            return f'{count} {unit}{"s" if count > 1 else ""} ago'
    return 'just now'


def doctor(config_path, workspace=None):
    """Every check as (mark, text, fix): ✓ fine, ! worth a look, ✗ broken. Contacts nothing outside
    this machine; source health comes from the service's own sync records."""
    results = []
    add = lambda mark, text, fix='': results.append((mark, text, fix))
    try:
        config = load(config_path)
    except SetupError as error:
        return [('✗', error.message, error.fix)]
    add('✓', f'config {config_path}')
    for key in ('telegram_owner', 'tailscale_owner', 'dashboard_host'):
        if not config.get(key):
            add('✗', f'{key} is empty in config.json', './install.sh, or set it in config.json')
    # Service
    if shutil.which('systemctl'):
        active = service_active()
        add('✓' if active else '✗', 'service ' + ('running' if active else 'not running'),
            '' if active else f'systemctl --user restart {UNIT_NAME}; then journalctl --user -u {UNIT_NAME} -n 50')
    up = healthy(config)
    add('✓' if up else '✗', f'health on 127.0.0.1:{config.get("port", 8787)} ' + ('OK' if up else 'not answering'),
        '' if up else f'journalctl --user -u {UNIT_NAME} -n 50')
    # OpenClaw bridge
    openclaw = tool(config, 'openclaw')
    if not openclaw:
        add('✗', 'openclaw CLI not found', 'install OpenClaw and rerun ./install.sh')
    else:
        try:
            plugins = json.loads(run([openclaw, 'plugins', 'list', '--json'], timeout=60).stdout or '{}').get('plugins', [])
        except (ValueError, AttributeError):
            plugins = None
        bridge = next((p for p in plugins or [] if p.get('id') == 'personal-assistant-bridge'), None)
        if plugins is None:
            add('!', 'could not list OpenClaw plugins', 'openclaw plugins list')
        elif not bridge:
            add('✗', 'OpenClaw bridge plugin not installed', './install.sh')
        elif not bridge.get('enabled') or bridge.get('status') not in (None, 'loaded'):
            add('✗', f'OpenClaw bridge plugin {"disabled" if not bridge.get("enabled") else bridge.get("status")}',
                'openclaw plugins enable personal-assistant-bridge && openclaw plugins reload personal-assistant-bridge --wait')
        else:
            add('✓', 'OpenClaw bridge plugin loaded')
    # Telegram
    owners = telegram_owners(config)
    owner = str(config.get('telegram_owner', ''))
    if not owners:
        add('!', 'could not confirm your Telegram ID in OpenClaw', 'message your bot; if it asks for pairing: openclaw pairing approve telegram <code>')
    elif owner in owners:
        add('✓', f'Telegram {owner} is approved in OpenClaw')
    else:
        add('✗', f'telegram_owner {owner} is not an approved Telegram sender in OpenClaw (found {", ".join(owners)})',
            'set telegram_owner in config.json to your own ID, or approve it: openclaw pairing list telegram')
    # Credentials (a source's own missing credential is reported with that source below)
    for name, path in sorted(config.get('credentials', {}).items()):
        path = Path(path).expanduser()
        if not path.exists() and name in ('api-token', 'owner-token'):
            add('✗', f'credential {name} missing', './install.sh')
        elif path.exists() and path.stat().st_mode & 0o077:
            add('!', f'credential {name} is readable by others', f'chmod 600 {path}')
    # Sources
    db = database(config)
    checkpoints, counts, last_review = {}, {}, None
    if db:
        with closing(db):
            try:
                checkpoints = {r['account_id']: dict(r) for r in db.execute('SELECT * FROM checkpoints')}
                counts = dict(db.execute('SELECT account_id, count(*) FROM events WHERE deleted=0 GROUP BY account_id').fetchall())
                last_review = db.execute("SELECT max(started_at) FROM analyses").fetchone()[0]
            except sqlite3.Error:
                pass
    for account in config.get('accounts', []):
        name, title = connect_name(account, config), f'{account["id"]} ({label(account.get("service", "gmail"))})'
        reason = missing(config, account)
        point = checkpoints.get(account['id']) or {}
        if reason:
            add('✗', f'{title}: not set up ({reason})', f'assistant connect {name}')
        elif not point.get('last_success') and point.get('error'):
            add('✗', f'{title}: {point["error"]}' + (f': {hint(point["error"])}' if hint(point['error']) else ''), f'assistant connect {name}')
        elif not point.get('last_success'):
            add('!', f'{title}: no sync yet', 'give it a few minutes after a restart; then assistant sources')
        elif point['last_success'] < (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2)).isoformat():
            add('✗' if point.get('error') else '!', f'{title}: last sync {ago(point["last_success"])}'
                + (f'; now failing with {point["error"]}' + (f': {hint(point["error"])}' if hint(point['error']) else '') if point.get('error') else ''),
                f'assistant connect {name}' if point.get('error') else 'assistant sources; journalctl --user -u ' + UNIT_NAME)
        elif point.get('error') and str(point.get('error_at') or '') >= str(point['last_success']):
            add('!', f'{title}: synced {ago(point["last_success"])}; the last try failed with {point["error"]}'
                + (f' ({hint(point["error"])})' if hint(point['error']) else ''), '' if hint(point['error']) else f'assistant connect {name}')
        else:
            add('✓', f'{title}: synced {ago(point["last_success"])}, {counts.get(account["id"], 0)} signals')
    if not config.get('accounts'):
        add('✗', 'no sources configured', 'assistant connect gmail (or any of: ' + ', '.join(sources()) + ')')
    # Reviews and delivery
    on = config.get('delivery_enabled') and (config.get('review_worker') or {}).get('enabled')
    if not on:
        add('!', 'reviews and Telegram cards are off', 'try one: assistant preview; turn them on: ./install.sh --activate')
    elif not last_review or last_review < (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2)).isoformat():
        add('!', 'no review in the last 2 days' if last_review else 'no review yet', 'assistant reviews; openclaw logs')
    else:
        add('✓', f'reviews on, last one {ago(last_review)}')
    # Dashboard and USER.md
    served = dashboard_served(config)
    if served:
        add('✓', f'dashboard {served}')
    else:
        add('!', 'dashboard not found in tailscale serve', ' '.join(serve_command(config)) + ' (or a custom domain: deploy/Caddyfile)')
    path = user_md_path(workspace)
    add('✓' if path.exists() else '!', f'{path} ' + ('exists' if path.exists() else 'missing: the agent does not know who you are'),
        '' if path.exists() else 'rerun ./install.sh for a starter one, or write it yourself')
    return results


def print_doctor(results, ui):
    for mark, text, fix in results:
        ui.say(f'  {mark} {text}')
        if fix and mark != '✓':
            ui.say(f'      fix: {fix}')
    broken, warn = sum(m == '✗' for m, *_ in results), sum(m == '!' for m, *_ in results)
    ui.say('\n' + ('All good.' if not broken and not warn else
                   ', '.join(x for x in (f'{broken} to fix' if broken else '', f'{warn} worth a look' if warn else '') if x) + '.'))
    return 1 if broken else 0

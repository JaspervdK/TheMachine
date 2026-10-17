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
import unicodedata
import urllib.error
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
# Per-account identity: never copied when a new account starts from one you already have.
IDENTITY = {'id', 'email', 'owner_emails', 'display_name', 'connection_id', 'team_id', 'user_id', 'slack_host', 'node_id',
            'identity_sha256', 'store', 'calendar_id', 'handle', 'login', 'url', 'workspace_id', 'stripe_account',
            'exclude', 'excluded_terms', 'scope'}
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


ENV_LINE = re.compile(r'^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=')


def env_names(path):
    """Variable names in a KEY=value credential file. Values are never read out."""
    try:
        lines = Path(path).expanduser().read_text().splitlines()
    except OSError:
        return []
    return [m[1] for m in map(ENV_LINE.match, lines) if m]


def set_env(path, variable, value):
    """Set one KEY=value line and keep every other line (other keys, comments) as it was."""
    path = Path(path).expanduser()
    try:
        lines = path.read_text().splitlines()
    except OSError:
        lines = []
    done, out = False, []
    for line in lines:
        match = ENV_LINE.match(line)
        if match and match[1] == variable:
            if not done:
                out.append(f'{variable}={value}')
            done = True
        else:
            out.append(line)
    write_private(path, '\n'.join(out + ([] if done else [f'{variable}={value}'])) + '\n')


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


def json5(text):
    """JSON with comments and trailing commas (openclaw.json is JSON5), without a JSON5 library."""
    out, i, string = [], 0, None
    while i < len(text):
        c = text[i]
        if string:
            out.append(c)
            if c == '\\':
                out.append(text[i + 1:i + 2])
                i += 1
            elif c == string:
                string = None
        elif c in '"\'':
            string = c
            out.append(c)
        elif text.startswith('//', i):
            i = text.find('\n', i)
            i = len(text) if i < 0 else i - 1
        elif text.startswith('/*', i):
            end = text.find('*/', i + 2)
            i = len(text) if end < 0 else end + 1
        else:
            out.append(c)
        i += 1
    cleaned = re.sub(r',(\s*[}\]])', r'\1', ''.join(out))
    cleaned = re.sub(r"'((?:[^'\\]|\\.)*)'", lambda m: json.dumps(m[1]), cleaned)
    return json.loads(re.sub(r'([{,]\s*)([A-Za-z_$][\w$]*)\s*:', r'\1"\2":', cleaned))


def openclaw_dir(root=None):
    """OpenClaw's state directory: OPENCLAW_STATE_DIR, else ~/.openclaw."""
    if root is None and os.environ.get('OPENCLAW_STATE_DIR', '').strip():
        return Path(os.environ['OPENCLAW_STATE_DIR'].strip()).expanduser()
    return Path(root or home()) / '.openclaw'


def openclaw_settings(root=None):
    """openclaw.json (OPENCLAW_CONFIG_PATH, else in the state directory), read-only; {} when unreadable."""
    path = Path(os.environ['OPENCLAW_CONFIG_PATH'].strip()).expanduser() if root is None and os.environ.get('OPENCLAW_CONFIG_PATH', '').strip() \
        else openclaw_dir(root) / 'openclaw.json'
    try:
        text = path.read_text()
    except OSError:
        return {}
    for parse in (json.loads, json5):
        try:
            settings = parse(text)
            return settings if isinstance(settings, dict) else {}
        except ValueError:
            continue
    return {}


def telegram_accounts(settings):
    """{account name: its settings} for OpenClaw's Telegram channel; the top level is 'default'."""
    channel = (settings.get('channels') or {}).get('telegram') or {}
    accounts = {k: v for k, v in (channel.get('accounts') or {}).items() if isinstance(v, dict)} if isinstance(channel, dict) else {}
    if isinstance(channel, dict) and (not accounts or channel.get('botToken') or channel.get('tokenFile')):
        accounts = {'default': accounts.get('default', channel), **accounts}
    return accounts


def telegram_account(owner, root=None):
    """The OpenClaw Telegram account (bot) the owner talks to: the one whose allowFrom has them,
    else 'default', else the only one there is."""
    accounts = telegram_accounts(openclaw_settings(root))
    for name, account in accounts.items():
        allow = account.get('allowFrom') or []
        if str(owner) in {telegram_id(a) for a in (allow if isinstance(allow, list) else [allow])}:
            return name
    return 'default' if 'default' in accounts or len(accounts) != 1 else next(iter(accounts))


def telegram_owners(config=None, root=None):
    """Telegram IDs OpenClaw already trusts, owner first: commands.ownerAllowFrom, then the
    channel's allowFrom, then approved pairing senders. Read-only; never prints the bot token."""
    found = []
    settings = openclaw_settings(root)
    if settings:
        owners = (settings.get('commands') or {}).get('ownerAllowFrom') or []
        found += [o for o in (owners if isinstance(owners, list) else [owners]) if str(o).lower().startswith(('telegram:', 'tg:'))]
        for scope in telegram_accounts(settings).values():
            allow = scope.get('allowFrom') or []
            found += allow if isinstance(allow, list) else [allow]
    state = Path((config or {}).get('openclaw_state') or openclaw_dir(root) / 'state/openclaw.sqlite').expanduser()
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
    return ','.join(chosen + (['gateway'] if gateway_unit() else []))


def discover(config):
    """[(what, command)] for accounts this machine already has that no source reads yet.
    Asks local tools only (gog, gh, OpenClaw, wacli); no tokens or messages are read."""
    accounts, found = config.get('accounts', []), []
    read = {str(a.get('email', '')).lower() for a in accounts if a.get('transport') == 'gog'}
    for email, _ in gog_accounts(tool(config, 'gog')):
        if email.lower() not in read:
            found.append((f'gog is signed in as {email}, but no source reads it', 'assistant connect gmail'))
    if not siblings(config, 'github') and shutil.which('gh') and setup_of('github'):
        login = run(['gh', 'api', 'user', '--jq', '.login'], timeout=20)
        if login.returncode == 0 and login.stdout.strip():
            found.append((f'gh is signed in as {login.stdout.strip()}', 'assistant connect github'))
    nodes = {a.get('node_id') for a in siblings(config, 'android')}
    for node in android_nodes(tool(config, 'openclaw')):
        if node['nodeId'] not in nodes:
            found.append((f'{node.get("displayName") or "an Android phone"} is paired with OpenClaw', 'assistant connect android'))
    linked = whatsapp_linked(tool(config, 'wacli'), home() / '.local/share/wacli')
    if linked and hashlib.sha256(linked.encode()).hexdigest() not in {a.get('identity_sha256') for a in siblings(config, 'whatsapp')}:
        found.append(('wacli has a linked WhatsApp', 'assistant connect whatsapp'))
    return found


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
    loader = getattr(assistant(), 'load_connector', None)
    return loader(service) if loader and service not in LABELS else None


def setup_of(service):
    """What `connect` asks for a non-built-in source: its connector's SETUP, or assistant.SETUPS
    for installs that build those sources in."""
    if service in LABELS:
        return {}
    return getattr(plugin(service), 'SETUP', None) or (getattr(assistant(), 'SETUPS', None) or {}).get(service) or {}


def label(service):
    if service in LABELS:
        return LABELS[service]
    kinds = list((getattr(plugin(service), 'KINDS', None) or {}).values())
    return setup_of(service).get('label') or (kinds[0] if kinds else service.title())


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
        variable = account.get('credential_variable') if name == account.get('credential') else None
        if variable and variable not in env_names(path):
            return f'{variable} is not in {Path(path).name}'
    for key, *_ in setup_of(account.get('service', 'gmail')).get('ask', []):
        if not account.get(key):
            return f'{key} is not set'
    return ''


def hint(error):
    return next((text for prefix, text in HINTS if str(error or '').startswith(prefix)), '')


def siblings(config, service):
    return [a for a in config.get('accounts', []) if a.get('service', 'gmail') == service]


def slug(text):
    text = unicodedata.normalize('NFKD', str(text)).encode('ascii', 'ignore').decode()
    return re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-') or 'account'


WEBMAIL = {'gmail', 'googlemail', 'outlook', 'hotmail', 'live', 'msn', 'yahoo', 'icloud', 'me', 'mac', 'aol', 'proton',
           'protonmail', 'pm', 'gmx', 'web', 'mail', 'yandex', 'zoho', 'fastmail', 'hey', 'tutanota', 'skynet', 'telenet'}


def company_of(email):
    """A company name guessed from a work address (alex@acme.io -> Acme, sam@mail.acme.co.uk -> Acme); '' for webmail."""
    parts = str(email or '').rpartition('@')[2].lower().split('.')[:-1]
    if parts and parts[-1] in ('co', 'com', 'org', 'net', 'ac', 'gov') and len(parts) > 1:
        parts = parts[:-1]
    name = parts[-1] if parts else ''
    return '' if not name or name in WEBMAIL or name == 'example' else name.capitalize()


def boundary_label(text, default='work:Main'):
    """work:<Company> or personal:<name> from what someone typed: 'Acme' is work:Acme, 'personal' or 'me'
    is personal:me, and a full label is kept."""
    text = str(text or '').strip()
    kind, colon, name = text.partition(':')
    if colon and kind.strip().lower() in ('work', 'personal', 'owner') and name.strip():
        return f'{kind.strip().lower()}:{name.strip()}'
    if text.lower().rstrip(':') in ('personal', 'private', 'me', 'home', 'family'):
        return 'personal:' + ('family' if text.lower() == 'family' else 'me')
    if text.lower().rstrip(':') in ('', 'work'):
        return default
    return 'work:' + (name.strip() if colon else text)


def ask_boundary(ui, default):
    return boundary_label(ui.ask('Company name, or "personal" (stored as work:<Company> or personal:<name>)', default), default)


def boundaries(config, service):
    """Boundaries a new account of this service could belong to, ones it does not cover yet first.
    Device and system boundaries (phone, WhatsApp, Gateway) are not offered for other sources."""
    devices = {'android', 'whatsapp', 'gateway'}
    known = [a['boundary'] for a in config.get('accounts', []) if a.get('boundary') and ':' in a['boundary']
             and not a['boundary'].startswith('owner:') and (a.get('service', 'gmail') not in devices or a.get('service') == service)]
    covered = {a.get('boundary') for a in siblings(config, service)}
    people = {a.get('boundary') for a in config.get('accounts', []) if a.get('service', 'gmail') in ('gmail', 'calendar', 'slack', 'salesflare')}
    # Boundaries with a mailbox or calendar (a company, a person) first, ones this source does not cover yet before
    # ones it does, then the busiest.
    return sorted(dict.fromkeys(known), key=lambda b: (b not in people, b in covered, -known.count(b)))


def new_account(config, service, account_id=None, ui=None):
    """A new account for this service. It starts from one you already have (same transport, keys,
    client and settings, without its identity), else from the example; its boundary and id follow
    your own naming."""
    ui = ui or UI(False)
    same = siblings(config, service)
    if same:
        first_ask = next(iter(setup_of(service).get('ask', [])), ('',))[0]
        account = {k: v for k, v in same[-1].items() if k not in IDENTITY and k != first_ask}
        template_id = None
    elif service in LABELS:
        name = next(k for k, v in SERVICES.items() if v == service)
        example = json.loads((ROOT / 'config.example.json').read_text())
        account = next(a for a in example['accounts'] if a['id'] == CONNECTORS[name])
        template_id = account['id']
    else:
        account = dict(setup_of(service).get('account') or {'id': service, 'service': service, 'boundary': 'work'})
        template_id = account['id']
    account = expand(json.loads(json.dumps(account)), home())
    account.pop('exclude', None)  # example noise rules are not yours
    options = boundaries(config, service)
    if service in ('android', 'whatsapp', 'gateway') and not same:
        pass  # keep the example's device boundary
    elif same or len(options) > 1:
        choice = ui.choose('Which part of your life is this account', [(b, b) for b in options] + [(None, 'something else')]) if options else None
        account['boundary'] = choice or ask_boundary(ui, account.get('boundary') if ':' in str(account.get('boundary')) else 'work:Main')
    elif options:
        account['boundary'] = options[0]
    elif str(account.get('boundary', '')).startswith('work') or ':' not in str(account.get('boundary', '')):
        account['boundary'] = ask_boundary(ui, account['boundary'] if ':' in str(account.get('boundary', '')) else 'work:Main')
    taken = {a['id'] for a in config.get('accounts', [])}
    suffix = 'crm' if service == 'salesflare' else service
    # Your ids follow <boundary name>-<source> (acme-gmail): name this one the same way.
    yours = any(a['id'] == f"{slug(str(a.get('boundary', '')).split(':', 1)[-1])}-{'crm' if a.get('service') == 'salesflare' else a.get('service', 'gmail')}"
                for a in config.get('accounts', []))
    if account_id:
        account['id'] = account_id
    elif template_id and template_id not in taken and not yours:
        account['id'] = template_id
    else:
        account['id'] = f"{slug(str(account.get('boundary', '')).split(':', 1)[-1])}-{suffix}"
    base, number = account['id'], 2
    while account['id'] in taken:
        account['id'], number = f'{base}-{number}', number + 1
    if account.get('email') == PLACEHOLDERS['email']:
        emails = [a['email'] for a in config.get('accounts', []) if a.get('email') and a['email'] != PLACEHOLDERS['email']]
        nearby = [e for e in emails if any(a.get('email') == e and a.get('boundary') == account.get('boundary') for a in config['accounts'])]
        if nearby or emails:
            account['email'] = (nearby or emails)[0]
            if 'owner_emails' in account:
                account['owner_emails'] = [account['email']]
    if account.get('transport') == 'gog':
        account['client'] = account.get('client') if same else next(
            (a['client'] for a in config.get('accounts', []) if a.get('transport') == 'gog'), account.get('client', 'default'))
        config.setdefault('credentials', {}).setdefault('gog-password', str(data_dir(config) / 'gog-password'))
    if account.get('credential'):
        config.setdefault('credentials', {}).setdefault(account['credential'], str(data_dir(config) / f'{account["credential"]}.env'))
    return account


def saved_keys(config, family):
    """(credential name, variable) for keys of this family already saved in any credential file. Names only."""
    return [(name, variable) for name, path in sorted(config.get('credentials', {}).items())
            for variable in env_names(path) if variable.startswith(family) and re.search(r'(KEY|TOKEN|SECRET)$', variable)]


def store_secret(config, account, ui, prompt, family=None):
    """Ask for a key without echo and save it (mode 600). Keys of the same kind you already saved,
    in any credential file, are offered first by name. Other lines in a shared file are kept."""
    if family:
        saved = sorted(saved_keys(config, family), key=lambda o: o != (account.get('credential'), account.get('credential_variable')))
        if saved:
            choice = ui.choose(prompt, [(o, f'the saved {o[1]} ({Path(config["credentials"][o[0]]).name})') for o in saved]
                               + [(None, 'a new key')])
            if choice:
                account['credential'], account['credential_variable'] = choice
                return
    variable = account.get('credential_variable')
    shared = any(a.get('id') != account['id'] and a.get('credential') == account['credential']
                 and a.get('credential_variable') == variable for a in config.get('accounts', []))
    if shared:  # a new key must not replace the one other sources still use
        account['credential'] = account['id']
    name = account['credential']
    path = Path(config.setdefault('credentials', {}).setdefault(name, str(data_dir(config) / f'{name}.env'))).expanduser()
    has = path.exists() and (variable in env_names(path) if variable else bool(path.read_text().strip()))
    value = ui.secret(prompt + (' (Enter keeps the saved one)' if has else ''))
    if not value:
        if has:
            return
        raise SetupError(f'No {prompt.split(" (")[0]} given', 'rerun this when you have it')
    if variable:
        set_env(path, variable, value.split('=', 1)[1].strip() if value.startswith(variable + '=') else value)
    else:
        write_private(path, value + '\n')


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
    own = account.get('email') if account.get('email') not in (None, '', PLACEHOLDERS['email']) else ''
    used = {str(a.get('email', '')).lower() for a in siblings(config, account.get('service', 'gmail')) if a['id'] != account['id']}
    nearby = [a['email'] for a in config.get('accounts', []) if a.get('transport') == 'gog' and a.get('boundary') == account.get('boundary')
              and a['id'] != account['id'] and a.get('email') not in (None, PLACEHOLDERS['email'])]
    unused = [e for e, _ in known if e.lower() not in used]
    email = ui.ask('Google account to read', own or next(iter(nearby + unused + [e for e, _ in known]), ''))
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
        # The service needs the keyring password too: reuse the one gog already uses here, never replace it.
        typed = os.environ.get('GOG_KEYRING_PASSWORD', '').strip()
        if typed:
            ui.say('  using your GOG_KEYRING_PASSWORD')
        elif known:
            typed = ui.secret('Password of your gog file keyring (it already holds ' + ', '.join(e for e, _ in known) + ')')
            if not typed:
                raise SetupError('No keyring password', 'run assistant connect again with the password your gog keyring uses '
                                 '(or export GOG_KEYRING_PASSWORD first)')
        else:
            typed = ui.secret('Password for the gog file keyring that keeps the Google token (Enter generates one)')
        write_private(password, (typed or secrets.token_urlsafe(24)) + '\n')
        ui.say('  keyring password saved to ' + str(password) + (' (generated)' if not typed else ''))
    if check(config, account) is None:
        ui.say(f'  {email} is already authorized')
    else:
        sign_in(config, account, gog, ui)
    if account.get('service') == 'calendar':
        pick_calendar(config, account, ui)


def sign_in(config, account, gog, ui):
    email, client = account['email'], account['client']
    ui.say('Signing in to Google with read-only access to Gmail and Calendar'
           + (' (open the link it prints, then paste the address you land on)' if headless() else ' (a browser opens)') + '...')
    args = [gog, 'auth', 'add', email, '--client', client, '--services', 'gmail,calendar', '--readonly', '--gmail-scope', 'readonly']
    if headless():
        args.append('--manual')
    try:
        subprocess.run(args, env=gog_env(config), timeout=900)
    except (OSError, subprocess.TimeoutExpired):
        pass


def gog_calendars(config, account):
    """Calendars this Google account can see (its own, shared and subscribed ones). Read-only."""
    result = run([config['gog'], 'calendar', 'calendars', '--json', '--results-only', '--account', account['email'],
                  '--client', account['client'], '--no-input'], env=gog_env(config), timeout=30)
    try:
        items = json.loads(result.stdout)
    except ValueError:
        return []
    items = items if isinstance(items, list) else (items or {}).get('items') or []
    return [c for c in items if isinstance(c, dict) and c.get('id')]


def pick_calendar(config, account, ui):
    """Your own calendar unless you pick a shared one (a partner's, a team's)."""
    calendars = gog_calendars(config, account) if ui.interactive else []
    if len(calendars) < 2:
        return
    name = lambda c: c.get('summaryOverride') or c.get('summary') or c['id']
    calendars.sort(key=lambda c: (not c.get('primary'), not c.get('selected'), name(c).lower()))
    current = account.get('calendar_id', 'primary')
    options = [('primary' if c.get('primary') else c['id'], name(c) + (' (your own)' if c.get('primary') else '')) for c in calendars]
    options.sort(key=lambda o: o[0] != current)
    chosen = ui.choose('Which calendar', options)
    if chosen == 'primary':
        account.pop('calendar_id', None)
    else:
        account['calendar_id'] = chosen
        account.setdefault('display_name', next(text for value, text in options if value == chosen))


def step_slack(config, account, ui):
    ui.say('Slack is read through Maton (maton.ai), which holds the Slack sign-in.\n'
           'In Maton: connect Slack, then copy your API key and the Slack connection ID.')
    account.setdefault('credential_variable', 'MATON_API_KEY')
    store_secret(config, account, ui, 'Maton API key', 'MATON_')
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
    account.update(team_id=me['team_id'], user_id=me['user_id'], email=(user.get('profile') or {}).get('email') or account.get('email', ''))
    if me.get('url'):
        account['slack_host'] = urllib.parse.urlparse(me['url']).netloc or account.get('slack_host')
    ui.say(f'  Slack: {account["email"] or me["user_id"]} in {account.get("slack_host") or me["team_id"]}')


def step_salesflare(config, account, ui):
    ui.say('Salesflare: create an API key in Salesflare\'s settings (API keys).')
    account.setdefault('credential_variable', 'SALESFLARE_API_KEY')
    store_secret(config, account, ui, 'Salesflare API key', 'SALESFLARE_')
    account.setdefault('api_base', 'https://api.salesflare.com')
    while True:
        try:
            me = assistant().Salesflare(config, account).get('/me')
            break
        except assistant().SourceError as error:
            other = str(error) == 'salesflare:HTTP_401' and ui.ask(
                f'{account["api_base"]} rejected that key. Is it for another Salesflare server (staging, self-hosted)? '
                'Its API address, or Enter to stop')
            if not other:
                raise SetupError(f'Salesflare did not answer: {error}', hint(error)) from None
            account['api_base'] = other.rstrip('/') if '://' in other else 'https://' + other.rstrip('/')
            host = urllib.parse.urlparse(account['api_base']).netloc
            first, _, rest = host.partition('.')
            account['app_base'] = 'https://' + ((first.replace('api', '') or 'app') + '.' + rest if rest else host)
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


def gateway_units():
    """The OpenClaw Gateway's systemd user units here (any name with openclaw in it), gateway ones first."""
    if not shutil.which('systemctl'):
        return []
    units = run(['systemctl', '--user', 'list-unit-files', '--type=service', '--no-legend', '--plain', '*openclaw*'], timeout=15).stdout.split()
    return sorted((u for u in units if u.endswith('.service')), key=lambda u: 'gateway' not in u)


def gateway_unit():
    """The Gateway unit to read logs from, or None when the Gateway does not run under systemd here."""
    units = gateway_units()
    return units[0] if units and shutil.which('journalctl') else None


def step_gateway(config, account, ui):
    """The Gateway's own systemd unit, also when it is named differently here."""
    units = gateway_units()
    if account.get('unit') not in units and units:
        account['unit'] = units[0]
    if not units:
        raise SetupError('No OpenClaw Gateway systemd unit found',
                         'the Gateway source reads its journal; install it as a service (openclaw gateway install) or remove this source')
    account.setdefault('unit', 'openclaw-gateway.service')


def step_plugin(config, account, ui):
    setup = setup_of(account.get('service', 'gmail'))
    if setup.get('note') and not setup.get('maton'):
        ui.say(setup['note'])
    for key, prompt, *rest in setup.get('ask', []):
        command = (setup.get('detect') or {}).get(key)
        detected = run(command, timeout=20).stdout.strip() if command and shutil.which(command[0]) else ''
        default = account.get(key) or detected or (rest[0].format(**account) if rest else '')
        account[key] = ui.ask(prompt, default)
        if not account[key]:
            raise SetupError(f'No {key} given', f'rerun: assistant connect {account.get("service", "gmail")}')
    own_key = account.get('transport') != 'maton' and Path(config.get('credentials', {}).get(account.get('credential', ''), '/nonexistent')).expanduser().exists()
    if setup.get('maton') and (not setup.get('secret') or (account.get('transport') == 'maton' or saved_keys(config, 'MATON_')) and ui.choose(
            f'How should it read {setup.get("label", account["service"])}', sorted(
                [('maton', 'through Maton, which you already use'), ('api', 'with its own key')],
                key=lambda o: o[0] != ('api' if own_key else 'maton'))) == 'maton'):
        account['transport'] = 'maton'
        account['connection_id'] = ui.ask(setup['maton'], account.get('connection_id', ''))
        if not account['connection_id']:
            raise SetupError('No Maton connection ID given', f'copy it from Maton, then rerun: assistant connect {account["service"]}')
        account.setdefault('credential', 'maton')
        account.setdefault('credential_variable', 'MATON_API_KEY')
        store_secret(config, account, ui, 'Maton API key', 'MATON_')
    elif setup.get('secret'):
        if setup.get('note') and setup.get('maton'):
            ui.say(setup['note'])
        if account.get('transport') == 'maton':
            account.update(transport=(setup.get('account') or {}).get('transport', 'api'))
            account.pop('connection_id', None)
            account.pop('credential_variable', None)
            account['credential'] = (setup.get('account') or {}).get('credential') or account['service']
        account.setdefault('credential', account.get('service', 'gmail'))
        store_secret(config, account, ui, setup['secret'], setup.get('family'))


STEPS = {'gmail': step_google, 'calendar': step_google, 'slack': step_slack, 'salesflare': step_salesflare,
         'whatsapp': step_whatsapp, 'android': step_android, 'gateway': step_gateway}


def sources():
    extra = getattr(assistant(), 'connector_services', None)
    return sorted({*SERVICES} - {'salesflare'}) + (extra() if extra else sorted(getattr(assistant(), 'SETUPS', None) or {}))


def connect(config_path, name, ui, account_id=None, restart=True):
    """Set up one source: ask, write config and credentials, find IDs, read it once; then restart
    the service and show the first sync. Returns the account. Raises SetupError with a fix."""
    config = load(config_path)
    service = SERVICES.get(name, name)
    if service not in LABELS and not setup_of(service):
        raise SetupError(f'Unknown source {name}', 'choose one of: ' + ', '.join(sources()))
    same = siblings(config, service)
    if account_id:
        account = next((a for a in same if a['id'] == account_id), None)
    elif any(missing(config, a) for a in same):
        account = next(a for a in same if missing(config, a))
    elif same and ui.interactive:
        account = ui.choose(f'You already have {label(service)}', [(a, f'redo {a["id"]}' + (f' ({a["email"]})' if a.get('email') else ''))
                                                                   for a in same] + [(None, f'add another {label(service)} account')])
    else:
        account = same[0] if same else None
    existing = account is not None
    ui.say(f'\n{label(service)}' + (f' ({account["id"]})' if existing else ': new account'))
    account = json.loads(json.dumps(account)) if existing else new_account(config, service, account_id, ui)
    if not existing:
        ui.say(f'  saving it as {account["id"]} in {account.get("boundary")}')
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
                return f'https://{host.removesuffix(":443")}{path.rstrip("/")}/'
    return None


def reachable(url):
    """Does anything answer HTTPS at this address (any status: login pages and 401s count)?"""
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method='HEAD'), timeout=5):
            return True
    except urllib.error.HTTPError:
        return True
    except (OSError, ValueError):
        return False


def dashboard_url(config):
    """Where the dashboard is reachable: your own dashboard_host (Caddy, a custom domain, another
    proxy) when it answers, else the tailnet address from tailscale serve, else None."""
    served = dashboard_served(config)
    host = str(config.get('dashboard_host') or '')
    if host and (not served or urllib.parse.urlparse(served).netloc != host):
        url = f'https://{host}/{config.get("dashboard_path", "/").strip("/")}'.rstrip('/') + '/'
        if reachable(url):
            return url
    return served


def serve_command(config):
    """tailscale serve for the dashboard: on its own port (machine.ts.net:8443), so it is its own origin and
    the installed dashboard app never collides with OpenClaw's web app or anything else on :443."""
    https = str(config.get('dashboard_host') or '').rpartition(':')[2]
    path = config.get('dashboard_path', '/').rstrip('/') or '/'
    return ['tailscale', 'serve', '--bg', f'--https={https if https.isdigit() else 443}', '--set-path', path,
            f'http://127.0.0.1:{config.get("port", 8787)}']


def serve_dashboard(config, ui):
    """Offer to publish the dashboard on the tailnet (only your tailnet can reach it)."""
    served = dashboard_url(config)
    if served:
        ui.say(f'Dashboard: {served}')
        return served
    if not shutil.which('tailscale'):
        return None
    command = serve_command(config)
    if not ui.yes(f'Make the dashboard reachable on your tailnet? ({" ".join(command)})', True):
        return None
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        output, failed = (result.stdout or '') + (result.stderr or ''), result.returncode
    except subprocess.TimeoutExpired as error:  # it waits when Serve still has to be enabled for the tailnet
        output, failed = ''.join(x.decode() if isinstance(x, bytes) else x or '' for x in (error.stdout, error.stderr)), 1
    except OSError as error:
        output, failed = str(error), 1
    served = dashboard_served(config)
    if failed or not served:
        link = re.search(r'https://login\.tailscale\.com/\S+', output)
        if link:
            ui.say(f'  Tailscale Serve is not enabled on your tailnet yet. Enable it at {link[0]}, then run:\n    ' + ' '.join(command))
        elif re.search(r'access denied|operator|permission', output, re.I):
            ui.say('  tailscale refused. Allow your user once with: sudo tailscale set --operator=$USER, then run:\n    ' + ' '.join(command))
        else:
            ui.say('  tailscale serve did not work' + (': ' + output.strip().splitlines()[-1][:300] if output.strip() else '')
                   + '\n    try it yourself: ' + ' '.join(command))
        return None
    ui.say(f'  Dashboard: {served} (add it to your phone\'s home screen)')
    return served


def user_md_path(workspace=None):
    return Path(workspace or os.environ.get('OPENCLAW_WORKSPACE') or home() / '.openclaw/workspace') / 'USER.md'


def user_md_is_template(path):
    """OpenClaw seeds USER.md with its template; that one still says nothing about the owner."""
    try:
        text = path.read_text()
    except OSError:
        return False
    return '# USER.md - User Model' in text and '- Prefer ...' in text and '# About me' not in text


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


PROFILE_HEADING = '## Observed from my messages'
PROFILE_MARK = '<!-- assistant profile '
# Empty starter lines a profile draft covers; they go when the draft is saved, so nothing says the same twice.
STARTER_BLANKS = {'role': r'- Role: *', 'languages': r'- Languages: *', 'tone': r'- Tone: *(?:\(for example[^\n]*\))? *',
                  'voices': r'- Tone: *(?:\(for example[^\n]*\))? *'}
STARTER_SECTIONS = {'matters': '## What matters to me\n\n- (customers, projects, people you never want to keep waiting)\n',
                    'leave_alone': '## Leave alone\n\n- (newsletters, tools or people the assistant should ignore)\n'}


def render_profile(profile, day):
    """The USER.md section for a profile draft (assistant profile)."""
    lines = [PROFILE_HEADING, f'{PROFILE_MARK}{day}' + (f' · from {profile["based_on"]}' if profile.get('based_on') else '')
             + ' · edit freely; delete this line to keep later drafts out -->', '']
    for key, title in (('role', 'Role'), ('tone', 'Tone'), ('greeting', 'Greeting'), ('sign_off', 'Sign-off'), ('habits', 'Habits')):
        if profile.get(key):
            lines.append(f'- {title}: {profile[key]}')
    if profile.get('languages'):
        lines.append('- Languages: ' + '; '.join(x.rstrip(' .;') for x in profile['languages']))
    if profile.get('examples'):
        lines.append('- Sounds like: ' + ' · '.join(f'"{e.strip(chr(34))}"' for e in profile['examples']))
    if profile.get('voices'):
        lines += ['', '### Per channel', *(f'- {channel}: {voice}' for channel, voice in profile['voices'].items())]
    for key, title in (('matters', 'What matters'), ('leave_alone', 'Leave alone')):
        if profile.get(key):
            lines += ['', f'### {title}', *(f'- {item}' for item in profile[key])]
    return '\n'.join(lines) + '\n'


def merge_profile(text, profile, day):
    """USER.md with the draft as its own section: replaces an earlier draft section whose marker line is
    still there, drops the empty starter lines it covers, and never touches anything the owner wrote.
    None when the owner made an earlier draft section their own (marker deleted)."""
    text = text or ''
    found = re.search(r'^' + re.escape(PROFILE_HEADING) + r'\n(.*?)(?=^## |\Z)', text, re.M | re.S)
    if found:
        if PROFILE_MARK not in found[1]:
            return None
        text = text[:found.start()] + text[found.end():]
    for key, pattern in STARTER_BLANKS.items():
        if profile.get(key):
            text = re.sub(r'^' + pattern + r'\n', '', text, flags=re.M)
    for key, section in STARTER_SECTIONS.items():
        if profile.get(key):
            text = text.replace(section, '')
    text = re.sub(r'\n{3,}', '\n\n', text).rstrip()
    return (text + '\n\n' if text else '') + render_profile(profile, day)


USER_MD_BUDGET = 4000  # OpenClaw chat sessions load this much of USER.md
# What goes first when a draft doesn't fit: the last examples, then the last people and projects, … the tone last.
PROFILE_DROP = ('examples', 'matters', 'leave_alone', 'habits', 'role', 'languages', 'greeting', 'sign_off', 'voices', 'tone')


def without_profile(text):
    """USER.md without an earlier draft section that still has its marker (the part a new draft keeps)."""
    found = re.search(r'^' + re.escape(PROFILE_HEADING) + r'\n(.*?)(?=^## |\Z)', text or '', re.M | re.S)
    return text[:found.start()] + text[found.end():] if found and PROFILE_MARK in found[1] else text or ''


def profile_room(text, limit=USER_MD_BUDGET):
    """Characters a draft section can use before USER.md passes what OpenClaw loads."""
    return limit - len(re.sub(r'\n{3,}', '\n\n', without_profile(text)).rstrip()) - 2


def fit_profile(text, profile, day, limit=USER_MD_BUDGET):
    """(merged USER.md, what was left out) with the draft trimmed to fit within limit, least useful
    lines first. Keeps at least one line; (None, []) when the owner made the section their own."""
    profile, left_out = {k: (list(v) if isinstance(v, list) else dict(v) if isinstance(v, dict) else v) for k, v in profile.items()}, []
    merged = merge_profile(text, profile, day)
    lines = lambda: sum(len(v) if isinstance(v, (list, dict)) else 1 for k, v in profile.items() if k in PROFILE_DROP and v)
    for key in PROFILE_DROP:
        while merged and len(merged) > limit and profile.get(key) and lines() > 1:
            if isinstance(profile[key], dict):  # voices: the least used channel goes first (they come most used first)
                channel, voice = profile[key].popitem()
                left_out.append(f'{channel} voice: {voice}')
                if not profile[key]:
                    profile.pop(key)
            elif isinstance(profile[key], list):
                left_out.append(f'{key.replace("_", " ")}: {profile[key].pop()}')
                if not profile[key]:
                    profile.pop(key)
            else:
                left_out.append(f'{key.replace("_", " ")}: {profile.pop(key)}')
            merged = merge_profile(text, profile, day)
    return merged, left_out


def says_how_you_write(path):
    """False for a USER.md that is only the starter: no draft section and the tone still blank."""
    try:
        text = path.read_text()
    except OSError:
        return False
    return PROFILE_HEADING in text or not re.search(r'^' + STARTER_BLANKS['tone'] + r'$', text, re.M)


def offer_user_md(config, ui, workspace=None):
    path = user_md_path(workspace)
    template = user_md_is_template(path)
    if path.exists() and not template:
        return False
    if not ui.yes(f'{"Add an About me section to" if template else "Write a starter"} {path} so your agent knows who you are? (edit it afterwards)', True):
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    text = starter_user_md(config)
    if template:
        text = path.read_text().rstrip() + '\n\n' + text
    write_private(path, text)
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
    """Every check as (mark, text, fix): ✓ fine, ! worth a look, ✗ broken. Contacts none of your
    accounts (only your own dashboard address); source health comes from the service's own sync records."""
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
    bots = telegram_accounts(openclaw_settings())
    configured = str(config.get('telegram_account') or 'default')
    if bots and configured not in bots:
        add('✗', f'telegram_account {configured} is not a Telegram account in OpenClaw (there: {", ".join(bots)})',
            f'set "telegram_account": "{telegram_account(owner)}" in config.json, then systemctl --user restart {UNIT_NAME}')
    elif bots and owner and telegram_account(owner) != configured and len(bots) > 1:
        add('!', f'you are approved on the Telegram account {telegram_account(owner)}, but cards go out through {configured}',
            f'set "telegram_account": "{telegram_account(owner)}" in config.json, then systemctl --user restart {UNIT_NAME}')
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
        if account.get('service') == 'gateway' and shutil.which('systemctl') and account.get('unit') not in gateway_units():
            add('✗', f'{title}: no systemd unit {account.get("unit")} here, so there is no Gateway log to read',
                f'assistant connect gateway (finds the unit), or remove {account["id"]} from config.json')
        elif reason:
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
    served = dashboard_url(config)
    if served:
        add('✓', f'dashboard {served}')
    else:
        add('!', f'dashboard not reachable at {config.get("dashboard_host") or "your dashboard_host"} nor in tailscale serve', ' '.join(serve_command(config)) + ' (or a custom domain: deploy/Caddyfile)')
    path = user_md_path(workspace)
    known = path.exists() and not user_md_is_template(path)
    if known and not says_how_you_write(path):
        add('!', f'{path} does not say how you write yet', 'assistant profile (drafts it from your own messages; you see it first)')
    else:
        add('✓' if known else '!', f'{path} ' + ('exists' if known else
            ('is still the OpenClaw template' if path.exists() else 'missing') + ': the agent does not know who you are'),
            '' if known else 'rerun ./install.sh for a starter one, or write it yourself')
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

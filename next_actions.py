"""Explicit, revision-bound next actions. No model or arbitrary command execution."""
import base64
import email.policy
import datetime
from email.message import EmailMessage
from email.utils import format_datetime
import hashlib
import html
from html.parser import HTMLParser
import json
import mimetypes
import os
from pathlib import Path
import shutil
import re
import subprocess
import tempfile
from urllib.parse import urlsplit

SEND_SCOPES = {'https://www.googleapis.com/auth/gmail.send',
               'https://www.googleapis.com/auth/gmail.compose',
               'https://www.googleapis.com/auth/gmail.modify', 'https://mail.google.com/'}


# Email attachments. A draft may attach files the preparation saved in its
# own folder or in memory/assistant/; nothing else (no credentials, no arbitrary home files).
# Each file is copied into the service's own store when the draft is saved, so it survives a
# reboot and the owner approves exactly those bytes: the SHA-256 is part of the draft revision.
_WORKSPACE = Path(os.environ.get('OPENCLAW_WORKSPACE') or Path.home() / '.openclaw/workspace').expanduser()
ATTACHMENT_SOURCES = (str(_WORKSPACE / 'data/assistant-attachments'), str(_WORKSPACE / 'memory/assistant'))
ATTACHMENT_STORE = str(Path.home() / '.local/share/personal-assistant/attachments')
MAX_ATTACHMENT_BYTES = 18 * 1024 * 1024  # Gmail's 25 MB limit applies after base64 encoding.


def attachment_store(config):
    return Path(config.get('attachment_dir', ATTACHMENT_STORE))


def human_size(size):
    return f'{size} B' if size < 1024 else f'{size / 1024:.0f} KB' if size < 1024 * 1024 else f'{size / 1048576:.1f} MB'


def attachment_list(action):
    return ', '.join(f"{a['name']} ({human_size(a['size'])})" for a in action.get('attachments', []))


def store_attachments(paths, config):
    if not isinstance(paths, list) or not 1 <= len(paths) <= 10:
        raise ValueError('attachments is a list of one to ten file paths')
    sources = [Path(s).resolve() for s in config.get('attachment_sources', ATTACHMENT_SOURCES)]
    store, records, total = attachment_store(config), [], 0
    for path in paths:
        if not isinstance(path, str) or '\x00' in path:
            raise ValueError('Attachment paths must be file paths')
        real = Path(path.replace('~', str(Path.home()), 1) if path.startswith('~/') else path).resolve()
        if not any(real.is_relative_to(source) for source in sources) or not real.is_file():
            raise ValueError('Attach only files you saved in workspace/data/assistant-attachments/<job id>/ or memory/assistant/')
        data = real.read_bytes()
        total += len(data)
        if not data or total > MAX_ATTACHMENT_BYTES:
            raise ValueError('Attachments must be non-empty and at most 18 MB together')
        name = real.name
        if len(name) > 150 or re.search(r'[\x00-\x1f/\\]', name):
            raise ValueError('Give attachments a plain file name of at most 150 characters')
        digest = hashlib.sha256(data).hexdigest()
        store.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not (store / digest).exists():
            tmp = store / (digest + '.tmp')
            tmp.write_bytes(data)
            tmp.replace(store / digest)
        records.append({'name': name, 'sha256': digest, 'size': len(data),
                        'type': mimetypes.guess_type(name)[0] or 'application/octet-stream'})
    if len({r['name'] for r in records}) != len(records):
        raise ValueError('Attachments need distinct file names')
    return records


def attachment_bytes(record, config):
    data = (attachment_store(config) / record['sha256']).read_bytes()
    if hashlib.sha256(data).hexdigest() != record['sha256']:
        raise ValueError('An attachment changed after you reviewed it. Nothing was sent.')
    return data


# Owner-approved sends: each needs the Send review and Send now (Telegram) or the dashboard dialog.
SEND_KINDS = ('email', 'email_demo')


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def actions(value):
    if isinstance(value,str):value=json.loads(value)
    return value.get('actions',[]) if value and value.get('kind')=='menu' else [value] if value else []


def selected(value,revision):
    return next((a for a in visible_actions(value) if a['revision']==revision),None)


def visible_actions(value):
    choices=actions(value)
    email_ready=any(a['kind'] in SEND_KINDS for a in choices)
    return [a for a in choices if a['kind']!='prepare' or (not email_ready and
            not re.match(r'(?i)^Draft (?:shorter|longer|another|alternative|follow-up)\b',a.get('label','')))]


def label(value):
    return value.get('label') or {'email':'Send','email_demo':'Send','prepare':'Prepare next step','open':'Open original'}.get(value['kind'],'Continue')


def preview(value):
    return email_preview(value) if value['kind'] in ('email','email_demo') else value['instructions']


def validate(value, config, allowed_accounts, recipient_options=None):
    if value is None:
        return None
    if isinstance(value,list):
        if not 1<=len(value)<=3:raise ValueError('Offer one to three useful next actions, not a menu of everything')
        if any(not isinstance(item,dict) for item in value):raise ValueError('Each next action must be one concrete choice')
        choices=[validate(item,config,allowed_accounts,recipient_options) for item in value]
        if len({item['revision'] for item in choices})!=len(choices) or len({label(item).lower() for item in choices})!=len(choices):
            raise ValueError('Next actions must be distinct concrete choices')
        return {'kind':'menu','actions':choices}
    if not isinstance(value, dict):
        raise ValueError('Next action must be an email draft, local preparation or original-page link')
    if value.get('label') is not None and (not isinstance(value['label'],str) or not 1<=len(value['label'])<=28
            or not re.fullmatch(r'[A-Za-z][A-Za-z0-9 ,’\-/]+',value['label'])
            or value['label'].lower() in ('done','mark done','dismiss','reject','snooze','change','prepare','continue','do it','yes')):
        raise ValueError('Use a short specific action label, not a generic or owner-status button')
    if value.get('kind') == 'open':
        if set(value) - {'kind', 'url', 'label'} or urlsplit(str(value.get('url', ''))).scheme != 'https':
            raise ValueError('Use an HTTPS original-page link, not executable instructions')
        if not value.get('label','').startswith(('Open ','View ','Read ')):
            raise ValueError('Link labels must say Open, View or Read, never pretend to execute a change')
        if len(value['url']) > 2000 or urlsplit(value['url']).username or not urlsplit(value['url']).hostname:
            raise ValueError('Invalid original-page link')
        result = dict(value)
    elif value.get('kind') == 'prepare':
        if set(value)-{'kind','label','instructions'} or not value.get('label'):
            raise ValueError('A follow-up preparation needs a specific label and instructions')
        # A follow-up is another Do it turn under the same limits so it may
        # apply a change; it can never pay, send or create calendar events.
        if not value['label'].startswith(('Draft ','Prepare ','Investigate ','Check ','Compare ','Research ',
                                          'Apply ','Fix ','Update ','Implement ','Test ','Set up ','Configure ')):
            raise ValueError('Follow-up labels describe drafting, checking, research or applying the task’s change')
        if not isinstance(value.get('instructions'),str) or not value['instructions'].strip() or len(value['instructions'])>2000:
            raise ValueError('Provide the concrete next preparation, at most 2000 characters')
        result=dict(value)
    elif value.get('kind') == 'email_demo':
        if set(value)-{'kind','label','subject','body'} or value.get('label','Send')!='Send':
            raise ValueError('The Send demo is preview-only with fixed fictional addresses')
        for field,limit in [('subject',250),('body',12000)]:
            if not isinstance(value.get(field),str) or not value[field].strip() or len(value[field])>limit or '\x00' in value[field]:
                raise ValueError('Provide the concrete demo email')
        if '\r' in value['subject'] or '\n' in value['subject']:raise ValueError('Invalid demo subject')
        result={'kind':'email_demo','label':'Send','from':'the-machine@example.invalid',
                'to':['alex@example.invalid'],'cc':[],'bcc':[],'subject':value['subject'],'body':value['body']}
    elif value.get('kind') == 'email':
        fields = {'kind', 'label','account_id', 'to', 'cc', 'bcc', 'subject', 'body', 'thread_id', 'in_reply_to', 'references', 'signature', 'snooze_days', 'attachments'}
        if set(value) - fields:
            raise ValueError('Unsupported email fields')
        if not isinstance(value.get('signature', True), bool):
            raise ValueError('signature is true (default: the Gmail signature is added) or false')
        account = next((a for a in config['accounts'] if a['id'] == value.get('account_id')
                        and a.get('service', 'gmail') == 'gmail' and a.get('transport') == 'gog'), None)
        if not account or account['id'] not in allowed_accounts:
            raise ValueError('Use the Gmail account belonging to this task, not another account')
        result = {'kind': 'email', 'account_id': account['id'], 'from': account['email']}
        if value.get('label'):
            if value['label']!='Send' and not value['label'].startswith('Send '):raise ValueError('Email button labels must say Send, such as Send reply')
            result['label']=value['label']
        for field in ('to', 'cc', 'bcc'):
            addresses = value.get(field, [])
            if not isinstance(addresses, list) or len(addresses) > 20 or any(
                    not isinstance(a, str) or len(a)>254 or not re.fullmatch(r'[^\s<>@,;\r\n]+@[^\s<>@,;\r\n]+\.[^\s<>@,;\r\n]+', a)
                    for a in addresses):
                raise ValueError('Recipients must be explicit email addresses')
            result[field] = addresses
        if not result['to']:
            # No verified recipient: the owner picks from the thread's own addresses, which the
            # service supplies. The model cannot add options.
            options = [a for a in (recipient_options or []) if a not in result['cc'] + result['bcc']][:6]
            if not options:
                raise ValueError('An email needs a recipient, and the task’s emails have no address to offer instead')
            result['to_options'] = options
        for field, limit in [('subject', 250), ('body', 12000)]:
            text = value.get(field)
            if not isinstance(text, str) or not text.strip() or len(text) > limit or '\x00' in text:
                raise ValueError('Provide a concrete subject and draft body within the size limit')
            if field == 'subject' and ('\r' in text or '\n' in text):
                raise ValueError('Subject cannot contain another header')
            result[field] = closing_spaced(text) if field == 'body' else text
        # The real Gmail signature is added at send time; the body ends at the name.
        result['signature'] = value.get('signature', True)
        if value.get('attachments'):
            result['attachments'] = store_attachments(value['attachments'], config)
        for field in ('thread_id', 'in_reply_to', 'references'):
            if value.get(field):
                text = value[field]
                pattern = r'[a-fA-F0-9]{1,64}' if field == 'thread_id' else r'<[^\s<>\r\n]+>' if field == 'in_reply_to' else r'<[^\s<>\r\n]+>(?: <[^\s<>\r\n]+>)*'
                if not isinstance(text, str) or len(text) > 1000 or not re.fullmatch(pattern, text):
                    raise ValueError('Invalid email thread header')
                result[field] = text
        if result.get('thread_id') and not result.get('in_reply_to'):
            raise ValueError('Thread replies need the original RFC Message-ID, not only a Gmail thread ID')
        days = value.get('snooze_days', 3 if result.get('thread_id') else 0)
        if isinstance(days, bool) or not isinstance(days, int) or not 0 <= days <= 14:
            raise ValueError('snooze_days is a whole number of days from 0 (no snooze) to 14')
        if days and not result.get('thread_id'):
            raise ValueError('Only a reply in an existing thread can be snoozed after sending')
        if days:
            # After sending, the thread leaves the inbox until then; a reply brings it back sooner.
            result['snooze_days'] = days
    else:
        raise ValueError('Only email drafts, local preparation and original-page links are supported; no payments or arbitrary writes')
    result['revision'] = hashlib.sha256(canonical(result).encode()).hexdigest()[:12]
    return result


PLACEHOLDER = re.compile(r'(?i)\[(?:name|first name|your name|recipient|company|date|time|link|url|amount|insert[^\]]*|x{2,}|\.\.\.|…)\]|\{\{[^}]*\}\}|<(?:name|link|date)>|\bTODO\b|\bTBD\b|\bXXX\b|lorem ipsum')
# Only the sender's own "here's the file" phrasing; "thanks for the attached invoice" is fine.
ATTACHED = re.compile(r"(?i)\b(?:I(?:'ve| have)? attached|attached (?:is|are|you'll find|please find)|(?:please )?find attached|see (?:the )?attached|I(?:'m| am) attaching|in bijlage (?:vind|stuur|zit)|(?:vind|stuur) (?:je|u) (?:hierbij|in bijlage)|bijgevoegd vind|ci-joint|anbei)\b")


def check(value, result='', own_addresses=()):
    """Mistakes a Do it result can't go out with: checked before it's saved, so the
    run fixes them itself instead of the owner tapping Change. Returns plain problems; empty is fine."""
    problems = []
    for action in actions(value):
        text = (action.get('subject') or '') + '\n' + (action.get('body') or '')
        if PLACEHOLDER.search(text):
            problems.append(f"“{action.get('label') or action['kind']}” still has a placeholder ({PLACEHOLDER.search(text)[0]}): fill it in from the evidence")
        if re.search(r'\[[^\]]+\]\((?!https?://)[^)]*\)', text):
            problems.append('A link has no real https:// address')
        if action['kind'] == 'email':
            if ATTACHED.search(action['body']) and not action.get('attachments'):
                problems.append('The email mentions an attachment but has none: add attachments:[<paths>] or reword it')
            own = {a.lower() for a in own_addresses if a}
            if own and action['to'] and set(a.lower() for a in action['to']) <= own:
                problems.append('The email only goes to your own address: use the other person’s verified address')
    if not any(a['kind'] in ('email', 'email_demo') for a in actions(value)) and re.search(r'(?im)^\s*\**subject:\**\s*\S', result or ''):
        problems.append('The result contains an email (a Subject: line) but no email next action: put the ready email in next_actions so it gets a Send button')
    return problems


def revise(action, subject=None, body=None):
    """The owner's own edit of a ready draft: same recipients, account and attachments, new text and revision."""
    if action['kind'] != 'email':
        raise ValueError('Only email drafts can be edited')
    result = {k: v for k, v in action.items() if k != 'revision'}
    if body is not None:
        if not isinstance(body, str) or not body.strip() or len(body) > 12000 or '\x00' in body:
            raise ValueError('The draft needs some text, within the size limit')
        result['body'] = closing_spaced(body)
    if subject is not None:
        if not isinstance(subject, str) or not subject.strip() or len(subject) > 250 or '\r' in subject or '\n' in subject or '\x00' in subject:
            raise ValueError('The subject needs one line of text')
        result['subject'] = subject
    result['revision'] = hashlib.sha256(canonical(result).encode()).hexdigest()[:12]
    return result


def closing_spaced(body):
    """Owner standard: a blank line between the closing ("Regards,") and the name."""
    lines = body.rstrip().split('\n')
    if (len(lines) >= 2 and lines[-2].strip().endswith(',') and len(lines[-2].strip()) <= 40
            and re.fullmatch(r"[A-Z][\w.'-]*(?: [A-Z][\w.'-]*)?", lines[-1].strip())):
        lines.insert(-1, '')
    return '\n'.join(lines)


LINK = re.compile(r'\[([^\[\]\n]{1,200})\]\((https://[^\s()<>]{1,2000})\)|(https://[^\s<>]*[^\s<>.,;:!?)\]])')


def body_text(body):
    return LINK.sub(lambda m: f'{m[1]} ({m[2]})' if m[1] else m[3], body)


def body_html(body):
    parts, last = [], 0
    for m in LINK.finditer(body):
        parts.append(html.escape(body[last:m.start()]))
        url, text = (m[2], m[1]) if m[1] else (m[3], m[3])
        parts.append(f'<a href="{html.escape(url)}">{html.escape(text)}</a>')
        last = m.end()
    parts.append(html.escape(body[last:]))
    return ''.join(parts).replace('\n', '<br>\n')


class SignatureText(HTMLParser):
    BREAKS = {'br', 'div', 'p', 'tr', 'li', 'table'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.href, self.anchor = [], None, []

    def handle_starttag(self, tag, attrs):
        if tag in self.BREAKS:
            self.parts.append('\n')
        elif tag == 'a':
            self.href, self.anchor = dict(attrs).get('href'), []

    def handle_endtag(self, tag):
        if tag in self.BREAKS:
            self.parts.append('\n')
        elif tag == 'a' and self.href is not None:
            text = ''.join(self.anchor).strip()
            self.parts.append(text if not text or self.href.startswith(('mailto:', 'tel:')) or text in self.href else f'{text} ({self.href})')
            self.href = None

    def handle_data(self, data):
        (self.anchor if self.href is not None else self.parts).append(data)


def signature_text(signature):
    parser = SignatureText()
    parser.feed(signature)
    parser.close()
    lines = [' '.join(line.split()) for line in ''.join(parser.parts).split('\n')]
    return '\n'.join(line for line in lines if line)


def email_preview(action):
    lines = ['From: ' + action['from'], 'To: ' + (', '.join(action['to']) or 'you choose: ' + ', '.join(action.get('to_options', [])))]
    for field in ('cc', 'bcc'):
        if action.get(field):
            lines.append(field.upper() + ': ' + ', '.join(action[field]))
    return ('\n'.join(lines) + '\nSubject: ' + action['subject'] + '\n\n' + action['body']
            + ('\n\n+ your Gmail signature' if action.get('signature') else '')
            + ('\n\nAttachments: ' + attachment_list(action) if action.get('attachments') else '')
            + ('\n' + snooze_note(action) if action.get('snooze_days') else ''))


def snooze_note(action):
    days = action['snooze_days']
    return f"After sending, the thread leaves your inbox for {days} day{'s' if days > 1 else ''} (a weekday morning; a reply brings it back sooner)." 


def mime_bytes(action, job, signature='', config=None):
    message = EmailMessage(policy=email.policy.SMTP)
    message['From'] = action['from']
    message['To'] = ', '.join(action['to'])
    for field in ('cc', 'bcc'):
        if action[field]:
            message[field] = ', '.join(action[field])
    message['Subject'] = action['subject']
    message['Message-ID'] = job['rfc_message_id']
    # The moment it goes out, not the approval: a re-approved or retried send can be days later.
    message['Date'] = format_datetime(datetime.datetime.now(datetime.timezone.utc))
    if action.get('in_reply_to'):
        message['In-Reply-To'] = action['in_reply_to']
        message['References'] = action.get('references', action['in_reply_to'])
    signature = signature if action.get('signature') else ''
    text = body_text(action['body']) + ('\n\n' + signature_text(signature) if signature else '')
    message.set_content(text, charset='utf-8')
    # Links behind their text, then a blank line and the real Gmail signature.
    page = '<div dir="ltr">' + body_html(action['body']) + '<br>\n<br>\n'
    if signature:
        page += '<div class="gmail_signature">' + signature + '</div>'
    message.add_alternative(page + '</div>', subtype='html', charset='utf-8')
    for record in action.get('attachments', []):
        maintype, _, subtype = record['type'].partition('/')
        message.add_attachment(attachment_bytes(record, config or {}), maintype=maintype, subtype=subtype or 'octet-stream',
                               filename=record['name'])
    return message.as_bytes()


def google_error_code(stdout):
    """The HTTP status Google returned, from gog's --json error, or None when it is unclear."""
    try:
        message = json.loads(stdout)['error']['message']
        codes = re.findall(r'"code":\s*(\d{3})', message)
        return int(codes[0]) if len(set(codes)) == 1 else None
    except (ValueError, KeyError, TypeError):
        return None


class EmailTransport:
    def __init__(self, config, credential, read):
        self.config, self.credential, self.read = config, credential, read
        self.signatures = {}

    def account(self, action):
        if action['kind']!='email':raise ValueError('Only a real owner-approved email can use the Gmail transport')
        account = next(a for a in self.config['accounts'] if a['id'] == action['account_id'])
        if account['email'].lower() != action['from'].lower():
            raise ValueError('The sender account changed; review the draft again')
        return account

    def env(self):
        env = os.environ.copy()
        env.update(GOG_KEYRING_BACKEND='file', GOG_KEYRING_PASSWORD=self.credential(self.config, 'gog-password'))
        env.pop('DBUS_SESSION_BUS_ADDRESS', None)
        return env

    def preflight(self, action):
        account = self.account(action)
        result = subprocess.run([self.config['gog'], 'auth', 'list', '--json', '--no-input'],
                                env=self.env(), capture_output=True, text=True, timeout=20)
        if result.returncode:
            raise ValueError('I could not verify Gmail permissions. Nothing was sent.')
        auth = next((a for a in json.loads(result.stdout).get('accounts', [])
                     if a['email'].lower() == account['email'].lower() and a.get('client') == account['client']), None)
        if not auth or not (set(auth.get('scopes', [])) & SEND_SCOPES):
            raise ValueError('Gmail is connected read-only. Sending permission needs to be enabled for this account; nothing was sent.')
        profile = self.read(self.config, account, 'gmail', 'v1', 'gmail.users.getProfile',
                            {'userId': 'me'}, 'https://www.googleapis.com/auth/gmail.readonly')
        if profile.get('emailAddress', '').lower() != action['from'].lower():
            raise ValueError('Gmail sender identity could not be verified. Nothing was sent.')
        for record in action.get('attachments', []):
            try:
                attachment_bytes(record, self.config)
            except OSError:
                raise ValueError(f"The attachment {record['name']} is missing. Nothing was sent.")
        if action.get('signature'):
            try:
                self.signature(action, account)
            except Exception:
                raise ValueError('I could not load your Gmail signature. Nothing was sent; tap Send again.')

    def signature(self, action, account):
        if not action.get('signature'):
            return ''
        key = action['from'].lower()
        if key not in self.signatures:  # Loaded by preflight, before the send is admitted.
            send_as = self.read(self.config, account, 'gmail', 'v1', 'gmail.users.settings.sendAs.get',
                                {'userId': 'me', 'sendAsEmail': action['from']}, 'https://www.googleapis.com/auth/gmail.readonly')
            self.signatures[key] = send_as.get('signature') or ''
        return self.signatures[key]

    def send(self, action, job):
        account = self.account(action)
        request = {'raw': base64.urlsafe_b64encode(mime_bytes(action, job, self.signature(action, account), self.config)).decode().rstrip('=')}
        if action.get('thread_id'):
            request['threadId'] = action['thread_id']
        # Private temporary file avoids exposing the email in process arguments.
        # The worker must have committed exact owner approval/admission first.
        with tempfile.NamedTemporaryFile(mode='w',suffix='.json') as body:
            body.write(canonical(request));body.flush()
            command = [self.config['gog'], 'api', 'call', 'gmail', 'v1', 'gmail.users.messages.send',
                       '--params', '{"userId":"me"}', '--body', '@'+body.name, '--scope', 'https://www.googleapis.com/auth/gmail.send',
                       '--account', account['email'], '--client', account['client'], '--allow-write', '--force', '--no-input', '--json']
            result = subprocess.run(command, env=self.env(), capture_output=True, text=True, timeout=60)
        if result.returncode:
            # Gmail answered with a client error (bad address, too large, no permission):
            # the request was refused, so nothing went out. Anything else stays unknown.
            code = google_error_code(result.stdout)
            if code and 400 <= code < 500 and code not in (408, 429):
                return {'rejected': code}
            return {}  # Unknown outcome: never retry a Gmail send after an error/timeout.
        result = json.loads(result.stdout)
        return {'message_id': result['id']} if result.get('id') else {}

    def reconcile(self, action, job):
        result = self.read(self.config, self.account(action), 'gmail', 'v1', 'gmail.users.messages.list',
                           {'userId': 'me', 'q': 'in:sent rfc822msgid:' + job['rfc_message_id'], 'maxResults': 2},
                           'https://www.googleapis.com/auth/gmail.readonly')
        messages = result.get('messages', [])
        if len(messages) == 1:
            return {'message_id': messages[0]['id']}
        return {}  # Absence or ambiguity never means it is safe to resend.


SNOOZE_LABEL = 'Snoozed'
MODIFY_SCOPE = 'https://www.googleapis.com/auth/gmail.modify'


class ThreadSnooze:
    """Gmail has no snooze API: archive the thread under a label, then return it to the inbox."""

    def __init__(self, config, credential, read):
        self.config, self.credential, self.read = config, credential, read

    def account(self, account_id):
        return next(a for a in self.config['accounts'] if a['id'] == account_id)

    def allowed(self, account):
        result = subprocess.run([self.config['gog'], 'auth', 'list', '--json', '--no-input'],
                                env=EmailTransport.env(self), capture_output=True, text=True, timeout=20)
        if result.returncode:
            raise RuntimeError('auth list failed')
        auth = next((a for a in json.loads(result.stdout).get('accounts', [])
                     if a['email'].lower() == account['email'].lower() and a.get('client') == account['client']), None)
        return bool(auth and {MODIFY_SCOPE, 'https://mail.google.com/'} & set(auth.get('scopes', [])))

    def write(self, account, method, params, body):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json') as file:
            file.write(canonical(body));file.flush()
            result = subprocess.run([self.config['gog'], 'api', 'call', 'gmail', 'v1', method, '--params', canonical(params),
                                     '--body', '@' + file.name, '--scope', MODIFY_SCOPE, '--account', account['email'],
                                     '--client', account['client'], '--allow-write', '--force', '--no-input', '--json'],
                                    env=EmailTransport.env(self), capture_output=True, text=True, timeout=60)
        if result.returncode:
            raise RuntimeError(f'{method} failed')
        return json.loads(result.stdout or '{}')

    def label(self, account):
        labels = self.read(self.config, account, 'gmail', 'v1', 'gmail.users.labels.list', {'userId': 'me'},
                           'https://www.googleapis.com/auth/gmail.readonly').get('labels', [])
        found = next((l['id'] for l in labels if l.get('name') == SNOOZE_LABEL), None)
        return found or self.write(account, 'gmail.users.labels.create', {'userId': 'me'},
                                   {'name': SNOOZE_LABEL, 'labelListVisibility': 'labelShow', 'messageListVisibility': 'show'})['id']

    def thread(self, account, thread_id):
        return self.read(self.config, account, 'gmail', 'v1', 'gmail.users.threads.get',
                           {'userId': 'me', 'id': thread_id, 'format': 'minimal'}, 'https://www.googleapis.com/auth/gmail.readonly')

    def thread_labels(self, account, thread_id):
        thread = self.thread(account,thread_id)
        return {label for message in thread.get('messages', []) for label in message.get('labelIds', [])}

    def snooze(self, account_id, thread_id, sent_message_id):
        account = self.account(account_id)
        if not self.allowed(account):
            return 'no_permission'
        messages=self.thread(account,thread_id).get('messages',[])
        sent=next((m for m in messages if m.get('id')==sent_message_id and 'SENT' in m.get('labelIds',[])),None)
        if not sent:
            raise RuntimeError('sent_message_not_confirmed')
        # Delayed/retried snooze must not hide a reply that already arrived.
        if any('SENT' not in m.get('labelIds',[]) and int(m.get('internalDate',0))>=int(sent.get('internalDate',0)) for m in messages):
            return 'returned'
        label = self.label(account)
        self.write(account, 'gmail.users.threads.modify', {'userId': 'me', 'id': thread_id},
                   {'addLabelIds': [label], 'removeLabelIds': ['INBOX']})
        return 'snoozed'

    def wake(self, account_id, thread_id):
        account = self.account(account_id)
        label = self.label(account)
        labels = self.thread_labels(account, thread_id)
        if label not in labels:
            return 'returned'  # The owner already moved it; leave it where it is.
        # A reply already brought it back: only drop the label. Otherwise return it unread.
        add = [] if 'INBOX' in labels else ['INBOX', 'UNREAD']
        self.write(account, 'gmail.users.threads.modify', {'userId': 'me', 'id': thread_id},
                   {'addLabelIds': add, 'removeLabelIds': [label]})
        return 'returned'

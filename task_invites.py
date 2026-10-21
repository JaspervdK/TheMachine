"""Coordinating with someone who is not on The Machine Network yet, by email (docs/network.md).

A handoff (and later follow-ups and planning) to a person outside the Network becomes an ordinary email draft on the
owner's card: the task in plain words, plus one short line inviting them to get a Machine of their own, with a link
that brings the task along. Nothing goes out until the owner reads it and presses Send.

The task behind the link is sealed here with a fresh key that only travels in the link's #fragment: the Network keeps
an opaque blob for at most 30 days and cannot read it. Their new Machine (or their browser) opens it with that key.

Sealing uses only the standard library, so it opens in a browser with WebCrypto too: keys enc = HMAC(k, "enc") and
mac = HMAC(k, "mac"); the body is XORed with HMAC(enc, nonce || block counter) (a PRF in counter mode) and the whole is
authenticated with HMAC(mac, nonce || ciphertext): byte 1 (version), 16-byte nonce, ciphertext, 32-byte tag.

What the other Machine adds is their words, kept to a fixed form and shown quoted, never read as instructions."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

KINDS = ('handoff', 'followup', 'schedule', 'intro')
LINK = re.compile(r'https://[A-Za-z0-9.-]+(?::\d+)?/t/([A-Za-z0-9_-]{22})#k=([A-Za-z0-9_-]{43})')
REF = re.compile(r'([A-Za-z0-9_-]{22})\.([A-Za-z0-9_-]{43})')
EMAIL = re.compile(r'[^@\s<>"]+@[^@\s<>"]+\.[A-Za-z]{2,}')
FIELDS = {'title': 120, 'summary': 1000, 'due': 10, 'from_name': 80, 'contact': 120}


def _b64(data):
    return base64.urlsafe_b64encode(data).decode().rstrip('=')


def _unb64(text):
    return base64.urlsafe_b64decode(text + '=' * (-len(text) % 4))


def _keys(key):
    return hmac.new(key, b'enc', hashlib.sha256).digest(), hmac.new(key, b'mac', hashlib.sha256).digest()


def _stream(enc, nonce, size):
    return b''.join(hmac.new(enc, nonce + i.to_bytes(4, 'big'), hashlib.sha256).digest() for i in range((size + 31) // 32))[:size]


def seal(task, key=None):
    """(sealed, key) for a task dict: both base64url. The key goes only into the link's #fragment."""
    key = key or os.urandom(32)
    enc, mac = _keys(key)
    nonce, plain = os.urandom(16), json.dumps(task, ensure_ascii=False).encode()
    body = bytes(a ^ b for a, b in zip(plain, _stream(enc, nonce, len(plain))))
    tag = hmac.new(mac, nonce + body, hashlib.sha256).digest()
    return _b64(b'\x01' + nonce + body + tag), _b64(key)


def unseal(sealed, key):
    raw, key = _unb64(sealed), _unb64(key)
    if len(raw) < 49 or raw[0] != 1 or len(key) != 32:
        raise ValueError('That task link is not one this Machine can open')
    enc, mac = _keys(key)
    nonce, body, tag = raw[1:17], raw[17:-32], raw[-32:]
    if not hmac.compare_digest(hmac.new(mac, nonce + body, hashlib.sha256).digest(), tag):
        raise ValueError('That task link is not one this Machine can open')
    return clean(json.loads(bytes(a ^ b for a, b in zip(body, _stream(enc, nonce, len(body))))))


def clean(task):
    """A task kept to its fixed form: short plain fields, nothing else."""
    if not isinstance(task, dict):
        raise ValueError('That task link holds no task')
    out = {k: ' '.join(str(task[k]).split())[:n] for k, n in FIELDS.items() if isinstance(task.get(k), str) and task[k].strip()}
    out['kind'] = task.get('kind') if task.get('kind') in KINDS else 'handoff'
    if 'due' in out and not re.fullmatch(r'\d{4}-\d{2}-\d{2}', out['due']):
        out.pop('due')
    if not out.get('title'):
        raise ValueError('That task link holds no task')
    return out


def parse_link(text):
    """(invite id, key) from a task link (…/t/<id>#k=<key>) or its short form <id>.<key>."""
    text = str(text or '').strip()
    found = LINK.search(text) or REF.fullmatch(text.removeprefix('#task='))
    if not found:
        raise ValueError('Paste the whole task link, with the part after the #')
    return found.group(1), found.group(2)


def first_name(name):
    return (str(name or '').split() or [''])[0]


def draft(task, to_name, me, link):
    """The email: the task in plain words first, then one line about The Machine with the link. Plain code, no model."""
    hello = f"Hi {first_name(to_name) or 'there'},"
    due = f"\n\nIt would be great to have it by {task['due']}." if task.get('due') else ''
    subject = f"Could you take this on: {task['title']}"[:120]
    body = (f"{hello}\n\nCould you take this on?\n\n{task['title']}\n{task.get('summary', '')}{due}\n\n"
            f"Just reply to this email to let me know.\n\nBest,\n{first_name(me) or me}\n\n"
            f"P.S. I keep track of things like this with The Machine, a personal assistant. If you'd like one too, this link "
            f"has this task ready for it: {link}")
    return subject, body


def network_url(config, fleet_module):
    found = fleet_module.settings(config)
    return (found or {}).get('url') or getattr(fleet_module, 'NETWORK', fleet_module.RELAY) + '/api/fleet'


def claim(base_url, invite_id, token=None, timeout=20):
    """The sealed task and who sent it, from the Network. No account needed: without the key it is unreadable."""
    headers = {'Content-Type': 'application/json', **({'Authorization': 'Bearer ' + token} if token else {})}
    request = urllib.request.Request(base_url.rstrip('/') + '/task-invite', method='POST', headers=headers,
                                     data=json.dumps({'action': 'claim', 'id': invite_id}).encode())
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read() or b'{}')
    except urllib.error.HTTPError as error:
        with error:
            try:
                message = json.loads(error.read() or b'{}').get('error') or ''
            except ValueError:
                message = ''
        raise ValueError(message or f'The Network said {error.code}') from None
    except urllib.error.URLError:
        raise ValueError('The Network is not reachable right now; try again in a minute') from None

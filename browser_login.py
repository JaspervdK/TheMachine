"""Signing in to a site in the assistant's browser from a small dialog on the dashboard, instead of the browser itself.

When a preparation or a source reader stops at a sign-in page, the owner types the username, password and, when the
page asks, a one-time code in the dashboard. The service puts them in that page over DevTools (CDP, loopback only)
and submits. This is its own code path: the agent and the model never see these values, nothing here logs them, and
errors never quote them.

"Save for next time" keeps a site's username and password in browser-logins.enc in the data folder, encrypted with a
key of its own (browser-logins.key, readable by this user only). Saved sign-ins are listed and forgotten on the
dashboard; a move (moving.py) only takes them along with a passphrase. One-time codes are never saved.
"""
import datetime as dt, json, os, secrets, subprocess, sys, threading, time, urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / 'integrations'))
import browser_cdp  # noqa: E402

LOOPBACK = ('127.0.0.1', 'localhost', '::1')
STORE, KEY = 'browser-logins.enc', 'browser-logins.key'
OPENSSL = 'openssl'
SETTLE = 10  # seconds a submitted page has to move on before the dialog says it is still there

# Finds the sign-in fields on the page: {u, p, c, next} (username, password, one-time code, a Next/Sign in button).
# Read-only, and never returns what is in a field.
_FIELDS = r'''
const fields = () => {
  const shown = e => { const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none' && !e.disabled && !e.readOnly; };
  const words = e => [e.name, e.id, e.autocomplete, e.getAttribute('aria-label'), e.placeholder].join(' ').toLowerCase();
  const inputs = [...document.querySelectorAll('input')].filter(shown);
  const texts = inputs.filter(e => /^(text|email|tel|number|)$/.test(e.type));
  const p = inputs.find(e => e.type === 'password' && e.autocomplete !== 'new-password');
  const c = texts.find(e => e.autocomplete === 'one-time-code' || (/otp|totp|2fa|mfa|one.?time|verif|security.?code|auth.?code|two.?factor|passcode|\bcode\b|\bpin\b/.test(words(e))
    && !/zip|post|promo|coupon|country|area|voucher/.test(words(e))));
  const named = e => e.autocomplete === 'username' || e.type === 'email' || /user|login|e-?mail|identifier|account|phone/.test(words(e));
  let u = texts.find(e => e !== c && named(e) && (!p || !p.form || e.form === p.form));
  if (!u && p) u = texts.filter(e => e !== c && e.form === p.form && (e.compareDocumentPosition(p) & 4)).pop();
  const buttons = [...document.querySelectorAll('button, input[type=submit], [role=button]')].filter(shown);
  const next = buttons.find(b => /^\s*(next|continue|sign ?in|log ?in|submit|verify|weiter|volgende|suivant|inloggen|aanmelden|anmelden|connexion)\b/i
    .test(b.innerText || b.value || b.getAttribute('aria-label') || ''));
  if (!p && !c && !(u && next && texts.length <= 2)) return null;  // a username alone counts on a short "Next" page only
  return {u, p, c, next};
};
'''

DETECT = '() => {' + _FIELDS + '''
  const f = fields();
  return f && {host: location.host, secure: location.protocol === 'https:', title: document.title.slice(0, 80),
               username: !!f.u, password: !!f.p, code: !!f.c};
}'''

# Focuses one field after checking the page is still on `host`, and selects what is in it, so typing replaces it.
FOCUS = 'function(host, which) {' + _FIELDS + '''
  if (location.host !== host) return 'moved';
  const f = fields(), e = f && f[which];
  if (!e) return 'missing';
  e.scrollIntoView({block: 'center'}); e.focus();
  try { e.select(); } catch (_) { e.value = ''; }
  return 'ok';
}'''

SUBMIT = 'function(host, mark) {' + _FIELDS + '''
  if (location.host !== host) return 'moved';
  window.__machineLogin = mark;
  const f = fields() || {}, field = f.p || f.c || f.u, form = field && field.form;
  const button = (form && [...form.querySelectorAll('button:not([type=button]):not([type=reset]), input[type=submit]')].find(b => b.offsetParent)) || f.next;
  if (button) { button.click(); return 'clicked'; }
  if (form) { form.requestSubmit ? form.requestSubmit() : form.submit(); return 'submitted'; }
  return 'enter';
}'''

# Still the same document, with the field filled last still filled, after the submit? Then empty the password field (without events: the site already has it)
# so it does not stay readable in the page.
SETTLED = 'function(mark, which, clear) {' + _FIELDS + '''
  if (window.__machineLogin !== mark) return 'moved';
  const f = fields(), e = f && f[which];
  if (!e || !e.value) return 'changed';
  if (clear && which === 'p') Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(e, '');
  return 'same';
}'''


def _loopback(base):
    parsed = urllib.parse.urlsplit(base)
    return parsed.scheme == 'http' and parsed.hostname in LOOPBACK


def endpoints(config):
    """The DevTools addresses of the assistant's browsers: the shared one and each source account's own. Loopback only."""
    found = []
    for base in [config.get('browser_cdp_url') or 'http://127.0.0.1:9222'] + [a.get('browser_cdp') for a in config.get('accounts', [])]:
        if isinstance(base, str) and _loopback(base) and base.rstrip('/') not in found:
            found.append(base.rstrip('/'))
    return found


def site(host):
    host = str(host or '').lower().split(':')[0]
    return host[4:] if host.startswith('www.') else host


def pages(config, store=None):
    """The open tabs that show a sign-in form: [{id, host, title, username, password, code, saved}]."""
    found = []
    for i, base in enumerate(endpoints(config)):
        try:
            tabs = [t for t in browser_cdp._http(base, '/json/list', timeout=3) if t.get('type') == 'page']
        except (browser_cdp.CdpError, TypeError, AttributeError):
            continue
        for tab in tabs:
            if urllib.parse.urlsplit(tab.get('url') or '').scheme not in ('http', 'https'):
                continue
            try:
                form = browser_cdp.evaluate(base, tab['id'], DETECT, timeout_ms=3000)
            except (browser_cdp.CdpError, OSError):
                continue
            if isinstance(form, dict) and form.get('host'):
                saved = store.get(form['host']) if store else None
                found.append({**form, 'id': f'{i}:{tab["id"]}', 'site': site(form['host']),
                              'saved': {'username': saved['username']} if saved else None})
    return found


_filling = threading.Lock()


def fill(config, page, host, username=None, password=None, code=None, settle=SETTLE):
    """Types what the owner gave into the sign-in page `page` (an id from pages()) on `host`, and submits it.

    Returns {'result': 'next'|'done'|'still_here', 'page': the sign-in form now shown, if any}. Errors are plain
    ValueErrors that never contain a value given here."""
    index, _, target = str(page or '').partition(':')
    bases = endpoints(config)
    if not index.isdigit() or int(index) >= len(bases) or not target:
        raise ValueError('That page is not open in the browser any more')
    base, given = bases[int(index)], {'u': username, 'p': password, 'c': code}
    given = {k: v for k, v in given.items() if isinstance(v, str) and v}
    if not given:
        raise ValueError('Nothing to fill in')
    if any(len(v) > 1000 or '\n' in v or '\r' in v for v in given.values()):
        raise ValueError('That is too long, or has a line break')
    if not _filling.acquire(timeout=30):
        raise ValueError('Another sign-in is still going on')
    try:
        try:
            with browser_cdp.Session(base, target, timeout=20) as tab:
                form = tab.run('(' + DETECT + ')')
                if not form or form.get('host') != host:
                    raise ValueError(f'The page in the browser is no longer the sign-in to {site(host)}')
                if not form['secure'] and urllib.parse.urlsplit('//' + host).hostname not in LOOPBACK:
                    raise ValueError('That page is not on a secure (https) address; open the browser to sign in there')
                wanted = {k: v for k, v in given.items() if form[{'u': 'username', 'p': 'password', 'c': 'code'}[k]]}
                if not wanted:
                    raise ValueError('The page does not ask for that right now')
                tab.call('Page.bringToFront')
                for which, value in wanted.items():
                    if tab.run(FOCUS, host, which) != 'ok':
                        raise ValueError(f'The page in the browser is no longer the sign-in to {site(host)}')
                    tab.call('Input.insertText', {'text': value})
                mark, last = secrets.token_hex(8), 'p' if 'p' in wanted else 'c' if 'c' in wanted else 'u'
                how = tab.run(SUBMIT, host, mark)
                if how == 'moved':
                    raise ValueError(f'The page in the browser is no longer the sign-in to {site(host)}')
                if how == 'enter':
                    for kind in ('keyDown', 'keyUp'):
                        tab.call('Input.dispatchKeyEvent', {'type': kind, 'key': 'Enter', 'code': 'Enter',
                                                            'windowsVirtualKeyCode': 13, 'text': '\r' if kind == 'keyDown' else ''})
        except browser_cdp.CdpError:
            raise ValueError('The browser did not take that; open the browser to sign in there') from None
        state, until = 'same', time.monotonic() + settle
        while time.monotonic() < until:
            time.sleep(0.5)
            try:
                with browser_cdp.Session(base, target, timeout=5) as tab:
                    state = tab.run(SETTLED, mark, last, False)
            except browser_cdp.CdpError:
                state = 'moved'  # mid-navigation; look again
                continue
            if state != 'same':
                break
        if state == 'same':
            try:
                with browser_cdp.Session(base, target, timeout=5) as tab:
                    state = tab.run(SETTLED, mark, last, True)
            except browser_cdp.CdpError:
                pass
        time.sleep(1.5 if state != 'same' else 0)  # the next step's page settles
        shown = None
        try:
            shown = browser_cdp.evaluate(base, target, DETECT, timeout_ms=3000)
        except browser_cdp.CdpError:
            pass
        if state == 'same':
            return {'result': 'still_here', 'page': shown and {**shown, 'id': page, 'site': site(shown['host'])}}
        if shown:
            return {'result': 'next', 'page': {**shown, 'id': page, 'site': site(shown['host'])}}
        return {'result': 'done', 'page': None}
    finally:
        _filling.release()


class LoginStore:
    """Saved sign-ins, {site: {username, password, saved_at}}, encrypted at rest (AES-256 with openssl, the key in a
    file of its own). Only the service reads them; list() never returns a password."""

    def __init__(self, folder):
        self.path, self.key_path = Path(folder) / STORE, Path(folder) / KEY
        self.lock = threading.Lock()

    def _key(self):
        if not self.key_path.exists():
            fd = os.open(self.key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as handle:
                handle.write(secrets.token_hex(32))
        return self.key_path.read_text().strip()

    def _openssl(self, data, decrypt):
        run = subprocess.run([OPENSSL, 'enc', '-aes-256-cbc', '-pbkdf2', '-iter', '100000', '-md', 'sha256', '-salt',
                              *(['-d'] if decrypt else []), '-pass', 'env:MACHINE_LOGIN_KEY'],
                             input=data, capture_output=True, env={**os.environ, 'MACHINE_LOGIN_KEY': self._key()})
        if run.returncode:
            raise ValueError('Saved sign-ins could not be ' + ('read' if decrypt else 'saved'))
        return run.stdout

    def _read(self):
        if not self.path.exists():
            return {}
        return json.loads(self._openssl(self.path.read_bytes(), True))

    def _write(self, logins):
        data = self._openssl(json.dumps(logins).encode(), False)
        temp = self.path.with_name(self.path.name + '.tmp')
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
        os.replace(temp, self.path)

    def get(self, host):
        with self.lock:
            return self._read().get(site(host))

    def save(self, host, username=None, password=None):
        with self.lock:
            logins = self._read()
            entry = logins.get(site(host)) or {}
            if username:
                entry['username'] = username
            if password:
                entry['password'] = password
            entry['saved_at'] = dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')
            logins[site(host)] = entry
            self._write(logins)

    def list(self):
        with self.lock:
            return sorted(({'site': k, 'username': v.get('username') or '', 'saved_at': v.get('saved_at')}
                           for k, v in self._read().items()), key=lambda x: x['site'])

    def forget(self, host):
        with self.lock:
            logins = self._read()
            gone = logins.pop(site(host), None) is not None
            if gone:
                self._write(logins)
            return gone

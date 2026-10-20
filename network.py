"""This Machine on The Machine Network: end-to-end sealing, an outbox, and delivery state (docs/network.md).

`Sealing` wraps the relay connection (fleet.Relay) the Fleet and Friends already use, so every message type goes
through it without each one knowing:

- The directory: each teammate's and friend's public keys are checked and pinned the first time they're seen; when a
  Machine's keys change later, the owner is warned (key_changes) and the new keys are pinned. This Machine publishes
  its own public keys when the directory doesn't show them yet.
- Asking: the question is sealed for each recipient's Machine (network_crypto), bound to who asks whom, what kind and
  the request id. A Machine without keys yet (from before) still gets plaintext until the Network's end date; one
  that had keys never gets plaintext again (a relay can't strip keys to read along).
- Answering: an answer to a sealed question is sealed for the asking Machine, bound to the question's id.
- Receiving: sealed questions and answers are opened and checked; anything that doesn't open, or plaintext from a
  Machine that seals, is dropped and logged, never shown.
- Outbox: an answer the relay didn't take because it was away (restarting, network down) is kept here and sent again
  with backoff, so a question is never acted on twice: this Machine remembers what it answered (network_taken) and
  sends that again if the same question comes back.

Private keys stay on this Machine, in network-keys.json (mode 600) next to the database."""
from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path

import network_crypto as nc

LOG = logging.getLogger('personal-assistant')
SCHEMA = '''
CREATE TABLE IF NOT EXISTS network_outbox(key TEXT PRIMARY KEY,method TEXT NOT NULL,path TEXT NOT NULL,body TEXT NOT NULL,
  tries INTEGER NOT NULL DEFAULT 0,next_at TEXT NOT NULL,created_at TEXT NOT NULL,error TEXT);
CREATE TABLE IF NOT EXISTS network_taken(ask_id TEXT PRIMARY KEY,at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS network_sent(ask_id TEXT PRIMARY KEY,at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS network_pins(member_id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,keys TEXT NOT NULL,name TEXT,
  first_at TEXT NOT NULL,changed_at TEXT);
CREATE TABLE IF NOT EXISTS network_sealed(ask_id TEXT PRIMARY KEY,kind TEXT NOT NULL,asker TEXT NOT NULL,me TEXT NOT NULL,at TEXT NOT NULL);
'''
OUTBOX_DAYS = 3          # an answer the relay can't take for this long is given up (the question expired there too)
TRUST_AFTER = 86400      # seconds after publishing our keys: from then on, plaintext from a Machine that seals is refused
KEEP_DAYS = 30


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')


def _later(seconds):
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=seconds)).isoformat(timespec='seconds')


def ensure(db):
    """The tables in their own shape: one of the same name in another shape (from before, or another feature's) is
    moved aside as <name>_old, never dropped."""
    for statement in SCHEMA.split(';'):
        if 'CREATE TABLE' not in statement:
            continue
        name = statement.split('EXISTS', 1)[1].split('(', 1)[0].strip()
        wanted = {c.strip().split()[0] for c in statement.split('(', 1)[1].rsplit(')', 1)[0].split(',') if c.strip() and not c.strip()[0].isdigit()}
        found = {r[1] for r in db.execute(f'PRAGMA table_info({name})')}
        if found and not wanted <= found:
            db.execute(f'ALTER TABLE {name} RENAME TO {name}_old')
    db.executescript(SCHEMA)


def keys_path(config):
    """Where this Machine's private keys live: next to its database. None (no sealing) without a database path."""
    found = config.get('network_keys') or (Path(config['database']).expanduser().parent / 'network-keys.json' if config.get('database') else None)
    return Path(found).expanduser() if found else None


class Sealing:
    """The relay connection, sealing and opening on the way (see the module docstring)."""

    def __init__(self, inner, fleet, private=None):
        self.inner, self.fleet = inner, fleet
        self.private = private
        self.public = nc.public_keys(private) if private else None
        self.url = getattr(inner, 'url', '')
        with fleet.connect() as db:
            ensure(db)

    # -- what the Fleet calls
    def call(self, method, path, body=None, timeout=40):
        route = path.split('?', 1)[0]
        if not self.private:
            return self.inner.call(method, path, body, timeout=timeout)
        if route == '/directory':
            return self._directory(self.inner.call(method, path, body, timeout=timeout))
        if route == '/ask' and method == 'POST' and isinstance(body, dict) and body.get('question') is not None:
            body = self._seal_ask(body)
            found = self.inner.call(method, path, body, timeout=timeout)
            with self.fleet.connect() as db:
                for member_id, ask_id in ((found or {}).get('ids') or {}).items():
                    if member_id in (body.get('sealed') or {}):
                        db.execute('INSERT OR IGNORE INTO network_sent(ask_id,at) VALUES(?,?)', (str(ask_id), now()))
            return found
        if route == '/answer' and method == 'POST':
            return self._answer(body, timeout)
        found = self.inner.call(method, path, body, timeout=timeout)
        if route == '/inbox':
            self._open_inbox(found)
        return found

    # -- the directory and keys
    def _directory(self, found):
        changes, publish = [], False
        with self.fleet.connect() as db:
            for group in (found or {}).get('fleets', []):
                for m in group.get('members', []):
                    if m.get('me'):
                        publish = publish or bool(self.public and (m.get('keys') or {}).get('box') != self.public['box'])
                        continue
                    checked = nc.check_keys(m.get('keys')) if m.get('keys') else None
                    if m.get('keys') and not checked:
                        LOG.warning('network keys do not check out member=%s', m.get('member_id'))
                        m['keys'] = None
                    if not checked:
                        continue
                    pin = db.execute('SELECT * FROM network_pins WHERE member_id=?', (m['member_id'],)).fetchone()
                    if not pin:
                        db.execute('INSERT INTO network_pins(member_id,fingerprint,keys,name,first_at) VALUES(?,?,?,?,?)',
                                   (m['member_id'], checked['fingerprint'], json.dumps(m['keys']), str(m.get('name') or '')[:80], now()))
                    elif pin['fingerprint'] != checked['fingerprint']:
                        db.execute('UPDATE network_pins SET fingerprint=?,keys=?,changed_at=? WHERE member_id=?',
                                   (checked['fingerprint'], json.dumps(m['keys']), now(), m['member_id']))
                        changes.append({'member_id': m['member_id'], 'name': str(m.get('name') or '')[:80], 'at': now(),
                                        'was': pin['fingerprint'], 'now': checked['fingerprint']})
        if publish:
            self._publish()
        if changes:
            seen = self.fleet.meta('network_key_changes', [])
            self.fleet.set_meta('network_key_changes', (changes + seen)[:20])
            for change in changes:
                LOG.warning('network keys changed member=%s', change['member_id'])
        return found

    def _publish(self):
        try:
            self.inner.call('POST', '/keys', self.public, timeout=30)
            if not self.fleet.meta('network_published_at'):
                self.fleet.set_meta('network_published_at', now())
        except Exception as error:  # an older Network without keys (404/405) or away: plaintext as before, never "off"
            LOG.info('network keys not published yet error=%s', type(error).__name__)

    def _group(self, ref):
        return (self.fleet.fleets().get(self.fleet.ref(ref)) or {})

    def _me(self, group):
        return next((m['member_id'] for m in group.get('members', []) if m.get('me')), None)

    def _keys_of(self, member_id):
        with self.fleet.connect() as db:
            pin = db.execute('SELECT keys FROM network_pins WHERE member_id=?', (member_id,)).fetchone()
        return json.loads(pin['keys']) if pin else None

    def _current_keys(self, group, member_id):
        """A member's keys as the directory shows them now, if they check out; else None."""
        m = next((x for x in group.get('members', []) if x.get('member_id') == member_id), None)
        return m.get('keys') if m and m.get('keys') and nc.check_keys(m['keys']) else None

    # -- asking
    def _seal_ask(self, body):
        group = self._group(body.get('fleet'))
        me = self._me(group)
        if not me:
            return body
        kind, question = body.get('kind'), body['question']
        if body.get('to'):
            to = list(body['to'])
        elif kind == 'followup':
            to = sorted({i.get('member_id') for i in question.get('items', []) if i.get('member_id') and i.get('member_id') != me})
        else:
            to = [m['member_id'] for m in group.get('members', []) if not m.get('me')]
        sealed, plain = {}, []
        for member_id in to:
            keys = self._current_keys(group, member_id)
            if keys:
                sealed[member_id] = nc.seal(question, self.private, keys, {'p': 'ask', 'k': kind, 'f': me, 't': member_id, 'r': body.get('request_id')})
            elif self._keys_of(member_id):
                raise ValueError("A teammate's Machine stopped showing its keys on The Machine Network; nothing was sent. Try again later.")
            else:
                plain.append(member_id)
        if not sealed:
            return body
        out = {**body, 'sealed': sealed, 'to': to}
        if not plain:
            out.pop('question')
        return out

    # -- answering, with the outbox
    def _answer(self, body, timeout):
        body = dict(body or {})
        ask_id = str(body.get('id') or '')
        with self.fleet.connect() as db:
            row = db.execute('SELECT * FROM network_sealed WHERE ask_id=?', (ask_id,)).fetchone()
        if row and body.get('answer') is not None:
            keys = self._keys_of(row['asker'])
            if keys:
                body['sealed'] = nc.seal(body.pop('answer'), self.private, keys,
                                         {'p': 'answer', 'k': row['kind'], 'f': row['me'], 't': row['asker'], 'r': ask_id})
        return self.send_or_keep('POST', '/answer', body, 'answer:' + ask_id + ':' + str(body.get('status')), timeout)

    def send_or_keep(self, method, path, body, key, timeout=40):
        """Send now; if the relay is away, keep it in the outbox (sent again with backoff) and say it's queued."""
        import fleet as fleet_module
        try:
            return self.inner.call(method, path, body, timeout=timeout)
        except (fleet_module.Unavailable, OSError) as error:
            with self.fleet.connect() as db:
                db.execute('INSERT OR REPLACE INTO network_outbox(key,method,path,body,tries,next_at,created_at,error) VALUES(?,?,?,?,0,?,?,?)',
                           (key, method, path, json.dumps(body), _later(30), now(), type(error).__name__))
            LOG.info('network kept for later path=%s error=%s', path, type(error).__name__)
            return {'ok': True, 'queued': True}

    def flush(self):
        """Send what waits in the outbox and is due; give up (logged) after OUTBOX_DAYS."""
        import fleet as fleet_module
        with self.fleet.connect() as db:
            rows = db.execute('SELECT * FROM network_outbox WHERE next_at<=? ORDER BY created_at LIMIT 20', (now(),)).fetchall()
        for row in rows:
            try:
                self.inner.call(row['method'], row['path'], json.loads(row['body']), timeout=30)
                done = True
            except (fleet_module.Unavailable, OSError) as error:
                done = False
                with self.fleet.connect() as db:
                    if row['created_at'] < (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=OUTBOX_DAYS)).isoformat(timespec='seconds'):
                        LOG.warning('network gave up path=%s after=%sd', row['path'], OUTBOX_DAYS)
                        db.execute('DELETE FROM network_outbox WHERE key=?', (row['key'],))
                    else:
                        db.execute('UPDATE network_outbox SET tries=tries+1,next_at=?,error=? WHERE key=?',
                                   (_later(min(3600, 30 * 2 ** min(row['tries'] + 1, 7))), type(error).__name__, row['key']))
                break  # the relay is still away: the rest waits too
            except (ValueError, fleet_module.Off):
                done = True  # it went through before (the question is closed there), or it can't go anymore
            if done:
                with self.fleet.connect() as db:
                    db.execute('DELETE FROM network_outbox WHERE key=?', (row['key'],))

    def waiting(self):
        with self.fleet.connect() as db:
            return db.execute('SELECT COUNT(*) FROM network_outbox').fetchone()[0]

    # -- receiving
    def _open_inbox(self, found):
        published = self.fleet.meta('network_published_at')
        strict = bool(published) and (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(published)).total_seconds() > TRUST_AFTER
        self._refreshed = False
        for ask in (found or {}).get('asks', []):
            if ask.get('sealed') is not None:
                try:
                    ask['question'], me = self._open(ask.pop('sealed'), ask, 'ask', ask.get('request_id'))
                    with self.fleet.connect() as db:
                        db.execute('INSERT OR IGNORE INTO network_sealed(ask_id,kind,asker,me,at) VALUES(?,?,?,?,?)',
                                   (str(ask['id']), str(ask.get('kind')), str(ask.get('from')), me, now()))
                except nc.SealError as error:
                    LOG.warning('network question dropped member=%s reason=%s', ask.get('from'), error)
                    ask['question'] = {}
            elif strict and self._keys_of(ask.get('from')):
                LOG.warning('network plaintext question dropped member=%s', ask.get('from'))
                ask['question'] = {}
        for item in (found or {}).get('answers', []):
            if item.get('sealed') is None:
                if item.get('answer') is not None and strict and self._keys_of(item.get('from')) and self._asked_sealed(item.get('id')):
                    LOG.warning('network plaintext answer dropped member=%s', item.get('from'))
                    item['answer'], item['status'] = None, 'declined'
                continue
            try:
                item['answer'], _ = self._open(item.pop('sealed'), item, 'answer', item.get('id'))
            except nc.SealError as error:
                LOG.warning('network answer dropped member=%s reason=%s', item.get('from'), error)
                item['answer'], item['status'] = None, 'declined'

    def _open(self, env, item, purpose, ref):
        """Opens what a teammate's Machine sealed, with the keys pinned for it; once per inbox, a fresh directory is
        fetched first when that fails (they joined or published their keys since): (body, my member id)."""
        for attempt in (0, 1):
            group = self._group(item.get('fleet_id') or item.get('fleet'))
            me, sender = self._me(group), self._keys_of(item.get('from'))
            try:
                if not sender or not me:
                    raise nc.SealError('no keys for the sender')
                return nc.open_sealed(env, self.private, sender, {'p': purpose, 'k': item.get('kind'), 'f': item.get('from'), 't': me, 'r': ref}), me
            except nc.SealError:
                if attempt or self._refreshed:
                    raise
                self._refreshed = True
                self.fleet.directory(refresh=True)

    def _asked_sealed(self, ask_id):
        with self.fleet.connect() as db:
            return bool(db.execute('SELECT 1 FROM network_sent WHERE ask_id=?', (str(ask_id),)).fetchone())

    # -- once per question, ever
    def taken(self, ask_id):
        with self.fleet.connect() as db:
            return bool(db.execute('SELECT 1 FROM network_taken WHERE ask_id=?', (str(ask_id),)).fetchone())

    def take(self, ask_id):
        with self.fleet.connect() as db:
            db.execute('INSERT OR IGNORE INTO network_taken(ask_id,at) VALUES(?,?)', (str(ask_id), now()))

    def tidy(self):
        cut = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=KEEP_DAYS)).isoformat(timespec='seconds')
        with self.fleet.connect() as db:
            db.execute('DELETE FROM network_taken WHERE at<?', (cut,))
            db.execute('DELETE FROM network_sealed WHERE at<?', (cut,))
            db.execute('DELETE FROM network_sent WHERE at<?', (cut,))

    def view(self):
        changes = self.fleet.meta('network_key_changes', [])
        return {'sealed': bool(self.private), 'fingerprint': nc.check_keys(self.public)['fingerprint'] if self.public else None,
                'key_changes': changes[:5], 'outbox': self.waiting()}


def wrap(inner, fleet, config):
    """The Fleet's relay connection, sealed when this Machine can keep keys (a database path), else as it was."""
    path = keys_path(config)
    if not path:
        return inner
    try:
        private = nc.load_or_make(path)
    except OSError as error:
        LOG.warning('network keys unavailable error=%s', type(error).__name__)
        return inner
    return Sealing(inner, fleet, private)



"""Message contents sealed at rest in The Machine's database (docs/security.md, "Message contents at rest").

Every source writes its signals through one table, `events`: a subject or title, a body, and metadata. When
`message_protection` is on, those are stored sealed, whatever the source (mail, chat, notifications, transcripts,
calendar descriptions, and any connector added later):

- `title` and `body` become sealed blobs;
- `metadata` keeps in the clear only the keys the app looks up in SQL (PLAIN_METADATA: thread and chat ids, sender
  and recipients, labels); every other key, including any a new connector adds, is sealed with the rest;
- the keyword index (`event_search`) holds keyed hashes of the words instead of the words;
- text the app derives from messages (card text, drafts, edits, prepared tool results) is sealed too (DERIVED).

The app itself reads everything as before: its connections open sealed values as they are read (`attach`). A
connection without the key (sqlite3 on the command line, a support tool) sees only blobs, and its writes to these
tables fail rather than store plain text.

The key is 32 random bytes in `message-content.key` next to the database, readable only by the owner's account. It
protects against reading the database without that account: a copied database file, a support tool that opens the
file, someone signed in as another user. It does not protect against root on the same server, which can read the key
or the running app's memory; nor against what other programs keep (OpenClaw's session transcripts, a chat bridge's
own store). Backups and exports carry the key with the database, so they stay readable for the owner.

Sealing: a fresh 16-byte nonce; the keystream is SHAKE256(key, nonce); a 16-byte BLAKE2b MAC over all of it
(encrypt-then-MAC); keys for sealing, MAC and the index are derived from the stored key with BLAKE2b. Standard
library only, as in network_crypto.py."""
from __future__ import annotations

import base64
import functools
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import unicodedata
from pathlib import Path

MAGIC = b'\x00\xffMC1\x00'
NONCE, TAG = 16, 16
KEY_NAME = 'message-content.key'
SEALED_KEY = 'mc1_sealed'
# Metadata keys the app queries in SQL (json_extract, instr): routing and identity, never what a message says
PLAIN_METADATA = frozenset({'thread_id', 'from', 'to', 'labels', 'notification_key_hash', 'user_id', 'from_me',
                            'channel_id', 'chat_id', 'conversation_id'})
# Text the app derives from messages: card text and the drafts behind it, edits, what a preparation's tools returned,
# a review's own summary, and what teammates' Machines asked and answered. JSON columns keep the named keys in the clear.
# Not here, on purpose: columns the app matches in SQL (promises.what is unique per message), and what the Machine
# learned (facts, rules, feedback, skills, profile), which support may read (docs/security.md).
DERIVED = {'suggestions': ('title', 'body', 'handled_hint'), 'feedback_prompts': ('title',),
           'draft_edits': ('before_subject', 'before_body', 'after_subject', 'after_body'),
           'graph_unnamed': ('titles',), 'preparation_calls': ('params', 'result'), 'analyses': ('summary',),
           'fleet_in': ('question', 'draft'), 'fleet_out': ('question', 'answer'), 'network_replies': ('body',)}
DERIVED_JSON = (('action_jobs', 'payload', frozenset({'kind'})), ('suggestions', 'next_action', frozenset({'kind'})),
                ('suggestions', 'outcome', frozenset({'state'})))
WORD = re.compile(r'[^\W_]+')
# The Machine's own signals (its Gateway's log, its own faults): logs, not anyone's message
SYSTEM_KINDS = ('gateway.', 'machine.')


class SealError(ValueError):
    pass


def key_path(database):
    return Path(database).expanduser().parent / KEY_NAME


def load_key(database, create=False):
    path = key_path(database)
    try:
        raw = path.read_bytes().strip()
    except FileNotFoundError:
        if not create:
            return None
        raw = base64.b64encode(secrets.token_bytes(32))
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as out:
            out.write(raw + b'\n')
            out.flush()
            os.fsync(out.fileno())
    key = base64.b64decode(raw)
    if len(key) != 32:
        raise SealError(f'{path} is not a message key')
    return key


class Sealer:
    def __init__(self, key):
        derive = lambda name: hashlib.blake2b(key=key, person=name, digest_size=32).digest()
        self._enc, self._mac, self._index = derive(b'mc1-seal'), derive(b'mc1-mac'), derive(b'mc1-index')
        self._word = functools.lru_cache(maxsize=100_000)(self._word)  # a mailbox's words repeat a lot

    def _stream(self, nonce, size):
        return hashlib.shake_256(self._enc + nonce).digest(size)

    def _tag(self, nonce, sealed):
        return hashlib.blake2b(MAGIC + nonce + sealed, key=self._mac, digest_size=TAG).digest()

    def seal(self, value):
        """A str becomes a sealed blob; anything else (None, numbers, blobs already sealed, '') stays as it is."""
        if not isinstance(value, str) or not value:
            return value
        data = value.encode('utf-8')
        nonce = os.urandom(NONCE)
        sealed = (int.from_bytes(data, 'little') ^ int.from_bytes(self._stream(nonce, len(data)), 'little')).to_bytes(len(data), 'little')
        return MAGIC + nonce + sealed + self._tag(nonce, sealed)

    def open(self, value):
        if not is_sealed(value):
            return value
        nonce, sealed, tag = value[len(MAGIC):len(MAGIC) + NONCE], value[len(MAGIC) + NONCE:-TAG], value[-TAG:]
        if len(nonce) != NONCE or not hmac.compare_digest(tag, self._tag(nonce, sealed)):
            raise SealError('A sealed message did not open: wrong key or a damaged database')
        data = (int.from_bytes(sealed, 'little') ^ int.from_bytes(self._stream(nonce, len(sealed)), 'little')).to_bytes(len(sealed), 'little')
        return data.decode('utf-8')

    def seal_json(self, text, plain=PLAIN_METADATA):
        """A JSON object keeps `plain` keys in the clear; the rest goes into one sealed member."""
        if not isinstance(text, str) or not text or SEALED_KEY in text:
            return text
        try:
            value = json.loads(text)
        except ValueError:
            value = None
        if not isinstance(value, dict):
            return self.seal(text)
        hidden = {k: v for k, v in value.items() if k not in plain}
        if not hidden:
            return text
        kept = {k: v for k, v in value.items() if k in plain}
        kept[SEALED_KEY] = base64.b64encode(self.seal(json.dumps(hidden, ensure_ascii=False))).decode()
        return json.dumps(kept, ensure_ascii=False, sort_keys=True, separators=(',', ':'))

    def open_json(self, text):
        if not is_sealed_json(text):
            return text
        value = json.loads(text)
        hidden = json.loads(self.open(base64.b64decode(value.pop(SEALED_KEY))))
        return json.dumps({**value, **hidden}, ensure_ascii=False, sort_keys=True, separators=(',', ':'))

    def open_any(self, value):
        if type(value) is bytes:
            return self.open(value)
        if type(value) is str:
            return self.open_json(value)
        return value

    def words(self, text):
        """The keyword index's words: as SQLite's unicode61 splits and folds them, then keyed-hashed."""
        text = self.open_any(text)
        if not isinstance(text, str):
            return text
        folded = ''.join(c for c in unicodedata.normalize('NFKD', text) if not unicodedata.combining(c)).casefold()
        return ' '.join(self._word(w) for w in WORD.findall(folded))

    def _word(self, word):
        return base64.b32encode(hashlib.blake2b(word.encode(), key=self._index, digest_size=8).digest()).decode().rstrip('=').lower()


def is_sealed(value):
    return type(value) is bytes and value.startswith(MAGIC)


def is_sealed_json(value):
    return type(value) is str and value.startswith('{') and f'"{SEALED_KEY}":' in value


def match(db, words, joiner):
    # `db`: a connection or the Store; either carries the sealer when the database is sealed
    """An FTS MATCH expression for these search words, each a phrase; hashed when the database is sealed."""
    sealer = getattr(db, 'sealer', None) if getattr(db, 'protected', False) else None
    phrases = []
    for word in words:
        if sealer:
            hashed = sealer.words(word)
            if hashed:
                phrases.append('"' + hashed + '"')
        else:
            phrases.append('"' + word.replace('"', '""') + '"')
    return joiner.join(phrases)


def plain_leaks(event, state=None, run=4):
    """Message text that would be stored in the clear: a run of `run` words of the event's title or body found in what
    stays readable (its source id and kind, plain metadata other than sender and recipients, the source's cursor).
    The connector tests run with MESSAGE_SEAL_AUDIT=1, which makes commit_page refuse such an event."""
    def words(value):
        return WORD.findall(json.dumps(value, ensure_ascii=False).casefold() if not isinstance(value, str) else value.casefold())
    text = words(event.get('title') or '') + ['\0'] + words(event.get('body') or '')
    runs = {tuple(text[i:i + run]) for i in range(len(text) - run + 1) if '\0' not in text[i:i + run]}
    if not runs:
        return []
    plain = {'source_id': event.get('source_id'), 'kind': event.get('kind'), 'cursor': state,
             **{f'metadata.{k}': v for k, v in (event.get('metadata') or {}).items() if k in PLAIN_METADATA - {'from', 'to'}}}
    if str(event.get('kind') or '').startswith(SYSTEM_KINDS):
        return []
    found = []
    for name, value in plain.items():
        seen = words(value) if value is not None else []
        if any(tuple(seen[i:i + run]) in runs for i in range(len(seen) - run + 1)):
            found.append(name)
    return found


class Connection(sqlite3.Connection):
    sealer, protected = None, False


def attach(db, sealer):
    """Let this connection read and write sealed values: functions for the triggers, and rows that open themselves."""
    db.sealer = sealer
    if not sealer:
        return db
    db.create_function('mc_seal', 1, sealer.seal)
    db.create_function('mc_seal_json', 2, lambda text, column: sealer.seal_json(text, plain_keys(column)))
    db.create_function('mc_words', 1, sealer.words, deterministic=True)
    opened = sealer.open_any

    def row(cursor, values):
        if any(type(v) is bytes or (type(v) is str and v[:1] == '{') for v in values):
            values = tuple(opened(v) for v in values)
        return sqlite3.Row(cursor, values)
    db.row_factory = row
    return db


def plain_keys(column):
    """`column` is 'table.column'."""
    if column == 'events.metadata':
        return PLAIN_METADATA
    return next((plain for t, c, plain in DERIVED_JSON if f'{t}.{c}' == column), frozenset())


# ------------------------------------------------------------------ the database's state

SEARCH_PLAIN = {
    'search_insert': "CREATE TRIGGER search_insert AFTER INSERT ON events BEGIN\n"
                     "  INSERT INTO event_search(rowid,title,body) VALUES(new.id,new.title,new.body); END",
    'search_update': "CREATE TRIGGER search_update AFTER UPDATE OF title,body ON events BEGIN\n"
                     "  INSERT INTO event_search(event_search,rowid,title,body) VALUES('delete',old.id,old.title,old.body);\n"
                     "  INSERT INTO event_search(rowid,title,body) VALUES(new.id,new.title,new.body); END",
    'search_delete': "CREATE TRIGGER search_delete AFTER DELETE ON events BEGIN\n"
                     "  INSERT INTO event_search(event_search,rowid,title,body) VALUES('delete',old.id,old.title,old.body); END"}
SEARCH_TABLE_PLAIN = "CREATE VIRTUAL TABLE event_search USING fts5(title,body,content='events',content_rowid='id')"
SEARCH_TABLE_SEALED = "CREATE VIRTUAL TABLE event_search USING fts5(title,body,content='')"
SEARCH_SEALED = {name: sql.replace('new.title', 'mc_words(new.title)').replace('new.body', 'mc_words(new.body)')
                 .replace('old.title', 'mc_words(old.title)').replace('old.body', 'mc_words(old.body)')
                 for name, sql in SEARCH_PLAIN.items()}


def _tables(db):
    return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _columns(db, table):
    return {r[1] for r in db.execute(f'PRAGMA table_info({table})')}


def sealed_columns(db):
    """[(table, column, json plain keys or None)] present in this database."""
    tables, out = _tables(db), []
    if 'events' in tables:
        out += [('events', 'title', None), ('events', 'body', None), ('events', 'metadata', PLAIN_METADATA)]
    for table, columns in DERIVED.items():
        if table in tables:
            present = _columns(db, table)
            out += [(table, c, None) for c in columns if c in present]
    for table, column, plain in DERIVED_JSON:
        if table in tables and column in _columns(db, table):
            out.append((table, column, plain))
    return out


def _guard_sql(table, column, plain):
    """Triggers that seal whatever reaches the table in the clear, from any code path."""
    value = f"mc_seal_json({column},'{table}.{column}')" if plain is not None else f'mc_seal({column})'
    when_new = (f"typeof(new.{column})='text' AND new.{column} IS NOT mc_seal_json(new.{column},'{table}.{column}')" if plain is not None
                else f"typeof(new.{column})='text' AND new.{column}!=''")
    return [f"CREATE TRIGGER mc_{table}_{column}_insert AFTER INSERT ON {table} WHEN {when_new} BEGIN "
            f"UPDATE {table} SET {column}={value} WHERE rowid=new.rowid; END",
            f"CREATE TRIGGER mc_{table}_{column}_update AFTER UPDATE OF {column} ON {table} WHEN {when_new} BEGIN "
            f"UPDATE {table} SET {column}={value} WHERE rowid=new.rowid; END"]


def triggers(db):
    return {r[0]: r[1] for r in db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'")}


def is_protected(db):
    return any(name.startswith('mc_') for name in triggers(db))


def _set_search(db, sealed):
    if 'event_search' not in _tables(db):
        return
    wanted = SEARCH_SEALED if sealed else SEARCH_PLAIN
    for name in wanted:
        db.execute(f'DROP TRIGGER IF EXISTS {name}')
    # Made anew from the table: the old index's words (in the clear, or hashed) go with it. Sealed, the index keeps no
    # link to the table's text (contentless), so nothing in SQLite reads the sealed columns through it.
    db.execute('DROP TABLE event_search')
    db.execute(SEARCH_TABLE_SEALED if sealed else SEARCH_TABLE_PLAIN)
    for sql in wanted.values():
        db.execute(sql)
    title, body = ('mc_words(title)', 'mc_words(body)') if sealed else ('title', 'body')
    db.execute(f'INSERT INTO event_search(rowid,title,body) SELECT id,{title},{body} FROM events')
    db.execute("INSERT INTO event_search(event_search) VALUES('optimize')")


def ensure_triggers(db):
    """A sealed database keeps its guards and hashed index triggers, also after a migration remade a table."""
    present = triggers(db)
    for table, column, plain in sealed_columns(db):
        for sql in _guard_sql(table, column, plain):
            name = sql.split()[2]
            if name not in present:
                db.execute(sql)
    for name, sql in SEARCH_SEALED.items():
        if 'event_search' in _tables(db) and present.get(name) != sql:
            db.execute(f'DROP TRIGGER IF EXISTS {name}')
            db.execute(sql)


def protect(db):
    """Seal every message in place. Run inside one transaction, on a connection `attach`ed to the key."""
    sealer = db.sealer
    counts = {}
    for name in SEARCH_PLAIN:  # the index is made anew below, once
        db.execute(f'DROP TRIGGER IF EXISTS {name}')
    for table, column, plain in sealed_columns(db):
        rows = db.execute(f'SELECT rowid,{column} FROM {table}').fetchall()  # opened by the row factory
        changed = 0
        for rowid, value in rows:
            new = sealer.seal_json(value, plain) if plain is not None else sealer.seal(value)
            if new is not value and new != value:
                db.execute(f'UPDATE {table} SET {column}=? WHERE rowid=?', (new, rowid))
                changed += 1
        counts[f'{table}.{column}'] = changed
    ensure_triggers(db)
    _set_search(db, True)
    return counts


def unprotect(db):
    """Open every sealed value in place and return to plain text and a plain index (rolling back, or an export)."""
    for name in list(triggers(db)):
        if name.startswith('mc_') or name in SEARCH_PLAIN:
            db.execute(f'DROP TRIGGER {name}')
    for table, column, plain in sealed_columns(db):
        for rowid, value in db.execute(f'SELECT rowid,{column} FROM {table}').fetchall():
            if is_sealed(value) or is_sealed_json(value):
                raise SealError('A value stayed sealed: this connection has no key')
            db.execute(f'UPDATE {table} SET {column}=? WHERE rowid=?', (value, rowid))
    _set_search(db, False)


def leftovers(db, seal=False):
    """How many values a sealed database still holds in the clear (0 when protection is complete). With `seal`, seals
    them: a table made after the database was sealed (joining a Fleet, say) is covered from then on."""
    sealer, found = db.sealer, 0
    for table, column, plain in sealed_columns(db):
        rows = db.execute(f"SELECT rowid,{column} FROM {table} WHERE typeof({column})='text' AND {column}!=''"
                          + (f" AND instr({column},'\"{SEALED_KEY}\":')=0" if plain is not None else '')).fetchall()
        for rowid, value in rows:
            new = sealer.seal_json(value, plain) if plain is not None else sealer.seal(value)
            if new != value:
                found += 1
                if seal:
                    db.execute(f'UPDATE {table} SET {column}=? WHERE rowid=?', (new, rowid))
    return found

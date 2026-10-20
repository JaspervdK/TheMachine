"""Finding people you know on the Network, privately, and suggesting a connection only when it would help (docs/network.md).

Two switches, both off until the owner turns them on:
- Find people I know: once a week the Machine checks its strongest contacts against the Network without uploading
  them. Each address is hashed onto a group and blinded with a secret only this Machine knows; the Network raises
  them to its own secret and returns them with its (blinded) list of people who chose to be found; the Machine
  compares. Only the matches are then looked up by address, to learn their username and name.
- Let people who have my email find me: the Network adds the owner's account email to that list.

A match becomes a card under Suggestions only when connecting has a concrete use now: an open card with them, or a
meeting with them soon. Other matches wait quietly in the Fleet tab's Network block. The card suggests what they are
to the owner from structure only: the same work email domain as the owner's work accounts reads as a colleague;
anyone else is "friend or family", the owner picks. Connect sends an ordinary friend request: nothing connects
until they accept, and what is shared then follows the owner's friend rules (Ask me first by default). Everything
the Machine looked up and sent is in the Fleet log. Leave the Network ends findability and every friendship."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import secrets

# The Network's group (cloud/discovery.py has the same): RFC 3526's 2048-bit MODP prime, its subgroup of squares.
P = int(
    'FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD129024E088A67CC74020BBEA63B139B22514A08798E3404DD'
    'EF9519B3CD3A431B302B0A6DF25F14374FE1356D6D51C245E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7ED'
    'EE386BFB5A899FA5AE9F24117C4B1FE649286651ECE45B3DC2007CB8A163BF0598DA48361C55D39A69163FA8FD24CF5F'
    '83655D23DCA3AD961C62F356208552BB9ED529077096966D670C354E4ABC9804F1746C08CA18217C32905E462E36CE3B'
    'E39E772C180E86039B2783A2EC07A28FB5C55DF06F4C52C9DE2BCBF6955817183995497CEA956AE515D2261898FA0510'
    '15728E5A8AACAA68FFFFFFFFFFFFFFFF', 16)
BYTES = (P.bit_length() + 7) // 8
MAX_CONTACTS = 1000          # the strongest contacts checked in one lookup (the Network's limit)
RESOLVE_BATCH = 50
LOOKUP_DAYS = 7              # how often the Machine looks again
LATER_DAYS = 30              # "Not now" hides a person this long, unless something new comes up with them
MEETING_DAYS = 14
STATUSES = ('new', 'requested', 'connected', 'later', 'never')

SCHEMA = """
CREATE TABLE IF NOT EXISTS network_people(email TEXT PRIMARY KEY,node_id TEXT,username TEXT NOT NULL,name TEXT NOT NULL,
  hint TEXT NOT NULL,role TEXT,relation TEXT,reasons TEXT NOT NULL DEFAULT '[]',status TEXT NOT NULL,found_at TEXT NOT NULL,
  decided_at TEXT);
"""


def normal(email):
    return str(email or '').strip().lower()


def bucket(email):
    return hashlib.sha256(normal(email).encode()).digest()[0]


def to_group(email):
    seed = normal(email).encode()
    wide = b''.join(hashlib.sha512(b'the-machine-network-discovery:%d:' % i + seed).digest() for i in range(5))
    return pow(int.from_bytes(wide, 'big') % P, 2, P)


def hexed(n):
    return format(n, 'x').rjust(BYTES * 2, '0')


def blind(emails, secret=None):
    """H(x)^a for each address, with one fresh secret a; and the set of their buckets (not which is whose)."""
    a = secret or (secrets.randbits(256) | 1)
    return a, [hexed(pow(to_group(e), a, P)) for e in emails], sorted({bucket(e) for e in emails})


def matches(emails, items, a, answer):
    """The addresses whose H(x)^ab the Network returned equal one of its people raised to our secret, H(y)^ba."""
    raised = answer.get('items') if isinstance(answer, dict) else None
    if not isinstance(raised, list) or len(raised) != len(items):
        return []
    found = set()
    for y in answer.get('found') or []:
        try:
            found.add(hexed(pow(int(str(y), 16), a, P)))
        except ValueError:
            continue
    return [e for e, x in zip(emails, raised) if isinstance(x, str) and x in found]


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')


def ensure(db):
    db.executescript(SCHEMA)


class Discovery:
    """Mixed into fleet.Fleet: the owner's Network discovery on their own Machine."""

    def network_settings(self):
        found = self.meta('network', {})
        return found if isinstance(found, dict) else {}

    def _set_network(self, **values):
        found = self.network_settings()
        found.update(values)
        self.set_meta('network', found)
        return found

    def network_contacts(self, db):
        """The strongest people in the relationship graph with an address, the owner's own addresses left out."""
        from fleet import _emails
        mine = {normal(a.get('email')) for a in self.config.get('accounts', [])} | {normal(e) for e in self.my_emails()}
        out, seen = [], set()
        for row in db.execute("SELECT * FROM graph_nodes WHERE kind='person' AND quiet=0 ORDER BY strength DESC LIMIT 3000"):
            for email in sorted(_emails(row)):
                if email in mine or email in seen:
                    continue
                seen.add(email)
                out.append((email, row['id'], row['name']))
                if len(out) >= MAX_CONTACTS:
                    return out
        return out

    def _hint(self, email):
        """colleague: the same work email domain as one of the owner's work accounts (not a free mail domain); else
        personal, and the owner says whether friend or family. Nothing is read from what anyone wrote."""
        import graph
        from fleet import account_emails
        domains = {e.rsplit('@', 1)[1] for e in account_emails(self.config)} - set(graph.FREEMAIL)
        return 'colleague' if email.rsplit('@', 1)[-1] in domains else 'personal'

    def _role(self, db, node_id, email):
        row = db.execute("SELECT role FROM person_roles WHERE identifier IN (?,?) AND source='owner'", (node_id or '', 'email:' + email)).fetchone() \
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='person_roles'").fetchone() else None
        return row['role'][:60] if row else None

    def network_reasons(self, db, node_id):
        """What connecting would help with now: an open card with them, a meeting with them soon. Empty: no card."""
        from fleet import OPEN_CARD
        if not node_id:
            return []
        ids = {r[0] for r in db.execute('SELECT event_id FROM graph_events WHERE node_id=? ORDER BY at DESC LIMIT 500', (node_id,))}
        out = []
        if ids:
            for card in db.execute(f"SELECT id,title,event_ids FROM suggestions WHERE status IN ({','.join('?' * len(OPEN_CARD))}) "
                                   'ORDER BY created_at DESC LIMIT 300', OPEN_CARD):
                if ids & set(json.loads(card['event_ids'] or '[]')):
                    out.append({'kind': 'card', 'card': card['id'], 'title': card['title'][:120]})
                    break
        start = dt.datetime.now(dt.timezone.utc)
        meeting = db.execute("SELECT e.occurred_at,e.title FROM graph_events g JOIN events e ON e.id=g.event_id WHERE g.node_id=? "
                             "AND e.kind='calendar.event' AND e.deleted=0 AND e.occurred_at>=? AND e.occurred_at<=? ORDER BY e.occurred_at LIMIT 1",
                             (node_id, start.isoformat(timespec='seconds'), (start + dt.timedelta(days=MEETING_DAYS)).isoformat(timespec='seconds'))).fetchone()
        if meeting:
            out.append({'kind': 'meeting', 'at': meeting['occurred_at'], 'title': meeting['title'][:120]})
        return out

    # -- the weekly lookup
    def discovery_round(self, force=False):
        """With "Find people I know" on: look the contacts up once a week (or now, when forced); every time, bring the
        reasons of the people found up to date, and mark the ones who became friends."""
        found = self.network_settings()
        if not found.get('find'):
            return 0
        due = force or not found.get('last') or found['last'] < (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=LOOKUP_DAYS)).isoformat(timespec='seconds')
        looked = 0
        if due:
            with self.connect() as db:
                ensure(db)
                contacts = self.network_contacts(db)
            if contacts:
                emails = [c[0] for c in contacts]
                a, items, buckets = blind(emails)
                answer = self.relay.call('POST', '/discover', {'action': 'match', 'items': items, 'buckets': buckets}, timeout=120)
                hits = matches(emails, items, a, answer)
                by_email = {c[0]: c for c in contacts}
                people = []
                for i in range(0, min(len(hits), 100), RESOLVE_BATCH):
                    batch = hits[i:i + RESOLVE_BATCH]
                    people += (self.relay.call('POST', '/discover', {'action': 'resolve', 'emails': batch}, timeout=60) or {}).get('people') or []
                with self.connect() as db:
                    for p in people:
                        email = normal(p.get('email'))
                        if email not in by_email or not isinstance(p.get('username'), str):
                            continue
                        node_id = by_email[email][1]
                        status = 'connected' if p.get('friends') else 'new'
                        db.execute('INSERT INTO network_people(email,node_id,username,name,hint,role,status,found_at) VALUES(?,?,?,?,?,?,?,?) '
                                   "ON CONFLICT(email) DO UPDATE SET username=excluded.username,name=excluded.name,node_id=excluded.node_id,"
                                   "status=CASE WHEN excluded.status='connected' THEN 'connected' ELSE network_people.status END",
                                   (email, node_id, p['username'][:40], str(p.get('name') or by_email[email][2])[:80], self._hint(email),
                                    self._role(db, node_id, email), status, now()))
                    self.log(db, 'out', self.friends_ref() or 'friends', None, 'Network', 'discover',
                             f'Checked {len(emails)} contacts privately; {len(people)} on the Network', 'done')
                looked = len(emails)
            self._set_network(last=now())
        friends = {m.get('username') for m in self.teammates(self.friends_ref())} if self.friends_ref() else set()
        with self.connect() as db:
            ensure(db)
            for row in db.execute("SELECT * FROM network_people WHERE status IN ('new','requested','later')").fetchall():
                status = 'connected' if row['username'] in friends else row['status']
                if status == 'later' and row['decided_at'] and row['decided_at'] < (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=LATER_DAYS)).isoformat(timespec='seconds'):
                    status = 'new'
                reasons = self.network_reasons(db, row['node_id'])
                if status == 'later' and reasons and json.dumps(reasons) != row['reasons'] and \
                        {r.get('card') or r.get('at') for r in reasons} - {r.get('card') or r.get('at') for r in json.loads(row['reasons'] or '[]')}:
                    status = 'new'  # something new came up with them
                db.execute('UPDATE network_people SET status=?,reasons=? WHERE email=?', (status, json.dumps(reasons), row['email']))
        return looked

    # -- the owner's own
    def network_action(self, do, email='', relation=''):
        """find_on / find_off, discoverable_on / discoverable_off, connect (as friend, family or colleague), later,
        never, leave."""
        email = normal(email)
        if do in ('find_on', 'find_off'):
            self._set_network(find=do == 'find_on', asked=True)
            with self.connect() as db:
                self.log(db, 'out', self.friends_ref() or 'friends', None, 'Network', 'discover',
                         'Find people I know: ' + ('on' if do == 'find_on' else 'off'), 'done')
            if do == 'find_on':
                try:
                    self.discovery_round(force=True)
                except Exception as error:  # the next round tries again
                    return {'find': True, 'note': f'Looking up later ({type(error).__name__})'}
            return {'find': do == 'find_on'}
        if do in ('discoverable_on', 'discoverable_off'):
            found = self.relay.call('POST', '/discover', {'action': 'discoverable', 'on': do == 'discoverable_on'}, timeout=30)
            self._set_network(discoverable=bool((found or {}).get('discoverable')))
            with self.connect() as db:
                self.log(db, 'out', self.friends_ref() or 'friends', None, 'Network', 'discover',
                         'Findable by my email: ' + ('on' if do == 'discoverable_on' else 'off'), 'done')
            return {'discoverable': self.network_settings().get('discoverable', False)}
        if do == 'leave':
            self.relay.call('POST', '/discover', {'action': 'leave'}, timeout=60)
            self._set_network(find=False, discoverable=False, asked=True)
            with self.connect() as db:
                ensure(db)
                db.execute('DELETE FROM network_people')
                self.log(db, 'out', self.friends_ref() or 'friends', None, 'Network', 'discover', 'Left the Network: friends removed, not findable', 'done')
            self.directory(refresh=True)
            return {'left': True}
        if do == 'asked':
            self._set_network(asked=True)
            return {'asked': True}
        with self.connect() as db:
            ensure(db)
            row = db.execute('SELECT * FROM network_people WHERE email=?', (email,)).fetchone()
        if not row:
            raise ValueError('Not someone found on the Network')
        if do == 'connect':
            relation = relation if relation in ('friend', 'family', 'colleague') else ('colleague' if row['hint'] == 'colleague' else 'friend')
            self.friend_action('add', row['username'])
            with self.connect() as db:
                db.execute("UPDATE network_people SET status='requested',relation=?,decided_at=? WHERE email=?", (relation, now(), email))
            return {'status': 'requested', 'relation': relation}
        if do in ('later', 'never'):
            with self.connect() as db:
                db.execute('UPDATE network_people SET status=?,decided_at=? WHERE email=?', (do, now(), email))
            return {'status': do}
        raise ValueError('Unknown Network action')

    def network_view(self):
        found = self.network_settings()
        with self.connect() as db:
            ensure(db)
            people = [{**{k: r[k] for k in ('email', 'username', 'name', 'hint', 'role', 'relation', 'status', 'found_at')},
                       'reasons': json.loads(r['reasons'] or '[]')}
                      for r in db.execute("SELECT * FROM network_people WHERE status<>'never' ORDER BY found_at DESC LIMIT 200")]
        return {'find': found.get('find'), 'discoverable': found.get('discoverable'), 'asked': bool(found.get('asked')),
                'last': found.get('last'), 'people': people}
